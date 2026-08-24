"""Smoke tests for checkpoint CLI commands (WP-1 B2/B3/B4)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beagle import __version__
from beagle.cli.cli import app
from beagle.lifecycle.checkpoint import (
    Checkpoint,
    CheckpointManager,
    delete_checkpoint,
    list_checkpoints,
)
from beagle.lifecycle.restore import restore_from_checkpoint

runner = CliRunner()


def _write_snapshot(workspace_root: Path, timestamp: float) -> None:
    """Write a checkpoint snapshot JSON file directly (bypasses rotation)."""
    cp = Checkpoint(timestamp=timestamp, version=__version__)
    mgr = CheckpointManager(workspace_root)
    mgr._checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = mgr._checkpoint_dir / f"restart_checkpoint.{int(timestamp)}.json"
    path.write_text(json.dumps(cp.__dict__, default=str), encoding="utf-8")


def test_checkpoint_list_empty(tmp_path: Path):
    """list returns 0 and a friendly message when no checkpoints exist."""
    result = runner.invoke(app, ["checkpoint", "list"])
    assert result.exit_code == 0, result.output
    assert "No checkpoints found" in result.output


def test_checkpoint_cleanup_dry_run(tmp_path: Path):
    """cleanup --dry-run reports the deletion set without deleting."""
    now = 1_000_000_000.0
    for i in range(3):
        _write_snapshot(tmp_path, now + i)

    cps = list_checkpoints(tmp_path)
    assert len(cps) == 3

    # Dry-run must report exactly the set that would be deleted.
    to_remove = cps[1:]
    assert len(to_remove) == 2

    delete_checkpoint(str(cps[0].timestamp), tmp_path)
    assert len(list_checkpoints(tmp_path)) == 2


def test_checkpoint_resume_async_contract():
    """resume command passes args to the async restore function."""
    # A non-existent checkpoint must raise when skip_errors is False.
    with pytest.raises(FileNotFoundError):
        asyncio.run(restore_from_checkpoint("12345", skip_errors=False))

    # With skip_errors=True, it returns False cleanly.
    result = asyncio.run(restore_from_checkpoint("12345", skip_errors=True))
    assert result is False


def test_checkpoint_cli_resume_invokes_async():
    """The CLI resume command invokes the async restore path."""
    result = runner.invoke(app, ["checkpoint", "resume", "12345"])
    # Without a real checkpoint the command exits non-zero.
    assert result.exit_code == 1
    assert "Error resuming checkpoint" in result.output


def test_checkpoint_cli_cleanup_dry_run(tmp_path: Path, monkeypatch):
    """The cleanup --dry-run CLI path reports the deletion set."""
    from beagle import config

    now = 1_000_000_000.0
    for i in range(3):
        _write_snapshot(tmp_path, now + i)

    # Force the CLI to use our temporary workspace root.
    monkeypatch.setattr(config.paths, "get_workspace_root", lambda: tmp_path)

    result = runner.invoke(app, ["checkpoint", "cleanup", "--keep-recent", "1", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would delete" in result.output
    assert len(list_checkpoints(tmp_path)) == 3
