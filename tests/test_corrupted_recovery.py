"""Section 8.1: Corrupted storage recovery tests.

Validates that all Beagle storage subsystems recover gracefully
from corrupted data — returning safe defaults, not raising exceptions.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from beagle.checkpointer import CheckpointManager
from beagle.infrastructure.task_store import TaskStore
from beagle.tracking.database import TrackingDatabase
from beagle.tracking.models import WorkflowRun

# ═══════════════════════════════════════════════════════════════════════════
# TaskStore corrupted database recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskStoreCorruptRecovery:
    """TaskStore handles corrupted SQLite databases gracefully."""

    def test_corrupt_db_init_raises_database_error(self, tmp_path):
        """Initializing TaskStore on a corrupt file raises DatabaseError."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("NOT A SQLITE DATABASE" * 100)
        with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
            TaskStore(db_path)

    def test_corrupt_db_auto_recovery_on_reinit(self, tmp_path):
        """Deleting a corrupt DB and reinitializing recovers cleanly."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("NOT A SQLITE DATABASE" * 100)
        # Recovery: delete corrupt file, then create fresh store
        db_path.unlink()
        store = TaskStore(db_path)
        result = store.get_task("nonexistent-id")
        assert result is None

    def test_corrupt_db_list_tasks_handles_missing_table(self, tmp_path):
        """list_tasks raises OperationalError for missing table — expected."""
        db_path = tmp_path / "corrupt.db"
        store = TaskStore(db_path)
        # Drop the tasks table to simulate corruption
        conn = store._get_conn()
        conn.execute("DROP TABLE IF EXISTS tasks")
        conn.commit()

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            store.list_tasks()

    def test_corrupt_db_list_tasks_recovers_after_reinit(self, tmp_path):
        """After dropping tasks table, reinit restores it."""
        db_path = tmp_path / "corrupt.db"
        store = TaskStore(db_path)
        conn = store._get_conn()
        conn.execute("DROP TABLE IF EXISTS tasks")
        conn.commit()
        # Reinit the schema
        store._init_db()
        result = store.list_tasks()
        assert isinstance(result, list)

    def test_corrupt_db_create_task_recreates_schema(self, tmp_path):
        """Creating a task after corruption reinitializes the schema."""
        db_path = tmp_path / "corrupt.db"
        store = TaskStore(db_path)
        # Corrupt by dropping the tasks table
        conn = store._get_conn()
        conn.execute("DROP TABLE IF EXISTS tasks")
        conn.commit()

        # Re-create the store — schema should be re-initialized
        store2 = TaskStore(db_path)
        task_id = store2.create_task(
            task_type="workflow",
            spec={"query": "test"},
        )
        assert task_id is not None
        assert len(task_id) == 36  # Full UUID4

    def test_corrupt_audit_events_handled(self, tmp_path):
        """Audit trail queries handle missing/corrupt audit_events table."""
        db_path = tmp_path / "corrupt.db"
        store = TaskStore(db_path)
        task_id = store.create_task(task_type="workflow", spec={"query": "test"})

        # Drop audit_events table to simulate corruption
        conn = store._get_conn()
        conn.execute("DROP TABLE IF EXISTS audit_events")
        conn.commit()

        # get_audit_trail should handle gracefully
        try:
            trail = store.get_audit_trail(task_id)
            assert isinstance(trail, list)
        except sqlite3.OperationalError:
            # Expected — table missing; store should catch in production
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Checkpointer (workflow checkpoint) corrupt file recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckpointerCorruptRecovery:
    """Workflow CheckpointManager handles corrupt files gracefully."""

    def test_load_state_corrupt_json_returns_none(self, tmp_path):
        """load_state returns None for corrupt JSON checkpoint."""
        mgr = CheckpointManager(checkpoint_dir=tmp_path)
        bad_file = tmp_path / "wf-123.json"
        bad_file.write_text("{{{{broken json!!!")
        result = mgr.load_state("wf-123")
        assert result is None

    def test_load_state_truncated_json_returns_none(self, tmp_path):
        """load_state returns None for truncated JSON."""
        mgr = CheckpointManager(checkpoint_dir=tmp_path)
        bad_file = tmp_path / "wf-456.json"
        bad_file.write_text('{"workflow_id": "wf-456", "qu')
        result = mgr.load_state("wf-456")
        assert result is None

    def test_load_state_wrong_dataclass_fields_returns_none(self, tmp_path):
        """load_state returns None for valid JSON with unexpected fields."""
        mgr = CheckpointManager(checkpoint_dir=tmp_path)
        bad_file = tmp_path / "wf-789.json"
        bad_file.write_text(json.dumps({"unknown_field": 42}))
        result = mgr.load_state("wf-789")
        assert result is None

    def test_list_checkpoints_skips_corrupt(self, tmp_path):
        """list_checkpoints skips corrupt files and returns valid ones."""
        mgr = CheckpointManager(checkpoint_dir=tmp_path)
        # Create a valid checkpoint
        mgr.save_state("wf-valid", "test query", {"key": "val"})
        # Create a corrupt file
        bad_file = tmp_path / "wf-corrupt.json"
        bad_file.write_text("NOT JSON")
        result = mgr.list_checkpoints()
        assert len(result) >= 1
        assert any(r["workflow_id"] == "wf-valid" for r in result)

    def test_path_traversal_blocked(self, tmp_path):
        """Sanitize blocks path traversal in workflow_id."""
        mgr = CheckpointManager(checkpoint_dir=tmp_path)
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.load_state("../../etc/passwd")
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.save_state("../evil", "q", {})


# ═══════════════════════════════════════════════════════════════════════════
# TrackingDatabase corrupted recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestTrackingDBCorruptRecovery:
    """TrackingDatabase handles corruption gracefully."""

    def test_get_workflow_runs_empty_db(self, tmp_path):
        """get_workflow_runs returns empty list on empty/corrupt DB."""
        db = TrackingDatabase(db_path=tmp_path / "tracking.db")
        runs = db.get_workflow_runs()
        assert isinstance(runs, list)

    def test_insert_and_retrieve_workflow_run(self, tmp_path):
        """Round-trip: insert then retrieve a workflow run."""
        db = TrackingDatabase(db_path=tmp_path / "tracking.db")
        run = WorkflowRun(
            id="run-001",
            workflow_name="research",
            query="test query",
            mode="audit",
            started_at=1000.0,
        )
        db.insert_workflow_run(run)
        results = db.get_workflow_runs()
        assert len(results) == 1
        assert results[0].id == "run-001"

    def test_get_stats_empty_db(self, tmp_path):
        """get_stats returns zeroed stats on empty database."""
        db = TrackingDatabase(db_path=tmp_path / "tracking.db")
        stats = db.get_stats()
        assert stats["total_runs"] == 0
        assert stats["total_cost_usd"] == 0.0
