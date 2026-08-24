"""Tests for beagle_session_bootstrap MCP tool (v13.12.9).

Format upgraded MD → XML for AI-authored artifacts. Reader prefers .xml,
falls back to .md for back-compat. Covers: missing .beagle dir, fresh + stale
detection on either format, XML resume_point heuristic, audit/ preview, git
metadata fields.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from beagle.infrastructure.tools._impl import (
    _detect_resume_point,
    _list_audit_files,
    _read_progress_md,
    beagle_session_bootstrap,
)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake repo root with .beagle/ and audit/ structure."""
    (tmp_path / ".beagle").mkdir()
    (tmp_path / "audit").mkdir()
    monkeypatch.setattr(
        "beagle.infrastructure.tools._impl._repo_root",
        lambda: tmp_path,
    )
    return tmp_path


def test_read_progress_missing_returns_exists_false(fake_repo: Path) -> None:
    result = _read_progress_md(fake_repo)
    assert result["exists"] is False


def test_read_progress_xml_preferred(fake_repo: Path) -> None:
    """When both .xml and .md exist, the XML version wins."""
    (fake_repo / ".beagle" / "progress.xml").write_text(
        '<?xml version="1.0"?><beagle_progress><next_step>from_xml</next_step></beagle_progress>'
    )
    (fake_repo / ".beagle" / "progress.md").write_text("NEXT_STEP: from_md\n")
    result = _read_progress_md(fake_repo)
    assert result["exists"] is True
    assert result["format"] == "xml"
    assert "from_xml" in result["content"]


def test_read_progress_md_legacy_fallback(fake_repo: Path) -> None:
    (fake_repo / ".beagle" / "progress.md").write_text("NEXT_STEP: legacy\n")
    result = _read_progress_md(fake_repo)
    assert result["exists"] is True
    assert result["format"] == "md"
    assert "legacy" in result["content"]


def test_read_progress_stale_detection(fake_repo: Path) -> None:
    p = fake_repo / ".beagle" / "progress.xml"
    p.write_text("<beagle_progress><next_step>old</next_step></beagle_progress>")
    old_mtime = time.time() - (30 * 86400)
    os.utime(p, (old_mtime, old_mtime))
    result = _read_progress_md(fake_repo)
    assert result["stale_days"] >= 29
    assert result["is_stale"] is True


def test_detect_resume_point_xml_element() -> None:
    progress = {
        "exists": True,
        "content": "<beagle_progress><step_just_done>x</step_just_done><next_step>Tranche B start</next_step></beagle_progress>",
    }
    phase: dict[str, object] = {"exists": False}
    assert _detect_resume_point(progress, phase) == "Tranche B start"


def test_detect_resume_point_md_legacy_line() -> None:
    progress = {
        "exists": True,
        "content": "STEP_JUST_DONE: Tranche A\nNEXT_STEP: Tranche B start\n",
    }
    phase: dict[str, object] = {"exists": False}
    assert _detect_resume_point(progress, phase) == "Tranche B start"


def test_detect_resume_point_skips_placeholder() -> None:
    progress = {
        "exists": True,
        "content": "<beagle_progress><next_step>(await user instruction)</next_step></beagle_progress>",
    }
    phase: dict[str, object] = {"exists": False}
    assert _detect_resume_point(progress, phase) is None


def test_detect_resume_point_phase_takes_priority() -> None:
    progress = {
        "exists": True,
        "content": "<beagle_progress><next_step>from progress</next_step></beagle_progress>",
    }
    phase = {"exists": True, "content": "<phase><next_step>from phase</next_step></phase>"}
    assert _detect_resume_point(progress, phase) == "from phase"


def test_list_audit_files_xml_and_md(fake_repo: Path) -> None:
    (fake_repo / "audit" / "00_test.xml").write_text("<audit>line</audit>\n")
    (fake_repo / "audit" / "01_test.md").write_text("# Audit 01\nLine 2\n")
    files = _list_audit_files(fake_repo)
    assert len(files) == 2
    paths = {f["path"] for f in files}
    assert "audit/00_test.xml" in paths
    assert "audit/01_test.md" in paths


def test_list_audit_files_empty_when_no_audit_dir(tmp_path: Path) -> None:
    assert _list_audit_files(tmp_path) == []


@pytest.mark.asyncio
async def test_session_bootstrap_returns_valid_json(fake_repo: Path) -> None:
    raw = await beagle_session_bootstrap()
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    for key in (
        "cwd",
        "repo_root",
        "git_branch",
        "git_recent_commits",
        "progress_md",
        "current_phase_md",
        "audit_files",
        "resume_point",
    ):
        assert key in payload, f"missing key: {key}"


@pytest.mark.asyncio
async def test_session_bootstrap_surfaces_resume_point_xml(fake_repo: Path) -> None:
    (fake_repo / ".beagle" / "progress.xml").write_text(
        '<?xml version="1.0"?><beagle_progress>'
        "<step_just_done>tranche A done</step_just_done>"
        "<next_step>deploy wheel</next_step></beagle_progress>"
    )
    raw = await beagle_session_bootstrap()
    payload = json.loads(raw)
    assert payload["resume_point"] == "deploy wheel"


@pytest.mark.asyncio
async def test_session_bootstrap_handles_missing_beagle_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "beagle.infrastructure.tools._impl._repo_root",
        lambda: tmp_path,
    )
    raw = await beagle_session_bootstrap()
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["progress_md"]["exists"] is False
    assert payload["audit_files"] == []
    assert payload["resume_point"] is None
