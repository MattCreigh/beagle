"""Reconciliation daemon — materialises the ``dag:outbox`` stream into SQLite.

Phase 1 of the DAG fault-recovery hardening: a background consumer that reads
the Redis Streams WAL written by :class:`beagle.fault_recovery.outbox.OutboxClient`
and upserts node lifecycle state into a SQLite database.

The SQLite store follows the ``TaskStore`` pattern in
``beagle.infrastructure.task_store`` (thread-local connections, WAL mode,
parameterised queries) but is a dedicated, idempotent ledger keyed on the
``(workflow_id, node_name)`` pair rather than on task ids.

Design
------
* **Idempotent upsert** — materialisation is keyed on ``(workflow_id,
  node_name)``; replaying the stream multiple times cannot duplicate rows.
* **Split-brain avoidance** — on start the daemon re-reads the stream from
  the beginning (``XRANGE 0``) to reconcile any events a previous incarnation
  may have half-consumed, then polls ``XREADGROUP >`` for new events.
* **Background asyncio task** — :meth:`ReconciliationDaemon.run` is an
  infinite polling loop designed to be spawned with ``asyncio.create_task``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beagle.infrastructure.sqlite_conn import ThreadLocalSQLite

from .outbox import OutboxClient, OutboxError

logger = logging.getLogger("Beagle.fault_recovery.reconciliation")

DEFAULT_DB_NAME = "dag_reconciliation.db"


class ReconciliationStore(ThreadLocalSQLite):
    """SQLite-backed idempotent ledger for DAG node lifecycle state.

    Connections are one-per-thread and all of them are released by ``close()``,
    from any thread (see :class:`~beagle.infrastructure.sqlite_conn.ThreadLocalSQLite`).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS dag_node_state (
        workflow_id TEXT NOT NULL,
        node_name   TEXT NOT NULL,
        dag_id      TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        state_snapshot_json TEXT,
        last_seen   TIMESTAMP NOT NULL,
        PRIMARY KEY (workflow_id, node_name)
    );
    CREATE INDEX IF NOT EXISTS idx_dag_node_state_dag ON dag_node_state(dag_id);
    """

    def __init__(self, db_path: Path | str) -> None:
        """Initialise the reconciliation store.

        Args:
            db_path: Path to the SQLite database file.
        """
        # D-19: the connection registry lives in the shared base so close() can
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

    def upsert(self, event: dict[str, Any]) -> None:
        """Idempotently upsert one stream event into the ledger.

        Keyed on ``(workflow_id, node_name)`` so replaying the stream is safe.

        Args:
            event: A decoded event dict (see :meth:`OutboxClient.read_events`).
        """
        workflow_id = str(event.get("workflow_id") or "")
        node_name = str(event.get("node_name") or "")
        dag_id = str(event.get("dag_id") or "")
        event_type = str(event.get("event_type") or "")
        snapshot = event.get("state_snapshot") or {}
        snapshot_json = json.dumps(snapshot)
        now = datetime.now(UTC).isoformat()
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO dag_node_state (
                    workflow_id, node_name, dag_id, event_type,
                    state_snapshot_json, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, node_name) DO UPDATE SET
                    dag_id = excluded.dag_id,
                    event_type = excluded.event_type,
                    state_snapshot_json = excluded.state_snapshot_json,
                    last_seen = excluded.last_seen
                """,
                (
                    workflow_id,
                    node_name,
                    dag_id,
                    event_type,
                    snapshot_json,
                    now,
                ),
            )

    def get_node_state(
        self, workflow_id: str, node_name: str
    ) -> dict[str, Any] | None:
        """Return the materialised state for one node, or ``None``."""
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT workflow_id, node_name, dag_id, event_type,
                   state_snapshot_json, last_seen
            FROM dag_node_state
            WHERE workflow_id = ? AND node_name = ?
            """,
            (workflow_id, node_name),
        ).fetchone()
        if row is None:
            return None
        return {
            "workflow_id": row["workflow_id"],
            "node_name": row["node_name"],
            "dag_id": row["dag_id"],
            "event_type": row["event_type"],
            "state_snapshot": (
                json.loads(row["state_snapshot_json"])
                if row["state_snapshot_json"]
                else {}
            ),
            "last_seen": row["last_seen"],
        }

    def list_workflows(self, dag_id: str | None = None) -> list[dict[str, Any]]:
        """List all materialised node-state rows, optionally filtered by dag_id."""
        conn = self._get_conn()
        if dag_id:
            rows = conn.execute(
                """
                SELECT workflow_id, node_name, dag_id, event_type,
                       state_snapshot_json, last_seen
                FROM dag_node_state WHERE dag_id = ?
                ORDER BY last_seen
                """,
                (dag_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT workflow_id, node_name, dag_id, event_type,
                       state_snapshot_json, last_seen
                FROM dag_node_state ORDER BY last_seen
                """
            ).fetchall()
        return [
            {
                "workflow_id": r["workflow_id"],
                "node_name": r["node_name"],
                "dag_id": r["dag_id"],
                "event_type": r["event_type"],
                "state_snapshot": (
                    json.loads(r["state_snapshot_json"])
                    if r["state_snapshot_json"]
                    else {}
                ),
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]

    # close() is inherited from ThreadLocalSQLite: it frees every thread's
    # connection, not only the caller's (D-19).


def default_db_path() -> Path:
    """Return the default reconciliation database path under ``get_data_root``."""
    from beagle.config.paths import get_data_root

    return get_data_root() / DEFAULT_DB_NAME


class ReconciliationDaemon:
    """Background asyncio daemon that materialises the outbox stream to SQLite.

    Spawn with ``asyncio.create_task(daemon.run())``; cancel the task (or send
    it :exc:`asyncio.CancelledError`) to stop the loop cleanly.
    """

    def __init__(
        self,
        outbox: OutboxClient | None = None,
        store: ReconciliationStore | None = None,
        poll_interval_seconds: float = 1.0,
        replay_on_start: bool = True,
        max_batch: int = 100,
    ) -> None:
        """Initialise the daemon.

        Args:
            outbox: Outbox client to read from. Defaults to a fresh client.
            store: Reconciliation store. Defaults to ``default_db_path()``.
            poll_interval_seconds: Sleep between ``XREADGROUP`` polls.
            replay_on_start: Whether to re-read the whole stream on start.
            max_batch: Max events to pull per ``XREADGROUP`` call.
        """
        self.outbox = outbox or OutboxClient()
        self.store = store or ReconciliationStore(default_db_path())
        self.poll_interval = poll_interval_seconds
        self.replay_on_start = replay_on_start
        self.max_batch = max_batch
        self._processed = 0
        # D-06: bounded exponential backoff for poll-loop errors. The old code
        # did `await self.run()` in the except handler — recursion that consumed
        # a stack frame per failure and re-read the whole stream each time.
        self._backoff_base = 0.5
        self._backoff_cap = 30.0
        self._backoff_attempt = 0

    def _backoff(self) -> float:
        """Return the next sleep duration, capped and jitter-free.

        Each successive error doubles the delay up to a hard cap, so a wedged
        Redis cannot grow the scheduler's delay without bound. The attempt
        counter is reset on a normal poll so a transient blip does not
        accumulate.
        """
        delay = min(
            self._backoff_base * (2 ** self._backoff_attempt), self._backoff_cap
        )
        self._backoff_attempt += 1
        return delay

    def _reset_backoff(self) -> None:
        self._backoff_attempt = 0

    @property
    def processed_count(self) -> int:
        """Number of events materialised in this daemon's lifetime."""
        return self._processed

    async def _replay(self) -> None:
        """Re-read the entire stream to reconcile any prior half-consumed events."""
        try:
            events = await self.outbox.read_events(last_id="0", count=100_000)
            for event in events:
                self._materialize(event)
            if events:
                logger.info(
                    "[Reconciliation] Replayed %d events from %s on start "
                    "(split-brain reconciliation).",
                    len(events),
                    self.outbox.stream,
                )
        except (
            OSError,
            RuntimeError,
            OutboxError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=outbox/redis soft failures only; a live poll loop cannot abort on a bug in this daemon
            logger.warning(
                "[Reconciliation] Stream replay failed (%s); continuing with "
                "live polling only.",
                exc,
            )

    def _materialize(self, event: dict[str, Any]) -> None:
        """Idempotently upsert one event into SQLite."""
        self.store.upsert(event)
        self._processed += 1

    async def run(self) -> None:
        """Run the polling loop until cancelled.

        On start, optionally re-reads the whole stream (split-brain avoidance),
        then polls ``XREADGROUP >`` for new events and materialises each into
        SQLite.
        """
        logger.info(
            "[Reconciliation] Starting daemon on stream %s (poll=%ss)",
            self.outbox.stream,
            self.poll_interval,
        )
        if self.replay_on_start:
            await self._replay()
        try:
            while True:
                # The poll body is guarded INSIDE the loop. D-06 (second
                # attempt): the guard previously wrapped the whole ``while
                # True``, so the first Redis blip left the loop for good —
                # one error killed the daemon instead of backing it off. The
                # recursion went away but the liveness did not come back.
                # Guarding the body keeps the stack depth constant AND keeps
                # the daemon polling after any number of consecutive errors.
                try:
                    events = await self.outbox.read_group(
                        last_id=">", count=self.max_batch
                    )
                    ids: list[str] = []
                    for event in events:
                        self._materialize(event)
                        eid = event.get("id")
                        if eid:
                            ids.append(str(eid))
                    if ids:
                        await self.outbox.ack(*ids)
                    self._reset_backoff()
                    delay = self.poll_interval
                except (
                    OSError,
                    RuntimeError,
                    OutboxError,
                    ValueError,
                    TypeError,
                ) as exc:  # RATIONALE=outbox/redis soft failures only; a live poll loop cannot abort on a bug in this daemon
                    logger.warning(
                        "[Reconciliation] Daemon poll loop errored (%s); "
                        "continuing.",
                        exc,
                    )
                    # Clears the whole-stream replay flag BEFORE the backoff
                    # sleep. Placed first, not after: a test driver that
                    # interrupts the sleep (or a real scheduler that cancels
                    # here) must still leave the daemon in the no-replay
                    # state, otherwise a restart re-reads the entire stream
                    # with count=100_000.
                    self.replay_on_start = False  # never re-read whole stream again
                    # Bounded exponential backoff, NOT a recursive restart.
                    # The old code did `await self.run()` here, which
                    # re-entered run() and re-read the whole stream with
                    # count=100_000 on every error — recursion that consumed a
                    # stack frame per failure and could never exit the loop.
                    # Each poll iteration is now independent, and the backoff
                    # is capped so a wedged Redis cannot grow the scheduler's
                    # delay without bound.
                    delay = self._backoff()
                # Single sleep, outside the guard: the happy path sleeps the
                # poll interval, the error path sleeps the bounded backoff,
                # and neither can be skipped by an exception in the other.
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(
                "[Reconciliation] Daemon cancelled — %d events processed.",
                self._processed,
            )
            raise


def make_daemon() -> ReconciliationDaemon:
    """Construct a fully-wired daemon from defaults (for CLI / tests)."""
    return ReconciliationDaemon()


__all__ = [
    "DEFAULT_DB_NAME",
    "ReconciliationDaemon",
    "ReconciliationStore",
    "default_db_path",
    "make_daemon",
]
