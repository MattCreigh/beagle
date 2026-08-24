"""Tests for Tracking Database and Models.

Comprehensive tests for SQLite tracking database and run models.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from beagle.events import WorkflowCompleted, WorkflowStarted
from beagle.tracking.database import (
    TrackingDatabase,
)
from beagle.tracking.differ import RunDiffer
from beagle.tracking.models import Finding, NodeRun, WorkflowRun
from beagle.tracking.recorder import (
    RunRecorder,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestModels:
    """Test tracking models."""

    def test_workflow_run_creation(self):
        """WorkflowRun can be created."""
        run = WorkflowRun(
            id="test-123",
            workflow_name="research",
            query="Test query",
            mode="single",
            started_at=time.time(),
        )
        assert run.id == "test-123"
        assert run.workflow_name == "research"
        assert run.success is False  # Default

    def test_workflow_run_to_dict(self):
        """WorkflowRun can convert to dict."""
        run = WorkflowRun(
            id="test-123",
            workflow_name="Research",
            query="Test query",
            mode="multi",
            started_at=1000.0,
            budget_usd=5.0,
        )
        d = run.to_dict()
        assert d["id"] == "test-123"
        assert d["workflow_name"] == "Research"

    def test_node_run_creation(self):
        """NodeRun can be created."""
        run = NodeRun(
            id="node-1",
            workflow_run_id="wf-1",
            node_name="research",
            skill_name="researcher",
            model="gpt-4",
            started_at=time.time(),
        )
        assert run.id == "node-1"
        assert run.node_name == "research"
        assert run.workflow_run_id == "wf-1"


class TestTrackingDatabase:
    """Test TrackingDatabase."""

    def test_database_creation(self):
        """Database can be created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = TrackingDatabase(db_path)
            assert db.db_path == db_path
            assert db_path.exists()

    def test_database_schema_initialized(self):
        """Database schema is initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            TrackingDatabase(db_path)

            # Check tables exist
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()

            assert "workflow_runs" in tables
            assert "node_runs" in tables
            assert "findings" in tables

    def test_insert_workflow_run(self):
        """Workflow run can be inserted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = TrackingDatabase(db_path)

            run = WorkflowRun(
                id="test-123",
                workflow_name="research",
                query="Test query",
                mode="single",
                started_at=time.time(),
            )
            db.insert_workflow_run(run)

            # Verify insertion
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", ("test-123",))
            row = cursor.fetchone()
            conn.close()

            assert row is not None
            assert row["workflow_name"] == "research"

    def test_update_workflow_run(self):
        """Workflow run can be updated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = TrackingDatabase(db_path)

            run = WorkflowRun(
                id="test-123",
                workflow_name="research",
                query="Test query",
                mode="single",
                started_at=time.time(),
            )
            db.insert_workflow_run(run)

            # Update
            run.completed_at = time.time()
            run.success = True
            run.total_cost_usd = 1.5
            run.total_tokens = 5000
            run.total_duration_seconds = 30.0
            run.nodes_completed = 5
            run.nodes_failed = 0

            db.update_workflow_run(run)

            # Verify update
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", ("test-123",))
            row = cursor.fetchone()
            conn.close()

            assert row["success"] == 1
            assert row["total_cost_usd"] == 1.5

    def test_insert_node_run(self):
        """Node run can be inserted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = TrackingDatabase(db_path)

            # Need workflow run first
            wf_run = WorkflowRun(
                id="wf-1",
                workflow_name="research",
                query="Test",
                mode="single",
                started_at=time.time(),
            )
            db.insert_workflow_run(wf_run)

            node_run = NodeRun(
                id="node-1",
                workflow_run_id="wf-1",
                node_name="research",
                skill_name="researcher",
                model="gpt-4",
                started_at=time.time(),
            )
            db.insert_node_run(node_run)

            # Verify
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM node_runs WHERE id = ?", ("node-1",))
            row = cursor.fetchone()
            conn.close()

            assert row is not None
            assert row["node_name"] == "research"


class TestRunRecorder:
    """Test RunRecorder."""

    def test_recorder_creation(self):
        """RunRecorder can be created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "recorder.db"
            db = TrackingDatabase(db_path)
            recorder = RunRecorder(db)
            assert recorder.db is not None


class TestDatabaseConcurrency:
    """Test database thread safety."""

    def test_concurrent_inserts(self):
        """Database handles concurrent inserts."""
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "concurrent.db"
            db = TrackingDatabase(db_path)

            errors = []
            insert_count = 10
            threads = []

            def insert_run(i):
                try:
                    run = WorkflowRun(
                        id=f"concurrent-{i}",
                        workflow_name="test",
                        query=f"Query {i}",
                        mode="single",
                        started_at=time.time(),
                    )
                    db.insert_workflow_run(run)
                except Exception as e:  # ruff: ignore[BLE001]
                    errors.append(str(e))

            for i in range(insert_count):
                t = threading.Thread(target=insert_run, args=(i,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            assert len(errors) == 0

            # Verify all inserted
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT COUNT(*) FROM workflow_runs")
            count = cursor.fetchone()[0]
            conn.close()

            assert count == insert_count


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ── Merged from test_tracking_inner.py (v1.0.0 consolidation) ────────
@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_tracking.db"
    return TrackingDatabase(db_path)


def test_database_creation(temp_db):
    """Test database creation and schema initialization."""
    assert temp_db.db_path.exists()
    with sqlite3.connect(temp_db.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        assert "workflow_runs" in tables
        assert "node_runs" in tables
        assert "findings" in tables


def test_workflow_run_insertion_retrieval(temp_db):
    """Test workflow run insertion and retrieval."""
    run = WorkflowRun(
        id="run_1",
        workflow_name="test_wf",
        query="test query",
        mode="audit",
        started_at=time.time(),
        budget_usd=10.0,
    )
    temp_db.insert_workflow_run(run)

    runs = temp_db.get_workflow_runs(limit=1)
    assert len(runs) == 1
    assert runs[0].id == "run_1"
    assert runs[0].workflow_name == "test_wf"


def test_node_run_insertion_update(temp_db):
    """Test node run insertion and update."""
    wf_id = "run_1"
    node_id = "node_1"

    node = NodeRun(
        id=node_id,
        workflow_run_id=wf_id,
        node_name="planning",
        skill_name="research-planner",
        model="m1",
        started_at=time.time(),
    )
    temp_db.insert_node_run(node)

    # Update
    node.completed_at = time.time()
    node.success = True
    node.cost_usd = 0.05
    node.tokens = 100
    temp_db.update_node_run(node)

    with sqlite3.connect(temp_db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM node_runs WHERE id = ?", (node_id,)).fetchone()
        assert row["success"] == 1
        assert row["cost_usd"] == 0.05


def test_findings_insertion_deduplication(temp_db):
    """Test findings insertion with deduplication logic."""
    f1 = Finding(
        id="f1",
        workflow_run_id="r1",
        node_name="n1",
        severity="high",
        category="bug",
        title="T1",
        description="D1",
        file_path="src/main.py",
        line_number=10,
    )

    id1 = temp_db.insert_finding(f1)
    assert id1 == "f1"

    # Insert same finding again (different ID, same tuple)
    f2 = Finding(
        id="f2",
        workflow_run_id="r2",
        node_name="n1",
        severity="high",
        category="bug",
        title="T1",
        description="D1",
        file_path="src/main.py",
        line_number=10,
    )
    id2 = temp_db.insert_finding(f2)

    # Should return original ID
    assert id2 == "f1"


def test_run_recorder_integration(temp_db):
    """Test that recorder correctly subscribes to events and records them."""
    recorder = RunRecorder(db=temp_db)

    # 1. Start Workflow
    start_event = WorkflowStarted(
        workflow_id="rec_wf_1",
        query="q",
        budget_usd=5.0,
        metadata={"workflow_name": "test_rec"},
    )
    recorder.handle_event(start_event)

    # Verify DB
    runs = temp_db.get_workflow_runs()
    assert any(r.id == "rec_wf_1" for r in runs)

    # 2. Complete Workflow
    comp_event = WorkflowCompleted(
        workflow_id="rec_wf_1",
        success=True,
        total_cost_usd=0.1,
        total_tokens=500,
        duration_seconds=10.0,
        completed_nodes=2,
        errors=0,
    )
    recorder.handle_event(comp_event)

    # Verify update
    run = next(r for r in temp_db.get_workflow_runs() if r.id == "rec_wf_1")
    assert run.success
    assert run.total_cost_usd == 0.1


def test_differ_logic(temp_db):
    """Test differ output with mock data."""
    # Add findings for run A
    temp_db.insert_finding(
        Finding(
            id="fa1",
            workflow_run_id="ra",
            node_name="n",
            severity="high",
            category="bug",
            title="Old Bug",
            description="D",
            file_path="f1",
        )
    )
    temp_db.insert_finding(
        Finding(
            id="fa2",
            workflow_run_id="ra",
            node_name="n",
            severity="medium",
            category="bug",
            title="Persistent Bug",
            description="D",
            file_path="f2",
        )
    )

    # Add findings for run B
    temp_db.insert_finding(
        Finding(
            id="fb1",
            workflow_run_id="rb",
            node_name="n",
            severity="low",
            category="bug",
            title="New Bug",
            description="D",
            file_path="f3",
        )
    )
    temp_db.insert_finding(
        Finding(
            id="fb2",
            workflow_run_id="rb",
            node_name="n",
            severity="medium",
            category="bug",
            title="Persistent Bug",
            description="D",
            file_path="f2",
        )
    )

    differ = RunDiffer(db=temp_db)
    diff = differ.compare("ra", "rb")

    assert len(diff.new_findings) == 1
    assert diff.new_findings[0].title == "New Bug"
    assert len(diff.resolved_findings) == 1
    assert diff.resolved_findings[0].title == "Old Bug"
    assert len(diff.persistent_findings) == 1
    assert diff.persistent_findings[0].title == "Persistent Bug"


def test_concurrent_writes(temp_db):
    """Test database handles concurrent writes (from multiple threads)."""
    import threading

    def worker(i):
        run = WorkflowRun(id=f"concurrent_{i}", workflow_name="wf", query="q")
        temp_db.insert_workflow_run(run)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    runs = temp_db.get_workflow_runs(limit=100)
    assert len([r for r in runs if r.id.startswith("concurrent_")]) == 20


def test_cli_history_output(temp_db):
    """Test history CLI command produces correct output."""
    from typer.testing import CliRunner

    from beagle.cli.cli import app

    temp_db.insert_workflow_run(
        WorkflowRun(
            id="cli_run_123",
            workflow_name="HistoryTest",
            query="q",
            started_at=time.time(),
        )
    )

    runner = CliRunner()
    with patch(
        "beagle.tracking.database.TrackingDatabase.get_instance",
        return_value=temp_db,
    ):
        result = runner.invoke(app, ["history", "--limit", "5"])
        assert result.exit_code == 0
        assert "HistoryTest" in result.stdout
        assert "cli_run_" in result.stdout


def test_cli_findings_output(temp_db):
    """Test findings CLI command produces correct output."""
    from typer.testing import CliRunner

    from beagle.cli.cli import app

    rid = "run_with_findings"
    temp_db.insert_workflow_run(WorkflowRun(id=rid, workflow_name="W", query="q"))
    temp_db.insert_finding(
        Finding(
            id="f1",
            workflow_run_id=rid,
            node_name="n",
            severity="critical",
            category="security",
            title="SQL Injection",
            description="D",
        )
    )

    runner = CliRunner()
    with patch(
        "beagle.tracking.database.TrackingDatabase.get_instance",
        return_value=temp_db,
    ):
        result = runner.invoke(app, ["findings", rid])
        assert result.exit_code == 0
        assert "SQL Injection" in result.stdout
        assert "CRIT" in result.stdout


def test_cli_diff_output(temp_db):
    """Test diff CLI command produces correct output."""
    from typer.testing import CliRunner

    from beagle.cli.cli import app

    # Setup runs
    temp_db.insert_workflow_run(WorkflowRun(id="run_a", workflow_name="W", query="q"))
    temp_db.insert_workflow_run(WorkflowRun(id="run_b", workflow_name="W", query="q"))

    # Finding only in B
    temp_db.insert_finding(
        Finding(
            id="fb",
            workflow_run_id="run_b",
            node_name="n",
            severity="high",
            category="bug",
            title="Mystery Bug",
            description="D",
        )
    )

    runner = CliRunner()
    with patch(
        "beagle.tracking.database.TrackingDatabase.get_instance",
        return_value=temp_db,
    ):
        result = runner.invoke(app, ["diff", "run_a", "run_b"])
        assert result.exit_code == 0
        assert "Mystery Bug" in result.stdout
        assert "New Findings" in result.stdout


def test_stats_aggregation(temp_db):
    """Test aggregate statistics calculation."""
    # Add some runs
    t = time.time()
    temp_db.insert_workflow_run(WorkflowRun(id="s1", workflow_name="W", query="q", started_at=t))
    temp_db.update_workflow_run(
        WorkflowRun(
            id="s1",
            workflow_name="W",
            query="q",
            success=True,
            total_cost_usd=1.5,
            total_tokens=1000,
        )
    )

    temp_db.insert_workflow_run(WorkflowRun(id="s2", workflow_name="W", query="q", started_at=t))
    temp_db.update_workflow_run(
        WorkflowRun(
            id="s2",
            workflow_name="W",
            query="q",
            success=False,
            total_cost_usd=0.5,
            total_tokens=500,
        )
    )

    stats = temp_db.get_stats(since_days=1)
    assert stats["total_runs"] == 2
    assert stats["total_cost_usd"] == 2.0
    assert stats["total_tokens"] == 1500
    assert stats["success_rate"] == 50.0
