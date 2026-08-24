"""WS4b: CLAUDE.md / standards.md generated views are thin XML pointers.

Per the prompt-substrate constraint (substrate is xml/yaml/toml; ``.md`` only
for human docs), the generated ``CLAUDE.md`` and ``.goose/standards.md`` must
NOT carry a duplicated doctrine copy — they are thin XML *pointers* to the
style-guide TOML SSOT. They keep a ``.md`` extension only because their
consumers read them by that path (Claude Code; recipes that ``cat CLAUDE.md``;
the rehydration system reading ``.goose/standards.md``). ``docs/DOCTRINE.md``
remains the one full human-readable report.

These tests pin the pointer contract so a regression that re-embeds doctrine —
and re-introduces the historical drift (a stale compaction threshold, an
execution stance contradicting the routing protocol) — fails here.

Every generator is exercised with an explicit tmp path so the test never
clobbers the repo's real CLAUDE.md / standards.md.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from beagle.style_guides.render import GooseTopOfMindRenderer

# Markers that would indicate the old full-content doctrine copy crept back in.
# (Checked against the *rendered* pointer, so they are robust against the
# explanatory text in render.py's own docstrings.)
FORBIDDEN_DOCTRINE_MARKERS = (
    "## Key Directives",
    "## Quick Reference",
    "## Package Structure",
    "## Runtime Standards",
    "GOOSE_AUTO_COMPACT_THRESHOLD",
    "default 0.7",
    "OPTIONAL",
    "datetime.now(timezone.utc)",
    "except Exception",
)

POINTER_GENERATORS = (
    ("claude_md_root", "render_claude_md", {"variant": "root"}),
    ("claude_md_package", "render_claude_md", {"variant": "package"}),
    ("standards", "render_standards_md", {}),
)


@pytest.fixture
def renderer() -> GooseTopOfMindRenderer:
    return GooseTopOfMindRenderer()


@pytest.mark.parametrize(
    "label,method,kwargs",
    POINTER_GENERATORS,
    ids=[g[0] for g in POINTER_GENERATORS],
)
def test_generated_md_is_thin_xml_pointer(renderer, tmp_path, label, method, kwargs):
    target = tmp_path / f"{label}.md"
    written = getattr(renderer, method)(path=target, **kwargs)
    assert written == target and target.is_file()
    text = target.read_text(encoding="utf-8")

    # 1. Well-formed XML rooted at <beagle_pointer>.
    root = ET.fromstring(text)
    assert root.tag == "beagle_pointer", (
        f"{label}: generated .md must be a thin <beagle_pointer> XML artefact, got root <{root.tag}>."
    )
    assert "<canonical_sources>" in text

    # 2. Carries NO duplicated doctrine (the drift surface we removed).
    leaked = [m for m in FORBIDDEN_DOCTRINE_MARKERS if m in text]
    assert not leaked, (
        f"{label}: pointer leaks doctrine marker(s) {leaked} — CLAUDE.md / "
        f"standards.md must POINT to the TOML SSOT, never restate it (that is "
        f"how the stale threshold + contradictory execution stance drifted in). "
        f"Edit the source TOML, not the generated view."
    )

    # 3. Thin — a pointer, not a document.
    assert len(text.encode("utf-8")) < 4096, (
        f"{label}: pointer is {len(text)} bytes — too large to be a pointer; "
        f"doctrine content has crept back in."
    )

    # 4. Points at the TOML SSOT.
    src_paths = [s.attrib.get("path", "") for s in root.iter("source")]
    assert any(p.endswith(".toml") for p in src_paths), (
        f"{label}: pointer must reference at least one .toml SSOT source; "
        f"found sources {src_paths}."
    )
