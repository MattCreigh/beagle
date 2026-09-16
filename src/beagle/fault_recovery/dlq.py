"""Dead Letter Queue (DLQ) for failed DAG nodes — SQLite-backed.

Phase 2 of the DAG fault-recovery hardening: when the LLM circuit breaker trips
(consecutive 429/504 responses reaching the threshold), the failed node is
routed to a durable dead-letter queue so it can be inspected and replayed later
via the ``beagle dlq`` CLI.

The store follows the ``TaskStore`` pattern (thread-local SQLite connections in
WAL mode, parameterised queries only) and is independent of the reconciliation
store in :mod:`beagle.fault_recovery.reconciliation` so that DLQ entries are
not clobbered by stream materialisation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beagle.infrastructure.sqlite_conn import ThreadLocalSQLite

logger = logging.getLogger("Beagle.fault_recovery.dlq")

DEFAULT_DB_NAME = "dag_dlq.db"


class DeadLetterQueue(ThreadLocalSQLite):
    """SQLite-backed dead-letter queue for failed DAG nodes.

    Connections are one-per-thread and all of them are released by ``close()``,
    from any thread. Supports enqueue, list, retry (re-enqueue onto the
    reconciliation ledger as pending) and delete.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS dlq_entries (
        dlq_id       TEXT PRIMARY KEY,
        workflow_id  TEXT NOT NULL,
        dag_id       TEXT NOT NULL,
        node_name    TEXT NOT NULL,
        error        TEXT,
        retry_count  INTEGER NOT NULL DEFAULT 0,
        last_attempt TIMESTAMP,
        payload_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_dlq_dag ON dlq_entries(dag_id);
    CREATE INDEX IF NOT EXISTS idx_dlq_workflow ON dlq_entries(workflow_id);
    """

    def __init__(self, db_path: Path | str) -> None:
        """Initialise the dead-letter queue.

        Args:
            db_path: Path to the SQLite database file.
        """
        # D-19: connection ownership lives in the shared base, so close() can
        # reach connections opened on other threads.
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Create the schema if it does not exist."""
        with self._get_conn() as conn:
            conn.executescript(self.SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """Context manager for a single write transaction."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # ── mutations ───────────────────────────────────────────────────────────
    def enqueue(
        self,
        workflow_id: str,
        dag_id: str,
        node_name: str,
        error: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Insert one failed node into the dead-letter queue.

        Idempotent in the sense that each call creates a distinct ``dlq_id``
        (full uuid4), so re-routing the same failure multiple times appends
        distinct entries rather than overwriting history.

        Args:
            workflow_id: The DAG execution identifier.
            dag_id: The DAG (workflow definition) identifier.
            node_name: The node that failed.
            error: Human-readable failure description (e.g. circuit-tripped).
            payload: Optional JSON-serialisable payload (state snapshot, etc.).

        Returns:
            The generated ``dlq_id``.
        """
        dlq_id = str(uuid.uuid4())
        payload_json = None if payload is None else _json_dumps(payload)
        now = datetime.now(UTC).isoformat()
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO dlq_entries (
                    dlq_id, workflow_id, dag_id, node_name, error,
                    retry_count, last_attempt, payload_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    dlq_id,
                    workflow_id,
                    dag_id,
                    node_name,
                    error,
                    now,
                    payload_json,
                ),
            )
        logger.warning(
            "[DLQ] Enqueued failed node %s (workflow=%s, dag=%s) as %s",
            node_name,
            workflow_id,
            dag_id,
            dlq_id,
        )
        return dlq_id

    def delete(self, dlq_id: str) -> bool:
        """Delete one DLQ entry.

        Args:
            dlq_id: The entry to remove.

        Returns:
            ``True`` if a row was deleted, ``False`` if the id was unknown.
        """
        with self._transaction() as cur:
            cur.execute("DELETE FROM dlq_entries WHERE dlq_id = ?", (dlq_id,))
            return cur.rowcount > 0

    def clear(self) -> int:
        """Delete all DLQ entries.

        Returns:
            The number of rows deleted.
        """
        with self._transaction() as cur:
            cur.execute("DELETE FROM dlq_entries")
            return cur.rowcount

    # ── queries ─────────────────────────────────────────────────────────────
    def list_entries(
        self,
        dag_id: str | None = None,
        workflow_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List DLQ entries, optionally filtered.

        Args:
            dag_id: Filter to one DAG.
            workflow_id: Filter to one workflow execution.
            limit: Maximum rows to return.

        Returns:
            A list of entry dicts.
        """
        conn = self._get_conn()
        sql = (
            "SELECT dlq_id, workflow_id, dag_id, node_name, error, "
            "retry_count, last_attempt, payload_json FROM dlq_entries"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if dag_id:
            clauses.append("dag_id = ?")
            params.append(dag_id)
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_attempt DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def retry(
        self,
        dag_id: str | None = None,
        workflow_id: str | None = None,
    ) -> int:
        """Replay failed nodes from the DLQ.

        "Replay" here marks matching entries for retry by incrementing
        ``retry_count`` and refreshing ``last_attempt``; the caller is expected
        to re-run the associated workflow nodes (the reconciliation ledger can
        then be consulted for the current node state).

        Args:
            dag_id: Restrict to entries of one DAG.
            workflow_id: Restrict to entries of one workflow.

        Returns:
            The number of entries marked for retry.
        """
        now = datetime.now(UTC).isoformat()
        with self._transaction() as cur:
            sql = (
                "UPDATE dlq_entries SET retry_count = retry_count + 1, "
                "last_attempt = ?"
            )
            clauses: list[str] = []
            params: list[Any] = [now]
            if dag_id:
                clauses.append("dag_id = ?")
                params.append(dag_id)
            if workflow_id:
                clauses.append("workflow_id = ?")
                params.append(workflow_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            cur.execute(sql, params)
            return cur.rowcount

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a SQLite row to a plain dict with JSON-decoded payload."""
        payload = row["payload_json"]
        return {
            "dlq_id": row["dlq_id"],
            "workflow_id": row["workflow_id"],
            "dag_id": row["dag_id"],
            "node_name": row["node_name"],
            "error": row["error"],
            "retry_count": row["retry_count"],
            "last_attempt": row["last_attempt"],
            "payload": json.loads(payload) if payload else None,
        }

    # close() is inherited from ThreadLocalSQLite: it frees every thread's
    # connection, not only the caller's (D-19).


def default_db_path() -> Path:
    """Return the default DLQ database path under ``get_data_root``."""
    from beagle.config.paths import get_data_root

    return get_data_root() / DEFAULT_DB_NAME


def _json_dumps(obj: dict[str, Any]) -> str:
    """JSON-encode a payload dict (never fails for dict-of-basics)."""
    return json.dumps(obj)


__all__ = [
    "DEFAULT_DB_NAME",
    "DeadLetterQueue",
    "default_db_path",
]
