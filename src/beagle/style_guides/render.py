"""Render all universal style guides to a single Top-of-Mind XML artefact.

The rendered output is consumed by goose's `tom` (Top Of Mind) extension
via the GOOSE_MOIM_MESSAGE_FILE environment variable.  Every goose turn —
in any cwd, with or without Beagle orchestration — gets this content injected
into the system prompt.

Architecture:
    loader.py  ──loads──▶  TOML dicts  ──render──▶  XML string  ──write──▶  file
    injector.py stays separate (per-file-edit injection inside dag.py:764).

Exclusion rule:
    Only `applies_to = ["*"]` (universal) guides are rendered.  Language-
    specific guides (.py, .yaml, etc.) fire on file-edit events in Beagle
    orchestration; they do NOT belong in the turn-level Top-of-Mind prompt.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from beagle.utils.atomic import atomic_write_text

from ._xml import xml_escape
from .loader import StyleGuideLoader
from .version_resolver import (
    get_config,
    get_model_fallback_chain,
    get_primary_model,
    get_pyproject,
    get_version,
    get_workflow_list,
)

logger = logging.getLogger("Beagle.style_guides.renderer")

_CANONICAL_PATH = Path.home() / ".config" / "goose" / "beagle_top_of_mind.xml"


class GooseTopOfMindRenderer:
    """Render style-guide TOMLs into an XML block for the Top-of-Mind injection.

    Accepts an optional *domain* argument to constrain rendering:
    when set (e.g. ``"python_backend"``, ``"general_cli"``), only
    ``beagle_core_directives.toml`` plus the named domain TOML are rendered.
    When ``None`` (default / ``cli.py`` startup), all universal
    (``applies_to = ["*"]``) guides are rendered as before.

    This prevents context-window bloat from concatenating all 7 TOML files
    globally when the session only needs a specific domain's rules.
    """

    # Canonical domains (must match the TOML filename stem)
    _KNOWN_DOMAINS: frozenset[str] = frozenset({"python_backend", "general_cli"})

    DEFAULT_CORE = "beagle_core_directives"

    # v13.21.3 (F3 fix): class-level cache mapping
    #   sha256(joined_text) -> fold_id
    # so re-renders with unchanged forbidden text don't allocate a new
    # fold on every call. The cache is invalidated automatically by the
    # text-hash key — any TOML edit changes the hash and triggers a
    # rebuild. Class-level (not instance) so all renderers share the
    # cache; in-process only (no disk persistence) since the source
    # TOML + CompressedStore already provide durable storage.
    _forbidden_fold_cache: ClassVar[dict[str, str]] = {}

    def __init__(
        self,
        loader: StyleGuideLoader | None = None,
        domain: str | None = None,
        target_root: Path | None = None,
    ) -> None:
        self.loader = loader or StyleGuideLoader()
        self.domain: str | None = domain
        # v13.22.2: per-repo render target. When set, the per-repo artefacts
        # (.goosehints, .goose/standards.md, CLAUDE.md) are written under
        # this root instead of the beagle package root.
        # The home-canonical artefacts (TOM, system instruction, compaction
        # prompt, doctrine report, project.json) are unaffected and still
        # emitted to ~/.config/goose/ and the beagle repo.
        # Validated eagerly to give a clear error instead of a confusing
        # downstream write failure.
        self._target_root: Path | None = Path(target_root).resolve() if target_root else None

    # ── public API ────────────────────────────────────────────────────────

    # Maximum byte size for compact rendering — keep under 2 KB so the
    # injected block never triggers a "read and respond" pause mid-task.
    COMPACT_MAX_BYTES: int = 2048

    def render(self, domain: str | None = None, compact: bool = False) -> str:
        """Render style guides to a single XML string.

        Args:
            domain: Optional domain name (e.g. ``"python_backend"``).
                    Overrides the instance-level ``self.domain`` if given.
                    When set, only ``beagle_core_directives.toml`` + the
                    domain TOML are rendered.  When ``None``, all universal
                    guides are rendered.
            compact: If True, emit only load-bearing sections
                     (CRITICAL_ROUTING_PROTOCOL + anti-patterns), capped
                     at COMPACT_MAX_BYTES.  Use this when context pressure
                     is high (>45%) to prevent the injected XML from
                     triggering a mid-task response pause.

        Returns:
            XML string, or empty string if no matching guides exist.

        """
        effective_domain = domain if domain is not None else self.domain
        guides = self._select_guides(effective_domain)
        if not guides:
            logger.warning("No matching style guides found (domain=%s)", effective_domain)
            return ""

        if compact:
            return self._render_compact_xml(guides, effective_domain)

        # v13.21.3 (F3 fix): apply the soft cap. First render with
        # all patterns; if the result exceeds ``FULL_SOFT_CAP_BYTES``,
        # re-render with only load_bearing patterns. If even the
        # load_bearing-only render exceeds the cap, keep it as-is
        # (the cap is soft — load_bearing is a hard floor).
        full_xml = self._render_full_xml(guides, effective_domain)
        if len(full_xml.encode("utf-8")) > self.FULL_SOFT_CAP_BYTES:
            load_bearing_only = self._render_full_xml(
                guides, effective_domain, tier_filter=self.PATTERN_TIER_LOAD_BEARING
            )
            if len(load_bearing_only.encode("utf-8")) < len(full_xml.encode("utf-8")):
                logger.info(
                    "render(): soft cap triggered — %d -> %d bytes "
                    "(background-tier patterns dropped)",
                    len(full_xml.encode("utf-8")),
                    len(load_bearing_only.encode("utf-8")),
                )
                return load_bearing_only
        return full_xml

    def render_with_placeholders(self, domain: str | None = None) -> tuple[str, list[dict]]:
        """Render with hydration placeholders for the Layered-Tom (E3) design.

        Returns:
            (xml_str, queries) — the XML contains ``<hydrator>`` blocks with
            ``<rag id="…"/>`` and ``<chat id="…"/>`` placeholders; *queries*
            is a parallel list of ``{"id": str, "source": "rag"|"chat",
            "query": str, "guide": str}`` dicts that the caller hands to
            :mod:`.tom_hydrator` to resolve.

        The placeholders are stable: the hydrator reads them in order and
        substitutes the matching ``<rag_result id="…">…</rag_result>`` /
        ``<chat_result id="…">…</chat_result>`` block. The TOML author
        declares the queries; the renderer emits placeholders; the
        hydrator resolves them. Three single-responsibility surfaces.

        This is the best-fit (E3) design — the renderer stays pure (no
        network), the hydrator is the only network-bound surface, and the
        TOML is the declarative contract. E1 (CLI-side composition) was
        rejected because it adds a second-artifact composition surface
        that the doctrine forbids ("Never resend the entire context for
        an update — transmit only the delta"). E2 (renderer-with-context)
        was rejected because it couples the renderer to the MCP server,
        breaking the pure-renderer invariant that all current tests assume.

        """
        effective_domain = domain if domain is not None else self.domain
        guides = self._select_guides(effective_domain)
        if not guides:
            return "", []

        # Collect the declarative queries from each guide's [meta]
        queries: list[dict] = []
        for guide in guides:
            meta = guide.get("meta", {}) or {}
            guide_name = meta.get("name", "unnamed")
            for q in meta.get("rag_queries", []) or []:
                qid = f"rag_{len(queries)}"
                queries.append({"id": qid, "source": "rag", "query": str(q), "guide": guide_name})
            for q in meta.get("chat_queries", []) or []:
                qid = f"chat_{len(queries)}"
                queries.append({"id": qid, "source": "chat", "query": str(q), "guide": guide_name})

        # Render the full XML (E3 path: with placeholders)
        xml = self._render_full_xml(guides, effective_domain)

        if not queries:
            return xml, queries

        # Insert <hydrator> block before </beagle_top_of_mind>
        hydrator_lines = ["  <hydrator>"]
        for q in queries:
            hydrator_lines.append(
                f'    <{q["source"]} id="{q["id"]}" query='
                f'"{self._xml_escape(q["query"])}" '
                f'guide="{self._xml_escape(q["guide"])}"/>'
            )
        hydrator_lines.append("  </hydrator>")
        hydrator_block = "\n".join(hydrator_lines)
        xml = xml.replace("</beagle_top_of_mind>", f"{hydrator_block}\n</beagle_top_of_mind>")
        return xml, queries

    def _render_full_xml(
        self,
        guides: list[dict],
        effective_domain: str | None,
        tier_filter: str | None = None,
    ) -> str:
        # Build the license-to-deviate element with 1-turn expiry
        # This is a structural XML element that forces model compliance
        license_to_deviate = (
            '  <license_to_deviate expires_after_turn="1" reason="explicit_user_override">'
            "<warning>Deviation from Beagle doctrine requires explicit user instruction. "
            "This license expires after ONE turn unless explicitly renewed. "
            "Default behavior: FOLLOW ALL RULES.</warning>"
            "</license_to_deviate>"
        )
        names = ", ".join(g["meta"].get("name", g.get("__file__", "?")) for g in guides)
        lines = [
            "<!--",
            "  Beagle Top-of-Mind Context — auto-generated by GooseTopOfMindRenderer.",
            "  Source: src/beagle/style_guides/guides/*.toml",
            f"  Domain: {effective_domain or 'universal'}",
            f"  Guides included: {names}",
            "  Injected every goose turn via tom extension (GOOSE_MOIM_MESSAGE_FILE).",
            "  Regenerate: beagle render-hints",
            "-->",
            "<beagle_top_of_mind>",
            license_to_deviate,
            "  <beagle_system_identity>",
            "    <role>Beagle Orchestrator — Beagle v13.16+</role>",
            "    <!-- Delegation doctrine is owned by [CRITICAL_ROUTING_PROTOCOL] in the",
            "         style-guide TOML (rendered below). Hardcoding it here would",
            "         produce a self-contradictory Top-of-Mind (see style-guide",
            "         anti-pattern: hardcoded behavioural directives in code). -->",
            "    <prime_directive>NEVER STOP. Never ask clarifying questions mid-task. "
            "Execute, validate, iterate. On ambiguity: assume most useful action.</prime_directive>",
            "    <context_management>",
            '      <proactive_compaction threshold="0.58" env_var="GOOSE_AUTO_COMPACT_THRESHOLD" tool="check_and_fold_context"/>',
            '      <post_final_answer_fold required="true"/>',
            "      <post_compaction_rehydration>Read .beagle/progress.xml immediately after compaction. "
            "Inject rehydration prompt. CONTINUE.</post_compaction_rehydration>",
            "    </context_management>",
            "  </beagle_system_identity>",
        ]

        for guide in guides:
            name = guide["meta"].get("name", guide["meta"].get("file", "unnamed"))
            lines.append(f'  <style_guide name="{self._xml_escape(name)}">')
            lines.extend(self._render_sections(guide, indent=4, tier_filter=tier_filter))
            lines.append("  </style_guide>")

        lines.append("</beagle_top_of_mind>")
        return "\n".join(lines) + "\n"

    def render_compact(self, domain: str | None = None) -> str:
        """Render only load-bearing sections (routing + anti-patterns) capped at 2 KB.

        Designed for mid-task injection when context pressure is >45%.
        The compact form is small enough that goose treats it as silent
        telemetry rather than a message to respond to.
        """
        return self.render(domain=domain, compact=True)

    def _render_compact_xml(self, guides: list[dict], domain: str | None) -> str:
        """Internal: render load-bearing sections capped at COMPACT_MAX_BYTES.

        Even in compact mode, include the coercive system identity and license elements
        to ensure rule compliance is maintained during high context pressure.
        """
        license_to_deviate = (
            '  <license_to_deviate expires_after_turn="1" reason="compact_mode">'
            "<warning>Deviation from Beagle doctrine requires explicit user instruction. "
            "This license expires after ONE turn unless explicitly renewed. "
            "Default behavior: FOLLOW ALL RULES.</warning>"
            "</license_to_deviate>"
        )
        system_identity_compact = (
            '  <beagle_system_identity compact="true">'
            "    <!-- Delegation doctrine comes from CRITICAL_ROUTING_PROTOCOL below. -->"
            "    <prime_directive>NEVER STOP. CONTINUE.</prime_directive>"
            "    <context_management>"
            '      <proactive_compaction threshold="0.58" tool="check_and_fold_context"/>'
            "      <post_compaction>Read .beagle/progress.xml. Rehydrate. CONTINUE.</post_compaction>"
            "    </context_management>"
            "  </beagle_system_identity>"
        )
        lines: list[str] = [
            "<!-- Beagle Top-of-Mind (compact — high context pressure) -->",
            '<beagle_top_of_mind compact="true">',
            license_to_deviate,
            system_identity_compact,
        ]

        for guide in guides:
            meta = guide.get("meta", {}) or {}
            name = self._xml_escape(meta.get("name", "unnamed"))
            lines.append(f'  <style_guide name="{name}">')

            # CRITICAL_ROUTING_PROTOCOL — always included
            for crit_key in (
                "CRITICAL_ROUTING_PROTOCOL",
                "critical_routing_protocol",
                "routing",
            ):
                section = guide.get(crit_key)
                if isinstance(section, dict):
                    lines.append(f"    <{crit_key}>")
                    for k, v in section.items():
                        v_esc = self._xml_escape(str(v))
                        lines.append(f"      <{k}>{v_esc}</{k}>")
                    lines.append(f"    </{crit_key}>")
                    break

            # v13.21.3 (F3 fix): tier-aware forbidden-list rendering.
            # Load-bearing rules keep their full text inline; background
            # rules carry only a 1-line summary. The full text of all
            # rules is content-addressed into a TurboQuant fold
            # (see _render_forbidden_section) so the runtime can
            # retrieve any rule's full text on demand via query_fold.
            lines.extend(self._render_forbidden_section(guide, indent=4))

            # v13.21.3 (F3 fix): emit load_bearing architecture
            # patterns in the compact path. Prior to this, the
            # compact renderer dropped ALL architecture patterns
            # (it was just routing + anti-patterns), which meant
            # the controller lost per-turn reminders of its core
            # contracts (delegate by default, XML substrate,
            # context folding, etc.) on the high-pressure turn
            # where it most needs them. The tier filter keeps
            # only the ~10 patterns the TOML author declared
            # load-bearing, capped by the existing 2 KB budget.
            load_bearing = self._tiered_patterns(guide, tier=self.PATTERN_TIER_LOAD_BEARING)
            if load_bearing:
                lines.append("    <load_bearing_patterns>")
                for p in load_bearing:
                    lines.append(f"      <pattern>{self._xml_escape(p)}</pattern>")
                lines.append("    </load_bearing_patterns>")

            lines.append("  </style_guide>")

        lines.append("</beagle_top_of_mind>")

        # Enforce the 2 KB cap
        full = "\n".join(lines) + "\n"
        if len(full) <= self.COMPACT_MAX_BYTES:
            return full

        # v13.21.3 (F3 fix): tier-aware anti-pattern trimming.
        # Step 1 — drop <forbidden_ref> (background-tier) entries one by
        # one. These are the cheapest to lose because the controller
        # only sees their 1-line summary, and the full text is in the
        # fold referenced by <forbidden_fulltext_fold>.
        candidate = full
        while len(candidate) > self.COMPACT_MAX_BYTES:
            marker = "\n      <forbidden_ref "
            idx = candidate.rfind(marker)
            if idx == -1:
                break
            end = candidate.find("/>", idx)
            if end == -1:
                break
            candidate = candidate[:idx] + candidate[end + len("/>") :]

        # Step 2 — drop <forbidden tier="load_bearing"> entries (the
        # longer ones that carry full text inline). The count stays in
        # the parent <anti_patterns> attribute so the controller knows
        # the canonical list is N items.
        while len(candidate) > self.COMPACT_MAX_BYTES:
            marker = '\n      <forbidden tier="load_bearing"'
            idx = candidate.rfind(marker)
            if idx == -1:
                break
            end = candidate.find("</forbidden>", idx)
            if end == -1:
                break
            candidate = candidate[:idx] + candidate[end + len("</forbidden>") :]

        # If still over cap, add truncation marker. The cut must (a) keep
        # the root close intact and (b) leave the truncated body in a
        # state where every opened tag is either closed or explicitly
        # re-closed by us — cutting mid-value would leave the truncated
        # tag unclosed and break parsing. The forbidden-trim loop above
        # usually gets us close to the cap, so this branch is the safety
        # net.
        if len(candidate) > self.COMPACT_MAX_BYTES:
            import re as _re

            root_close = "</beagle_top_of_mind>"
            # Not an assert: `python -O` strips assert statements, and this
            # guard is what stops the truncation below from cutting a
            # malformed document. It has to hold in an optimised run too.
            if not candidate.rstrip().endswith(root_close):
                raise RuntimeError(
                    "Compact renderer expected trailing </beagle_top_of_mind> "
                    "before truncation; got: " + repr(candidate[-80:])
                )
            # Strip the trailing close so we can snap to the last
            # completed element.
            body = candidate.rstrip()[: -len(root_close)]
            cutoff = self.COMPACT_MAX_BYTES - (
                len(root_close) + 80  # room for marker + closes + slack
            )
            # Snap back to the last `</…>` before cutoff.
            truncated = body[:cutoff]
            last_close = truncated.rfind("</")
            if last_close > 0:
                truncated = truncated[:last_close]
            truncated = truncated.rstrip("\n")

            # Walk the open-tag stack and close any unclosed elements so
            # the truncated body is well-formed XML. We track the stack
            # only for *element* tags (skip self-closing `<…/>` and
            # comments / processing instructions).
            #
            # The `rest` class excludes `/` so a trailing self-closing
            # slash is always captured by the `self` group — otherwise
            # `<proactive_compaction .../>` would be misread as
            # non-self-closing and pushed onto the stack, producing
            # `</proactive_compaction>` later.
            open_tags: list[str] = []
            tag_re = _re.compile(
                r"<\s*(?P<close>/)?\s*(?P<name>[A-Za-z_][\w:.-]*)"
                r"(?P<rest>[^<>/]*)"
                r"(?P<self>/)?\s*>"
            )
            for m in tag_re.finditer(truncated):
                if m.group("close"):
                    name = m.group("name")
                    if open_tags and open_tags[-1] == name:
                        open_tags.pop()
                elif not m.group("self") and not m.group("rest").strip().startswith("<!--"):
                    open_tags.append(m.group("name"))

            # Append the marker, then close the open tags in reverse.
            # The root (`beagle_top_of_mind`) is always on the stack
            # because we snap BEFORE the trailing close; it will be
            # re-closed by the open-tag stack, so do NOT append
            # root_close separately or we get a doubled close.
            candidate = (
                truncated
                + "\n<!-- truncated to fit 2 KB cap -->\n"
                + "".join(f"</{t}>" for t in reversed(open_tags))
                + "\n"
            )

        return candidate

    def render_session_start(self) -> str:
        """Render session-start guides (applies_to = ["session_start"]) to XML.

        These are injected one-shot at the start of a session (e.g. via
        .goosehints) rather than every turn.
        """
        guides = self._session_start_guides()
        if not guides:
            return ""

        lines = [
            "<!--",
            "  Beagle Session-Start Context — auto-generated by GooseTopOfMindRenderer.",
            "  Source: src/beagle/style_guides/guides/*.toml",
            "  Injected one-shot at session start via .goosehints.",
            "  Regenerate: beagle render-hints",
            "-->",
            "<beagle_session_start>",
        ]

        for guide in guides:
            name = guide["meta"].get("name", guide["meta"].get("file", "unnamed"))
            lines.append(f'  <style_guide name="{self._xml_escape(name)}">')
            lines.extend(self._render_sections(guide, indent=4))
            lines.append("  </style_guide>")

        lines.append("</beagle_session_start>")
        return "\n".join(lines) + "\n"

    def render_to_file(self, path: Path, domain: str | None = None) -> Path:
        """Atomic write via tempfile + os.replace. Returns the path."""
        content = self.render(domain=domain)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".xml.tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(path))
        except (OSError, RuntimeError, ValueError):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        logger.info("Rendered Top-of-Mind artefact → %s (%d bytes)", path, len(content))
        return path

    # Hard cap on the canonical artefact size; consistent with the
    # documented 25 KB doctrine bundle cap in
    # ``beagle_core_directives.toml``. The renderer never exceeds this
    # for the master-session render; the compact path uses a tighter
    # 2 KB cap (COMPACT_MAX_BYTES).
    CANONICAL_MAX_BYTES = 25600

    # v13.21.3 — F3 fix: soft cap on the full render. When the full
    # XML exceeds this threshold, the renderer drops background-tier
    # architecture patterns from the tail and re-emits. The cap is
    # *soft* (best-effort): if even the load_bearing-only output
    # would exceed it, we keep what fits and log a warning rather
    # than dropping load_bearing items.
    #
    # The cap is sized to fit the v13.21.3 doctrine-coherence additions
    # + the PRESERVED_FILE_DISPOSITION rule without dropping
    # background-tier patterns that other tests (e.g.
    # test_beagle_core_directives_in_top_of_mind_render) treat as
    # load-bearing keywords (datetime.now, uuid.uuid4,
    # additionalProperties). The cap was 16 KB pre-v13.21.3 and
    # was raised to 40 KB to fit the v13.21.3 content (~33 KB
    # full render with all patterns). The cap is still "soft" —
    # a 40 KB render is still well below the goose prompt budget
    # and matches :attr:`DOCTRINE_BUNDLE_MAX_BYTES` exactly.
    FULL_SOFT_CAP_BYTES = 40_000

    # v13.21.3 — F3 fix: tier hierarchy for architecture patterns. The
    # TOML author declares ``load_bearing_patterns`` (a list of
    # pattern-name prefixes) in the architecture section; everything
    # not named there is implicitly ``background``. The tier check
    # uses string-prefix match so the TOML author does not have to
    # repeat the long pattern text — the prefix is the short
    # ``:``-terminated identifier at the start of each pattern.
    PATTERN_TIER_LOAD_BEARING = "load_bearing"
    PATTERN_TIER_BACKGROUND = "background"

    # v13.21.3 — F5 fix: hydration freshness TTL. When the canonical
    # artefact contains a ``<hydrated>`` block, its content is
    # embedded in the artefact (not re-fetched every turn). To honour
    # the "live at call-time" goal from the E3 design without paying
    # the full re-render cost on every turn, we treat the hydrated
    # block as fresh for ``HYDRATION_TTL_SECONDS`` after its
    # ``hydrated_at=`` attribute is written. After that, the next
    # render skips the short-circuit and re-hydrates.
    #
    # 60s is the lower bound: shorter would be wasteful (a goose
    # session is typically <30s per turn), longer would let a RAG
    # index update go unseen for >1 minute. Set via
    # ``render_canonical(max_age_seconds=...)`` to override.
    HYDRATION_TTL_SECONDS = 60.0

    def emit(
        self,
        target: str,
        domain: str | None = None,
        *,
        scope: Path | None = None,
        layers: tuple[str, ...] = ("global", "directory", "task"),
        target_dir: Path | None = None,
    ) -> str:
        """Emit the rendered directive through a front-end target.

        This is the axis-1 interface: a front end (goose, claude, pi,
        OpenClaw-over-MCP) picks a target and receives the directive in the
        shape it needs, without adding a new ``render_*`` method per target.
        The renderer stays pure and offline; a target that needs a network
        call belongs in ``tom_hydrator.py``.

        Args:
            target: Target name (``goosehints``, ``claude_md``,
                ``top_of_mind_xml``, ``mcp_resource``).
            domain: Render domain (defaults to the full doctrine).
            scope: Directory the directive is scoped to.
            layers: Ordered layer names to include.
            target_dir: Where file targets write their artefact.

        Returns:
            The target's status string.

        Raises:
            KeyError: When the target name is unknown.

        """
        from beagle.style_guides.targets.base import EmitOptions
        from beagle.style_guides.targets.base import emit as _emit

        content = self.render(domain=domain)
        options = EmitOptions(
            scope=Path(scope) if scope else None,
            layers=layers,
            target_dir=Path(target_dir) if target_dir else None,
        )
        return _emit(target, content, options)

    def render_canonical(
        self,
        domain: str | None = None,
        max_age_seconds: float | None = None,
        *,
        force: bool = False,
    ) -> Path:
        """Write to ~/.config/goose/beagle_top_of_mind.xml and return path.

        Args:
            domain: Optional domain override (passed through to render).
            max_age_seconds: Override the hydration TTL used by rule 3.
                ``None`` (default) uses :attr:`HYDRATION_TTL_SECONDS`.
                A value of ``0`` means a zero-second TTL, so any existing
                hydration block is stale. This is a TTL override, NOT a force.
            force: When true, render immediately, before rule 1 is evaluated
                and without reading the destination. This is the only way to
                bypass the cache policy. It is what the watchdog uses after a
                RAG index update.

        Caching rules (F5 fix, replaces the prior TOML-mtime-only
        short-circuit):

        1. If the dest does not exist → render.
        2. If any source TOML is newer than the dest → render (the
           declarative content has changed).
        3. If the dest contains a ``<hydrated>`` block whose
           ``hydrated_at=`` is older than the TTL → render (the
           live RAG/chat content is stale).
        4. Otherwise → short-circuit, return the existing dest.

        ``force`` is evaluated before rule 1 and reads no file. Every other
        argument only tunes the rules above; none of them can force.

        The hydration-age check is a defence-in-depth: the primary
        re-render trigger is a TOML mtime change, but "live at
        call-time" requires a separate clock for the hydrated block.
        The two clocks are intentionally decoupled so a quiet TOML
        still gets a fresh static snapshot every TTL window.

        """
        # <invariant>
        #   force is evaluated before every cache rule and reads no file. A force
        #   that consults the destination is not a force: v1.2.0 (TM-1, BGL-049)
        #   replaced exactly that, where a zero max_age_seconds was routed through
        #   _hydration_is_stale and returned False on any artefact with no
        #   <hydrated> block, which is every production artefact.
        # </invariant>
        if force:
            logger.debug("Top-of-Mind forced render requested; bypassing the cache policy")
            return self.render_to_file_hydrated(_CANONICAL_PATH, domain=domain)

        dest = _CANONICAL_PATH
        if dest.exists():
            source_mtimes = self._newest_source_mtime()
            dest_mtime = dest.stat().st_mtime
            # Rule 1+2: TOML-driven staleness.
            if source_mtimes <= dest_mtime:
                # Rule 3: hydration-age staleness. We only check this
                # if rule 1+2 short-circuited; a TOML change always
                # forces a re-render regardless of hydration age.
                ttl = max_age_seconds if max_age_seconds is not None else self.HYDRATION_TTL_SECONDS
                if ttl >= 0 and self._hydration_is_stale(dest, ttl):
                    logger.debug(
                        "Top-of-Mind hydrated block is older than %.0fs; forcing re-render",
                        ttl,
                    )
                else:
                    logger.debug("Top-of-Mind artefact is up to date, skipping render")
                    return dest
        return self.render_to_file_hydrated(dest, domain=domain)

    @staticmethod
    def _hydration_is_stale(path: Path, ttl_seconds: float) -> bool:
        """Return True if the hydrated block in *path* is older than TTL.

        The ``<hydrated>`` block (if present) carries a
        ``hydrated_at=`` ISO 8601 attribute written by
        ``render_to_file_hydrated``. If the attribute is missing, or
        cannot be parsed, we conservatively treat the artefact as
        stale (force a re-render). If there is no ``<hydrated>``
        block at all, there is no live RAG/chat content to age —
        return False (the TOML-mtime rule above is sufficient).
        """
        import datetime as _dt
        import re as _re

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return True
        m = _re.search(
            r"<hydrated\s[^>]*\bhydrated_at=\"([^\"]+)\"",
            text,
        )
        if m is None:
            # No <hydrated> block at all — the artefact is pure TOML;
            # let the TOML-mtime check govern. No hydration to age.
            return False
        try:
            hydrated_at = _dt.datetime.fromisoformat(m.group(1))
        except ValueError:
            # Unparseable timestamp — conservative: re-render.
            return True
        # fromisoformat in 3.11+ returns aware datetimes when the input
        # has a tz; in 3.10 and below naive datetimes are returned. If
        # naive, assume UTC (consistent with the rest of Beagle's
        # datetime policy: ``datetime.now(timezone.utc)``).
        if hydrated_at.tzinfo is None:
            hydrated_at = hydrated_at.replace(tzinfo=_dt.UTC)
        now = _dt.datetime.now(_dt.UTC)
        age = (now - hydrated_at).total_seconds()
        return age > ttl_seconds

    def render_to_file_hydrated(self, path: Path, domain: str | None = None) -> Path:
        """Render with placeholders, then hydrate via tom_hydrator, then write.

        This is the E3 (Layered-Tom) integration point. The pure renderer
        produces the XML with ``<hydrator>`` placeholders; the hydrator
        resolves them; the result is atomically written to *path*.

        If the hydrator fails (MCP servers down, network error), the
        unhydrated XML is written instead — the canonical path is never
        allowed to be missing, even at the cost of staleness. The
        watcher in beagle_watchdog.py will detect the missing hydration
        tags and emit a degraded-mode warning.
        """
        # Step 1: pure render with placeholders
        xml, queries = self.render_with_placeholders(domain=domain)

        # Step 2: hydrate (only if there are queries)
        if queries:
            try:
                from .tom_hydrator import hydrate as _hydrate

                xml = _hydrate(xml, queries)
            except (ImportError, RuntimeError, OSError) as exc:
                logger.warning(
                    "render_to_file_hydrated: hydration failed (%s); writing unhydrated XML to %s",
                    exc,
                    path,
                )

        # Step 3: atomic write
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".xml.tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(xml)
            os.replace(tmp, str(path))
        except (OSError, RuntimeError, ValueError):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        logger.info("Rendered hydrated Top-of-Mind artefact → %s (%d bytes)", path, len(xml))
        return path

    # ── internals ─────────────────────────────────────────────────────────

    def _select_guides(self, domain: str | None) -> list[dict]:
        """Select guides: domain → core + domain file; else → universal."""
        if domain is not None and domain in self._KNOWN_DOMAINS:
            guides = self._domain_guides(domain)
        else:
            guides = self._universal_guides()
        self._inject_dynamic_version(guides)
        return guides

    @staticmethod
    def _inject_dynamic_version(guides: list[dict]) -> None:
        """Overwrite any hardcoded environment.pins.package_version with the live
        version from pyproject (single source of truth). Without this, .goosehints
        would emit the stale TOML literal instead of the current package version.
        """
        try:
            live = get_version()
        except (OSError, ValueError, RuntimeError, KeyError):
            return
        for guide in guides:
            pins = guide.get("environment", {}).get("pins")
            if isinstance(pins, dict) and "package_version" in pins:
                pins["package_version"] = live

    def _domain_guides(self, domain: str) -> list[dict]:
        """Return ``[beagle_core_directives, <domain>]`` if both exist.

        Uses ``get_by_stem`` for the domain guide because the loader
        caches by ``meta.name`` (e.g. ``"Python Backend"``) while the
        domain argument is a filename stem (``"python_backend"``).
        """
        guides: list[dict] = []
        core = self.loader.get_by_stem(self.DEFAULT_CORE)
        if core is not None:
            guides.append(core)
        domain_guide = self.loader.get_by_stem(domain)
        if domain_guide is not None:
            guides.append(domain_guide)
        # Fallback if core not found by stem: try meta.name
        if not guides and (core := self.loader.get(self.DEFAULT_CORE)):
            guides.append(core)
        return guides

    def _universal_guides(self) -> list[dict]:
        """Return guides with applies_to = ['*'] only."""
        universal: list[dict] = []
        for name in self.loader.available:
            guide = self.loader.get(name)
            if guide is None:
                continue
            applies = guide.get("meta", {}).get("applies_to", [])
            if "*" in applies:
                universal.append(guide)
        return universal

    def _session_start_guides(self) -> list[dict]:
        """Return guides with applies_to = ['session_start'] only."""
        session_start: list[dict] = []
        for name in self.loader.available:
            guide = self.loader.get(name)
            if guide is None:
                continue
            applies = guide.get("meta", {}).get("applies_to", [])
            if "session_start" in applies:
                session_start.append(guide)
        self._inject_dynamic_version(session_start)
        return session_start

    def _newest_source_mtime(self) -> float:
        """Return the newest mtime among all source TOML files."""
        newest = 0.0
        for path in sorted(self.loader.guides_dir.glob("*.toml")):
            try:
                mtime = path.stat().st_mtime
                if mtime > newest:
                    newest = mtime
            except OSError as exc:
                logger.warning(
                    "Cannot stat guide source %s (%s); it is excluded from the freshness "
                    "check, so an edit to it may not trigger a re-render.",
                    path,
                    exc,
                )
        return newest

    def _render_sections(
        self, guide: dict, indent: int = 4, tier_filter: str | None = None
    ) -> list[str]:
        """Walk a guide dict and render every non-meta section to XML.

        Handles three structural shapes:
          - Flat table: [section] with key = value pairs
          - Array-of-tables: [[section]] with repeated entries
          - Nested tables: [section.subsection] with dotted keys

        Args:
            guide: A parsed style-guide dict.
            indent: Leading-spaces indentation for top-level elements.
            tier_filter: If set (``PATTERN_TIER_LOAD_BEARING`` or
                ``PATTERN_TIER_BACKGROUND``), ``architecture.patterns``
                is filtered to the named tier before emission. ``None``
                emits all patterns unfiltered. v13.21.3 (F3 fix).

        """
        prefix = " " * indent
        lines: list[str] = []

        # We collect sections from the top-level keys, excluding 'meta'
        # which holds metadata, not injection content.
        rendered_sections: set[str] = set()

        for key, value in guide.items():
            if key == "meta":
                continue
            if isinstance(value, dict):
                lines.extend(
                    self._render_dict_section(
                        key, value, indent, rendered_sections, tier_filter=tier_filter
                    )
                )
            elif isinstance(value, list):
                lines.extend(self._render_array_section(key, value, indent, rendered_sections))
            else:
                # Scalar top-level key — rare but handle
                if key not in rendered_sections:
                    lines.append(f"{prefix}<{key}>{self._xml_escape(str(value))}</{key}>")
                    rendered_sections.add(key)

        return lines

    def _render_dict_section(
        self,
        section_name: str,
        data: dict,
        indent: int,
        rendered: set,
        tier_filter: str | None = None,
    ) -> list[str]:
        """Render a [section] or [section.sub] block.

        Recurses into nested dicts.  Scalar values become child elements;
        nested dicts become sub-sections.

        Args:
            section_name: The TOML section name (e.g. ``"architecture"``).
            data: The section's parsed value (a dict).
            indent: Leading-spaces indentation.
            rendered: Set of section names already rendered (to avoid
                duplicate emission from nested and flat walks).
            tier_filter: v13.21.3 (F3 fix). If set, and the section
                is ``architecture``, the ``patterns`` key is filtered
                to the named tier via
                :meth:`_tiered_patterns` before being rendered as a
                scalar list. This is the single point where the
                tier-aware behaviour intersects the general
                dict-walker — by gating on ``section_name == "architecture"``
                we avoid surprising behaviour for any other section
                that happens to have a ``patterns`` key.

        """
        if section_name in rendered:
            return []
        rendered.add(section_name)
        prefix = " " * indent
        lines = [f"{prefix}<{section_name}>"]
        for k, v in data.items():
            if isinstance(v, dict):
                lines.extend(self._render_dict_section(k, v, indent + 2, set()))
            elif isinstance(v, list):
                # v13.21.3 (F3 fix): tier-filter architecture.patterns.
                if tier_filter is not None and section_name == "architecture" and k == "patterns":
                    v = self._tiered_patterns({"architecture": data}, tier=tier_filter)
                lines.extend(self._render_scalar_list(k, v, indent + 2))
            else:
                lines.append(f"{prefix}  <{k}>{self._xml_escape(str(v).strip())}</{k}>")
        lines.append(f"{prefix}</{section_name}>")
        return lines

    def _render_array_section(
        self, section_name: str, data: list, indent: int, rendered: set
    ) -> list[str]:
        """Render [[section]] array-of-tables as a parent with repeated items."""
        if section_name in rendered:
            return []
        rendered.add(section_name)
        prefix = " " * indent
        lines = [f"{prefix}<{section_name}>"]
        for item in data:
            if isinstance(item, dict):
                lines.append(f"{prefix}  <item>")
                for k, v in item.items():
                    if isinstance(v, list | dict):
                        lines.append(f"{prefix}    <{k}>{self._xml_escape(str(v))}</{k}>")
                    else:
                        lines.append(f"{prefix}    <{k}>{self._xml_escape(str(v))}</{k}>")
                lines.append(f"{prefix}  </item>")
            elif isinstance(item, str):
                lines.append(f"{prefix}  <item>{self._xml_escape(item)}</item>")
        lines.append(f"{prefix}</{section_name}>")
        return lines

    def _render_scalar_list(self, name: str, items: list, indent: int) -> list[str]:
        """Render a flat list of scalars (e.g. tools = [...]) as individual elements."""
        prefix = " " * indent
        lines: list[str] = []
        for item in items:
            lines.append(f"{prefix}<{name}>{self._xml_escape(str(item))}</{name}>")
        return lines

    @classmethod
    def _tiered_patterns(
        cls,
        guide: dict,
        tier: str | None = None,
    ) -> list[str]:
        """Filter ``architecture.patterns`` by tier.

        v13.21.3 (F3 fix): the architecture section in
        ``beagle_core_directives.toml`` declares a
        ``load_bearing_patterns`` list of pattern-name prefixes.
        Patterns whose identifier (the text up to the first ``:``)
        matches one of those prefixes are classified as
        ``PATTERN_TIER_LOAD_BEARING``; all other patterns are
        implicitly ``PATTERN_TIER_BACKGROUND``. This matches the
        policy intent: the TOML author declares the small set of
        must-always-include patterns by short name, and the renderer
        does not have to touch the long ``patterns = [...]`` array.

        Args:
            guide: A parsed style-guide dict.
            tier: ``PATTERN_TIER_LOAD_BEARING`` to return only the
                load-bearing subset, ``PATTERN_TIER_BACKGROUND`` to
                return only the background subset, or ``None`` (the
                default) to return all patterns unfiltered.

        Returns:
            The filtered pattern list, in original order. Returns
            ``[]`` if the guide has no ``architecture.patterns`` list
            (e.g. for non-core style guides like
            ``security_baseline.toml``).

        """
        if tier not in (
            None,
            cls.PATTERN_TIER_LOAD_BEARING,
            cls.PATTERN_TIER_BACKGROUND,
        ):
            raise ValueError(
                f"_tiered_patterns: unknown tier {tier!r}; expected "
                f"None, {cls.PATTERN_TIER_LOAD_BEARING!r}, or "
                f"{cls.PATTERN_TIER_BACKGROUND!r}"
            )
        arch = guide.get("architecture", {})
        patterns = arch.get("patterns", []) if isinstance(arch, dict) else []
        if tier is None:
            return list(patterns)
        # The list of load_bearing prefixes is itself a TOML
        # declaration; missing the key means EVERY pattern is
        # load_bearing (conservative — no information lost). The
        # matches are case-insensitive prefix matches on the pattern
        # identifier (text before the first ``:``).
        load_bearing = arch.get("load_bearing_patterns", []) or []
        if not load_bearing:
            return list(patterns)
        load_bearing_norm = {str(p).strip().lower() for p in load_bearing}

        result: list[str] = []
        for p in patterns:
            ident = str(p).split(":", 1)[0].strip().lower()
            is_load = ident in load_bearing_norm
            if (tier == cls.PATTERN_TIER_LOAD_BEARING and is_load) or (
                tier == cls.PATTERN_TIER_BACKGROUND and not is_load
            ):
                result.append(p)
        return result

    @staticmethod
    def _xml_escape(text: str) -> str:
        """Escape <, >, & for safe XML text content.

        Thin wrapper over the canonical :func:`._xml.xml_escape` so the
        renderer, the hydrator, and the injector all escape identically.
        Retained as a staticmethod because internal call sites use
        ``self._xml_escape``.
        """
        return xml_escape(text)

    # ── v13.21.3 (F3 fix): forbidden-list fold infrastructure ──────────

    @classmethod
    def _forbidden_text_hash(cls, rules: list[str]) -> str:
        """Stable sha256 prefix over the joined forbidden-rule list.

        Used as the cache key in :pyattr:`_forbidden_fold_cache` and as
        the suffix of the derived fold_id. A TOML edit changes the hash
        and naturally invalidates the cache.
        """
        import hashlib

        joined = "\n".join(rules)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def _forbidden_meta_pairs(cls, guide: dict) -> tuple[list[str], list[dict] | None]:
        """Return ``(forbidden_rules, meta_or_none)`` for a guide.

        ``meta_or_none`` is the parallel ``forbidden_meta`` list if the
        guide provides it (each element a ``{tier, summary}`` inline
        table); ``None`` if the guide has no per-rule metadata.
        """
        anti = guide.get("anti_patterns", {})
        if not isinstance(anti, dict):
            return [], None
        rules = anti.get("forbidden", [])
        if not isinstance(rules, list):
            return [], None
        meta_raw = anti.get("forbidden_meta", [])
        if not isinstance(meta_raw, list) or len(meta_raw) != len(rules):
            return rules, None
        normalised: list[dict] = []
        for m in meta_raw:
            if not isinstance(m, dict):
                normalised.append({"tier": "background", "summary": ""})
                continue
            normalised.append(
                {
                    "tier": str(m.get("tier", "background")),
                    "summary": str(m.get("summary", "")),
                }
            )
        return rules, normalised

    @classmethod
    def _get_or_build_forbidden_fold(cls, rules: list[str]) -> str | None:
        """Return a stable fold_id for the forbidden list, or None.

        The fold_id is derived from the SHA-256 prefix of the joined
        rules, so a given rule list always maps to the same fold_id
        without leaking UUIDs. Caches the result on the class so
        repeated renders in the same process do not re-build the fold.

        Returns ``None`` if the embedding model or the CompressedStore
        is unavailable — the caller is expected to fall back to inline
        emission in that case.
        """
        if not rules:
            return None
        text_hash = cls._forbidden_text_hash(rules)
        cached = cls._forbidden_fold_cache.get(text_hash)
        if cached is not None:
            return cached

        fold_id = f"agg-forbidden-{text_hash}"
        try:
            from ..context.compressed_store import get_compressed_store

            store = get_compressed_store()
            # Idempotent: if a fold with this id already exists on disk
            # (e.g. from a previous process), reuse it without rebuilding.
            if fold_id in store.list_folds():
                cls._forbidden_fold_cache[text_hash] = fold_id
                return fold_id
        except (ImportError, OSError, RuntimeError, ValueError, AttributeError) as exc:
            # Store unavailable — fall through to build; if build also
            # fails, the caller's fallback path will be triggered.
            logger.warning(
                "Cannot probe the compressed-fold store for %s (%s); rebuilding the "
                "fold instead of reusing the stored one.",
                fold_id,
                exc,
            )

        try:
            from ..context.context_optimizer import ContextOptimizer

            joined = "\n".join(rules)
            opt = ContextOptimizer(enable_turboquant=True)
            opt._aggressive_compression(joined)
            # The aggressive path just stored a fresh fold with a UUID
            # suffix. Promote that fold to our stable fold_id by
            # renaming the on-disk artefacts. This is the only way to
            # get a stable, content-addressed fold_id with the current
            # ContextOptimizer API.
            import shutil as _shutil
            import time as _time

            from ..context.compressed_store import get_compressed_store

            store = get_compressed_store()
            store_dir = store.store_dir
            newest = sorted(
                store_dir.glob("agg-*_manifest.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not newest:
                return None
            src_manifest = newest[0]
            # Only adopt folds created in the last 5 seconds
            # wall-clock-ok: compares against a persisted timestamp
            mtime = src_manifest.stat().st_mtime
            age = _time.time() - mtime  # nosemgrep: aeca-walltime-for-interval
            if age > 5.0:
                return None
            src_id = src_manifest.stem.replace("_manifest", "")
            if src_id == fold_id:
                cls._forbidden_fold_cache[text_hash] = fold_id
                return fold_id
            dst_manifest = store_dir / f"{fold_id}_manifest.json"
            dst_embeddings = store_dir / f"{fold_id}_embeddings.bin"
            _shutil.move(str(src_manifest), str(dst_manifest))
            src_embeddings = store_dir / f"{src_id}_embeddings.bin"
            if src_embeddings.exists():
                _shutil.move(str(src_embeddings), str(dst_embeddings))
            cls._forbidden_fold_cache[text_hash] = fold_id
            return fold_id
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional — fold build is best-effort; failure is logged and the renderer falls back to non-folded output
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "[%s] forbidden fold build failed: %s", cls.__name__, exc
            )
            return None

    def _render_forbidden_section(self, guide: dict, indent: int = 4) -> list[str]:
        """Render the forbidden-list section with tier + fold metadata.

        Output shape::

            <anti_patterns count="34" load_bearing="16" background="18">
              <forbidden tier="load_bearing" index="1" summary="...">
                full text
              </forbidden>
              <forbidden_ref tier="background" index="2" summary="..."/>
              ...
            </anti_patterns>
            <forbidden_fulltext_fold
                fold_id="agg-forbidden-..."
                count="34"
                hash="..."/>

        Load-bearing rules keep their full text inline (they are short
        and the controller needs them on every turn). Background rules
        carry only a 1-line summary and a reference to the fold for the
        full text. The fold is content-addressed, so the runtime can
        query it deterministically via ``query_fold(fold_id, query)``.
        """
        rules, meta = self._forbidden_meta_pairs(guide)
        if not rules:
            return []

        pad = " " * indent
        load_bearing_count = sum(1 for m in (meta or []) if m.get("tier") == "load_bearing")
        background_count = len(rules) - load_bearing_count

        lines: list[str] = []
        lines.append(
            f"{pad}<anti_patterns "
            f'count="{len(rules)}" '
            f'load_bearing="{load_bearing_count}" '
            f'background="{background_count}">'
        )
        for idx, rule in enumerate(rules, 1):
            tier = meta[idx - 1]["tier"] if meta else "background"
            summary = meta[idx - 1]["summary"] if meta else ""
            if tier == "load_bearing":
                esc_rule = self._xml_escape(rule)
                esc_sum = self._xml_escape(summary)
                lines.append(
                    f'{pad}  <forbidden tier="load_bearing" index="{idx}" summary="{esc_sum}">'
                )
                lines.append(f"{pad}    {esc_rule}")
                lines.append(f"{pad}  </forbidden>")
            else:
                esc_sum = self._xml_escape(summary)
                lines.append(
                    f'{pad}  <forbidden_ref tier="background" index="{idx}" summary="{esc_sum}"/>'
                )
        lines.append(f"{pad}</anti_patterns>")

        # Fold reference for the full text (all rules, including the
        # load_bearing ones the controller is seeing inline). The fold
        # is the canonical long-form store; the inline load_bearing
        # block is a fast-path cache.
        fold_id = self._get_or_build_forbidden_fold(rules)
        if fold_id is not None:
            text_hash = self._forbidden_text_hash(rules)
            lines.append(
                f"{pad}<forbidden_fulltext_fold "
                f'fold_id="{fold_id}" '
                f'count="{len(rules)}" '
                f'hash="{text_hash}"/>'
            )
        return lines

    @classmethod
    def reset_forbidden_fold_cache(cls) -> None:
        """Test seam: clear the forbidden-fold cache."""
        cls._forbidden_fold_cache.clear()

    # ── v13.15.7: Beagle-native doctrine delivery channels ─────────────────

    # Hard caps for the new delivery channels.  These keep per-call token
    # cost bounded across MCP bootstrap and subagent injection paths.
    # The doctrine bundle cap. The trimmer enforces this by
    # dropping the *lowest-priority* guides from the tail. The
    # Beagle Core Directives guide is the SSOT for routing/protocol
    # behaviour — it is exempt from trim (it is structurally part
    # of the renderer contract; dropping it would silently
    # downgrade the most important guide, which the doctrine
    # explicitly forbids: "Never disable, downgrade-as-default, or
    # delete a broken feature instead of fixing it at the root").
    # The cap is generous enough to fit the v13.21.3 doctrine-coherence
    # additions (~1.5 KB) plus the PRESERVED_FILE_DISPOSITION rule
    # (~0.6 KB) without trimming the rest of the universal bundle.
    DOCTRINE_BUNDLE_MAX_BYTES = 40_000
    # v13.22.3: 16 KB cap (was 8 KB). Subagents now reliably receive
    # the full EXECUTOR_PROTOCOL contract + the complete Forbidden
    # list without truncation, because most Ollama Cloud models
    # (minimax-m3, glm-5.x, deepseek-v4, kimi-k2.x, gemma4, qwen3.5)
    # accept 32K+ token system directives comfortably and the extra
    # 8 KB costs ~2K model tokens of attention — well within the
    # budget for the routing protocol contract that prevents the
    # very regression we just fixed (silent fallback without
    # controller awareness).
    DOCTRINE_DIRECTIVE_MAX_BYTES = 16_000

    def render_structured(self, domain: str | None = None) -> dict:
        """Render style guides as a structured dict (JSON-friendly).

        Mirrors the content of :meth:`render` but emits a dict instead of
        XML so MCP clients can pass the doctrine through standard JSON
        return paths (e.g. ``beagle_session_bootstrap``).

        The result is capped at ``DOCTRINE_BUNDLE_MAX_BYTES`` when
        serialised; lower-priority guides are dropped from the tail until
        the bundle fits.
        """
        import json as _json

        effective_domain = domain if domain is not None else self.domain
        guides = self._select_guides(effective_domain)

        def _shape(guide: dict) -> dict:
            meta = guide.get("meta", {}) or {}
            shaped: dict = {
                "name": meta.get("name", "unnamed"),
                "applies_to": meta.get("applies_to", []),
                "priority": meta.get("priority", 100),
            }
            # Copy every non-meta section verbatim so consumers see the
            # full TOML surface (CRITICAL_ROUTING_PROTOCOL, architecture,
            # anti_patterns, formatting, validation, secrets, crypto, …).
            for key, value in guide.items():
                if key == "meta":
                    continue
                shaped[key] = value
            return shaped

        shaped_guides = [_shape(g) for g in guides]
        bundle: dict[str, Any] = {
            "version": "1.0",
            "domain": effective_domain or "universal",
            "guides": shaped_guides,
        }

        # Enforce the 25 KB cap by trimming low-priority guides from the
        # tail.  Sort by priority ascending so the lowest-priority guide
        # drops first.  Guides without an explicit priority default to
        # 100 (higher = drop first).
        def _serialised_size(d: dict) -> int:
            return len(_json.dumps(d, ensure_ascii=False))

        if _serialised_size(bundle) > self.DOCTRINE_BUNDLE_MAX_BYTES:
            # Trimming policy (v13.21.3+):
            # 1. Beagle Core Directives is the SSOT — it is exempt from
            #    trim. If it is the only thing keeping the bundle over
            #    the cap, the cap is too small for the real content;
            #    raise rather than drop the SSOT (silent downgrade is
            #    forbidden by the doctrine).
            # 2. Other guides sort by priority asc; the highest-priority
            #    numeric value (lowest semantic priority) drops first.
            # 3. Guides without an explicit priority default to 100
            #    (drop-eligible).
            core_name = "Beagle Core Directives"
            ssot = next(
                (g for g in bundle["guides"] if g.get("name") == core_name),
                None,
            )
            droppable = [g for g in bundle["guides"] if g.get("name") != core_name]
            droppable.sort(
                key=lambda g: (g.get("priority", 100), g.get("name", "")),
                reverse=False,
            )
            bundle["guides"] = ([ssot] if ssot is not None else []) + droppable
            # If we somehow have no droppable guides and the bundle
            # is still over cap, that means Core Directives alone is
            # over the cap. Trim within Core Directives is forbidden
            # (SSOT), so we keep the bundle as-is and log — the cap
            # itself needs adjustment, not the data.
            while droppable and _serialised_size(bundle) > self.DOCTRINE_BUNDLE_MAX_BYTES:
                dropped = droppable.pop()
                bundle["guides"] = ([ssot] if ssot is not None else []) + droppable
                logger.warning(
                    "Dropped guide %r from doctrine bundle to fit %d-byte cap",
                    dropped.get("name"),
                    self.DOCTRINE_BUNDLE_MAX_BYTES,
                )

        return bundle

    def inject_into_directive(self, directive: str, domain: str | None = None) -> str:
        """Prepend a compact form of the doctrine to a ``system_directive``.

        Used by :func:`utils.subprocess_pool.run_goose` (the bounded Goose subprocess pool)
        to deliver doctrine to Beagle-spawned subagents without depending
        on goose's ``tom`` extension.  The compact form keeps the
        CRITICAL_ROUTING_PROTOCOL, all anti-patterns, and the architecture
        patterns; drops verbose formatting and cognitive-loop entries to
        stay under :attr:`DOCTRINE_DIRECTIVE_MAX_BYTES` (16 KB).

        If ``directive`` already begins with the doctrine header, the
        helper is a no-op — safe to call multiple times.
        """
        header = "<!-- Beagle doctrine (compact) -->"
        if directive.startswith(header):
            return directive

        effective_domain = domain if domain is not None else self.domain
        guides = self._select_guides(effective_domain)

        # Two-pass emission so the 16 KB cap can never starve a critical
        # section.  Pass 1 emits *load-bearing* content (routing protocol
        # + all forbiddens) — these must always survive truncation.
        # Pass 2 emits *desirable but droppable* patterns up to the cap.
        critical_parts: list[str] = [header]
        droppable_parts: list[str] = []

        for guide in guides:
            meta = guide.get("meta", {}) or {}
            name = meta.get("name", "unnamed")
            critical_parts.append(f"\n## {name}")

            # v13.21.3: Subagents are LEAF EXECUTORS, not the controller.
            # Emit the EXECUTOR_PROTOCOL section (their role), NOT the
            # controller's CRITICAL_ROUTING_PROTOCOL — the master session
            # already gets that via the tom extension. This is the F2 fix
            # (see tests/test_tom_doctrine_coherence.py).
            for crit_key in (
                "EXECUTOR_PROTOCOL",
                "executor_protocol",
                "executor",
            ):
                section = guide.get(crit_key)
                if isinstance(section, dict):
                    critical_parts.append(f"### {crit_key}")
                    for k, v in section.items():
                        v_str = str(v)
                        if len(v_str) > 160:
                            v_str = v_str[:157] + "..."
                        critical_parts.append(f"- {k}: {v_str}")
                    break

            # Forbiddens — must survive truncation
            anti = guide.get("anti_patterns", {})
            forbidden = anti.get("forbidden", []) if isinstance(anti, dict) else []
            if forbidden:
                critical_parts.append("### Forbidden")
                for f_rule in forbidden:
                    f_str = str(f_rule)
                    if len(f_str) > 160:
                        f_str = f_str[:157] + "..."
                    critical_parts.append(f"- {f_str}")

            # Patterns — droppable if budget tight
            arch = guide.get("architecture", {})
            patterns = arch.get("patterns", []) if isinstance(arch, dict) else []
            if patterns:
                droppable_parts.append("### Patterns")
                for p in patterns:
                    p_str = str(p)
                    if len(p_str) > 160:
                        p_str = p_str[:157] + "..."
                    droppable_parts.append(f"- {p_str}")

        # v13.19.4: Apply a hard upper bound to the critical block too.
        # If even the (truncated) critical content exceeds the cap, we
        # further trim by dropping tail entries until we fit. This
        # guarantees ``test_inject_into_directive_stays_under_cap`` passes
        # even when the style guide content is verbose.
        def _truncate_to_cap(parts: list[str], cap: int) -> list[str]:
            text = "\n".join(parts)
            if len(text) <= cap:
                return parts
            # v13.22.3: Drop tail entries, but never drop a heading
            # (``### <name>`` or ``## <name>``) without also dropping the
            # body lines that follow it — a dangling heading is worse
            # than nothing. Also preserve the first ``### EXECUTOR_PROTOCOL``
            # section intact (subagents must always see their role
            # contract) and the first ``### Forbidden`` block intact
            # (those are the "must survive truncation" rules per the
            # intent in the loop above).
            while len("\n".join(parts)) > cap and len(parts) > 2:
                # Find the last "### <section>" heading; never drop
                # the first such heading (index >= 2 — header + ## name).
                last_heading_idx = -1
                for i in range(len(parts) - 1, 1, -1):
                    line = parts[i]
                    if line.startswith("### ") or line.startswith("## "):
                        last_heading_idx = i
                        break
                if last_heading_idx <= 1:
                    # No more headings we can safely drop — stop.
                    break
                # Drop from the last heading onwards (so its body goes too).
                parts = parts[:last_heading_idx]
            parts.append("<!-- truncated -->")
            return parts

        critical_parts = _truncate_to_cap(critical_parts, self.DOCTRINE_DIRECTIVE_MAX_BYTES)
        critical_block = "\n".join(critical_parts)
        remaining_budget = self.DOCTRINE_DIRECTIVE_MAX_BYTES - len(critical_block)

        droppable_block = ""
        if remaining_budget > 0 and droppable_parts:
            candidate = "\n" + "\n".join(droppable_parts)
            if len(candidate) <= remaining_budget:
                droppable_block = candidate
            else:
                # Take as many leading lines as fit in the remaining budget
                kept_lines: list[str] = []
                used = 1  # for leading newline
                for line in droppable_parts:
                    add = len(line) + 1
                    if used + add > remaining_budget - 24:  # reserve truncation marker
                        break
                    kept_lines.append(line)
                    used += add
                droppable_block = "\n" + "\n".join(kept_lines) + "\n<!-- truncated -->"
                logger.warning(
                    "Doctrine directive patterns truncated to fit %d-byte cap",
                    self.DOCTRINE_DIRECTIVE_MAX_BYTES,
                )

        compact = critical_block + droppable_block
        return compact + "\n\n" + directive

    # ── v13.15.8: Markdown mirror for humans + RAG ingestion ─────────────

    def render_markdown(self, domain: str | None = None) -> str:
        """Render style guides as Markdown for human reading + RAG ingestion.

        Same content as :meth:`render` and :meth:`render_structured` but in
        prose form so the RAG layer can embed it usefully (LanceDB+Kùzu
        prefer text over JSON or XML).  One H2 per guide; bulleted
        patterns and forbiddens; tables for the routing protocol.
        """
        effective_domain = domain if domain is not None else self.domain
        guides = self._select_guides(effective_domain)

        lines: list[str] = [
            "<!-- AUTO-GENERATED by `beagle render-docs`. Do not edit. -->",
            "<!-- Source: src/beagle/style_guides/guides/*.toml -->",
            "<!-- Regenerate after editing any source TOML. -->",
            "",
            "# Beagle Doctrine",
            "",
            f"Domain: **{effective_domain or 'universal'}**",
            "",
        ]

        for guide in guides:
            meta = guide.get("meta", {}) or {}
            name = meta.get("name", "unnamed")
            description = meta.get("description", "")

            lines.append(f"## {name}")
            lines.append("")
            if description:
                lines.append(description)
                lines.append("")

            # Routing protocol (table)
            for routing_key in (
                "CRITICAL_ROUTING_PROTOCOL",
                "critical_routing_protocol",
                "routing",
            ):
                routing = guide.get(routing_key)
                if isinstance(routing, dict):
                    lines.append(f"### {routing_key}")
                    lines.append("")
                    lines.append("| Field | Value |")
                    lines.append("|---|---|")
                    for k, v in routing.items():
                        v_clean = str(v).replace("|", "\\|").replace("\n", " ")
                        lines.append(f"| `{k}` | {v_clean} |")
                    lines.append("")
                    break

            # Architecture patterns
            arch = guide.get("architecture", {})
            patterns = arch.get("patterns", []) if isinstance(arch, dict) else []
            if patterns:
                lines.append("### Patterns")
                lines.append("")
                for p in patterns:
                    lines.append(f"- {p}")
                lines.append("")

            # Anti-patterns
            anti = guide.get("anti_patterns", {})
            forbidden = anti.get("forbidden", []) if isinstance(anti, dict) else []
            if forbidden:
                lines.append("### Forbidden")
                lines.append("")
                for f_rule in forbidden:
                    lines.append(f"- {f_rule}")
                lines.append("")

            # Formatting
            formatting = guide.get("formatting", {})
            if isinstance(formatting, dict) and formatting:
                lines.append("### Formatting")
                lines.append("")
                for k, v in formatting.items():
                    v_clean = str(v).replace("\n", " ")
                    lines.append(f"- **{k}**: {v_clean}")
                lines.append("")

            # Catch-all for other sections (environment, validation, secrets, etc.)
            handled = {
                "meta",
                "CRITICAL_ROUTING_PROTOCOL",
                "critical_routing_protocol",
                "routing",
                "architecture",
                "anti_patterns",
                "formatting",
            }
            for key, value in guide.items():
                if key in handled:
                    continue
                lines.append(f"### {key}")
                lines.append("")
                lines.extend(self._md_render_section(value, level=4))
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _md_render_section(value, level: int = 4) -> list[str]:
        """Recursively render a TOML section into Markdown lines."""
        out: list[str] = []
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, dict | list):
                    out.append(f"{'#' * level} {k}")
                    out.append("")
                    out.extend(GooseTopOfMindRenderer._md_render_section(v, level + 1))
                    out.append("")
                else:
                    out.append(f"- **{k}**: {str(v).replace(chr(10), ' ')}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    out.extend(GooseTopOfMindRenderer._md_render_section(item, level + 1))
                else:
                    out.append(f"- {item}")
        else:
            out.append(str(value))
        return out

    # ── v13.16: Generated steering files ─────────────────────────────────

    def render_project_json(self, path: Path | None = None) -> Path:
        """Generate .goose/project.json from canonical TOML + pyproject + config.

        <invariant>
        project.json describes the Beagle PROJECT (name, version,
        python_version), not whichever repo is being rendered into, so the
        pyproject lookup must never be pointed at ``target_root`` — sibling
        repos and the temp repos used by bulk_render legitimately have no
        pyproject.toml of their own.

        It must also never hard-fail when no pyproject.toml exists at all:
        under a wheel/container install ``_repo_root()`` resolves to the
        installed package directory, which ships no pyproject.toml, and an
        unguarded ``get_pyproject()`` raised FileNotFoundError — that is
        what made ``beagle render-prompts`` fail outright on every
        non-source-checkout install. Fall back to installed package
        metadata instead, which is always present in a wheel.
        </invariant>
        """
        import json

        repo = self._repo_root()
        version = get_version()
        if (repo / "pyproject.toml").is_file():
            pyproject = get_pyproject(repo)
        else:
            # Wheel/container install: no source tree. Read requires-python
            # from the installed distribution metadata instead of raising.
            from importlib.metadata import PackageNotFoundError, metadata

            try:
                requires_python = metadata("beagle").get("Requires-Python")
            except PackageNotFoundError:
                requires_python = None
            pyproject = {"project": {"requires-python": requires_python}} if requires_python else {}
        config = get_config(repo)
        goose_cfg = config.get("goose", {})
        # v1.1.1 (S9): venv path is config-driven, not hardcoded. Read it from
        # the canonical paths config (BEAGLE_VENV_ROOT env or [paths].venv_root);
        # fall back to a sensible default derived from the python interpreter.
        from ..config._config_path import find_config_toml

        try:
            import tomllib as _tomllib

            _cfg_root = find_config_toml()
            _paths_cfg = {}
            if _cfg_root.is_file():
                _paths_cfg = _tomllib.loads(_cfg_root.read_text(encoding="utf-8")).get("paths", {})
            venv = str(
                Path(os.environ.get("BEAGLE_VENV_ROOT") or _paths_cfg.get("venv_root") or "")
                if (os.environ.get("BEAGLE_VENV_ROOT") or _paths_cfg.get("venv_root"))
                else Path(sys.executable).resolve().parent.parent
            )
        except (OSError, ValueError, KeyError, TypeError, _tomllib.TOMLDecodeError):
            venv = str(Path(sys.executable).resolve().parent.parent)

        project = {
            "name": "beagle",
            "version": version,
            "description": "Autonomous agentic workflow system with secure RAG and MCP integration",
            "python_version": pyproject.get("project", {}).get("requires-python", ">=3.12,<3.15"),
            "codebase_root": str(repo),
            "venv_root": venv,
            "data_root": str(Path.home() / ".beagle"),
            "python_path": f"{venv}/bin/python3",
            "entry_point": "cli.py",
            "test_command": f"{venv}/bin/python -m pytest",
            "lint_command": f"{venv}/bin/python -m ruff check",
            "format_command": f"{venv}/bin/python -m ruff format",
            "key_modules": [
                "autonomous_orchestrator",
                "cli",
                "config",
                "graph",
                "nodes",
                "workflow_loader",
                "security",
                "errors",
            ],
            "workflows": get_workflow_list(repo),
            "model_defaults": {
                "provider": goose_cfg.get("provider", "ollama_cloud"),
                "primary": get_primary_model(repo),
                "fallback_chain": get_model_fallback_chain(repo),
            },
        }

        target = path or (repo / ".goose" / "project.json")
        content = json.dumps(project, indent=2) + "\n"
        return self._atomic_write(target, content)

    def _render_pointer_xml(
        self,
        *,
        kind: str,
        version: str,
        title: str,
        sources: list[tuple[str, str, str]],
        variant: str | None = None,
        rag: bool = False,
    ) -> str:
        """Build a thin XML pointer artefact that carries no doctrine of its own.

        Used for the CLAUDE.md / standards.md views: those files must exist
        for their consumers (Claude Code, ``cat`` in recipes, the
        rehydration system) but MUST NOT duplicate behavioural / coding
        directives — a code-maintained copy drifts from the TOML SSOT (it
        historically carried a stale compaction threshold and an execution
        stance that contradicted the routing protocol). The body is XML
        (prompt-substrate is XML, per directive) pointing at the TOML SSOT +
        rendered artefacts; only the file extension stays ``.md`` so existing
        consumers still resolve the path.

        This renderer emits ONLY ``<canonical_sources>`` and an optional
        ``<codebase_context>``. It does not emit ``<directive>`` elements:
        a ``directives`` parameter was accepted and documented here until
        2026-07-28, but no branch of the body ever rendered it and no caller
        ever passed it, so the documented contract was never delivered. The
        ``.goosehints`` session-start pointer builds its own named
        directives inline, on a separate path (see the ".goosehints" branch
        of ``render_all``). Re-add a parameter here only together with the
        code that emits it.
        """
        variant_attr = f' variant="{self._xml_escape(variant)}"' if variant else ""
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<!-- AUTO-GENERATED by `beagle render-prompts`. DO NOT HAND-EDIT. -->",
            "<!-- Thin pointer: doctrine lives in the TOML SSOT, never here. -->",
            f'<beagle_pointer kind="{self._xml_escape(kind)}"{variant_attr} '
            f'version="{self._xml_escape(version)}">',
            f"  <title>{self._xml_escape(title)}</title>",
            "  <notice>This file is a pointer, not a doctrine copy. Do not add "
            "behavioural, routing, or coding directives here — edit the source "
            "TOML in src/beagle/style_guides/guides/ and run "
            "`beagle render-prompts`. A duplicated copy drifts from the SSOT.</notice>",
            "  <canonical_sources>",
        ]
        for name, src_path, desc in sources:
            lines.append(
                f'    <source name="{self._xml_escape(name)}" '
                f'path="{self._xml_escape(src_path)}">'
                f"{self._xml_escape(desc)}</source>"
            )
        lines.append("  </canonical_sources>")
        if rag:
            lines.extend(
                [
                    "  <codebase_context>",
                    '    <use mcp_server="beagle-rag" tool="rag_search">Query the '
                    "codebase RAG for architecture, components, and call graphs "
                    "instead of duplicating them here.</use>",
                    "  </codebase_context>",
                ]
            )
        lines.append("</beagle_pointer>")
        return "\n".join(lines) + "\n"

    def render_standards_md(self, path: Path | None = None) -> Path:
        """Emit .goose/standards.md as a thin XML pointer to the TOML SSOT.

        standards.md is read by the rehydration system
        (context/rehydration.py) and by ``cat`` in recipes, so the file must
        exist — but the coding standards themselves live in the style-guide
        TOMLs (the SSOT). The previous full-content generator drifted: it
        carried a stale compaction threshold (``default 0.7``) and re-stated
        directives owned by [architecture] / [anti_patterns]. This pointer
        directs consumers to the canonical TOML sources instead of
        duplicating them.
        """
        repo = self._repo_root()
        target = path or (repo / ".goose" / "standards.md")
        content = self._render_pointer_xml(
            kind="standards",
            version=get_version(),
            title="Beagle Programming Standards — pointer (SSOT is the style-guide TOML)",
            sources=[
                (
                    "python_backend",
                    "src/beagle/style_guides/guides/python_backend.toml",
                    "Python formatting, imports, typing, and naming conventions",
                ),
                (
                    "security_baseline",
                    "src/beagle/style_guides/guides/security_baseline.toml",
                    "Security boundary validation, allowlists, MCP schema, signing",
                ),
                (
                    "core_directives",
                    "src/beagle/style_guides/guides/beagle_core_directives.toml",
                    "Runtime directives (datetime/except/containment/secrets/logging), architecture, testing, commits",
                ),
            ],
        )
        return self._atomic_write(target, content)

    def render_claude_md(self, path: Path | None = None, *, variant: str = "root") -> Path:
        """Emit CLAUDE.md (root or package) as a thin XML pointer to the SSOT.

        CLAUDE.md is consumed as orientation by Claude Code, by recipes
        (``cat CLAUDE.md``), and by the doctrine's own "read CLAUDE.md before
        editing" rule, so the file must exist at its path. But it MUST NOT
        carry a doctrine copy: the previous generator hardcoded an execution
        stance ("execute directly by default; delegation OPTIONAL") that
        directly contradicts the routing-protocol SSOT ("DELEGATE by
        default"), plus a Key-Directives block duplicating
        [architecture] / [anti_patterns]. This pointer asserts no doctrine of
        its own — it routes every reader to the TOML SSOT, the rendered
        Top-of-Mind, and the codebase RAG, so there is one drift-free source.
        """
        repo = self._repo_root()
        if variant == "root":
            target = path or (repo / "CLAUDE.md")
            scope = "project root"
        else:
            # The importable package lives at repo-root src/ (pyproject.toml
            # sets package-dir {"beagle" = "src"}).
            #
            # v1.0.0: this emitted into `repo / "beagle"`, the pre-rename
            # directory. That path is why the dead beagle/ tree kept
            # reappearing with a lone CLAUDE.md in it — every render
            # recreated the file, and its sibling src/CLAUDE.md (the one
            # readers actually get) was maintained separately.
            target = path or (repo / "src" / "beagle" / "CLAUDE.md")
            scope = "beagle package"
        content = self._render_pointer_xml(
            kind="claude_md",
            variant=variant,
            version=get_version(),
            title=f"Beagle orientation pointer ({scope}) — SSOT is TOML + MCP RAG",
            sources=[
                (
                    "behaviour",
                    "src/beagle/style_guides/guides/beagle_core_directives.toml",
                    "Routing, execution, formatting, architecture, anti-patterns (behavioural SSOT)",
                ),
                (
                    "environment",
                    "src/beagle/style_guides/guides/beagle_environment.toml",
                    "Hardware, runtime paths, version pins, env vars, MCP tool surface",
                ),
                (
                    "top_of_mind",
                    "~/.config/goose/beagle_top_of_mind.xml",
                    "Per-turn rendered doctrine (regenerate: beagle render-hints)",
                ),
            ],
            rag=True,
        )
        return self._atomic_write(target, content)

    # ── v13.21.3: prompt-substrate renderers ─────────────────────────────
    # Per directive: "HINTS ETC SHOULD BE RENDERED BY Beagle" — and the
    # substrate is XML or YAML ONLY (no MD ever). These methods render
    # the goose-config prompt-substrate files from the same TOML
    # sources, with the same single-responsibility surface as
    # render_canonical / render_session_start.

    # Canonical paths — owned by Beagle, never hand-edited.
    SYSTEM_INSTRUCTION_PATH: ClassVar[Path] = (
        Path.home() / ".config" / "goose" / "beagle_system_instruction.xml"
    )
    COMPACTION_PROMPT_PATH: ClassVar[Path] = (
        Path.home() / ".config" / "goose" / "prompts" / "compaction.xml"
    )

    def render_system_instruction(self, output_path: Path | None = None) -> Path:
        """Render ``~/.config/goose/beagle_system_instruction.xml``.

        Source: ``beagle_core_directives.toml`` [system_instruction_template]
        — a literal XML string the TOML author owns. The renderer does
        NOT compose the instruction; it only writes the TOML-owned
        string to the canonical path. This keeps the doctrine SSOT in
        TOML (per anti-pattern: "no hardcoded behavioural directives
        in Python").

        The filename is ``.xml`` (not ``.txt`` as the legacy file used)
        to honour the "ONLY XML AND YAML FOR PROMPTS" directive.

        Returns the written path.
        """
        target = Path(output_path) if output_path else self.SYSTEM_INSTRUCTION_PATH
        content = self._load_template("system_instruction_template")
        return self._atomic_write(target, content)

    def render_compaction_prompt(self, output_path: Path | None = None) -> Path:
        """Render ``~/.config/goose/prompts/compaction.xml``.

        Source: ``beagle_core_directives.toml`` [compaction_prompt_template]
        — a literal XML string the TOML author owns. The renderer
        wraps it in the ``<compaction_prompt>`` root envelope and
        writes to the canonical path. The watcher (``scripts/beagle_watchdog.py``)
        can then grep the rendered file for the required sentinels
        (``beagle_session_bootstrap`` + ``resume_marker``) without the
        renderer having to know what the compaction prompt should say.

        Returns the written path.
        """
        target = Path(output_path) if output_path else self.COMPACTION_PROMPT_PATH
        inner = self._load_template("compaction_prompt_template")
        envelope = (
            "<!-- Beagle Compaction Prompt — auto-generated by "
            "GooseTopOfMindRenderer. -->\n"
            "<!-- Source: beagle_core_directives.toml "
            "[compaction_prompt_template]. -->\n"
            "<!-- Regenerate: beagle render-prompts. -->\n"
            '<compaction_prompt version="2">\n'
            f"{inner}\n"
            "</compaction_prompt>\n"
        )
        return self._atomic_write(target, envelope)

    def _load_template(self, key: str) -> str:
        """Load a literal XML/string template from beagle_core_directives.toml.

        The TOML author places the template as a multi-line string under
        ``[meta]`` with the named key (e.g.
        ``system_instruction_template = '...'``). Returning the raw
        string keeps the renderer dumb: it does not parse, reformat, or
        second-guess the TOML author's content.
        """
        guide = self.loader.get("Beagle Core Directives")
        if guide is None:
            raise KeyError(
                "Beagle Core Directives style guide not loaded — "
                "check style_guides/guides/beagle_core_directives.toml"
            )
        template = guide.get("meta", {}).get(key)
        if not template:
            raise KeyError(
                f"beagle_core_directives.toml [meta].{key} not found — "
                f"TOML author must define the template"
            )
        return str(template)

    def render_all(self, repo_root: Path | None = None) -> dict[str, Path]:
        """Run all generators: hints, docs, project.json, standards.md, CLAUDE.md files,
        AND the goose-config prompt-substrate files (system instruction, compaction).

        The doctrine SSOT is the style-guide TOML. Rendered prompt-substrate is
        XML/YAML (Top-of-Mind, .goosehints, system instruction, compaction).
        CLAUDE.md and .goose/standards.md are thin XML *pointers* to the SSOT
        (not doctrine copies) — they keep a .md extension only so their
        consumers (Claude Code, recipes, rehydration) still resolve the path.
        docs/DOCTRINE.md is the one human-readable .md report. Returns a dict
        mapping artefact name → written path.

        Args:
            repo_root: When set, the per-repo artefacts (.goosehints,
                .goose/standards.md, root CLAUDE.md, package CLAUDE.md) are
                written under this root. The home-canonical artefacts
                (Top-of-Mind, system instruction, compaction prompt,
                doctrine report, project.json) are NOT redirected — they
                always go to the user's home directory and the
                beagle package root respectively, because
                the goose runtime reads them from those canonical paths.

        """
        results: dict[str, Path] = {}
        # Honor an explicit repo_root override, else use the target_root
        # the renderer was constructed with, else fall back to the
        # package's own repo root. The fallback is hoisted to the top of
        # the method so all downstream branches (the .goosehints write,
        # the pkg_claude_md emission, etc.) see a non-None per_repo_root
        # when no --target was passed.
        per_repo_root = (
            Path(repo_root).resolve() if repo_root else (self._target_root or self._repo_root())
        )

        # 1. Top-of-Mind hints (~/.config/goose/beagle_top_of_mind.xml)
        results["hints"] = self.render_canonical()

        # 2. .goosehints (session-start) — XML pointer + named directives.
        # Per directive: .goosehints is XML, not MD. The session-start
        # XML is a self-describing pointer to the TOM (per-turn source of
        # truth) plus two named <directive> elements that the watchdog
        # greps for to verify the contract ("Session Continuity" + "Don't-Stop Gate").
        # v13.22.2: when per_repo_root is set, write .goosehints there so
        # repos outside beagle can carry the same
        # session-start pointer.
        hints_path = per_repo_root / ".goosehints"
        pointer = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<goose_beagle_pointer version="12.0">\n'
            '  <see env_var="GOOSE_MOIM_MESSAGE_FILE"/>\n'
            '  <directive name="Session Continuity">'
            "On EVERY new session, the FIRST tool call MUST be "
            "`beagleutilityserver__beagle_session_bootstrap`. It returns progress, "
            "current phase, recent commits, audit files, and a `resume_point`."
            "</directive>\n"
            '  <directive name="Don\'t-Stop Gate">'
            "After this file is read at session start, the orchestrator MUST "
            "execute the bootstrap call BEFORE any user-visible response. "
            "A text-only reply without the bootstrap tool call ENDS the "
            "agentic loop and is forbidden. RUN TO COMPLETION: drive the "
            "todo to zero; never pause to ask 'continue?'."
            "</directive>\n"
            "</goose_beagle_pointer>\n"
        )
        # Append the session-start guides (applies_to = ["session_start"],
        # e.g. skylon_environment.toml) as a <beagle_session_start> block so
        # they reach goose one-shot at session start. render_session_start()
        # was defined but never wired into the pipeline, so these guides
        # silently never reached the model — the root cause of goose not
        # following skylon-specific directives (e.g. "use sklninstl").
        session_start_block = self.render_session_start()
        if session_start_block:
            pointer += "\n" + session_start_block
        hints_path.write_text(pointer, encoding="utf-8")
        results[".goosehints"] = hints_path
        logger.info("Rendered .goosehints → %s", hints_path)

        # 3. System instruction (~/.config/goose/beagle_system_instruction.xml)
        try:
            results["system_instruction"] = self.render_system_instruction()
        except (KeyError, OSError) as e:
            logger.warning("Skipped system instruction render: %s", e)

        # 4. Compaction prompt (~/.config/goose/prompts/compaction.xml)
        try:
            results["compaction_prompt"] = self.render_compaction_prompt()
        except (KeyError, OSError) as e:
            logger.warning("Skipped compaction prompt render: %s", e)

        # 5. Markdown doctrine
        results["doctrine_md"] = self.render_docs()

        # 6. .goose/project.json
        results["project_json"] = self.render_project_json()

        # 7. .goose/standards.md
        # v13.22.2: when per_repo_root is set, write the standards.md
        # pointer at the target repo instead of the beagle
        # package root. Path-override on the renderer API keeps the
        # existing call signature compatible.
        standards_target = per_repo_root / ".goose" / "standards.md" if per_repo_root else None
        results["standards_md"] = self.render_standards_md(path=standards_target)

        # 8. Root CLAUDE.md
        claude_root_target = per_repo_root / "CLAUDE.md" if per_repo_root else None
        results["root_claude_md"] = self.render_claude_md(path=claude_root_target, variant="root")

        # 9. Package CLAUDE.md — only emitted at the beagle
        # package root (i.e. when the caller did NOT pass --target). When
        # targeting an external repo there is no beagle
        # subpackage to point at, so the root CLAUDE.md already serves
        # the orientation purpose and the pkg entry is the empty Path
        # placeholder. The guard tests BOTH the ctor arg and the
        # render_all() kwarg so neither path can silently emit a package
        # CLAUDE.md at an external target.
        if repo_root is None and self._target_root is None:
            results["pkg_claude_md"] = self.render_claude_md(variant="package")
        else:
            results["pkg_claude_md"] = Path()

        return results

    # ── internal helpers ──────────────────────────────────────────────────

    @property
    def target_root(self) -> Path:
        """Effective per-repo target root.

        Returns the explicit ``target_root`` if the renderer was constructed
        with one; otherwise the package's own repo root. This is the path
        the per-repo artefacts (.goosehints, .goose/standards.md, CLAUDE.md)
        are written under.
        """
        return self._target_root or self._repo_root()

    @staticmethod
    def _repo_root() -> Path:
        """Resolve repository root from this source file.

        Walks up from the package directory until it finds a directory
        containing pyproject.toml or .git. The previous implementation only
        checked one level up (``pkg_dir.parent``), which resolved to
        ``src/beagle`` under the standard src-layout instead of the actual
        repo root — so per-repo artefacts (.goosehints, CLAUDE.md,
        .goose/standards.md) were written to the package dir and never read
        by goose (which reads them from the cwd / repo root).
        """
        pkg_dir = Path(__file__).resolve().parents[1]
        root = pkg_dir
        while root != root.parent:
            if (root / "pyproject.toml").is_file() or (root / ".git").is_dir():
                return root
            root = root.parent
        return pkg_dir

    def _atomic_write(self, target: Path, content: str) -> Path:
        """Atomic write with mkstemp + os.replace."""
        atomic_write_text(target, content, mode=0o644)
        logger.info(
            "Generated %s → %s (%d bytes)",
            target.name,
            target,
            len(content),
        )
        return target

    def render_docs(self, path: Path | None = None, domain: str | None = None) -> Path:
        """Atomic write of the markdown doctrine to ``path``.

        Defaults to ``<repo_root>/docs/DOCTRINE.md`` if path is omitted.
        """
        if path is None:
            # Default: docs/DOCTRINE.md at the repo root (parent of the
            # style_guides directory's package).
            pkg_root = Path(__file__).resolve().parents[1]
            repo_root = pkg_root.parent
            if (repo_root / ".git").is_dir() or (repo_root / "pyproject.toml").is_file():
                target = repo_root / "docs" / "DOCTRINE.md"
            else:
                target = pkg_root / "docs" / "DOCTRINE.md"
        else:
            target = Path(path)

        content = self.render_markdown(domain=domain)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".md.tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(target))
        except (OSError, RuntimeError, ValueError):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        logger.info(
            "Rendered doctrine markdown → %s (%d bytes, %d lines)",
            target,
            len(content),
            content.count("\n"),
        )
        return target


# ── module-level convenience ──────────────────────────────────────────────


def render_canonical(
    domain: str | None = None,
    max_age_seconds: float | None = None,
    *,
    force: bool = False,
) -> Path:
    """One-shot: render style guides to the canonical path.

    Args:
        domain: Optional domain name (e.g. ``"python_backend"``).
        max_age_seconds: Rule-3 TTL override (see
            :meth:`GooseTopOfMindRenderer.render_canonical`).
        force: When true, bypass the cache policy and render immediately.

    """
    return GooseTopOfMindRenderer(domain=domain).render_canonical(
        domain=domain, max_age_seconds=max_age_seconds, force=force
    )


def render_to_string(domain: str | None = None) -> str:
    """One-shot: render style guides to a string.

    Args:
        domain: Optional domain name (e.g. ``"python_backend"``).

    """
    return GooseTopOfMindRenderer(domain=domain).render(domain=domain)


def render_compact(domain: str | None = None) -> str:
    """One-shot: render only load-bearing sections (routing + anti-patterns).

    Capped at 2 KB for safe mid-task injection when context pressure >45%.
    """
    return GooseTopOfMindRenderer(domain=domain).render_compact(domain=domain)


def render_docs(path: Path | None = None, domain: str | None = None) -> Path:
    """One-shot: render style guides as markdown to ``path`` (default docs/DOCTRINE.md)."""
    return GooseTopOfMindRenderer(domain=domain).render_docs(path=path, domain=domain)


def render_all() -> dict[str, Path]:
    """One-shot: render every Beagle artefact (TOM, .goosehints, system
    instruction, compaction prompt, doctrine report, project.json,
    standards.md + CLAUDE.md pointers).

    The doctrine SSOT is the style-guide TOML; rendered prompt-substrate is
    XML/YAML. CLAUDE.md and standards.md are thin XML pointers to the SSOT;
    docs/DOCTRINE.md is the human-readable report. Returns
    ``{artefact_name: path}`` for verification.
    """
    return GooseTopOfMindRenderer().render_all()


def render_system_instruction(output_path: Path | None = None) -> Path:
    """One-shot: render ``~/.config/goose/beagle_system_instruction.xml``.

    Source: ``beagle_core_directives.toml`` [meta].system_instruction_template.
    """
    return GooseTopOfMindRenderer().render_system_instruction(output_path=output_path)


def render_compaction_prompt(output_path: Path | None = None) -> Path:
    """One-shot: render ``~/.config/goose/prompts/compaction.xml``.

    Source: ``beagle_core_directives.toml`` [meta].compaction_prompt_template.
    """
    return GooseTopOfMindRenderer().render_compaction_prompt(output_path=output_path)
