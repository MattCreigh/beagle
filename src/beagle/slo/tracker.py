"""SLO tracker — persists SLI measurements and computes compliance.

Subscribes to EventBus events and maintains a rolling 28-day window
of metrics in SQLite via the existing tracking database.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beagle.events import (
    EventBus,
    NodeCompleted,
    NodeFailed,
    ToolEscalated,
    WorkflowCompleted,
    get_event_bus,
)
from beagle.events.events import BudgetExhausted

from .indicators import SLIType
from .policy import BudgetState, ErrorBudgetPolicy

logger = logging.getLogger("Beagle.slo.tracker")


_SLO_SCHEMA = """
CREATE TABLE IF NOT EXISTS slo_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sli TEXT NOT NULL,
    event_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    value REAL NOT NULL,
    bad BOOLEAN NOT NULL DEFAULT 0,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_slo_sli ON slo_measurements(sli, timestamp);
CREATE INDEX IF NOT EXISTS idx_slo_workflow ON slo_measurements(workflow_id);
CREATE INDEX IF NOT EXISTS idx_slo_time ON slo_measurements(timestamp);

CREATE TABLE IF NOT EXISTS slo_compliance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sli TEXT NOT NULL,
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    total_events INTEGER NOT NULL DEFAULT 0,
    bad_events INTEGER NOT NULL DEFAULT 0,
    compliance_percent REAL NOT NULL DEFAULT 0.0,
    budget_percent REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'unknown',
    snapshot_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slo_snap_time ON slo_compliance_snapshots(snapshot_at);
"""


@dataclass
class ComplianceReport:
    """Aggregated compliance report across all SLIs."""

    generated_at: float
    window_days: int
    measurements: dict[str, dict[str, Any]] = field(default_factory=dict)
    budgets: dict[str, BudgetState] = field(default_factory=dict)
    overall_status: str = "unknown"
    worst_sli: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window_days": self.window_days,
            "measurements": self.measurements,
            "budgets": {k: v.to_dict() for k, v in self.budgets.items()},
            "overall_status": self.overall_status,
            "worst_sli": self.worst_sli,
        }


class SLOTracker:
    """Tracks SLO compliance by subscribing to EventBus events.

    Usage:
        tracker = SLOTracker()
        await tracker.start()
        # Events are automatically recorded
        report = tracker.generate_report()
        await tracker.stop()
    """

    def __init__(
        self,
        db_path: Path | None = None,
        window_days: int = 28,
        event_bus: EventBus | None = None,
    ) -> None:
        if db_path is None:
            # get_data_root(), not get_workspace_root(): workspace_root is the
            # package install tree under a wheel, while tracking/database.py
            # already anchors the same DB at get_data_root() — the two
            # disagreeing anchors meant the SLO tracker and the tracking
            # database could open different tracking.db files.
            from beagle.config.paths import get_data_root

            db_path = get_data_root() / "tracking.db"
        self.db_path = db_path
        self.window_days = window_days
        self._event_bus = event_bus or get_event_bus()
        self._policy = ErrorBudgetPolicy()
        self._running = False
        self._subs: list[Any] = []
        self._init_db()

    # ── Database ──────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_SLO_SCHEMA)
        conn.commit()
        conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _record(
        self,
        sli: str,
        event_id: str,
        workflow_id: str,
        value: float,
        bad: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        import json

        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO slo_measurements "
                "(sli, event_id, workflow_id, timestamp, value, bad, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sli,
                    event_id,
                    workflow_id,
                    time.time(),
                    value,
                    1 if bad else 0,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            conn.commit()

    # ── Event handlers ───────────────────────────────────────────────────────

    async def _on_workflow_completed(self, event: WorkflowCompleted) -> None:
        success = 1.0 if event.success else 0.0
        bad = not event.success
        self._record(
            SLIType.WORKFLOW_SUCCESS_RATE,
            event.workflow_id or "",
            event.workflow_id or "",
            success,
            bad=bad,
            metadata={"duration_seconds": event.duration_seconds},
        )
        if event.duration_seconds > 0:
            bad_e2e = event.duration_seconds > 600
            self._record(
                SLIType.E2E_LATENCY_P95,
                event.workflow_id or "",
                event.workflow_id or "",
                event.duration_seconds,
                bad=bad_e2e,
            )
        # Budget accuracy: if cost > 0 check vs budget
        if event.total_cost_usd > 0 and event.budget_usd > 0:
            ratio = event.total_cost_usd / event.budget_usd
            self._record(
                SLIType.BUDGET_ACCURACY,
                event.workflow_id or "",
                event.workflow_id or "",
                1.0 if ratio <= 2.0 else 0.0,
                bad=ratio > 2.0,
                metadata={"ratio": ratio, "budget": event.budget_usd},
            )

    async def _on_node_completed(self, event: NodeCompleted) -> None:
        if event.duration_seconds > 0:
            bad = event.duration_seconds > 120
            self._record(
                SLIType.NODE_LATENCY_P95,
                event.node_name,
                event.workflow_id or "",
                event.duration_seconds,
                bad=bad,
                metadata={"node_name": event.node_name},
            )

    async def _on_node_failed(self, event: NodeFailed) -> None:
        self._record(
            SLIType.NODE_LATENCY_P95,
            event.node_name,
            event.workflow_id or "",
            event.duration_seconds or 0.0,
            bad=True,
            metadata={"node_name": event.node_name, "error": event.error},
        )

    async def _on_tool_call(self, event: Any) -> None:
        # ToolEscalated signals MCP failure that required escalation
        if isinstance(event, ToolEscalated):
            self._record(
                SLIType.MCP_AVAILABILITY,
                event.tool_name,
                event.workflow_id or "",
                0.0,
                bad=True,
                metadata={"error": event.error},
            )
        elif hasattr(event, "status"):
            ok = event.status not in ("failed", "blocked")
            self._record(
                SLIType.MCP_AVAILABILITY,
                event.tool_name,
                event.workflow_id or "",
                1.0 if ok else 0.0,
                bad=not ok,
            )

    async def _on_budget_exhausted(self, event: BudgetExhausted) -> None:
        logger.warning(
            f"Budget exhausted for {event.tenant_id}: "
            f"${event.current_cost:.2f} / ${event.budget:.2f}"
        )

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to EventBus events."""
        if self._running:
            return
        # EventBus dispatches by pattern, so each handler only ever receives its
        # matching concrete event type. Cast to EventCallback for mypy (the
        # handlers' narrower parameter types are a runtime guarantee, not a
        # variance violation).
        from typing import cast

        from beagle.events.bus import EventCallback

        self._subs = [
            self._event_bus.subscribe(
                "workflow.completed", cast(EventCallback, self._on_workflow_completed)
            ),
            self._event_bus.subscribe(
                "node.completed", cast(EventCallback, self._on_node_completed)
            ),
            self._event_bus.subscribe("node.failed", cast(EventCallback, self._on_node_failed)),
            self._event_bus.subscribe("tool.escalated", cast(EventCallback, self._on_tool_call)),
            self._event_bus.subscribe(
                "budget.exhausted", cast(EventCallback, self._on_budget_exhausted)
            ),
        ]
        self._running = True
        logger.info("SLOTracker started")

    async def stop(self) -> None:
        """Unsubscribe from EventBus events."""
        from contextlib import suppress

        for sub in self._subs:
            with suppress(Exception):
                sub.unsubscribe()
        self._subs.clear()
        self._running = False
        logger.info("SLOTracker stopped")

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_window_counts(self, sli: str) -> tuple[int, int]:
        """Get (total_events, bad_events) for an SLI in the current window."""
        # wall-clock-ok: compares against a persisted timestamp
        window_start = time.time() - (self.window_days * 86400)
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, SUM(bad) as bad "
                "FROM slo_measurements WHERE sli = ? AND timestamp > ?",
                (sli, window_start),
            ).fetchone()
            return int(row["total"] or 0), int(row["bad"] or 0)

    def get_compliance_percent(self, sli: str) -> float:
        """Calculate compliance percentage for an SLI."""
        total, bad = self.get_window_counts(sli)
        if total == 0:
            return 100.0
        return ((total - bad) / total) * 100.0

    def generate_report(self) -> ComplianceReport:
        """Generate a full compliance report."""
        report = ComplianceReport(
            generated_at=time.time(),
            window_days=self.window_days,
        )
        measurements: dict[str, tuple[int, int]] = {}
        for sli_type in SLIType:
            total, bad = self.get_window_counts(sli_type.value)
            measurements[sli_type.value] = (total, bad)
            compliance = ((total - bad) / total * 100) if total else 100.0
            report.measurements[sli_type.value] = {
                "total_events": total,
                "bad_events": bad,
                "compliance_percent": round(compliance, 2),
            }

        report.budgets = self._policy.check_all(measurements, self.window_days)
        worst = self._policy.get_worst(report.budgets)
        if worst:
            report.overall_status = worst.status.value
            report.worst_sli = worst.sli
        else:
            report.overall_status = "healthy"

        self._persist_snapshot(report)
        return report

    def _persist_snapshot(self, report: ComplianceReport) -> None:
        window_end = time.time()
        window_start = window_end - (self.window_days * 86400)
        with self._get_conn() as conn:
            for sli, data in report.measurements.items():
                state = report.budgets.get(sli)
                conn.execute(
                    "INSERT INTO slo_compliance_snapshots "
                    "(sli, window_start, window_end, total_events, "
                    "bad_events, compliance_percent, budget_percent, "
                    "status, snapshot_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sli,
                        window_start,
                        window_end,
                        data["total_events"],
                        data.get("bad_events", 0),
                        data["compliance_percent"],
                        state.budget_percent if state else 100.0,
                        state.status.value if state else "unknown",
                        report.generated_at,
                    ),
                )
            conn.commit()

    def query_history(self, sli: str, limit: int = 100) -> list[dict[str, Any]]:
        """Query recent measurements for an SLI."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM slo_measurements WHERE sli = ? ORDER BY timestamp DESC LIMIT ?",
                (sli, limit),
            ).fetchall()
            return [dict(r) for r in rows]
