"""Regression: a workflow run must persist at least one row to workflow_runs
and one row to node_runs. Without this, the entire tracking subsystem could
silently break and no CI signal would fire.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from beagle.events import (
    NodeCompleted,
    NodeStarted,
    WorkflowCompleted,
    WorkflowStarted,
)
from beagle.events.bus import get_event_bus
from beagle.tracking.database import TrackingDatabase
from beagle.tracking.recorder import RunRecorder


@pytest.fixture
def isolated_tracking_db(tmp_path, monkeypatch):
    """Point the tracking subsystem at an isolated DB for this test."""
    db_path = tmp_path / "tracking.db"
    monkeypatch.setenv("BEAGLE_DATA_ROOT", str(tmp_path))
    # Reset singletons so the new env is picked up
    TrackingDatabase._instance = None
    yield db_path
    TrackingDatabase._instance = None


def test_workflow_lifecycle_persists_rows(isolated_tracking_db):
    rec = RunRecorder()
    rec.start()

    wf_id = str(uuid.uuid4())
    bus = get_event_bus()

    bus.publish(
        WorkflowStarted(
            workflow_id=wf_id,
            query="t",
            budget_usd=0.1,
            mode="audit",
            metadata={"workflow_name": "test"},
        )
    )
    bus.publish(NodeStarted(workflow_id=wf_id, node_name="n1", model="m", skill_name="s"))
    bus.publish(
        NodeCompleted(workflow_id=wf_id, node_name="n1", cost=0.0, tokens=0, duration_seconds=0.01)
    )
    bus.publish(
        WorkflowCompleted(
            workflow_id=wf_id,
            success=True,
            total_cost_usd=0.0,
            total_tokens=0,
            duration_seconds=0.01,
            completed_nodes=1,
            errors=0,
        )
    )

    time.sleep(0.5)  # let async callbacks drain

    conn = sqlite3.connect(str(isolated_tracking_db))
    wf_rows = conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    nd_rows = conn.execute("SELECT COUNT(*) FROM node_runs").fetchone()[0]
    assert wf_rows >= 1, "workflow_runs has no rows after lifecycle events"
    assert nd_rows >= 1, "node_runs has no rows after node lifecycle events"
