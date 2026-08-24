"""SP-5: tests for blocks/xml_blocks/base (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The XMLBlock loader parses
<block> XML files (used by the XML block framework). These exercise loading,
field parsing, and error handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.blocks.xml_blocks.base import XMLBlock, load_xml_block


def test_load_xml_block_full(tmp_path: Path) -> None:
    """A complete <block> parses into an XMLBlock."""
    xml = (
        "<block name='render'>"
        "<description>Render a template</description>"
        "<inputs>text, mode</inputs>"
        "<outputs>output</outputs>"
        "<template>Hello {text}</template>"
        "</block>"
    )
    p = tmp_path / "render.xml"
    p.write_text(xml, encoding="utf-8")
    block = load_xml_block(p)
    assert block.name == "render"
    assert block.description == "Render a template"
    assert block.inputs == ["text", "mode"]
    assert block.outputs == ["output"]
    assert block.template == "Hello {text}"
    assert block.raw_xml == xml


def test_load_xml_block_name_from_filename(tmp_path: Path) -> None:
    """name defaults to the file stem when the root has no name attr."""
    xml = "<block><description>d</description></block>"
    p = tmp_path / "my_block.xml"
    p.write_text(xml, encoding="utf-8")
    block = load_xml_block(p)
    assert block.name == "my_block"


def test_load_xml_block_empty_lists(tmp_path: Path) -> None:
    """Missing inputs/outputs default to empty lists."""
    xml = "<block name='min'><description>d</description></block>"
    p = tmp_path / "min.xml"
    p.write_text(xml, encoding="utf-8")
    block = load_xml_block(p)
    assert block.inputs == []
    assert block.outputs == []
    assert block.template == ""


def test_load_xml_block_rejects_wrong_root(tmp_path: Path) -> None:
    """A non-<block> root raises ValueError."""
    p = tmp_path / "bad.xml"
    p.write_text("<notblock/>", encoding="utf-8")
    with pytest.raises(ValueError):
        load_xml_block(p)


def test_load_xml_block_strips_whitespace(tmp_path: Path) -> None:
    """Inputs/outputs split on commas and strip whitespace."""
    xml = "<block name='x'><inputs> a ,  b , c </inputs></block>"
    p = tmp_path / "x.xml"
    p.write_text(xml, encoding="utf-8")
    block = load_xml_block(p)
    assert block.inputs == ["a", "b", "c"]


def test_xml_block_defaults() -> None:
    """XMLBlock dataclass has safe defaults."""
    b = XMLBlock(name="x")
    assert b.description == ""
    assert b.inputs == []
    assert b.outputs == []
    assert b.template == ""
