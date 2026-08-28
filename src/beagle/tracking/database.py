"""SQLite database manager for run tracking."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import SCHEMA, Finding, NodeRun, WorkflowRun

logger = logging.getLogger("Beagle.tracking.db")


class TrackingDatabase:
    """Manager for the SQLite tracking database."""

    _instance: TrackingDatabase | None = None
    _lock = threading.RLock()  # v13.19.3: RLock to allow get_instance() → _init_db() → _get_conn() reentrance during shutdown. Lock() (non-reentrant) deadlocks the _flush_database path and produces a ~28s hang on every CLI invocation.

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            # The tracking DB is runtime STATE, so it anchors to data_root,
            # not workspace_root.
            #
            # <invariant>
            # get_workspace_root() anchors *assets* (recipes, config.toml) and
            # under a wheel install resolves INTO site-packages; get_data_root()
            # anchors *writable state* and is the only one that honours
            # $BEAGLE_DATA_ROOT / config.paths.data_root / XDG. Using
            # workspace_root here (until 2026-07-28) meant the tracking DB was
            # created inside the installed package directory, so `beagle stats`
            # reported 0 runs after real workflows and no operator override of
            # the state location had any effect. get_data_root()'s own
            # docstring names "tracking DBs" as its responsibility.
            # </invariant>
            from beagle.config.paths import get_data_root

            db_path = get_data_root() / "tracking.db"

        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @classmethod
    def get_instance(cls, db_path: Path | None = None) -> TrackingDatabase:
        with cls._lock:
            if cls._instance is None:
                cls._instance = TrackingDatabase(db_path)
            return cls._instance

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection with WAL mode for read concurrency.

        Uses threading.local() to cache one connection per thread, avoiding
        the overhead of opening a new connection per query. WAL mode enables
        concurrent readers while a writer holds the lock.
        """
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        """Initialize schema and run migrations."""
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        logger.debug(f"Tracking database initialized at {self.db_path}")

    def insert_workflow_run(self, run: WorkflowRun):
        """Insert a new workflow run."""
        query = """
        INSERT INTO workflow_runs (id, workflow_name, query, mode, started_at, budget_usd)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._get_conn() as conn:
            conn.execute(
                query,
                (
                    run.id,
                    run.workflow_name,
                    run.query,
                    run.mode,
                    run.started_at,
                    run.budget_usd,
                ),
            )
            conn.commit()

    def update_workflow_run(self, run: WorkflowRun):
        """Update an existing workflow run."""
        query = """
        UPDATE workflow_runs SET
            completed_at = ?, success = ?, total_cost_usd = ?,
            total_tokens = ?, total_duration_seconds = ?,
            nodes_completed = ?, nodes_failed = ?, error_summary = ?
        WHERE id = ?
        """
        with self._get_conn() as conn:
            conn.execute(
                query,
                (
                    run.completed_at,
                    int(run.success),
                    run.total_cost_usd,
                    run.total_tokens,
                    run.total_duration_seconds,
                    run.nodes_completed,
                    run.nodes_failed,
                    run.error_summary,
                    run.id,
                ),
            )
            conn.commit()

    def insert_node_run(self, run: NodeRun):
        """Insert a new node run."""
        query = """
        INSERT INTO node_runs (id, workflow_run_id, node_name, skill_name, model, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._get_conn() as conn:
            conn.execute(
                query,
                (
                    run.id,
                    run.workflow_run_id,
                    run.node_name,
                    run.skill_name,
                    run.model,
                    run.started_at,
                ),
            )
            conn.commit()

    def update_node_run(self, run: NodeRun):
        """Update an existing node run."""
        query = """
        UPDATE node_runs SET
            completed_at = ?, success = ?, input_tokens = ?,
            output_tokens = ?, cost_usd = ?, duration_seconds = ?,
            attempts = ?, result_hash = ?, error = ?
        WHERE id = ?
        """
        with self._get_conn() as conn:
            conn.execute(
                query,
                (
                    run.completed_at,
                    int(run.success),
                    run.input_tokens,
                    run.output_tokens,
                    run.cost_usd,
                    run.duration_seconds,
                    run.attempts,
                    run.result_hash,
                    run.error,
                    run.id,
                ),
            )
            conn.commit()

    def insert_finding(self, finding: Finding):
        """Insert a new finding with deduplication logic."""
        # Find if this finding already exists from a previous run
        # tuple: (file_path, line_number, title)
        query_exists = """
        SELECT id FROM findings
        WHERE file_path = ? AND line_number = ? AND title = ? AND status = 'open'
        ORDER BY id DESC LIMIT 1
        """

        with self._get_conn() as conn:
            row = conn.execute(
                query_exists, (finding.file_path, finding.line_number, finding.title)
            ).fetchone()

            if row:
                # Finding exists, we could update it or skip
                return row["id"]

            # New finding
            query_insert = """
            INSERT INTO findings (
                id, workflow_run_id, node_name, severity, category,
                file_path, line_number, title, description,
                suggested_fix, first_seen_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(
                query_insert,
                (
                    finding.id,
                    finding.workflow_run_id,
                    finding.node_name,
                    finding.severity,
                    finding.category,
                    finding.file_path,
                    finding.line_number,
                    finding.title,
                    finding.description,
                    finding.suggested_fix,
                    finding.workflow_run_id,
                    finding.status,
                ),
            )
            conn.commit()
            return finding.id

    def get_workflow_runs(self, limit: int = 20) -> list[WorkflowRun]:
        """Retrieve recent workflow runs."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [WorkflowRun(**dict(row)) for row in rows]

    def get_findings_for_run(self, run_id: str) -> list[Finding]:
        """Retrieve all findings for a specific run."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM findings WHERE workflow_run_id = ?", (run_id,)
            ).fetchall()
            return [Finding(**dict(row)) for row in rows]

    def get_stats(self, since_days: int = 7) -> dict[str, Any]:
        """Get aggregate statistics."""
        import time

        # wall-clock-ok: compares against a persisted timestamp
        threshold = time.time() - (since_days * 86400)

        with self._get_conn() as conn:
            total_runs = conn.execute(
                "SELECT COUNT(*) FROM workflow_runs WHERE started_at > ?", (threshold,)
            ).fetchone()[0]
            total_cost = (
                conn.execute(
                    "SELECT SUM(total_cost_usd) FROM workflow_runs WHERE started_at > ?",
                    (threshold,),
                ).fetchone()[0]
                or 0.0
            )
            total_tokens = (
                conn.execute(
                    "SELECT SUM(total_tokens) FROM workflow_runs WHERE started_at > ?",
                    (threshold,),
                ).fetchone()[0]
                or 0
            )
            success_rate = 0.0
            if total_runs > 0:
                successes = conn.execute(
                    "SELECT COUNT(*) FROM workflow_runs WHERE success = 1 AND started_at > ?",
                    (threshold,),
                ).fetchone()[0]
                success_rate = (successes / total_runs) * 100

            return {
                "total_runs": total_runs,
                "total_cost_usd": total_cost,
                "total_tokens": total_tokens,
                "success_rate": success_rate,
                "period_days": since_days,
            }

    def record_model_outcome(
        self,
        model: str,
        provider: str,
        node_type: str,
        success: bool,
        latency_seconds: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        failure_reason: str = "",
    ) -> None:
        """Record a model execution outcome for learned routing.

        Updates running averages in the model_performance table.
        Uses INSERT OR UPDATE to maintain one row per
        (model, provider, node_type).
        """
        import time

        now = time.time()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, total_executions, success, failure, "
                "avg_latency_seconds, avg_input_tokens, "
                "avg_output_tokens, avg_cost_usd, "
                "last_failure_reason "
                "FROM model_performance "
                "WHERE model=? AND provider=? AND node_type=?",
                (model, provider, node_type),
            ).fetchone()

            if row:
                total = row["total_executions"] + 1
                n = row["total_executions"]
                new_avg_lat = (row["avg_latency_seconds"] * n + latency_seconds) / total
                new_avg_in = (row["avg_input_tokens"] * n + input_tokens) / total
                new_avg_out = (row["avg_output_tokens"] * n + output_tokens) / total
                new_avg_cost = (row["avg_cost_usd"] * n + cost_usd) / total

                conn.execute(
                    "UPDATE model_performance SET "
                    "total_executions=?, success=success+?, "
                    "failure=failure+?, "
                    "avg_latency_seconds=?, avg_input_tokens=?, "
                    "avg_output_tokens=?, avg_cost_usd=?, "
                    "last_failure_reason=?, last_used_at=? "
                    "WHERE id=?",
                    (
                        total,
                        1 if success else 0,
                        0 if success else 1,
                        new_avg_lat,
                        new_avg_in,
                        new_avg_out,
                        new_avg_cost,
                        (failure_reason if not success else row["last_failure_reason"]),
                        now,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO model_performance "
                    "(model, provider, node_type, success, failure, "
                    "total_executions, avg_latency_seconds, "
                    "avg_input_tokens, avg_output_tokens, "
                    "avg_cost_usd, last_failure_reason, "
                    "last_used_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        model,
                        provider,
                        node_type,
                        1 if success else 0,
                        0 if success else 1,
                        latency_seconds,
                        float(input_tokens),
                        float(output_tokens),
                        cost_usd,
                        failure_reason if not success else "",
                        now,
                        now,
                    ),
                )
            conn.commit()
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Failed to record model outcome: {e}")

    def query_model_rankings(
        self,
        node_type: str = "",
        min_executions: int = 3,
    ) -> list[dict]:
        """Query model performance rankings for learned routing.

        Returns models sorted by success_rate DESC, avg_latency ASC.
        Only includes models with at least min_executions runs.

        Args:
            node_type: Filter by node type
                (empty = all types aggregated).
            min_executions: Minimum executions for ranking.

        Returns:
            List of dicts with model, provider, success_rate, etc.

        """
        conn = self._get_conn()
        try:
            if node_type:
                rows = conn.execute(
                    "SELECT model, provider, node_type, "
                    "CAST(success AS REAL) / "
                    "MAX(total_executions, 1) as success_rate, "
                    "avg_latency_seconds, avg_cost_usd, "
                    "total_executions, last_failure_reason, "
                    "last_used_at "
                    "FROM model_performance "
                    "WHERE node_type=? "
                    "AND total_executions >= ? "
                    "ORDER BY success_rate DESC, "
                    "avg_latency_seconds ASC",
                    (node_type, min_executions),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT model, provider, "
                    "'' as node_type, "
                    "CAST(SUM(success) AS REAL) / "
                    "MAX(SUM(total_executions), 1) "
                    "as success_rate, "
                    "AVG(avg_latency_seconds) "
                    "as avg_latency_seconds, "
                    "AVG(avg_cost_usd) as avg_cost_usd, "
                    "SUM(total_executions) "
                    "as total_executions, "
                    "'' as last_failure_reason, "
                    "MAX(last_used_at) as last_used_at "
                    "FROM model_performance "
                    "GROUP BY model, provider "
                    "HAVING SUM(total_executions) >= ? "
                    "ORDER BY success_rate DESC, "
                    "avg_latency_seconds ASC",
                    (min_executions,),
                ).fetchall()

            return [dict(row) for row in rows]
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Failed to query model rankings: {e}")
            return []
