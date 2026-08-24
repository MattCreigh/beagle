"""SP-5: tests for tracking/models (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The tracking database models
(WorkflowRun, NodeRun, Finding) and their SCHEMA had no direct tests. These
exercise construction, defaults, and to_dict.
"""

from __future__ import annotations

import sqlite3

from beagle.tracking.models import (
    SCHEMA,
    Finding,
    NodeRun,
    WorkflowRun,
)


def test_workflow_run_defaults() -> None:
    """WorkflowRun has sensible defaults for a pending run."""
    r = WorkflowRun(id="r1", workflow_name="audit", query="q")
    assert r.mode == "audit"
    assert r.success is False
    assert r.total_cost_usd == 0.0
    assert r.completed_at is None


def test_workflow_run_to_dict() -> None:
    """to_dict serializes all fields."""
    r = WorkflowRun(id="r1", workflow_name="audit", query="q", total_cost_usd=1.5, total_tokens=100)
    d = r.to_dict()
    assert d["id"] == "r1"
    assert d["total_cost_usd"] == 1.5
    assert d["total_tokens"] == 100


def test_node_run_defaults() -> None:
    """NodeRun has a default attempts=1 and success=False."""
    n = NodeRun(
        id="n1",
        workflow_run_id="r1",
        node_name="plan",
        skill_name="researcher",
        model="glm",
        started_at=0.0,
    )
    assert n.attempts == 1
    assert n.success is False
    assert n.input_tokens == 0


def test_finding_defaults() -> None:
    """Finding defaults to status='open' and None file fields."""
    f = Finding(
        id="f1",
        workflow_run_id="r1",
        node_name="validation",
        severity="high",
        category="security",
        title="t",
        description="d",
    )
    assert f.status == "open"
    assert f.file_path is None
    assert f.line_number is None


def test_schema_creates_tables(tmp_path) -> None:
    """SCHEMA creates the expected tables in a fresh SQLite db."""
    db = sqlite3.connect(tmp_path / "test.db")
    db.executescript(SCHEMA)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"workflow_runs", "node_runs", "findings"} <= tables
    db.close()


def test_schema_is_idempotent(tmp_path) -> None:
    """Running SCHEMA twice on the same db does not fail."""
    db = sqlite3.connect(tmp_path / "test.db")
    db.executescript(SCHEMA)
    db.executescript(SCHEMA)  # no error
    db.close()
