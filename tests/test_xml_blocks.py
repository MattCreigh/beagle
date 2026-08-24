"""Tests for XML block contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.blocks.xml_blocks.base import load_xml_block

# The stdlib blocks ship inside the package, which lives at repo-root src/
# (pyproject.toml: package-dir {"beagle" = "src"}).
#
# v1.0.0: each test built this path inline as
# `Path(__file__).parent.parent / "beagle" / "beagle" / "blocks" / ...` — the
# pre-rename *nested* layout (beagle/beagle/), which has not existed since the
# nested duplicate package was dissolved. Asserting the directory exists turns
# a future layout move into one loud failure instead of three FileNotFoundErrors.
STDLIB_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "beagle" / "blocks" / "xml_blocks" / "stdlib"
)
assert STDLIB_DIR.is_dir(), f"stdlib blocks not found at {STDLIB_DIR} — update STDLIB_DIR"


def test_load_plan_xml():
    block = load_xml_block(STDLIB_DIR / "plan.xml")
    assert block.name == "plan"
    assert "Generate a plan" in block.description
    assert "plan" in block.outputs


def test_load_verify_xml():
    block = load_xml_block(STDLIB_DIR / "verify.xml")
    assert block.name == "verify"
    assert "success" in block.outputs


def test_load_decide_xml():
    block = load_xml_block(STDLIB_DIR / "decide.xml")
    assert block.name == "decide"
    assert "decision" in block.outputs


def test_load_invalid_root(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<notblock></notblock>")
    with pytest.raises(ValueError):
        load_xml_block(bad)
