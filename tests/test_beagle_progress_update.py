"""Tests for beagle_progress_update MCP tool (v13.12.9).

Format upgraded MD → XML (AI-authored, AI-consumed). Covers: validation,
atomic write, XML well-formedness, MD-legacy cleanup, missing-arg rejection.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from beagle.infrastructure.tools._impl import (
    beagle_progress_update,
)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "beagle.infrastructure.tools._impl._repo_root",
        lambda: tmp_path,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_progress_update_writes_xml_format(fake_repo: Path) -> None:
    raw = await beagle_progress_update(
        step_just_done="Finished Tranche D",
        next_step="Build wheel",
        remaining=["Tranche B", "Tranche C", "Tranche E"],
        critical_context="version=13.12.9",
    )
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["format"] == "xml"
    target = fake_repo / ".beagle" / "progress.xml"
    assert target.exists()
    root = ET.parse(target).getroot()
    assert root.tag == "beagle_progress"
    assert (root.findtext("step_just_done") or "").strip() == "Finished Tranche D"
    assert (root.findtext("next_step") or "").strip() == "Build wheel"
    _elem = root.find("remaining_steps")
    steps = [s.text for s in _elem] if _elem is not None else []
    assert steps == ["Tranche B", "Tranche C", "Tranche E"]
    assert (root.findtext("critical_context") or "").strip() == "version=13.12.9"


@pytest.mark.asyncio
async def test_progress_update_handles_empty_remaining(fake_repo: Path) -> None:
    raw = await beagle_progress_update(step_just_done="single-step", next_step="done")
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    target = fake_repo / ".beagle" / "progress.xml"
    root = ET.parse(target).getroot()
    remaining = root.find("remaining_steps")
    assert remaining is not None and len(list(remaining)) == 0


@pytest.mark.asyncio
async def test_progress_update_xml_escapes_special_chars(fake_repo: Path) -> None:
    raw = await beagle_progress_update(
        step_just_done="ran <cmd> & got 'quote' \"dq\"",
        next_step="<next>",
    )
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    # Re-parse — if escaping is broken, ET.parse raises.
    root = ET.parse(fake_repo / ".beagle" / "progress.xml").getroot()
    assert "<cmd>" in (root.findtext("step_just_done") or "")
    assert "<next>" in (root.findtext("next_step") or "")


@pytest.mark.asyncio
async def test_progress_update_rejects_blank_step(fake_repo: Path) -> None:
    raw = await beagle_progress_update(step_just_done="", next_step="x")
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert "step_just_done" in payload["message"]


@pytest.mark.asyncio
async def test_progress_update_rejects_blank_next(fake_repo: Path) -> None:
    raw = await beagle_progress_update(step_just_done="x", next_step="   ")
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert "next_step" in payload["message"]


@pytest.mark.asyncio
async def test_progress_update_creates_beagle_dir_if_missing(fake_repo: Path) -> None:
    beagle_dir = fake_repo / ".beagle"
    if beagle_dir.exists():
        for f in beagle_dir.iterdir():
            f.unlink()
        beagle_dir.rmdir()
    assert not beagle_dir.exists()
    raw = await beagle_progress_update(step_just_done="x", next_step="y")
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert (fake_repo / ".beagle" / "progress.xml").exists()


@pytest.mark.asyncio
async def test_progress_update_overwrites_xml(fake_repo: Path) -> None:
    beagle_dir = fake_repo / ".beagle"
    beagle_dir.mkdir(exist_ok=True)
    (beagle_dir / "progress.xml").write_text(
        '<?xml version="1.0"?><beagle_progress><next_step>OLD</next_step></beagle_progress>'
    )
    await beagle_progress_update(step_just_done="new", next_step="newer")
    root = ET.parse(beagle_dir / "progress.xml").getroot()
    assert (root.findtext("next_step") or "").strip() == "newer"


@pytest.mark.asyncio
async def test_progress_update_removes_legacy_md(fake_repo: Path) -> None:
    beagle_dir = fake_repo / ".beagle"
    beagle_dir.mkdir(exist_ok=True)
    legacy = beagle_dir / "progress.md"
    legacy.write_text("# stale legacy\nNEXT_STEP: old\n")
    assert legacy.exists()
    await beagle_progress_update(step_just_done="x", next_step="y")
    assert (beagle_dir / "progress.xml").exists()
    assert not legacy.exists(), "legacy progress.md should be removed once XML is written"
