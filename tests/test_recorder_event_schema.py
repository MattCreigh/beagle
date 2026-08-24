"""Regression: tracking.recorder must not crash on the canonical event shapes
that node_executor emits. The recorder reads event.skill_name and event.success;
those fields exist on NodeStarted and NodeCompleted (v13.14.8+).
"""

from __future__ import annotations

import dataclasses

from beagle.events import (
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    WorkflowCompleted,
    WorkflowStarted,
)


def test_node_started_has_skill_name():
    fields = {f.name for f in dataclasses.fields(NodeStarted)}
    assert "skill_name" in fields


def test_node_completed_has_success():
    fields = {f.name for f in dataclasses.fields(NodeCompleted)}
    assert "success" in fields


def test_recorder_handles_node_started_without_skill_name():
    """Backwards-compat: emitters that don't set skill_name still parse."""
    from beagle.tracking.recorder import RunRecorder

    rec = RunRecorder()
    # Should not raise even though skill_name is the default ""
    rec.handle_event(NodeStarted(workflow_id="t", node_name="n", model="m"))


def test_recorder_handles_node_completed():
    from beagle.tracking.recorder import RunRecorder

    rec = RunRecorder()
    rec.handle_event(NodeStarted(workflow_id="t", node_name="n", model="m"))
    rec.handle_event(
        NodeCompleted(
            workflow_id="t",
            node_name="n",
            result="",
            cost=0.0,
            tokens=0,
            duration_seconds=0.1,
        )
    )


def test_recorder_handles_node_failed():
    from beagle.tracking.recorder import RunRecorder

    rec = RunRecorder()
    rec.handle_event(NodeStarted(workflow_id="t", node_name="n", model="m"))
    rec.handle_event(NodeFailed(workflow_id="t", node_name="n", error="x", attempt=1))


def test_workflow_lifecycle_events_compatible():
    """Existing fields the recorder uses on workflow events must remain stable."""
    ws = {f.name for f in dataclasses.fields(WorkflowStarted)}
    wc = {f.name for f in dataclasses.fields(WorkflowCompleted)}
    assert {"query", "mode", "budget_usd"}.issubset(ws)
    assert {
        "success",
        "total_cost_usd",
        "total_tokens",
        "duration_seconds",
        "completed_nodes",
        "errors",
    }.issubset(wc)
