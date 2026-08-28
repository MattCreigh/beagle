"""Event bus subscriber that records run data to the database."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from beagle.events import (
    BeagleEvent,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    WorkflowCompleted,
    WorkflowStarted,
    get_event_bus,
)

from .database import TrackingDatabase
from .models import Finding, NodeRun, WorkflowRun

logger = logging.getLogger("Beagle.tracking.recorder")


class RunRecorder:
    """Subscriber that persists events to the tracking database."""

    def __init__(self, db: TrackingDatabase | None = None):
        self.db = db or TrackingDatabase.get_instance()
        self._node_run_ids: dict[
            tuple[str, str], str
        ] = {}  # (workflow_id, node_name) -> node_run_id

    def handle_event(self, event: BeagleEvent):
        """Process incoming events and update database."""
        try:
            if isinstance(event, WorkflowStarted):
                self._record_workflow_start(event)
            elif isinstance(event, WorkflowCompleted):
                self._record_workflow_completion(event)
            elif isinstance(event, NodeStarted):
                self._record_node_start(event)
            elif isinstance(event, NodeCompleted):
                self._record_node_completion(event)
            elif isinstance(event, NodeFailed):
                self._record_node_failure(event)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"RunRecorder failed to process event {type(event).__name__}: {e}")

    def _record_workflow_start(self, event: WorkflowStarted):
        run = WorkflowRun(
            id=event.workflow_id,
            workflow_name=event.metadata.get("workflow_name", "unknown"),
            query=event.query,
            mode=event.mode,
            started_at=event.timestamp,
            budget_usd=event.budget_usd,
        )
        self.db.insert_workflow_run(run)

    def _record_workflow_completion(self, event: WorkflowCompleted):
        # Retrieve existing run or create minimal
        run = WorkflowRun(
            id=event.workflow_id,
            workflow_name="",
            query="",
            completed_at=event.timestamp,
            success=event.success,
            total_cost_usd=event.total_cost_usd,
            total_tokens=event.total_tokens,
            total_duration_seconds=event.duration_seconds,
            nodes_completed=event.completed_nodes,
            nodes_failed=event.errors,
        )
        self.db.update_workflow_run(run)

    def _record_node_start(self, event: NodeStarted):
        node_run_id = str(uuid.uuid4())
        self._node_run_ids[event.workflow_id, event.node_name] = node_run_id

        run = NodeRun(
            id=node_run_id,
            workflow_run_id=event.workflow_id,
            node_name=event.node_name,
            skill_name=event.skill_name,
            model=event.model,
            started_at=event.timestamp,
        )
        self.db.insert_node_run(run)

    def _record_node_completion(self, event: NodeCompleted):
        node_run_id = self._node_run_ids.get((event.workflow_id, event.node_name))
        if not node_run_id:
            return

        # NodeRun stores the token split (input_tokens / output_tokens are
        # separate DB columns); it has never had a `tokens` field. Passing
        # one raised TypeError on EVERY node completion, so update_node_run()
        # never ran and the tracking DB recorded no cost, tokens, duration or
        # success — which is why `beagle stats` reported 0 runs and $0.00 after
        # real workflows. The `# type: ignore[call-arg]` here was mypy
        # correctly flagging the bad kwarg; it was silenced instead of fixed.
        run = NodeRun(
            id=node_run_id,
            workflow_run_id=event.workflow_id,
            node_name=event.node_name,
            skill_name="",
            model="",
            started_at=0,
            completed_at=event.timestamp,
            success=event.success,
            cost_usd=event.cost,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            duration_seconds=event.duration_seconds,
            result_hash=hashlib.sha256(event.result.encode()).hexdigest() if event.result else None,
        )
        self.db.update_node_run(run)

    def _record_node_failure(self, event: NodeFailed):
        node_run_id = self._node_run_ids.get((event.workflow_id, event.node_name))
        if not node_run_id:
            return

        run = NodeRun(
            id=node_run_id,
            workflow_run_id=event.workflow_id,
            node_name=event.node_name,
            skill_name="",
            model="",
            started_at=0,
            completed_at=event.timestamp,
            success=False,
            error=event.error,
            attempts=event.attempt,
        )
        self.db.update_node_run(run)

    def record_findings(self, output: Any):
        """Extract and persist findings from a WorkflowOutput object."""
        # Use local import to avoid circular dependency if schema imports anything from here
        from beagle.output.schema import WorkflowOutput

        if not isinstance(output, WorkflowOutput):
            return

        db_findings = []
        for finding in output.findings:
            # Map Finding (output) to Finding (tracking/models)
            db_finding = Finding(
                id=finding.id,
                workflow_run_id=output.workflow_id,
                node_name="synthesis",  # Or specific node if known
                severity=finding.severity,
                category=finding.category,
                title=finding.title,
                description=finding.description,
                file_path=finding.file_path,
                line_number=finding.line_start,
                suggested_fix=finding.suggested_fix,
                status="open",
            )
            self.db.insert_finding(db_finding)
            db_findings.append(db_finding)

        # Phase 8.2: Update Memory Index
        if db_findings:
            try:
                from beagle.memory.memory_index import MemoryIndex
                from beagle.utils.env_manager import (
                    get_workspace_root,
                )

                memory_index = MemoryIndex(get_workspace_root())
                # Use output.findings (original objects) for indexing
                memory_index.update_from_findings(output.findings)
                logger.info(f"Updated memory index with {len(output.findings)} findings")
            except ImportError as e:
                logger.warning(f"Failed to update memory index: {e}")

    def start(self):
        """Subscribe to the global event bus."""
        get_event_bus().subscribe("*", self.handle_event)
        logger.debug("RunRecorder active and subscribed to all events")


# Global recorder instance
_recorder: RunRecorder | None = None


def get_recorder() -> RunRecorder:
    """Get or create the global run recorder."""
    global _recorder
    if _recorder is None:
        _recorder = RunRecorder()
    return _recorder


def start_recorder():
    """Start the global run recorder."""
    get_recorder().start()
