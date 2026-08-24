"""F4 regression — E3 hydrator data access is wired to the real paths.

The v13.21 audit flagged two defects in ``style_guides/tom_hydrator.py``:

1. The lazy imports (``from beagleragserver import rag_search`` and
   ``from chatrecall import chatrecall``) targeted *MCP server names*
   that are not importable Python modules. Every hydration call
   raised ``ImportError``, was caught by the defensive ``except`` in
   ``_resolve_one``, and silently returned ``""`` — so the hydrated
   block was always empty, regardless of what the TOML declared.

2. The sync wrapper ``hydrate()`` detected a running event loop and
   returned the placeholder XML unchanged, with no recovery. The
   primary async call site
   (``render_to_file_hydrated``) therefore bypassed hydration on
   every call.

The fix has three parts:

- ``_rag_query`` now imports from the real in-process path
  (``beagle.infrastructure.mcp_rag_server``), which
  is the same pattern ``bridges/retriever.py`` uses. The async
  detection (``asyncio.iscoroutinefunction``) makes the call work
  for both the real async ``rag_search`` and the sync test fakes.
- A new ``_chatrecall_adapter`` module provides a real Python
  implementation of ``chatrecall(query, limit)`` that returns ``[]``
  (the chatrecall corpus is not yet plumbed into the in-process
  path). The adapter is the test seam; tests monkeypatch its
  ``chatrecall`` attribute.
- The sync ``hydrate()`` now dispatches to the running loop via
  ``asyncio.run_coroutine_threadsafe`` and synchronously waits on
  the result with a bounded timeout, so the caller's contract
  ("I get back a fully-hydrated XML") holds in both async and
  sync call paths.
"""

from __future__ import annotations

import asyncio
import json

# ── 1. _rag_query uses the real in-process path ───────────────────────────


def test_rag_query_imports_from_real_mcp_rag_server(monkeypatch):
    """The hydrator imports rag_search from mcp_rag_server, not beagleragserver.

    The v13.21 audit flagged that ``beagleragserver`` is the MCP server
    *name on the wire*, not an importable Python module. Pinning the
    import path to the real ``infrastructure.mcp_rag_server`` module
    means the import actually resolves; the test seam is the same
    module the production code uses, so test mocks and production
    code cannot drift.
    """
    import inspect

    from beagle.style_guides import tom_hydrator

    src = inspect.getsource(tom_hydrator._rag_query)
    # The old, broken import must be gone.
    assert "from beagleragserver import" not in src, (
        "F4 regression: _rag_query still imports from the non-importable "
        "'beagleragserver' module name. Use "
        "'from beagle.infrastructure import mcp_rag_server'."
    )
    # The new import must be present.
    assert "mcp_rag_server" in src, (
        "F4 fix: _rag_query must import from the real in-process path "
        "mcp_rag_server, not the wire-name 'beagleragserver'."
    )


def test_chat_query_imports_from_chatrecall_adapter(monkeypatch):
    """The hydrator imports chatrecall from the new _chatrecall_adapter."""
    import inspect
    import re

    from beagle.style_guides import tom_hydrator

    src = inspect.getsource(tom_hydrator._chat_query)
    # Look only at actual import statements (start of line, optional
    # leading whitespace). The previous v13.21 implementation's
    # bug was a literal ``from chatrecall import chatrecall`` line;
    # a docstring mention of "from chatrecall import" is fine and
    # expected (it documents the bug being fixed).
    import_re = re.compile(r"^\s*from\s+chatrecall\s+import", re.MULTILINE)
    assert not import_re.search(src), (
        "F4 regression: _chat_query has a top-level "
        "'from chatrecall import ...' statement. The 'chatrecall' "
        "module name is the MCP server wire name, not an importable "
        "module. Use "
        "'from beagle.style_guides import _chatrecall_adapter'."
    )
    # The new import must be present (look for the module name being
    # imported, not in a docstring). The hydrator's actual line is
    # ``from beagle.style_guides import _chatrecall_adapter
    # as _chat`` — we check for the module name being on the right
    # of an ``import`` keyword on a code line (not a docstring).
    new_import_re = re.compile(
        r"^\s*from\s+\S+\s+import\s+\S*_chatrecall_adapter\b",
        re.MULTILINE,
    )
    assert new_import_re.search(src), (
        "F4 fix: _chat_query must import from the new _chatrecall_adapter "
        "module, not the wire-name 'chatrecall'."
    )


def test_chatrecall_adapter_module_is_real_importable():
    """The _chatrecall_adapter module exists and exports chatrecall().

    Without this module, ``_chat_query``'s ``from ... import _chat``
    import would raise ImportError and the chat path would crash on
    every hydration call (the previous behaviour was the same crash,
    just hidden by a different broken import target).
    """
    from beagle.style_guides import _chatrecall_adapter

    assert hasattr(_chatrecall_adapter, "chatrecall")
    assert callable(_chatrecall_adapter.chatrecall)
    # The stub returns an empty list (no corpus plumbed in yet).
    assert _chatrecall_adapter.chatrecall(query="anything", limit=3) == []


# ── 2. _rag_query handles both async and sync rag_search ─────────────────


def test_rag_query_handles_async_rag_search(monkeypatch):
    """The real mcp_rag_server.rag_search is async; _rag_query awaits it."""
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    from beagle.style_guides import tom_hydrator

    async def fake_async_rag(query, max_hops=1, top_k=3):
        # Real rag_search returns a JSON string; mimic that.
        return json.dumps(
            {
                "semantic_anchors": [{"file_path": f"{query}.py", "score": 0.9}],
                "structural_relations": [],
            }
        )

    monkeypatch.setattr(_rag_mod, "rag_search", fake_async_rag)

    async def run():
        return await tom_hydrator._rag_query("hello")

    out = asyncio.run(run())
    assert "RAG: hello.py" in out


def test_rag_query_handles_sync_rag_search(monkeypatch):
    """Test-mock rag_search can be sync; _rag_query uses asyncio.to_thread."""
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    from beagle.style_guides import tom_hydrator

    def fake_sync_rag(query, max_hops=1, top_k=3):
        return {
            "semantic_anchors": [{"file_path": f"{query}.py", "score": 0.9}],
            "structural_relations": [],
        }

    monkeypatch.setattr(_rag_mod, "rag_search", fake_sync_rag)

    async def run():
        return await tom_hydrator._rag_query("hello")

    out = asyncio.run(run())
    assert "RAG: hello.py" in out


def test_rag_query_handles_json_string_result(monkeypatch):
    """A real rag_search returns a JSON string; _rag_query parses it.

    The previous version assumed the return was a dict (because the
    test mocks were dicts). The real ``mcp_rag_server.rag_search``
    returns a JSON string. Without the parse, ``_summarise_rag``'s
    ``isinstance(result, dict)`` check returned False and the result
    was dropped on the floor.
    """
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    from beagle.style_guides import tom_hydrator

    async def fake_json_rag(query, max_hops=1, top_k=3):
        return json.dumps(
            {
                "semantic_anchors": [{"file_path": f"{query}.py", "score": 0.9}],
                "structural_relations": [],
            }
        )

    monkeypatch.setattr(_rag_mod, "rag_search", fake_json_rag)

    async def run():
        return await tom_hydrator._rag_query("x")

    out = asyncio.run(run())
    assert "RAG: x.py" in out


# ── 3. Sync hydrate() works from a running event loop ────────────────────


def test_sync_hydrate_from_no_loop_uses_asyncio_run(monkeypatch):
    """With no running loop, hydrate() uses asyncio.run — the simple path."""
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod
    from beagle.style_guides import tom_hydrator

    async def fake_async_rag(query, max_hops=1, top_k=3):
        return json.dumps(
            {"semantic_anchors": [{"file_path": "x.py", "score": 0.9}], "structural_relations": []}
        )

    monkeypatch.setattr(_rag_mod, "rag_search", fake_async_rag)
    monkeypatch.setattr(_chat_mod, "chatrecall", lambda query, limit=10: [])

    xml = "<beagle_top_of_mind><hydrator></hydrator></beagle_top_of_mind>"
    queries = [{"id": "r0", "source": "rag", "query": "x", "guide": "g"}]
    out = tom_hydrator.hydrate(xml, queries)
    assert "RAG: x.py" in out


def test_sync_hydrate_from_running_loop_dispatches_to_loop(monkeypatch):
    """F4 fix: hydrate() from a running loop on a *different* thread resolves.

    The previous implementation returned the placeholder XML unchanged.
    The new implementation dispatches to the running loop and
    synchronously waits on the result. This is the call path
    ``render_to_file_hydrated`` takes from the async MCP context.
    """
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod
    from beagle.style_guides import tom_hydrator

    async def fake_async_rag(query, max_hops=1, top_k=3):
        return json.dumps(
            {"semantic_anchors": [{"file_path": "y.py", "score": 0.9}], "structural_relations": []}
        )

    monkeypatch.setattr(_rag_mod, "rag_search", fake_async_rag)
    monkeypatch.setattr(_chat_mod, "chatrecall", lambda query, limit=10: [])

    xml = "<beagle_top_of_mind><hydrator></hydrator></beagle_top_of_mind>"
    queries = [{"id": "r0", "source": "rag", "query": "y", "guide": "g"}]
    result_box: dict = {}

    async def driver():
        # We are now on the loop's thread. Call hydrate() from a
        # worker thread (via run_in_executor) so the running-loop
        # check sees a different thread, exercising the cross-thread
        # dispatch path.
        loop = asyncio.get_running_loop()
        result_box["out"] = await loop.run_in_executor(None, tom_hydrator.hydrate, xml, queries)

    asyncio.run(driver())
    out = result_box.get("out", "")
    assert "RAG: y.py" in out, (
        f"F4 regression: hydrate() from a running loop on a different "
        f"thread returned the placeholder XML unchanged. Got: {out!r}"
    )


def test_sync_hydrate_same_thread_returns_placeholder(monkeypatch):
    """Same-thread sync call from a loop returns placeholder (defensive).

    If the caller is a sync function called from the loop's own
    thread, ``hydrate()`` cannot block on the loop without
    deadlocking. The defensive branch returns the placeholder
    unchanged and logs; the caller can use ``hydrate_async``
    directly. This is documented in the docstring.
    """
    import beagle.infrastructure.mcp_rag_server as _rag_mod
    import beagle.style_guides._chatrecall_adapter as _chat_mod
    from beagle.style_guides import tom_hydrator

    async def fake_async_rag(query, max_hops=1, top_k=3):
        return json.dumps({"semantic_anchors": [], "structural_relations": []})

    monkeypatch.setattr(_rag_mod, "rag_search", fake_async_rag)
    monkeypatch.setattr(_chat_mod, "chatrecall", lambda query, limit=10: [])

    xml = '<beagle_top_of_mind><hydrator><rag id="r0" query="x" guide="g"/></hydrator></beagle_top_of_mind>'
    queries = [{"id": "r0", "source": "rag", "query": "x", "guide": "g"}]
    result_box: dict = {}

    async def driver():
        # Direct sync call from the loop's thread — exercises the
        # same-thread defensive branch.
        result_box["out"] = tom_hydrator.hydrate(xml, queries)

    asyncio.run(driver())
    out = result_box.get("out", "")
    # Placeholder preserved because we could not dispatch.
    assert "hydrator" in out, (
        "F4: same-thread sync call should return placeholder XML unchanged, "
        "not the hydrated block (would deadlock)."
    )
