"""Comprehensive audit trail for Goose workflow execution.

Provides tamper-evident logging of all workflow events including
node execution, validation results, cost tracking, and errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.audit")


class AuditEventType(StrEnum):
    """Types of audit events."""

    # Workflow lifecycle
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    WORKFLOW_RESUMED = "workflow.resumed"

    # Node execution
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    NODE_RETRYING = "node.retrying"

    # Validation
    VALIDATION_PASSED = "validation.passed"
    VALIDATION_FAILED = "validation.failed"
    VALIDATION_TIMEOUT = "validation.timeout"

    # Security
    SECURITY_BLOCKED = "security.blocked"
    # Renamed from SECRET_SCRUBBED (SP-11): the member is an event-type label,
    # not a credential. The wire value is unchanged, so audit records written
    # before the rename still resolve.
    CREDENTIAL_SCRUBBED = "security.scrubbed"
    QUERY_REJECTED = "security.query_rejected"

    # Cost
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXCEEDED = "budget.exceeded"

    # Checkpointing
    CHECKPOINT_SAVED = "checkpoint.saved"
    CHECKPOINT_LOADED = "checkpoint.loaded"

    # Approval
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    APPROVAL_TIMEOUT = "approval.timeout"


@dataclass
class AuditEvent:
    """Represents a single audit event."""

    event_type: AuditEventType | str
    timestamp: float = field(default_factory=time.time)
    workflow_id: str = ""
    node_name: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    previous_hash: str = ""
    event_hash: str = ""

    def compute_hash(self, secret_key: bytes | None = None) -> str:
        """Compute hash for tamper detection.

        Args:
            secret_key: Optional HMAC key for signature

        Returns:
            Hex-encoded hash

        """
        content = json.dumps(
            {
                "event_type": str(self.event_type),
                "timestamp": self.timestamp,
                "workflow_id": self.workflow_id,
                "node_name": self.node_name,
                "data": self.data,
                "previous_hash": self.previous_hash,
            },
            sort_keys=True,
            default=str,
        )

        if secret_key:
            return hmac.new(secret_key, content.encode(), hashlib.sha256).hexdigest()
        else:
            return hashlib.sha256(content.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_type": str(self.event_type),
            "timestamp": self.timestamp,
            "workflow_id": self.workflow_id,
            "node_name": self.node_name,
            "data": self.data,
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEvent:
        """Create from dictionary."""
        return cls(
            event_type=data["event_type"],
            timestamp=data.get("timestamp", time.time()),
            workflow_id=data.get("workflow_id", ""),
            node_name=data.get("node_name", ""),
            data=data.get("data", {}),
            previous_hash=data.get("previous_hash", ""),
            event_hash=data.get("event_hash", ""),
        )


def get_audit_dir() -> Path:
    """Get the audit log directory."""
    from beagle.config.paths import get_workspace_root

    workspace = get_workspace_root()
    audit_dir = workspace / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir


class AuditTrail:
    """Manages audit trail for workflow execution."""

    def __init__(
        self,
        workflow_id: str,
        audit_dir: Path | None = None,
        secret_key: bytes | None = None,
    ):
        """Initialize audit trail.

        Args:
            workflow_id: Unique workflow identifier
            audit_dir: Custom audit directory
            secret_key: HMAC key for tamper-evident signatures

        """
        self.workflow_id = workflow_id
        self.audit_dir = audit_dir or get_audit_dir()
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.secret_key = secret_key
        self.audit_file = self.audit_dir / f"{workflow_id}.jsonl"
        self._last_hash = ""

    def log(
        self,
        event_type: AuditEventType | str,
        node_name: str = "",
        **data: Any,
    ) -> AuditEvent:
        """Log an audit event.

        Args:
            event_type: Type of event
            node_name: Name of node (if applicable)
            **data: Additional event data

        Returns:
            The logged AuditEvent

        """
        event = AuditEvent(
            event_type=event_type,
            workflow_id=self.workflow_id,
            node_name=node_name,
            data=data,
            previous_hash=self._last_hash,
        )

        # Compute and set hash
        event.event_hash = event.compute_hash(self.secret_key)
        self._last_hash = event.event_hash

        # Append to JSONL file
        with open(self.audit_file, "a", encoding="utf-8") as f:
            f.write(event.to_json() + "\n")

        logger.debug(f"[AUDIT] {event_type}: {node_name or 'workflow'}")
        return event

    def workflow_started(self, query: str, budget_usd: float) -> AuditEvent:
        """Log workflow start event."""
        return self.log(
            AuditEventType.WORKFLOW_STARTED,
            query=query[:500],  # Truncate long queries
            budget_usd=budget_usd,
        )

    def workflow_completed(
        self,
        total_tokens: int,
        total_cost: float,
        duration_seconds: float,
    ) -> AuditEvent:
        """Log workflow completion."""
        return self.log(
            AuditEventType.WORKFLOW_COMPLETED,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            duration_seconds=duration_seconds,
        )

    def workflow_failed(self, error: str) -> AuditEvent:
        """Log workflow failure."""
        return self.log(
            AuditEventType.WORKFLOW_FAILED,
            error=error[:1000],
        )

    def node_started(self, node_name: str, attempt: int) -> AuditEvent:
        """Log node execution start."""
        return self.log(
            AuditEventType.NODE_STARTED,
            node_name=node_name,
            attempt=attempt,
        )

    def node_completed(
        self,
        node_name: str,
        duration_ms: float,
        tokens: int,
        cost: float,
    ) -> AuditEvent:
        """Log node completion."""
        return self.log(
            AuditEventType.NODE_COMPLETED,
            node_name=node_name,
            duration_ms=duration_ms,
            tokens=tokens,
            cost_usd=cost,
        )

    def node_failed(
        self,
        node_name: str,
        error: str,
        attempt: int,
    ) -> AuditEvent:
        """Log node failure."""
        return self.log(
            AuditEventType.NODE_FAILED,
            node_name=node_name,
            error=error[:500],
            attempt=attempt,
        )

    def validation_passed(self, node_name: str) -> AuditEvent:
        """Log EVH validation pass."""
        return self.log(
            AuditEventType.VALIDATION_PASSED,
            node_name=node_name,
        )

    def validation_failed(
        self,
        node_name: str,
        reason: str,
    ) -> AuditEvent:
        """Log EVH validation failure."""
        return self.log(
            AuditEventType.VALIDATION_FAILED,
            node_name=node_name,
            reason=reason[:500],
        )

    def budget_warning(self, current: float, budget: float) -> AuditEvent:
        """Log budget warning."""
        return self.log(
            AuditEventType.BUDGET_WARNING,
            current_usd=current,
            budget_usd=budget,
            percentage=round(current / budget * 100, 1) if budget > 0 else 0,
        )

    def budget_exceeded(self, current: float, budget: float) -> AuditEvent:
        """Log budget exceeded."""
        return self.log(
            AuditEventType.BUDGET_EXCEEDED,
            current_usd=current,
            budget_usd=budget,
        )

    def security_blocked(
        self,
        operation: str,
        reason: str,
    ) -> AuditEvent:
        """Log security block."""
        return self.log(
            AuditEventType.SECURITY_BLOCKED,
            operation=operation,
            reason=reason[:500],
        )

    def get_events(self) -> list[AuditEvent]:
        """Load all events from audit file.

        Returns:
            List of AuditEvent objects

        """
        events = []  # type: ignore[var-annotated]
        if not self.audit_file.exists():
            return events

        with open(self.audit_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        events.append(AuditEvent.from_dict(data))
                    except json.JSONDecodeError:
                        logger.warning(f"[AUDIT] Invalid JSON in audit log: {line[:50]}...")

        return events

    def verify_integrity(self) -> tuple[bool, list[str]]:
        """Verify audit trail integrity.

        Returns:
            Tuple of (is_valid, list of error messages)

        """
        events = self.get_events()
        errors = []
        expected_previous = ""

        for i, event in enumerate(events):
            # Check previous hash chain
            if event.previous_hash != expected_previous:
                errors.append(
                    f"Event {i}: Previous hash mismatch. "
                    f"Expected {expected_previous[:16]}..., got {event.previous_hash[:16]}..."
                )

            # Verify event hash
            computed = AuditEvent(
                event_type=event.event_type,
                timestamp=event.timestamp,
                workflow_id=event.workflow_id,
                node_name=event.node_name,
                data=event.data,
                previous_hash=event.previous_hash,
            ).compute_hash(self.secret_key)

            if computed != event.event_hash:
                errors.append(
                    f"Event {i}: Hash mismatch. "
                    f"Computed {computed[:16]}..., stored {event.event_hash[:16]}..."
                )

            expected_previous = event.event_hash

        return len(errors) == 0, errors

    def get_summary(self) -> dict[str, Any]:
        """Get summary of audit trail.

        Returns:
            Summary statistics

        """
        events = self.get_events()

        summary = {
            "workflow_id": self.workflow_id,
            "total_events": len(events),
            "event_counts": {},
            "nodes_executed": set(),
            "nodes_failed": set(),
            "validation_failures": 0,
            "security_blocks": 0,
        }

        for event in events:
            # Count event types
            event_type = str(event.event_type)
            summary["event_counts"][event_type] = summary["event_counts"].get(event_type, 0) + 1  # type: ignore[attr-defined,index]

            # Track nodes
            if event.node_name:
                if event.event_type == AuditEventType.NODE_COMPLETED:
                    summary["nodes_executed"].add(event.node_name)  # type: ignore[attr-defined]
                elif event.event_type == AuditEventType.NODE_FAILED:
                    summary["nodes_failed"].add(event.node_name)  # type: ignore[attr-defined]

            # Track failures
            if event.event_type == AuditEventType.VALIDATION_FAILED:
                summary["validation_failures"] += 1  # type: ignore[operator]
            elif event.event_type == AuditEventType.SECURITY_BLOCKED:
                summary["security_blocks"] += 1  # type: ignore[operator]

        # Convert sets to lists for JSON serialization
        summary["nodes_executed"] = list(summary["nodes_executed"])  # type: ignore[call-overload]
        summary["nodes_failed"] = list(summary["nodes_failed"])  # type: ignore[call-overload]

        return summary


def list_audit_trails(audit_dir: Path | None = None) -> list[dict[str, Any]]:
    """List all audit trails.

    Args:
        audit_dir: Custom audit directory

    Returns:
        List of audit trail summaries

    """
    audit_dir = audit_dir or get_audit_dir()
    trails = []

    for path in audit_dir.glob("*.jsonl"):
        workflow_id = path.stem
        trail = AuditTrail(workflow_id, audit_dir)

        try:
            events = trail.get_events()
            if events:
                trails.append(
                    {
                        "workflow_id": workflow_id,
                        "events": len(events),
                        "first_event": events[0].timestamp if events else None,
                        "last_event": events[-1].timestamp if events else None,
                        "path": str(path),
                    }
                )
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"[AUDIT] Failed to read {path}: {e}")

    # Sort by last event time
    trails.sort(key=lambda x: x.get("last_event", 0), reverse=True)  # type: ignore[arg-type,return-value]
    return trails


if __name__ == "__main__":
    # Demo audit trail
    import uuid

    workflow_id = f"demo-{uuid.uuid4().hex}"
    trail = AuditTrail(workflow_id)

    # Log some events
    trail.workflow_started("Analyze the codebase", budget_usd=10.0)
    trail.node_started("PlanningPhase", attempt=1)
    trail.node_completed("PlanningPhase", duration_ms=1234, tokens=500, cost=0.001)
    trail.validation_passed("PlanningPhase")
    trail.node_started("ExecutionPhase", attempt=1)
    trail.node_failed("ExecutionPhase", "Timeout error", attempt=1)
    trail.node_started("ExecutionPhase", attempt=2)
    trail.node_completed("ExecutionPhase", duration_ms=2345, tokens=1000, cost=0.002)
    trail.workflow_completed(total_tokens=1500, total_cost=0.003, duration_seconds=45.6)

    # Verify integrity
    is_valid, errors = trail.verify_integrity()
    logger.warning(f"Integrity check: {'PASSED' if is_valid else 'FAILED'}")
    if errors:
        for e in errors:
            logger.info(f"  - {e}")

    # Get summary
    logger.info("\nSummary:")
    summary = trail.get_summary()
    logger.info(json.dumps(summary, indent=2))

    # List all trails
    logger.info("\nAll audit trails:")
    for t in list_audit_trails():
        logger.info(f"  {t['workflow_id']}: {t['events']} events")

    # Cleanup demo
    Path(get_audit_dir() / f"{workflow_id}.jsonl").unlink()
