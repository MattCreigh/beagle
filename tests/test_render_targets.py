"""Tests for the axis-1 render-target interface (C1).

These verify that the renderer emits directives through the target
interface: the goosehints / claude_md / top_of_mind_xml file targets write
their file, the mcp_resource target returns a payload and writes nothing,
and the renderer's public ``emit`` method routes correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.style_guides.render import GooseTopOfMindRenderer
from beagle.style_guides.targets.base import EmitOptions, TargetRegistry


@pytest.fixture
def renderer() -> GooseTopOfMindRenderer:
    """A fresh renderer instance."""
    return GooseTopOfMindRenderer()


def test_target_registry_names(renderer: GooseTopOfMindRenderer) -> None:
    """The four built-in targets are registered."""
    names = TargetRegistry.names()
    assert {"goosehints", "claude_md", "top_of_mind_xml", "mcp_resource"} <= set(names)


def test_target_registry_unknown_raises(renderer: GooseTopOfMindRenderer) -> None:
    """An unknown target raises KeyError."""
    with pytest.raises(KeyError):
        renderer.emit("no_such_target")


def test_goosehints_target_writes_file(tmp_path: Path, renderer: GooseTopOfMindRenderer) -> None:
    """The goosehints target writes a .goosehints file in the scope dir."""
    status = renderer.emit("goosehints", scope=tmp_path, target_dir=tmp_path)
    assert str(tmp_path / ".goosehints") in status
    written = tmp_path / ".goosehints"
    assert written.is_file()
    content = written.read_text(encoding="utf-8")
    assert len(content) > 0
    assert "beagle" in content.lower()


def test_claude_md_target_writes_file(tmp_path: Path, renderer: GooseTopOfMindRenderer) -> None:
    """The claude_md target writes a CLAUDE.md file."""
    status = renderer.emit("claude_md", scope=tmp_path, target_dir=tmp_path)
    assert str(tmp_path / "CLAUDE.md") in status
    assert (tmp_path / "CLAUDE.md").is_file()


def test_mcp_resource_returns_payload_and_writes_nothing(
    tmp_path: Path, renderer: GooseTopOfMindRenderer
) -> None:
    """The mcp_resource target returns a payload and writes no file."""
    status = renderer.emit("mcp_resource", scope=tmp_path, target_dir=tmp_path)
    assert "mcp_resource" in status
    assert "payload_chars" in status
    assert not (tmp_path / "beagle_top_of_mind.xml").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".goosehints").exists()


def test_direct_emit_interface(renderer: GooseTopOfMindRenderer) -> None:
    """The module-level emit() routes through the registry."""
    from beagle.style_guides.targets.base import emit

    status = emit(
        "mcp_resource",
        "<x/>",
        EmitOptions(scope=Path("/tmp"), layers=("global",)),
    )
    assert "payload_chars" in status
