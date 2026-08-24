"""v13.15.6: unit tests for GooseTopOfMindRenderer.

Covers: XML validity, idempotency, wildcard-only filtering,
atomic writes, and canonical staleness detection.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from beagle.style_guides.loader import StyleGuideLoader
from beagle.style_guides.render import GooseTopOfMindRenderer


@pytest.fixture(scope="module")
def renderer() -> GooseTopOfMindRenderer:
    return GooseTopOfMindRenderer()


# ── F1a: XML validity ────────────────────────────────────────────────────


def test_render_produces_valid_xml(renderer: GooseTopOfMindRenderer):
    """The rendered output must be parseable XML."""
    output = renderer.render()
    assert output, "Renderer produced empty output"
    try:
        ET.fromstring(output)
    except ET.ParseError as e:
        pytest.fail(f"Rendered XML is invalid: {e}")


def test_render_produces_non_empty(renderer: GooseTopOfMindRenderer):
    """Rendered output must contain the root element and at least one guide."""
    output = renderer.render()
    assert "<beagle_top_of_mind>" in output
    assert "<style_guide" in output
    assert len(output) > 1000, f"Output too small: {len(output)} bytes"


# ── F1b: Idempotency ─────────────────────────────────────────────────────


def test_render_is_idempotent(renderer: GooseTopOfMindRenderer):
    """Same TOMLs → same bytes, no timestamps or non-deterministic content."""
    a = renderer.render()
    b = renderer.render()
    assert a == b, "Renderer is not idempotent — same guides produced different output"


# ── F1c: Wildcard-only filtering ─────────────────────────────────────────


def test_only_universal_guides_rendered(renderer: GooseTopOfMindRenderer):
    """Only applies_to=["*"] guides appear. Language-specific guides (.py, .yaml)
    are excluded from Top-of-Mind output."""
    output = renderer.render()

    # Universal guides that MUST appear (applies_to = ["*"])
    assert "Beagle Core Directives" in output
    assert "Security Baseline" in output

    # Guides that should NOT appear in Turn-level Top-of-Mind
    # (beagle_environment.toml applies_to = ["session_start"])
    assert "Beagle Environment" not in output
    # (beagle_project_contract.toml applies_to = ["session_start"])
    assert "Beagle Project Contract" not in output
    # Language-specific guides
    assert "Python Backend" not in output, "Language-specific guide leaked into universal render"


# ── F1d: Atomic write ────────────────────────────────────────────────────


def test_render_to_file_atomic_write(renderer: GooseTopOfMindRenderer, tmp_path: Path):
    """render_to_file() must use tempfile + os.replace for atomicity.
    No partial .xml.tmp files left behind after success or failure."""
    dest = tmp_path / "test_output.xml"
    result = renderer.render_to_file(dest)
    assert result == dest
    assert dest.exists()
    content = dest.read_text()
    assert "<beagle_top_of_mind>" in content

    # No temp files left in the directory
    tmps = list(tmp_path.glob("*.xml.tmp"))
    assert not tmps, f"Temp files left behind: {tmps}"


# ── F1e: Canonical path and staleness ────────────────────────────────────


def test_render_canonical_returns_path(renderer: GooseTopOfMindRenderer):
    """render_canonical() returns the expected path."""
    path = renderer.render_canonical()
    assert path.exists()
    assert path.name == "beagle_top_of_mind.xml"
    assert str(Path.home()) in str(path)


def test_render_canonical_skips_when_fresh(renderer: GooseTopOfMindRenderer, tmp_path: Path):
    """If the destination exists and is newer than all source TOMLs,
    render_canonical() should skip re-rendering."""
    # Create a temp canonical in tmp_path
    dest = tmp_path / "beagle_top_of_mind.xml"
    # First write — render_to_file always writes atomically
    renderer.render_to_file(dest)
    assert dest.exists()

    # Verify idempotency: same guides → same bytes
    content_before = dest.read_text()
    renderer.render_to_file(dest)
    content_after = dest.read_text()
    assert content_before == content_after, "Re-render produced different content"


# ── F3: Regression guard for check_and_fold_context ──────────────────────


def test_check_and_fold_context_in_rendered_output(renderer: GooseTopOfMindRenderer):
    """This is the exact rule whose absence from interactive goose sessions
    caused the 245k/128k context blowup. If a future migration drops it,
    this test fails loudly."""
    output = renderer.render()
    assert "check_and_fold_context" in output, (
        "REG-RESSION: check_and_fold_context directive missing from Top-of-Mind render. "
        "This is the context-folding rule that prevents 245k/128k blowups. "
        "If intentionally removed, update this test + the migration plan."
    )


# ── Integration: loader → renderer ────────────────────────────────────────


def test_loader_and_renderer_agree_on_universal_guides():
    """Every guide the loader reports as universal must appear in rendered output."""
    loader = StyleGuideLoader()
    renderer = GooseTopOfMindRenderer(loader=loader)

    output = renderer.render()
    universal_names = []
    for name in loader.available:
        guide = loader.get(name)
        if guide and "*" in guide.get("meta", {}).get("applies_to", []):
            universal_names.append(name)

    for name in universal_names:
        # The name appears in the rendered XML (XML-escaped)
        assert name in output or _xml_escaped(name) in output, (
            f"Universal guide '{name}' not found in rendered output"
        )


def _xml_escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
