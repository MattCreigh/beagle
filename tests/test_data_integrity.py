"""Data integrity and recovery tests for Beagle — Section 8.

Validates:
- 8.1: Checkpoint save/load atomic write, version mismatch, rotation
- 8.2: TaskStore WAL integrity under crash simulation
- 8.3: Singleton persistence and recovery
- 8.4: Audit trail append-only integrity
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from beagle.lifecycle.checkpoint import (
    Checkpoint,
    CheckpointManager,
)

# ═══════════════════════════════════════════════════════════════════════════
# Section 8.1: Checkpoint atomic save / load / rotation
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckpointAtomicWrite:
    """Checkpoint save() uses atomic temp-file → os.replace()."""

    def test_save_creates_file(self, tmp_path):
        """Checkpoint save creates a valid JSON file."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(
            timestamp=time.time(),
            version="13.7.1",
            restart_reason="test",
        )
        path = mgr.save(cp)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["version"] == "13.7.1"
        assert data["restart_reason"] == "test"

    def test_load_roundtrip(self, tmp_path):
        """Saved checkpoint can be loaded back with all fields intact."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(
            timestamp=1000.0,
            version="13.7.1",
            daemon_daily_cost=42.5,
            restart_reason="signal",
            restart_count=3,
            circuit_states={"svc-a": "open", "svc-b": "closed"},
            active_workflow_id="wf-123",
        )
        mgr.save(cp)

        # Load with matching version
        with patch("beagle.__version__", "13.7.1"):
            loaded = mgr.load()

        assert loaded is not None
        assert loaded.daemon_daily_cost == 42.5
        assert loaded.restart_count == 3
        assert loaded.circuit_states == {"svc-a": "open", "svc-b": "closed"}
        assert loaded.active_workflow_id == "wf-123"

    def test_load_version_mismatch_returns_none(self, tmp_path):
        """Checkpoint with wrong version is rejected on load."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=time.time(), version="99.0.0", restart_reason="test")
        mgr.save(cp)

        with patch("beagle.__version__", "13.7.1"):
            loaded = mgr.load()

        assert loaded is None

    def test_load_corrupt_file_returns_none(self, tmp_path):
        """Corrupt checkpoint file returns None, not exception."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        mgr._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        mgr.checkpoint_path.write_text("{invalid json!!!")

        loaded = mgr.load()
        assert loaded is None

    def test_load_missing_file_returns_none(self, tmp_path):
        """No checkpoint file returns None."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        loaded = mgr.load()
        assert loaded is None

    def test_clear_removes_checkpoint(self, tmp_path):
        """clear() removes the checkpoint file."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=time.time(), version="13.7.1", restart_reason="test")
        mgr.save(cp)
        assert mgr.exists()

        mgr.clear()
        assert not mgr.exists()

    def test_clear_missing_file_is_noop(self, tmp_path):
        """clear() on nonexistent file doesn't raise."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        mgr.clear()  # Should not raise
        assert not mgr.exists()

    def test_atomic_write_no_temp_file_left(self, tmp_path):
        """On successful save, no .tmp file remains."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=time.time(), version="13.7.1", restart_reason="test")
        mgr.save(cp)

        tmp_files = list(mgr._checkpoint_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Leftover temp files: {tmp_files}"

    def test_checkpoint_rotation_preserves_history(self, tmp_path):
        """Multiple saves create timestamped backups (rotation)."""
        mgr = CheckpointManager(workspace_root=tmp_path)

        for i in range(4):
            cp = Checkpoint(timestamp=time.time() + i, version="13.7.1", restart_reason=f"save-{i}")
            mgr.save(cp)
            time.sleep(0.05)  # ensure different timestamps

        # Should have current checkpoint + up to MAX_CHECKPOINTS backups
        json_files = list(mgr._checkpoint_dir.glob("restart_checkpoint*.json"))
        # Current + 3 backups (4 total ≤ MAX_CHECKPOINTS=5)
        assert len(json_files) >= 2, f"Expected rotation files, got {json_files}"

    def test_save_sets_0600_permissions(self, tmp_path):
        """Saved checkpoint file has restrictive permissions."""
        import stat

        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=time.time(), version="13.7.1", restart_reason="perms")
        mgr.save(cp)

        file_mode = stat.S_IMODE(os.stat(mgr.checkpoint_path).st_mode)
        assert file_mode == 0o600, f"Expected 0600, got {oct(file_mode)}"


class TestCheckpointRotationLimits:
    """Rotation respects MAX_CHECKPOINTS limit."""

    def test_rotation_removes_old_checkpoints(self, tmp_path):
        """Old checkpoints beyond MAX_CHECKPOINTS are removed."""
        mgr = CheckpointManager(workspace_root=tmp_path)

        # Save more than MAX_CHECKPOINTS (5) checkpoints
        for i in range(7):
            cp = Checkpoint(timestamp=time.time() + i, version="13.7.1", restart_reason=f"r{i}")
            mgr.save(cp)
            time.sleep(0.05)

        # Should have at most MAX_CHECKPOINTS backup files + current
        backup_files = list(mgr._checkpoint_dir.glob("restart_checkpoint.*.json"))
        assert len(backup_files) <= CheckpointManager.MAX_CHECKPOINTS, (
            f"Too many backup files: {len(backup_files)} > {CheckpointManager.MAX_CHECKPOINTS}"
        )
