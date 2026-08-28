"""
OpenClaw Task Store
===================
SQLite-backed task persistence with audit trail.

Provides durable storage for tasks with full audit history.

SEC-004: The SQLite database may contain sensitive query text and task payloads.
Deploy on an encrypted volume (e.g., LUKS, eCryptfs) to protect data at rest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class TaskStore:
    """SQLite-backed task persistence with audit trail.

    Thread-safe implementation with connection pooling.
    """

    SCHEMA = """
    -- Task definitions
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        constraints_json TEXT,
        audit_config_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by TEXT DEFAULT 'goose',
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        result_json TEXT,
        error TEXT,
        retry_count INTEGER DEFAULT 0
    );

    -- Audit events (append-only)
    CREATE TABLE IF NOT EXISTS audit_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_data_json TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(task_id)
    );

    -- Metrics (one row per task)
    CREATE TABLE IF NOT EXISTS task_metrics (
        task_id TEXT PRIMARY KEY,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        duration_seconds REAL DEFAULT 0.0,
        tool_calls_count INTEGER DEFAULT 0,
        error_count INTEGER DEFAULT 0,
        FOREIGN KEY (task_id) REFERENCES tasks(task_id)
    );

    -- Indexes for common queries
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
    CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events(task_id);
    CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp);
    """

    VALID_STATUSES = frozenset({"pending", "queued", "running", "completed", "failed", "cancelled"})

    VALID_TYPES = frozenset({"workflow", "skill", "delegate", "tool_invocation"})

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection with WAL mode for concurrent read/write."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent read/write performance
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def close(self) -> None:
        """Close the thread-local database connection."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn

    def __del__(self) -> None:
        """Ensure connection cleanup on garbage collection."""
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """Context manager for transactions."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:  # broad catch intentional
            conn.rollback()
            raise

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._transaction() as cur:
            cur.executescript(self.SCHEMA)

    # -------------------------------------------------------------------------
    # Task CRUD
    # -------------------------------------------------------------------------

    def create_task(
        self,
        task_type: str,
        spec: dict[str, Any],
        constraints: dict[str, Any] | None = None,
        audit_config: dict[str, Any] | None = None,
        created_by: str = "goose",
    ) -> str:
        """Create a new task.

        Args:
            task_type: Type of task (workflow, skill, delegate, tool_invocation)
            spec: Task specification
            constraints: Execution constraints (timeout, budget, retries)
            audit_config: Audit configuration (log_level, capture_inputs)
            created_by: Who created this task

        Returns:
            Generated task_id

        """
        if task_type not in self.VALID_TYPES:
            raise ValueError(f"Invalid task_type: {task_type}. Must be one of {self.VALID_TYPES}")

        task_id = str(uuid.uuid4())  # Full 122-bit entropy — negligible collision even at billions

        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO tasks (task_id, task_type, spec_json,
                                   constraints_json, audit_config_json, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    task_id,
                    task_type,
                    json.dumps(spec),
                    json.dumps(constraints) if constraints else None,
                    json.dumps(audit_config) if audit_config else None,
                    created_by,
                ),
            )

            # Initialize metrics row
            cur.execute(
                """
                INSERT INTO task_metrics (task_id)
                VALUES (?)
            """,
                (task_id,),
            )

            # Create audit event
            self._add_audit_event(
                cur,
                task_id,
                "created",
                {"task_type": task_type, "created_by": created_by},
            )

        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get task by ID."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None

        return self._row_to_dict(row)

    def list_tasks(
        self,
        status: str | None = None,
        task_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List tasks with optional filtering."""
        conn = self._get_conn()
        cur = conn.cursor()

        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []

        if status and status != "all":
            if status not in self.VALID_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            query += " AND status = ?"
            params.append(status)

        if task_type:
            query += " AND task_type = ?"
            params.append(task_type)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cur.execute(query, params)
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def update_task_status(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Update task status.

        Args:
            task_id: Task to update
            status: New status
            result: Optional result data (for completed tasks)
            error: Optional error message (for failed tasks)

        Returns:
            True if updated, False if task not found

        """
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")

        now = datetime.now(UTC).isoformat()

        with self._transaction() as cur:
            # <invariant>
            # The statement is a single literal. It was previously assembled by
            # joining a list of column-assignment fragments into an f-string,
            # which read as SQL-by-string-construction to every scanner even
            # though only literals were joined. Each optional column now keeps
            # its own value with a CASE guard, so which columns change is driven
            # by bound parameters rather than by the shape of the SQL text.
            # The set of columns written for a given call is identical to the
            # previous behaviour.
            # </invariant>
            cur.execute(
                """
                UPDATE tasks SET
                    status       = :status,
                    started_at   = CASE WHEN :set_started   THEN :now    ELSE started_at   END,
                    completed_at = CASE WHEN :set_completed THEN :now    ELSE completed_at END,
                    result_json  = CASE WHEN :set_result    THEN :result ELSE result_json  END,
                    error        = CASE WHEN :set_error     THEN :error  ELSE error        END
                WHERE task_id = :task_id
                """,
                {
                    "status": status,
                    "now": now,
                    "set_started": status == "running",
                    "set_completed": status in ("completed", "failed", "cancelled"),
                    "set_result": bool(result),
                    "result": json.dumps(result) if result else None,
                    "set_error": bool(error),
                    "error": error,
                    "task_id": task_id,
                },
            )

            if cur.rowcount == 0:
                return False

            # Add audit event
            self._add_audit_event(
                cur, task_id, f"status_{status}", {"result": result, "error": error}
            )

            return True

    def cancel_task(self, task_id: str, reason: str = "") -> bool:
        """Cancel a task."""
        with self._transaction() as cur:
            # Check if task can be cancelled
            cur.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return False

            current_status = row["status"]
            if current_status in ("completed", "failed", "cancelled"):
                return False  # Cannot cancel final states

            # Update status
            cur.execute(
                "UPDATE tasks SET status = 'cancelled', error = ? WHERE task_id = ?",
                (f"Cancelled: {reason}" if reason else "Cancelled", task_id),
            )

            # Add audit event
            self._add_audit_event(cur, task_id, "cancelled", {"reason": reason})

            return True

    # -------------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------------

    def update_metrics(
        self,
        task_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_seconds: float = 0.0,
        tool_calls_count: int = 0,
        error_count: int = 0,
    ) -> bool:
        """Update task metrics (incremental)."""
        with self._transaction() as cur:
            cur.execute(
                """
                UPDATE task_metrics
                SET input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    cost_usd = cost_usd + ?,
                    duration_seconds = duration_seconds + ?,
                    tool_calls_count = tool_calls_count + ?,
                    error_count = error_count + ?
                WHERE task_id = ?
            """,
                (
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    duration_seconds,
                    tool_calls_count,
                    error_count,
                    task_id,
                ),
            )
            return cur.rowcount > 0

    def get_metrics(self, task_id: str) -> dict[str, Any] | None:
        """Get metrics for a task."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM task_metrics WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    # -------------------------------------------------------------------------
    # Audit Trail
    # -------------------------------------------------------------------------

    def _add_audit_event(
        self,
        cur: sqlite3.Cursor,
        task_id: str,
        event_type: str,
        event_data: dict[str, Any] | None,
    ) -> None:
        """Add audit event (internal, called within transaction)."""
        cur.execute(
            """
            INSERT INTO audit_events (task_id, event_type, event_data_json)
            VALUES (?, ?, ?)
        """,
            (task_id, event_type, json.dumps(event_data) if event_data else None),
        )

    def add_audit_event(
        self, task_id: str, event_type: str, event_data: dict[str, Any] | None = None
    ) -> bool:
        """Add audit event (public API)."""
        with self._transaction() as cur:
            # Check task exists
            cur.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
            if not cur.fetchone():
                return False

            self._add_audit_event(cur, task_id, event_type, event_data)
            return True

    def get_audit_trail(self, task_id: str) -> list[dict[str, Any]]:
        """Get full audit trail for a task."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT event_id, event_type, event_data_json, timestamp
            FROM audit_events
            WHERE task_id = ?
            ORDER BY timestamp ASC
        """,
            (task_id,),
        )

        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "event_data": json.loads(row["event_data_json"])
                if row["event_data_json"]
                else None,
                "timestamp": row["timestamp"],
            }
            for row in cur.fetchall()
        ]

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert row to dictionary with JSON parsing."""
        result = dict(row)
        for key in (
            "spec_json",
            "constraints_json",
            "audit_config_json",
            "result_json",
        ):
            if result.get(key):
                result[key] = json.loads(result[key])
        return result

    # -------------------------------------------------------------------------
    # Bulk Operations
    # -------------------------------------------------------------------------

    def get_active_task_count(self) -> int:
        """Count tasks in non-final states."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running', 'queued')")
        return cur.fetchone()[0]

    def cleanup_old_tasks(self, days: int = 30) -> int:
        """Remove tasks older than N days (completed/failed/cancelled only)."""
        cutoff = datetime.now(UTC) - timedelta(days=days)

        with self._transaction() as cur:
            # Delete audit events first
            cur.execute(
                """
                DELETE FROM audit_events
                WHERE task_id IN (
                    SELECT task_id FROM tasks
                    WHERE status IN ('completed', 'failed', 'cancelled')
                    AND created_at < ?
                )
            """,
                (cutoff.isoformat(),),
            )

            # Delete metrics
            cur.execute(
                """
                DELETE FROM task_metrics
                WHERE task_id IN (
                    SELECT task_id FROM tasks
                    WHERE status IN ('completed', 'failed', 'cancelled')
                    AND created_at < ?
                )
            """,
                (cutoff.isoformat(),),
            )

            # Delete tasks
            cur.execute(
                """
                DELETE FROM tasks
                WHERE status IN ('completed', 'failed', 'cancelled')
                AND created_at < ?
            """,
                (cutoff.isoformat(),),
            )

            return cur.rowcount


# Singleton instance
_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_task_store(db_path: Path | str | None = None) -> TaskStore:
    """Get or create singleton TaskStore instance.

    Thread-safe: uses a module-level lock to prevent races during
    initialization. Once created, the singleton is returned without
    locking (Python's GIL makes the None check safe for reads).
    """
    global _store
    if _store is None:
        with _store_lock:
            # Double-check after acquiring lock
            if _store is None:
                if db_path is None:
                    db_path = Path.home() / ".local" / "share" / "openclaw" / "tasks.db"
                _store = TaskStore(db_path)
    return _store
