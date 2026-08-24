"""Section 8.4: State serialization size bounds.

Verifies that serialized Beagle state fits within reasonable limits
to prevent OOM and disk exhaustion in production.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from beagle.core.orchestrator_types import AgentState
from beagle.infrastructure.task_store import TaskStore
from beagle.lifecycle.checkpoint import Checkpoint
from beagle.tracking.models import Finding, WorkflowRun


class TestCheckpointSizeBounds:
    """Lifecycle Checkpoint serialization stays within reasonable bounds."""

    def test_minimal_checkpoint_size(self):
        """A minimal checkpoint is under 1 KB."""
        cp = Checkpoint(timestamp=1000.0, version="13.7.1", restart_reason="test")
        data = json.dumps(asdict(cp))
        assert len(data) < 1024, f"Minimal checkpoint {len(data)} bytes > 1KB"

    def test_large_checkpoint_with_errors_still_bounded(self):
        """Checkpoint with 100 errors stays under 100 KB."""
        errors = [f"Error {i}: something went wrong" for i in range(100)]
        cp = Checkpoint(
            timestamp=1000.0,
            version="13.7.1",
            restart_reason="error_burst",
            daemon_deferred_triggers=errors[:10],
            rate_limiter_backoff={f"svc-{i}": 1.5 for i in range(50)},
            circuit_states={f"svc-{i}": "open" for i in range(50)},
        )
        data = json.dumps(asdict(cp))
        assert len(data) < 100_000, f"Large checkpoint {len(data)} bytes > 100KB"

    def test_checkpoint_with_max_circuit_states(self):
        """Checkpoint with 500 circuit states stays under 50 KB."""
        cp = Checkpoint(
            timestamp=1000.0,
            version="13.7.1",
            restart_reason="scale",
            circuit_states={f"service-{i:04d}": "closed" for i in range(500)},
        )
        data = json.dumps(asdict(cp))
        assert len(data) < 50_000, f"500-circuit checkpoint {len(data)} bytes > 50KB"


class TestTaskStorePayloadBounds:
    """TaskStore spec/constraints JSON payloads stay bounded."""

    def test_task_spec_json_under_1mb(self, tmp_path):
        """A single task's spec_json stays under 1 MB."""
        store = TaskStore(tmp_path / "tasks.db")
        task_id = store.create_task(
            task_type="workflow",
            spec={"query": "test", "data": "x" * 100_000},  # 100KB spec
        )
        task = store.get_task(task_id)
        spec_json = json.dumps(task["spec_json"])
        assert len(spec_json) < 1_000_000

    def test_audit_event_json_under_100kb(self, tmp_path):
        """Audit event data stays under 100 KB per event."""
        store = TaskStore(tmp_path / "tasks.db")
        task_id = store.create_task(task_type="workflow", spec={"query": "test"})
        store.add_audit_event(task_id, "test_event", {"detail": "x" * 10_000})
        trail = store.get_audit_trail(task_id)
        for event in trail:
            event_data = json.dumps(event.get("event_data", {}))
            assert len(event_data) < 100_000, "Audit event data exceeds 100KB"


class TestAgentStateSerializationBounds:
    """AgentState serialization stays within reasonable bounds."""

    def test_minimal_agent_state_size(self):
        """A minimal AgentState serializes under 1 KB."""
        state = AgentState(query="hello")
        data = json.dumps(asdict(state))
        assert len(data) < 1024, f"Minimal state {len(data)} bytes > 1KB"

    def test_agent_state_with_large_report(self):
        """AgentState with a 50KB final_report serialize cleanly."""
        state = AgentState(
            query="test query",
            final_report="A" * 50_000,
        )
        data = json.dumps(asdict(state), default=str)
        assert len(data) > 50_000  # Must contain the report
        assert len(data) < 200_000  # But not blow up

    def test_agent_state_metadata_bounded(self):
        """AgentState with large metadata doesn't exceed 10 MB."""
        state = AgentState(
            query="test",
            metadata={"keys": [f"key-{i}" for i in range(1000)]},
        )
        data = json.dumps(asdict(state), default=str)
        assert len(data) < 10_000_000, f"State with metadata {len(data)} bytes > 10MB"

    def test_agent_state_errors_bounded(self):
        """AgentState with 1000 errors stays bounded."""
        state = AgentState(
            query="test",
            errors=[f"Error: node-{i} failed with timeout" for i in range(1000)],
        )
        data = json.dumps(asdict(state), default=str)
        assert len(data) < 1_000_000, f"State with 1K errors {len(data)} bytes > 1MB"


class TestTrackingModelSerializationBounds:
    """Tracking models serialize within reasonable limits."""

    def test_workflow_run_serialization_under_1kb(self):
        """WorkflowRun serializes under 1 KB."""
        run = WorkflowRun(
            id="run-001",
            workflow_name="research",
            query="test",
            mode="audit",
            started_at=1000.0,
        )
        data = json.dumps(run.to_dict())
        assert len(data) < 1024

    def test_finding_serialization_under_1kb(self):
        """Finding serializes under 1 KB."""
        finding = Finding(
            id="find-001",
            workflow_run_id="run-001",
            node_name="researcher",
            severity="high",
            category="security",
            title="SQL Injection",
            description="Input not sanitized",
            suggested_fix="Use parameterized queries",
        )
        data = json.dumps(asdict(finding))
        assert len(data) < 1024
