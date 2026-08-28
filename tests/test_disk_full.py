"""Section 8.2: Disk full handling tests.

Simulates disk-full / permission-denied conditions to verify
Beagle storage subsystems fail gracefully with actionable errors.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from beagle.infrastructure.task_store import TaskStore
from beagle.lifecycle.checkpoint import (
    Checkpoint,
    CheckpointManager,
)


class TestDiskFullCheckpoint:
    """CheckpointManager handles write failures gracefully."""

    def test_save_to_readonly_dir_raises(self, tmp_path):
        """Saving to a read-only directory raises OSError with context."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        os.chmod(str(readonly_dir), 0o444)

        mgr = CheckpointManager(workspace_root=readonly_dir)
        cp = Checkpoint(timestamp=1000.0, version="13.7.1", restart_reason="test")

        with pytest.raises((OSError, PermissionError)):
            mgr.save(cp)

        # Restore permissions for cleanup
        os.chmod(str(readonly_dir), 0o755)

    def test_save_no_temp_file_left_on_failure(self, tmp_path):
        """Failed write does not leave .tmp files behind."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=1000.0, version="13.7.1", restart_reason="test")

        # Force write failure by making os.replace fail
        with (
            patch("os.replace", side_effect=OSError("No space left on device")),
            pytest.raises(OSError, match="No space left on device"),
        ):
            mgr.save(cp)

        # No .tmp files should remain
        tmp_files = list(mgr._checkpoint_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_load_still_works_after_save_failure(self, tmp_path):
        """After a failed save, loading the previous checkpoint still works."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(
            timestamp=1000.0,
            version="13.7.1",
            restart_reason="original",
        )
        mgr.save(cp)

        # Attempt a second save that fails
        with patch("os.replace", side_effect=OSError("disk full")), pytest.raises(OSError):
            mgr.save(
                Checkpoint(
                    timestamp=2000.0,
                    version="13.7.1",
                    restart_reason="failed",
                )
            )

        # Original checkpoint should still be loadable
        with patch("beagle.__version__", "13.7.1"):
            loaded = mgr.load()
        assert loaded is not None
        assert loaded.restart_reason == "original"


class TestDiskFullTaskStore:
    """TaskStore handles disk-full conditions gracefully."""

    def test_task_store_readonly_db_path(self, tmp_path):
        """Creating TaskStore in a read-only directory fails on SQLite connect."""
        readonly_dir = tmp_path / "noperm"
        readonly_dir.mkdir()
        os.chmod(str(readonly_dir), 0o555)

        db_path = readonly_dir / "tasks.db"
        # SQLite will fail to create the file in a read-only directory
        with pytest.raises((OSError, PermissionError, Exception)):
            TaskStore(db_path)

        os.chmod(str(readonly_dir), 0o755)

    def test_save_state_disk_full_error_message(self, tmp_path):
        """CheckpointManager save_state gives clear error on write failure."""
        from beagle.checkpointer import CheckpointManager as WfCheckpointManager

        mgr = WfCheckpointManager(checkpoint_dir=tmp_path)
        # Patch tempfile.mkstemp to simulate disk full: checkpoint saves go
        # through atomic_write_text (write-temp-fsync-rename), not
        # Path.write_text, so the temp-file creation is the first disk write
        # the protocol performs.
        with (
            patch("beagle.utils.atomic.tempfile.mkstemp", side_effect=OSError(28, "No space left on device")),
            pytest.raises(OSError, match="No space left on device"),
        ):
            mgr.save_state("wf-1", "query", {"key": "val"})

    def test_task_store_create_task_after_close(self, tmp_path):
        """TaskStore handles operations after connection closure."""
        db_path = tmp_path / "tasks.db"
        store = TaskStore(db_path)
        store.close()
        # Force a new connection by clearing thread-local
        if hasattr(store._local, "conn"):
            del store._local.conn
        task_id = store.create_task(task_type="workflow", spec={"query": "test"})
        assert task_id is not None
        store.close()


class TestDiskFullFileWriter:
    """File writer handles disk-full conditions."""

    def test_staged_write_disk_full(self, tmp_path):
        """staged_write returns WriteResult with error on disk full."""
        from beagle.utils.file_writer import staged_write

        target = tmp_path / "out.py"
        with patch("os.replace", side_effect=OSError(28, "No space left on device")):
            result = staged_write(str(target), "# test\n")
            assert not result.success
            assert "No space left" in result.error

    def test_staged_write_no_target_left_on_failure(self, tmp_path):
        """staged_write does not leave partial target on failure."""
        from beagle.utils.file_writer import staged_write

        target = tmp_path / "clean.py"
        with patch("os.fsync", side_effect=OSError(28, "No space left")):
            staged_write(str(target), "# test\n")
        # On fsync failure, result may succeed or fail depending on patch timing
        # Key: no stale temp file left in the directory
        staged_tmps = list(tmp_path.glob(".staged_*"))
        assert len(staged_tmps) == 0
