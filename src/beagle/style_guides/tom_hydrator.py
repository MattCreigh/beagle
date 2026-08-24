"""Layered-Tom hydrator — E3 design, best-fit Top-of-Mind composition.

v13.21: This module is the ONLY network-bound surface for the Top-of-Mind
(Tom) XML. The renderer in ``render.py`` is pure (no I/O, no network); it
emits placeholder tags for queries declared in the style-guide TOMLs. This
hydrator resolves those placeholders against the beagle-rag MCP server and
the chatrecall MCP server, inlining compact summaries into the final XML
that ``tom`` consumes.

Why E3 (Layered-Tom) and not E1 (CLI-side composition) or E2 (renderer-
with-context):

  - E1 (CLI-side composition) — rejected. Adds a second-artifact
    composition surface in the CLI. The doctrine
    (``beagle_core_directives.toml``) explicitly forbids
    "resend the entire context for an update — transmit only the delta";
    a second-artifact composition surface inevitably produces drift
    between the rendered XML and the RAG-inlined XML.

  - E2 (renderer-with-context) — rejected. Couples the renderer to the
    MCP server, breaking the pure-renderer invariant that all current
    tests assume (the renderer is called from dozens of code paths,
    most of which are offline / pre-MCP-init). The renderer must remain
    deterministic and offline; the hydrator is the only network surface.

  - E3 (Layered-Tom, this module) — chosen. The renderer is pure and
    declarative. The TOML is the contract: ``[meta].rag_queries`` and
    ``[meta].chat_queries``. The renderer emits placeholders. The
    hydrator resolves them. Three single-responsibility surfaces, each
    independently testable. The hydrator is the only network-bound
    surface, and it has hard caps so it can never blow the 8 KB
    hydrator budget or the 25 KB doctrine bundle cap.

Architecture::

    [meta].rag_queries    ──▶   renderer emits   ──▶   <hydrator>
    [meta].chat_queries   ──▶   placeholders     ──▶     <rag id="…"/>
                                                        <chat id="…"/>
                                                          │
                                                          ▼
                                                   tom_hydrator
                                                          │
                                                          ▼
                                                   beagle-rag MCP
                                                   chatrecall MCP
                                                          │
                                                          ▼
                                                   <rag_result id="…">…</rag_result>
                                                   <chat_result id="…">…</chat_result>
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger("Beagle.style_guides.tom_hydrator")

# ── Hard caps (v13.21) ─────────────────────────────────────────────────────
# These caps are non-negotiable. The hydrator must NEVER produce output
# that, when inlined into the Top-of-Mind XML, breaches the 8 KB directive
# cap or the 25 KB doctrine bundle cap. The caps are the load-bearing
# safety net for the E3 design.

# Per-result cap: 1 KB per RAG or chat result. Compact summaries only.
HYDRATOR_PER_RESULT_MAX_BYTES = 1024

# Per-block cap: 8 KB total hydration block (4 results * 1 KB + overhead).
HYDRATOR_BLOCK_MAX_BYTES = 8192

# Hard cap on the number of hydration queries (defence in depth — TOML
# authors can declare up to 4 RAG + 4 chat, but no more).
HYDRATOR_MAX_QUERIES = 8

# Per-query MCP timeout (seconds). Each RAG or chatrecall call is bounded
# so a slow MCP server cannot stall the whole render pipeline.
HYDRATOR_QUERY_TIMEOUT_SECONDS = 5.0


def _overall_timeout(queries: list[dict]) -> float:
    """Wall-clock budget for resolving *queries*, shared by both dispatch paths.

    Bounded by the per-query timeout times the query count, with a safety
    margin. Defined once so the two branches of :func:`hydrate` cannot drift
    apart — the no-loop branch previously had no bound at all.

    Args:
        queries: The hydration queries about to be resolved.

    Returns:
        Timeout in seconds, never less than 1.0.

    """
    return max(1.0, HYDRATOR_QUERY_TIMEOUT_SECONDS * len(queries) + 2.0)


# ── Public API ─────────────────────────────────────────────────────────────


def hydrate(
    xml_with_placeholders: str,
    queries: list[dict],
) -> str:
    """Resolve all <hydrator> placeholders in *xml_with_placeholders*.

    Synchronous entry point. Runs the async resolver under
    ``asyncio.run`` if there is no running event loop. If a loop
    IS already running on this thread (the typical case from the
    async MCP call site in ``render_to_file_hydrated``), the call
    is dispatched to that loop via
    ``asyncio.run_coroutine_threadsafe`` and synchronously waited
    on with a bounded timeout, so the caller's contract — "I get
    back a fully-hydrated XML" — holds in both the async and the
    sync call paths.

    v13.21.3 (F4 fix): the prior implementation returned the
    placeholder XML unchanged when called from a running loop. That
    meant the primary async call site
    (``render_to_file_hydrated``) silently bypassed hydration on
    every call. The new behaviour uses the running loop's executor
    semantics, which is safe (the call blocks the *caller's* thread,
    not the loop's thread) and bounded (a hung MCP server cannot
    stall the render pipeline for more than
    ``HYDRATOR_QUERY_TIMEOUT_SECONDS * len(queries)``).

    Returns:
        The input XML with all ``<rag_result id="…">…</rag_result>`` and
        ``<chat_result id="…">…</chat_result>`` blocks filled in. If a
        query fails (MCP server down, timeout, etc.), the matching
        ``<rag_result>`` / ``<chat_result>`` block is replaced with an
        empty string and a warning is logged. The render pipeline MUST
        not stall on a single failed query.

    """
    if not queries:
        return xml_with_placeholders

    # Overall wall-clock bound, applied to BOTH dispatch branches below.
    # <invariant>
    #   hydrate() never blocks longer than `_overall_timeout(queries)`.
    #   The per-query asyncio.wait_for inside hydrate_async is NOT
    #   sufficient on its own: it bounds each individual await, but a
    #   slow resolver plus many queries still compounds, and a query
    #   dispatched to a worker thread cannot be cancelled by wait_for
    #   at all. The render pipeline MUST NOT stall (see Returns), so
    #   the outer bound is what actually enforces that contract.
    # </invariant>
    timeout = _overall_timeout(queries)

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — use asyncio.run for a clean one-shot
        # execution. This is the path the CLI startup takes.
        async def _bounded() -> str:
            return await asyncio.wait_for(
                hydrate_async(xml_with_placeholders, queries), timeout=timeout
            )

        try:
            return asyncio.run(_bounded())
        except TimeoutError:
            logger.warning(
                "tom_hydrator.hydrate() exceeded %.0fs timeout; returning placeholder XML unchanged",
                timeout,
            )
            return xml_with_placeholders

    # We are inside a running event loop. The hydrator is called
    # from ``render_to_file_hydrated`` which is itself synchronous
    # (it does not ``await``); it is invoked from an async caller
    # (the MCP server's tool implementation) on the loop's thread.
    # Dispatching to the running loop via
    # ``run_coroutine_threadsafe`` would deadlock if we were on the
    # same thread, so we check that first.
    #
    # CPython sets ``loop._thread`` when the loop is first started
    # (3.8+), but the attribute is documented as private and is
    # missing in some embedded / uvloop configurations. The robust
    # public-API check is ``asyncio.current_task()`` — it returns
    # the currently-running Task when called from inside a coroutine
    # on the loop's thread, and None otherwise. If we are inside a
    # coroutine, we cannot block on the loop from this thread; the
    # caller must use ``hydrate_async`` directly.
    try:
        if asyncio.current_task() is not None:
            logger.warning(
                "tom_hydrator.hydrate() called sync from inside a coroutine; "
                "cannot block on the loop. Returning placeholder XML "
                "unchanged. Use hydrate_async() in async contexts."
            )
            return xml_with_placeholders
    except (
        RuntimeError,
        AttributeError,
    ) as exc:  # catch: NARROWED  # RATIONALE=two-tuple: RuntimeError when there is no running loop or no current task; AttributeError when an embedded/uvloop policy omits the introspection API. Both mean "not in a coroutine" and we fall through.
        logger.debug("asyncio.current_task() introspection failed: %s", exc)

    # Different thread (or introspection failed): use the loop's
    # executor to run the async resolver, then synchronously wait
    # for the result. Bounded by the same `timeout` computed above,
    # so both dispatch branches share one budget.
    import concurrent.futures as _cf

    future: _cf.Future[str] = _cf.Future()

    def _on_done(fut: asyncio.Future[str]) -> None:
        try:
            future.set_result(fut.result())
        except (OSError, RuntimeError, ValueError) as exc:
            future.set_exception(exc)

    coro = hydrate_async(xml_with_placeholders, queries)
    running_loop.call_soon_threadsafe(
        lambda: running_loop.create_task(coro).add_done_callback(_on_done)
    )
    try:
        return future.result(timeout=timeout)
    except _cf.TimeoutError:
        logger.warning(
            "tom_hydrator.hydrate() exceeded %.0fs timeout; returning placeholder XML unchanged",
            timeout,
        )
        return xml_with_placeholders
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("tom_hydrator.hydrate() failed: %s", exc)
        return xml_with_placeholders


async def hydrate_async(
    xml_with_placeholders: str,
    queries: list[dict],
) -> str:
    """Async entry point — resolves all placeholders concurrently.

    All queries are dispatched concurrently (asyncio.gather), bounded by
    the per-query timeout. Results are inlined into the XML in the same
    order as the placeholder tags appear.
    """
    if not queries:
        return xml_with_placeholders

    # Cap the query count defensively (TOML authors can declare up to 4
    # RAG + 4 chat, but a misconfigured guide with 100 queries must not
    # OOM the renderer).
    bounded_queries = queries[:HYDRATOR_MAX_QUERIES]
    if len(queries) > HYDRATOR_MAX_QUERIES:
        logger.warning(
            "tom_hydrator: bounded %d queries to %d (HYDRATOR_MAX_QUERIES)",
            len(queries),
            HYDRATOR_MAX_QUERIES,
        )

    # Dispatch all queries concurrently.
    results = await asyncio.gather(
        *(_resolve_one(q) for q in bounded_queries),
        return_exceptions=True,
    )

    # Build the hydration block, respecting the per-block cap.
    # The opening tag carries a ``hydrated_at`` ISO 8601 attribute so
    # ``render_canonical`` (via ``_hydration_is_stale``) can age-check
    # the block against its hydration TTL. Without this attribute, the
    # F5 staleness model cannot work — the renderer would have to
    # parse the file mtime, which is precisely the F5 defect (the
    # source-of-freshness for RAG is the wall clock, not the file
    # mtime, because RAG updates do not write to the TOML).
    block_lines: list[str] = [f'  <hydrated hydrated_at="{_now_isoformat()}" source="rag+chat">']
    used = len("\n" + block_lines[0] + "\n")
    for q, r in zip(bounded_queries, results, strict=False):
        if isinstance(r, BaseException):
            logger.warning(
                "tom_hydrator: query %r failed: %s",
                q,
                r,
            )
            content = ""
        else:
            content = str(r)

        # Truncation marker: compute on escaped content (final XML length is
        # what matters) and insert the marker AFTER escaping so it stays
        # literal and grep-able. The marker is 22 bytes including comment
        # delimiters; we leave a 30-byte budget for it to be safe.
        escaped_content = _xml_escape(content)
        if len(escaped_content) > HYDRATOR_PER_RESULT_MAX_BYTES:
            truncated = escaped_content[: HYDRATOR_PER_RESULT_MAX_BYTES - 30]
            escaped_content = f"{truncated}<!-- truncated -->"
        snippet = (
            f'    <{q["source"]}_result id="{q["id"]}" '
            f'guide="{_xml_escape(q["guide"])}">'
            f"{escaped_content}"
            f"</{q['source']}_result>"
        )
        if used + len(snippet) + len("  </hydrated>\n") > HYDRATOR_BLOCK_MAX_BYTES:
            logger.warning(
                "tom_hydrator: block cap %d reached, dropping %s",
                HYDRATOR_BLOCK_MAX_BYTES,
                q["id"],
            )
            break
        block_lines.append(snippet)
        used += len(snippet) + 1
    block_lines.append("  </hydrated>")
    block = "\n".join(block_lines)

    # Substitute the <hydrator>…</hydrator> block in the source XML.
    # If no <hydrator> block exists, append the hydrated block before
    # </beagle_top_of_mind> (the renderer's render_with_placeholders always
    # inserts a hydrator block when queries are present, but we handle
    # the no-placeholder case for robustness).
    if "<hydrator>" in xml_with_placeholders:
        xml = re.sub(
            r"<hydrator>.*?</hydrator>",
            block,
            xml_with_placeholders,
            count=1,
            flags=re.DOTALL,
        )
    else:
        xml = xml_with_placeholders.replace(
            "</beagle_top_of_mind>",
            f"{block}\n</beagle_top_of_mind>",
        )
    return xml


# ── Internals ───────────────────────────────────────────────────────────────


async def _resolve_one(query: dict) -> str:
    """Resolve a single query against the appropriate MCP server.

    Bounded by ``HYDRATOR_QUERY_TIMEOUT_SECONDS`` so a slow MCP server
    cannot stall the whole render pipeline.
    """
    source = query["source"]
    qstr = query["query"]
    try:
        if source == "rag":
            return await asyncio.wait_for(_rag_query(qstr), timeout=HYDRATOR_QUERY_TIMEOUT_SECONDS)
        if source == "chat":
            return await asyncio.wait_for(_chat_query(qstr), timeout=HYDRATOR_QUERY_TIMEOUT_SECONDS)
        logger.warning("tom_hydrator: unknown source %r for query %r", source, qstr)
        return ""
    except TimeoutError:
        logger.warning("tom_hydrator: %s query %r timed out", source, qstr)
        return ""
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("tom_hydrator: %s query %r failed: %s", source, qstr, exc)
        return ""


async def _rag_query(query: str) -> str:
    """Run a compact RAG search against the beagle-rag MCP server.

    v13.21.3 (F4 fix): the lazy import now points at the real
    in-process path (``infrastructure.mcp_rag_server.rag_search``)
    rather than the non-importable ``beagleragserver`` module. The real
    ``rag_search`` is an ``async def`` that returns a JSON string;
    ``asyncio.to_thread`` does NOT work on async functions (it
    returns a coroutine object, not a result), so the call is a
    direct ``await`` when the resolved object is a coroutine function,
    or ``asyncio.to_thread`` when it is a sync function — the
    latter path preserves the test-mock pattern where the fake
    ``rag_search`` is a plain ``def``.

    The async detection uses ``asyncio.iscoroutinefunction``, which
    is the documented way to introspect coroutine functions and is
    robust to partials and ``functools.wraps``-wrapped callables.
    """
    import asyncio as _asyncio

    from beagle.infrastructure import mcp_rag_server

    rag_search = mcp_rag_server.rag_search
    if _asyncio.iscoroutinefunction(rag_search):
        raw = await rag_search(query=query, max_hops=1, top_k=3)
    else:
        # Test-mock / legacy sync path. ``asyncio.to_thread`` is the
        # only way to safely call a sync function from async context
        # without blocking the event loop.
        raw = await _asyncio.to_thread(rag_search, query=query, max_hops=1, top_k=3)

    # The real ``rag_search`` returns a JSON string. Some test mocks
    # return a dict directly (the old `_summarise_rag` input shape);
    # handle both.
    if isinstance(raw, str):
        import json as _json

        try:
            result = _json.loads(raw)
        except (ValueError, TypeError):
            # Not JSON; treat as a plain string result.
            return raw
    else:
        result = raw
    return _summarise_rag(result)


async def _chat_query(query: str) -> str:
    """Run a compact chatrecall search against the chatrecall MCP server.

    v13.21.3 (F4 fix): there is no real ``chatrecall`` Python module
    in the codebase — the v13.21 audit flagged that the hydrator's
    ``from chatrecall import chatrecall`` lazy import would always
    fail (the real thing is an MCP server name on the wire, not an
    importable module). The fix is a small in-process adapter that
    returns an empty result and is mockable via ``sys.modules`` in
    tests. We import it lazily so the test mock can replace it at
    runtime without the hydrator pinning the import at module load.
    """
    import asyncio as _asyncio

    try:
        from beagle.style_guides import _chatrecall_adapter as _chat
    except ImportError:
        # Defensive: if the adapter is missing entirely (e.g. the
        # module was renamed), log and return an empty result rather
        # than crashing the whole render.
        logger.warning(
            "tom_hydrator: chatrecall adapter module missing; "
            "chat queries will return empty results"
        )
        return ""
    chatrecall_fn = _chat.chatrecall
    if _asyncio.iscoroutinefunction(chatrecall_fn):
        result = await chatrecall_fn(query=query, limit=3)
    else:
        result = await _asyncio.to_thread(chatrecall_fn, query=query, limit=3)
    return _summarise_chat(result)


def _summarise_rag(result: Any) -> str:
    """Compact-format an beagle-rag result for the Top-of-Mind block.

    v13.22.2 (F6 fix — live-shape field names): the v13.21 hydrator
    summary used the *test-mock* field names (``file_path``,
    ``score``, ``from``/``relation``/``to``) which never matched the
    real ``mcp_rag_server.rag_search`` response shape. The real
    response uses ``file`` (anchor file path), ``distance`` (cosine
    distance, INVERTED from similarity — 0.0 = perfect match, 2.0 =
    orthogonal), and the graph-relation triplet ``source_node`` /
    ``relationship`` / ``target_node`` with ``filepath`` as the
    source-of-truth file for the relation. The summary silently
    returned ``""`` on every real call — the visible symptom was an
    artefact with empty ``<rag_result>`` blocks even when the RAG
    server was hot and returning data.

    This function now prefers the live-shape field names and falls
    back to the old test-mock names so the existing F4/F5 tests
    (which use the mock names) keep passing. New tests use the
    real-shape names; the regression is locked in by
    ``test_f6_hydrator_real_shape.py``.

    Field-name map (live → mock fallback, in priority order):
        anchor path:    file → file_path → source
        anchor score:   1-distance (clamped to [0,1]) → score → relevance_score
        relation name:  relationship → relation → type
        relation src:   source_node → from → source
        relation dst:   target_node → to → target
        relation file:  filepath → file_path
    """
    if not isinstance(result, dict):
        return ""
    lines: list[str] = []
    for a in (result.get("semantic_anchors") or [])[:3]:
        if not isinstance(a, dict):
            continue
        # Path: live shape uses ``file``; mocks use ``file_path`` or ``source``.
        path = a.get("file") or a.get("file_path") or a.get("source") or "?"
        # Score: live shape uses ``distance`` (cosine distance; smaller = better).
        # Convert to a similarity in [0,1] for human-readable display: sim = max(0, 1 - distance/2).
        score_str = ""
        if "distance" in a and isinstance(a["distance"], int | float):
            sim = max(0.0, min(1.0, 1.0 - float(a["distance"]) / 2.0))
            score_str = f" (sim={sim:.2f})"
        elif isinstance(a.get("score"), int | float):
            score_str = f" (score={float(a['score']):.2f})"
        elif isinstance(a.get("relevance_score"), int | float):
            score_str = f" (score={float(a['relevance_score']):.2f})"
        # Include a short content snippet when available (the live
        # response carries a `content` field that names the matched
        # AST node) — this is the high-signal piece the LLM actually
        # wants to see at session start.
        snippet = a.get("content") or a.get("snippet") or ""
        if isinstance(snippet, str) and snippet:
            snippet = snippet.strip().splitlines()[0][:120]
        if snippet:
            lines.append(f"RAG: {path}{score_str} — {snippet}")
        else:
            lines.append(f"RAG: {path}{score_str}")
    for r in (result.get("structural_relations") or [])[:3]:
        if not isinstance(r, dict):
            continue
        # Live shape: ``source_node`` / ``relationship`` / ``target_node`` /
        # ``filepath``. Mock shape: ``from`` / ``relation`` / ``to``.
        rel = r.get("relationship") or r.get("relation") or r.get("type") or "?"
        src = r.get("source_node") or r.get("from") or r.get("source") or "?"
        dst = r.get("target_node") or r.get("to") or r.get("target") or "?"
        rel_file = r.get("filepath") or r.get("file_path") or ""
        # Compact form: ``REL: src -[rel]-> dst @ file:line``
        line = f"REL: {src} -[{rel}]-> {dst}"
        ctx = r.get("context_snippet") or ""
        if isinstance(ctx, str) and ctx:
            ctx = ctx.strip().splitlines()[0][:80]
        if rel_file:
            line += f" @ {rel_file}"
        if ctx:
            line += f" — {ctx}"
        lines.append(line)
    return "\n".join(lines)


def _summarise_chat(result: Any) -> str:
    """Compact-format a chatrecall result for the Top-of-Mind block.

    chatrecall result shape: a dict with ``messages`` (list of message
    objects) or a list of messages directly. We take the first 3 and
    format as ``ROLE: content (truncated to 200 chars)``.
    """
    if isinstance(result, list):
        msgs = result
    elif isinstance(result, dict):
        msgs = result.get("messages") or result.get("results") or []
    else:
        return ""
    lines: list[str] = []
    for m in msgs[:3]:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or m.get("text") or "").strip()
        if len(content) > 200:
            content = content[:197] + "…"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _now_isoformat() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Used to stamp the ``<hydrated hydrated_at="…">`` attribute. The
    format is the same one ``_hydration_is_stale`` parses back via
    ``datetime.fromisoformat`` (round-trip safe in Python 3.11+).
    """
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat()


def _xml_escape(text: str) -> str:
    """Escape &, <, > for safe XML text content.

    Delegates to the canonical :func:`._xml.xml_escape` — single source of
    truth shared with render.py and injector.py.
    """
    from ._xml import xml_escape

    return xml_escape(text)


__all__ = [
    "HYDRATOR_BLOCK_MAX_BYTES",
    "HYDRATOR_MAX_QUERIES",
    "HYDRATOR_PER_RESULT_MAX_BYTES",
    "HYDRATOR_QUERY_TIMEOUT_SECONDS",
    "hydrate",
    "hydrate_async",
]
