"""Redis Streams write-ahead log (WAL) outbox for DAG scheduler fault recovery.

Phase 1 of the DAG fault-recovery hardening: a durable, replay-safe write-ahead
log that records in-flight node state to a Redis Stream *before* a worker
executes a node and *after* it completes. The worker never mutates LanceDB
directly; the reconciliation daemon (:mod:`beagle.fault_recovery.reconciliation`)
materialises the stream into SQLite.

Design goals
------------
* **Idempotent** — writing the same ``(workflow_id, node_name, event_type)``
  tuple twice is harmless (the reconciliation upsert is keyed on that pair).
* **Replay-safe** — the stream is append-only; a consumer group with a stored
  last-delivered ID replays from where it left off, and ``XRANGE`` supports
  full re-reads on restart to avoid split-brain.
* **Best-effort** — this is a hardening aid, not a hard dependency. If Redis is
  unreachable (or the ``redis`` package is missing), every write degrades to a
  debug log and returns ``False``; the workflow continues unaffected.

Usage::

    from beagle.fault_recovery.outbox import OutboxClient

    outbox = OutboxClient()  # reads redis://localhost:6379 by default
    await outbox.write_pending(
        workflow_id="wf-1", node_name="research", dag_id="dag-1",
        state_snapshot={"status": "running"},
    )
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("Beagle.fault_recovery.outbox")

# Event types written to the stream.
EVENT_PENDING = "pending"
EVENT_COMPLETED = "completed"

# Default Redis stream used by the outbox.
DEFAULT_STREAM = "dag:outbox"

# Default consumer-group name for XREADGROUP consumers.
DEFAULT_GROUP = "dag-reconciliation"

# Socket timeouts for the aioredis client (D-07). Without these both default to
# unlimited: a hung Redis blocks the workflow even though this module
# documents itself as best-effort. Taken from a module constant, not a literal
# at the call site, so a single change governs both the connect and the
# read/write side.
_SOCKET_TIMEOUT = 5.0
_SOCKET_CONNECT_TIMEOUT = 5.0

# Approximate stream cap (D-08). Redis Streams have no automatic trimming, so
# an unbounded append path grows memory without limit. XADD with MAXLEN ~
# keeps the stream within ~N entries; the tilde form is approximate.
_MAXLEN = 100_000


def _default_consumer_name() -> str:
    """Return ``hostname:pid`` for this process.

    D-08: every OutboxClient previously took the static consumer name
    ``outbox-client``, so two daemons in one group shared it. That gave
    partitioned delivery with no ordering guarantee, and since the SQLite
    upsert is last-writer-wins on ``(workflow_id, node_name)`` a node could be
    recorded pending after it had already completed. Two daemons must be
    distinct group members.
    """
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"


class OutboxError(RuntimeError):
    """Raised when a Redis stream operation fails in a non-degradable way.

    Note: callers are expected to treat outbox failures as soft. This error is
    primarily used internally and by callers that explicitly opt into
    fail-hard behaviour.
    """


class OutboxClient:
    """Redis Streams-based write-ahead log for DAG node lifecycle events.

    The client is intentionally thin: it appends structured events to a Redis
    Stream and provides read helpers for replay and consumer-group polling.
    Redis access is lazy — the ``redis`` package is imported on first use, so a
    deployment without Redis still imports this module cleanly.
    """

    def __init__(
        self,
        stream: str = DEFAULT_STREAM,
        redis_url: str = "redis://localhost:6379",
        group: str = DEFAULT_GROUP,
        consumer: str | None = None,
    ) -> None:
        """Initialise the outbox client.

        Args:
            stream: Name of the Redis stream to append to.
            redis_url: Redis connection URL (``redis://host:port/db``).
            group: Consumer-group name used by ``XREADGROUP`` consumers.
            consumer: Consumer name. Defaults to ``hostname:pid`` so two
                daemons in one group are distinct members (D-08). A shared
                static name gave partitioned delivery with no ordering
                guarantee and let a node be recorded pending after it
                completed.
        """
        self.stream = stream
        self.redis_url = redis_url
        self.group = group
        self.consumer = consumer or _default_consumer_name()
        self._redis: Any | None = None  # lazily-imported redis.asyncio client
        self._redis_available: bool | None = None  # tri-state: None=unknown

    # ── redis client bootstrap (lazy, best-effort) ──────────────────────────
    def _get_redis(self) -> Any | None:
        """Return the lazily-initialised redis.asyncio client, or None.

        The ``redis`` package is imported on first use so a missing dependency
        or a down server degrades to ``None`` (soft failure) instead of raising
        at import time. The client is created once and reused.
        """
        if self._redis is not None:
            return self._redis
        if self._redis_available is False:
            return None
        try:
            import redis.asyncio as aioredis
        except ImportError:
            self._redis_available = False
            logger.warning(
                "[Outbox] redis package not installed; outbox WAL is disabled "
                "(best-effort hardening continues without it)."
            )
            return None
        try:
            # D-07: without socket_timeout / socket_connect_timeout both default
            # to unlimited, so a hung Redis blocks the workflow even though this
            # module documents itself as best-effort. Both come from the module
            # constant, not a literal at the call site.
            self._redis = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_timeout=_SOCKET_TIMEOUT,
                socket_connect_timeout=_SOCKET_CONNECT_TIMEOUT,
            )
            self._redis_available = True
        except (ValueError, OSError, TypeError) as exc:  # connection-URL errors
            self._redis_available = False
            logger.warning("[Outbox] Cannot initialise redis client: %s", exc)
            return None
        return self._redis

    async def close(self) -> None:
        """Close the underlying Redis connection, if any."""
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None
            self._redis_available = None

    # ── event writing ────────────────────────────────────────────────────────
    def _build_event(
        self,
        workflow_id: str,
        node_name: str,
        event_type: str,
        dag_id: str,
        state_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        """Build a Redis-Stream-ready field dict for one event."""
        return {
            "workflow_id": workflow_id,
            "node_name": node_name,
            "event_type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "state_snapshot": json.dumps(dict(state_snapshot or {})),
            "dag_id": dag_id,
        }

    async def _append(
        self,
        *,
        workflow_id: str,
        node_name: str,
        event_type: str,
        dag_id: str,
        state_snapshot: Mapping[str, Any] | None,
    ) -> str | None:
        """Append one event to the stream and return its stream ID.

        Returns ``None`` (and logs) on any soft failure; raises
        :class:`OutboxError` only for caller-requested hard failures, which is
        not the default.
        """
        client = self._get_redis()
        if client is None:
            logger.debug(
                "[Outbox] Redis unavailable — skipping %s event for %s/%s",
                event_type,
                workflow_id,
                node_name,
            )
            return None
        fields = self._build_event(
            workflow_id=workflow_id,
            node_name=node_name,
            event_type=event_type,
            dag_id=dag_id,
            state_snapshot=state_snapshot,
        )
        try:
            # D-08: Redis Streams have no automatic trimming. Without MAXLEN the
            # stream grows without bound as the scheduler appends pending/completed
            # events for every node of every workflow. The tilde form is
            # approximate and keeps the stream within ~_MAXLEN entries.
            stream_id = await client.xadd(
                self.stream,
                fields,
                maxlen=_MAXLEN,
                approximate=True,
            )
            logger.debug(
                "[Outbox] Appended %s event %s to %s (workflow=%s node=%s)",
                event_type,
                stream_id,
                self.stream,
                workflow_id,
                node_name,
            )
            return str(stream_id)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=outbox is best-effort; these are the redis/connection/serialisation failure modes the append can actually raise
            logger.warning(
                "[Outbox] Failed to append %s event to stream %s (%s); "
                "outbox is best-effort so the workflow continues.",
                event_type,
                self.stream,
                exc,
            )
            return None

    async def write_pending(
        self,
        workflow_id: str,
        node_name: str,
        dag_id: str,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Write a ``pending`` event before a worker executes a node.

        Args:
            workflow_id: The DAG execution identifier.
            node_name: The node being started.
            dag_id: The DAG (workflow definition) identifier.
            state_snapshot: Optional JSON-serialisable node state snapshot.

        Returns:
            The Redis stream ID, or ``None`` on a soft (best-effort) failure.
        """
        return await self._append(
            workflow_id=workflow_id,
            node_name=node_name,
            event_type=EVENT_PENDING,
            dag_id=dag_id,
            state_snapshot=state_snapshot,
        )

    async def write_completed(
        self,
        workflow_id: str,
        node_name: str,
        dag_id: str,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Write a ``completed`` event after a worker finishes a node.

        The worker never writes directly to LanceDB; this is the sole durable
        completion signal the reconciliation daemon consumes.

        Args:
            workflow_id: The DAG execution identifier.
            node_name: The node that finished.
            dag_id: The DAG identifier.
            state_snapshot: Optional JSON-serialisable final node state.

        Returns:
            The Redis stream ID, or ``None`` on a soft failure.
        """
        return await self._append(
            workflow_id=workflow_id,
            node_name=node_name,
            event_type=EVENT_COMPLETED,
            dag_id=dag_id,
            state_snapshot=state_snapshot,
        )

    # ── reading / replay ─────────────────────────────────────────────────────
    async def read_events(
        self,
        last_id: str = "0",
        count: int = 100,
    ) -> list[dict[str, Any]]:
        """Read events from the stream via ``XRANGE`` (full replay).

        ``last_id="0"`` reads the entire stream from the beginning, which is
        the split-brain-avoidance path used by the reconciliation daemon on
        restart.

        Args:
            last_id: Stream ID to start reading from ("0" = start).
            count: Maximum number of events to return per call.

        Returns:
            A list of event dicts: ``{id, workflow_id, node_name, event_type,
            timestamp, state_snapshot, dag_id}``.
        """
        client = self._get_redis()
        if client is None:
            return []
        try:
            raw = await client.xrange(self.stream, min=last_id, max="+", count=count)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=redis server errors only; the read path is a pure query with no side effects
            logger.warning("[Outbox] XRANGE failed on %s (%s)", self.stream, exc)
            return []
        return [self._decode_entry(eid, fields) for eid, fields in raw]

    async def read_group(
        self,
        last_id: str = ">",
        count: int = 100,
    ) -> list[dict[str, Any]]:
        """Read events as a consumer-group member via ``XREADGROUP``.

        The ``dag-reconciliation`` group is created idempotently on first use,
        so multiple reconciliation daemons can share the workload without
        double-processing.

        Args:
            last_id: ``">"`` (new events) or a concrete ID for replay.
            count: Maximum number of events to return per call.

        Returns:
            A list of event dicts (same shape as :meth:`read_events`).
        """
        client = self._get_redis()
        if client is None:
            return []
        try:
            await client.xgroup_create(
                self.stream, self.group, id="0", mkstream=True
            )
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=BUSYGROUP is the expected case; these are the redis error modes the group-create can actually raise
            logger.debug(
                "[Outbox] Consumer group %s on %s already exists or errored (%s)",
                self.group,
                self.stream,
                exc,
            )
        try:
            result = await client.xreadgroup(
                groupname=self.group,
                consumername=self.consumer,
                streams={self.stream: last_id},
                count=count,
            )
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=redis server errors only; a failed read must not abort the poll loop
            logger.warning("[Outbox] XREADGROUP failed on %s (%s)", self.stream, exc)
            return []
        entries: list[dict[str, Any]] = []
        for _stream_name, payload in result or []:
            for eid, fields in payload:
                entries.append(self._decode_entry(eid, fields))
        return entries

    async def ack(self, *stream_ids: str) -> None:
        """Acknowledge processed events (``XACK``) in the consumer group."""
        client = self._get_redis()
        if client is None or not stream_ids:
            return
        try:
            await client.xack(self.stream, self.group, *stream_ids)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=redis server errors only; an ack failure is a soft loss, not a crash
            logger.debug("[Outbox] XACK failed on %s (%s)", self.stream, exc)

    async def xautoclaim(
        self,
        start_id: str = "0-0",
        count: int = 100,
    ) -> list[dict[str, Any]]:
        """Reclaim pending entries orphaned by a dead consumer (XAUTOCLAIM).

        D-08: entries read by XREADGROUP but never acked stay in the
        pending-entries list forever, so a consumer that dies mid-batch
        starves the group. XAUTOCLAIM hands them to the living consumer so the
        ledger converges without a full group reset.

        Args:
            start_id: Cursor for the pending-entries scan; ``"0-0"`` starts at
                the beginning.
            count: Maximum number of entries to claim per call.

        Returns:
            The reclaimed event dicts (decoded the same way as
            :meth:`read_group`).
        """
        client = self._get_redis()
        if client is None:
            return []
        try:
            result = await client.xautoclaim(
                name=self.stream,
                groupname=self.group,
                consumername=self.consumer,
                min_idle_time=0,
                start_id=start_id,
                count=count,
            )
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:  # RATIONALE=redis server errors only; reclaim failure is best-effort
            logger.debug("[Outbox] XAUTOCLAIM failed on %s (%s)", self.stream, exc)
            return []
        # xautoclaim returns (next_cursor, [(eid, fields), ...], deleted_ids). The
        # cursor is the next scan position; it is not surfaced here because
        # reclaim is a fire-and-forget housekeeping pass, not a paginated API.
        if not result:
            return []
        entries_raw = result[1] if len(result) > 1 else []
        return [self._decode_entry(eid, fields) for eid, fields in entries_raw]

    def _decode_entry(
        self, eid: str, fields: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Decode a raw Redis stream entry into a structured event dict."""
        decoded: dict[str, Any] = {"id": eid}
        for key in (
            "workflow_id",
            "node_name",
            "event_type",
            "timestamp",
            "state_snapshot",
            "dag_id",
        ):
            decoded[key] = fields.get(key, "")
        snapshot_raw = decoded.get("state_snapshot") or ""
        if snapshot_raw:
            try:
                decoded["state_snapshot"] = json.loads(snapshot_raw)
            except json.JSONDecodeError:
                decoded["state_snapshot"] = {}
        return decoded


__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_STREAM",
    "EVENT_COMPLETED",
    "EVENT_PENDING",
    "OutboxClient",
    "OutboxError",
]
