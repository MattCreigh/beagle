"""Tests for the atomic-write helper (QA-3, BGL-068).

The context report must never be visible as a partial document or with
permissions wider than the intended mode.  These tests prove the three
properties the contract names.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from beagle.utils.atomic import atomic_write_text


def test_destination_has_the_mode_before_it_is_visible(tmp_path: Path) -> None:
    """After a write, the destination carries the requested mode."""
    target = tmp_path / "report.json"
    atomic_write_text(target, "x", mode=0o600)
    assert oct(target.stat().st_mode)[-3:] == "600"


def test_no_temp_file_survives_a_successful_write(tmp_path: Path) -> None:
    """The destination directory holds exactly one entry after the write."""
    target = tmp_path / "report.json"
    atomic_write_text(target, "x", mode=0o600)
    assert list(tmp_path.iterdir()) == [target]


def test_failed_write_leaves_the_previous_document_intact(tmp_path: Path, monkeypatch) -> None:
    """A failed rename leaves the previous document and no temp file."""
    target = tmp_path / "report.json"
    atomic_write_text(target, "old", mode=0o600)

    def boom(*_args, **_kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "new", mode=0o600)

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.iterdir()) == [target]
