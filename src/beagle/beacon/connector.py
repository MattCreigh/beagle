# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Agent-side connector: the ring fast path and the socket RPC fallback.

See plans/beagle-beacon-coordination.xml WP-5, decisions D-04 and D-06.

:class:`CoordSession` is the one object an agent talks to. It routes each
operation to a ring (fire-and-forget, ~2.9us, decision D-04) or the socket
(synchronous, D-04) per the fixed split. On the ring path, a single write
that fails — the ring is full, or the payload does not fit — falls back to
the socket; an operation is NEVER silently dropped (decision D-06).

Operator override, 2026-08-21: orpheus is a REQUIRED runtime dependency
(reverses the "orpheus not installed" branch of the original D-06 fallback).
EphemeralRingConnector.attach() always provisions a ring; the only remaining
fallback is the per-write case above.

2026-08-25: orpheus is now OPTIONAL. When the wheel is absent the connector
leaves its ring unprovisioned and every op falls back to the socket (D-06) —
an operation is NEVER silently dropped. The socket RPC is the built-in
path and works with zero proprietary deps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

# orpheus / beacon_lib are the optional proprietary ring transport. Without
# them the connector degrades to the socket RPC path (D-06) — every op still
# succeeds over the socket, so orpheus is never a hard dependency.
try:
    import orpheus
    from beacon_lib.codec import encode_signal
except ImportError:  # pragma: no cover - exercised only when the wheel is absent
    orpheus = None  # type: ignore[assignment]
    encode_signal = None  # type: ignore[assignment]

from beagle.beacon import contact
from beagle.beacon.archive import elect_flush_owner, flush_archive
from beagle.beacon.backend import StoreClient
from beagle.beacon.backends import get_driver
from beagle.beacon.contact import Channel, Unreachable
from beagle.beacon.keys import BeaconPaths, filehash
from beagle.beacon.records import AgentRecord
from beagle.config.config import get_config

logger = logging.getLogger("Beagle.beacon.connector")

_RING_SLOT_SIZE = 4096
_RING_SLOT_COUNT = 64
# Channel rings (WP-5B) are provisioned by contact.py at a larger size —
# they carry peer payloads, not telemetry — and MUST be opened at the same
# slot_size/slot_count the writer used, or orpheus raises a header mismatch.
_CHANNEL_SLOT_SIZE = 16384
_CHANNEL_SLOT_COUNT = 32
_RING_FILE_MODE = 0o600


@dataclass(frozen=True)
class LockResult:
    """The outcome of a file-lock acquire attempt."""

    ok: bool
    holder: str


class EphemeralRingConnector:
    """The agent's own outbound ring: fire-and-forget writes to Beacon.

    Creates its ring file on attach (as the writer, reset=True — this is
    the one-time provisioning the ring file needs) and unlinks it on
    detach. If Beacon itself unlinks this file first (because this agent's
    lease expired without a clean detach — the crash-cleanup path), a
    later write simply fails and the caller falls back to the socket; nothing
    reads a half-written or reused ring file, because a fresh attach always
    recreates it with reset=True.
    """

    def __init__(self, paths: BeaconPaths, agent_id: str) -> None:
        self._paths = paths
        self._agent_id = agent_id
        self._ring: Any = None

    @property
    def available(self) -> bool:
        """Whether this connector has a live ring to write to."""
        return self._ring is not None

    def attach(self) -> None:
        """Create and open this agent's outbound ring.

        When the optional ``orpheus`` wheel is absent this is a no-op: the
        connector stays ring-less, ``write()`` returns False, and the caller
        falls back to the socket (D-06).
        """
        if orpheus is None:  # pragma: no cover - exercised only without the wheel
            logger.warning(
                "orpheus not installed; agent %s uses socket RPC only",
                self._agent_id,
            )
            return
        ring_path = self._paths.agent_ring_path(self._agent_id, direction="in")
        self._ring = orpheus.OrpheusRing(
            str(ring_path), "writer", True, _RING_SLOT_SIZE, _RING_SLOT_COUNT, "fifo"
        )
        ring_path.chmod(_RING_FILE_MODE)

    def detach(self) -> None:
        """Unlink this agent's ring file. Idempotent."""
        self._ring = None
        ring_path = self._paths.agent_ring_path(self._agent_id, direction="in")
        ring_path.unlink(missing_ok=True)

    def write(self, op: str, args: dict[str, Any]) -> bool:
        """Attempt to write one intent to the ring.

        Returns:
            True if the write was committed to the ring. False if the ring
            is unavailable, full, or the payload does not fit — in every
            False case the caller MUST fall back to the socket (D-06); this
            method never raises for a routine "can't ring it" condition.

        """
        if self._ring is None:
            return False
        if encode_signal is None:  # pragma: no cover - orpheus absent
            return False
        try:
            payload = encode_signal(op, self._agent_id, args, slot_size=_RING_SLOT_SIZE)
        except ValueError as exc:
            logger.warning("ring write for %s falling back to socket: %s", op, exc)
            return False

        buf = self._ring.reserve()
        if buf is None:
            logger.warning(
                "ring full for agent %s, falling back to socket for %s", self._agent_id, op
            )
            return False
        buf[: len(payload)] = payload
        self._ring.commit(len(payload))
        return True


class SocketRpcClient:
    """The synchronous RPC path: a store client talking directly to the store."""

    def __init__(self, paths: BeaconPaths, *, connect_timeout_s: float = 2.0) -> None:
        coord = get_config().coord
        driver = get_driver(coord.backend)
        self._client: StoreClient = driver.connect(
            paths,
            connect_timeout_s=connect_timeout_s,
            options=coord.backend_options,
        )

    def close(self) -> None:
        self._client.close()

    def lock_file(self, agent_id: str, path: str, *, lock_ttl_s: int) -> LockResult:
        """Synchronous ACQUIRE — invariant I-2, never sent over a ring."""
        key = f"lock:{filehash(path)}"
        if self._client.set(key, agent_id, nx=True, ex=lock_ttl_s):
            return LockResult(ok=True, holder=agent_id)
        holder = self._client.get(key) or ""
        return LockResult(ok=False, holder=holder)

    def list_agents(self) -> list[AgentRecord]:
        agent_ids = self._client.smembers("agent:list")
        records = []
        for agent_id in agent_ids:
            data = self._client.hgetall(f"agent:{agent_id}")
            if not data:
                continue  # lease expired; not yet swept from agent:list
            records.append(AgentRecord.from_hash(data))
        return records

    def whoami(self, agent_id: str) -> AgentRecord | None:
        data = self._client.hgetall(f"agent:{agent_id}")
        if not data:
            return None
        return AgentRecord.from_hash(data)

    def attach_agent(self, record: AgentRecord, *, agent_ttl_s: int) -> None:
        """Register a newly attached agent's initial record.

        Also sets the permanent ``beacon:ever_attached`` marker (R3):
        server.py's teardown clock must never start on a roster that has
        never once been non-empty — a JIT-spawned Beacon racing its own
        first attach must not tear itself down before that attach lands.
        Sampling agent:list for this on the server's poll loop instead was
        tried and is racy: an attach immediately followed by a detach (as
        every coord_attach/coord_detach test does) can complete inside one
        100ms poll interval, so the roster is never OBSERVED non-empty even
        though it certainly was one for real. A permanent store key is the
        only source of truth that cannot be missed by a sampler.
        """
        self._client.hset(f"agent:{record.agent_id}", mapping=record.to_hash())
        self._client.expire(f"agent:{record.agent_id}", agent_ttl_s)
        self._client.sadd("agent:list", record.agent_id)
        self._client.set("beacon:ever_attached", "1")

    def detach_agent(self, agent_id: str) -> None:
        """Remove an agent's record on a clean detach."""
        self._client.delete(f"agent:{agent_id}")
        self._client.srem("agent:list", agent_id)

    def activity(self, limit: int) -> list[str]:
        return self._client.lrange("event", 0, limit - 1)


class CoordSession:
    """The one object an agent talks to. Routes each op per decision D-04."""

    def __init__(
        self,
        paths: BeaconPaths,
        agent_id: str,
        *,
        agent_ttl_s: int = 15,
        lock_ttl_s: int = 120,
        event_log_maxlen: int = 500,
        connect_timeout_s: float = 2.0,
    ) -> None:
        self.agent_id = agent_id
        self._paths = paths
        self._agent_ttl_s = agent_ttl_s
        self._lock_ttl_s = lock_ttl_s
        self._event_log_maxlen = event_log_maxlen
        self._ring = EphemeralRingConnector(paths, agent_id)
        self._rpc = SocketRpcClient(paths, connect_timeout_s=connect_timeout_s)
        self._out_ring: Any = None

    def attach(self, record: AgentRecord) -> None:
        """Join this Beacon: open the outbound ring and register the record."""
        self._ring.attach()
        self._rpc.attach_agent(record, agent_ttl_s=self._agent_ttl_s)

    def detach(self) -> None:
        """Leave this Beacon: unlink the ring, remove the record, and — if
        this was the last agent — contend for the flush-owner election and
        archive the store (WP-6, D-07).
        """
        self._rpc.detach_agent(self.agent_id)
        self._ring.detach()

        if self._rpc._client.scard("agent:list") == 0 and elect_flush_owner(
            self._rpc._client, self.agent_id
        ):
            try:
                flush_archive(self._rpc._client, self._paths)
            except OSError:
                logger.exception("last-detach archive flush failed for %s", self.agent_id)

    def close(self) -> None:
        self._rpc.close()

    # -- ring path, fire-and-forget, falls back to the socket on failure --

    def _ring_or_socket(self, op: str, args: dict[str, Any], socket_fallback) -> None:
        if not self._ring.write(op, args):
            socket_fallback()

    def heartbeat(self, **fields: Any) -> None:
        args = {**fields, "agent_ttl_s": self._agent_ttl_s}

        def fallback() -> None:
            self._rpc._client.hset(
                f"agent:{self.agent_id}", mapping={k: str(v) for k, v in fields.items()}
            )
            self._rpc._client.expire(f"agent:{self.agent_id}", self._agent_ttl_s)
            self._rpc._client.sadd("agent:list", self.agent_id)

        self._ring_or_socket("heartbeat", args, fallback)

    def event(self, action: str, path: str) -> None:
        args = {"action": action, "path": path, "event_log_maxlen": self._event_log_maxlen}

        def fallback() -> None:
            record = json.dumps(
                {"kind": "event", "agent_id": self.agent_id, "action": action, "path": path},
                separators=(",", ":"),
            )
            self._rpc._client.lpush("event", record)
            self._rpc._client.ltrim("event", 0, self._event_log_maxlen - 1)

        self._ring_or_socket("event", args, fallback)

    def unlock_file(self, path: str) -> None:
        args = {"filehash": filehash(path)}

        def fallback() -> None:
            key = f"lock:{filehash(path)}"
            holder = self._rpc._client.get(key)
            if holder == self.agent_id:
                self._rpc._client.delete(key)

        self._ring_or_socket("unlock_file", args, fallback)

    # -- socket path, synchronous --

    def lock_file(self, path: str) -> LockResult:
        return self._rpc.lock_file(self.agent_id, path, lock_ttl_s=self._lock_ttl_s)

    def list_agents(self) -> list[AgentRecord]:
        return self._rpc.list_agents()

    def whoami(self) -> AgentRecord | None:
        return self._rpc.whoami(self.agent_id)

    def activity(self, limit: int = 50) -> list[str]:
        return self._rpc.activity(limit)

    # -- WP-5B: contact directory and peer rendezvous ------------------------
    #
    # open_channel and sweep are store-mutating operations that also touch
    # the local filesystem (ring file creation), but neither requires being
    # invoked from inside the Beacon server process: the store mutations go
    # over the same socket connection lock_file/list_agents already use, and
    # ring files are just mmap'd files under a runtime dir this process
    # already has paths for. Beacon's RingPoller.attach() must have already
    # provisioned the callee's out-ring (WP-7 wires that to coord_attach);
    # until then, integration tests attach the poller side directly.

    def open_channel(self, peer_id: str, kind: str) -> Channel | Unreachable:
        """Open a pairwise channel to peer_id. See beagle.beacon.contact."""
        return contact.open_channel(self._rpc._client, self._paths, self.agent_id, peer_id, kind)

    def poll_offers(self) -> list[dict[str, Any]]:
        """Drain pending channel offers from this agent's own out-ring.

        Returns:
            An empty list if this agent's out-ring has not been provisioned
            yet (Beacon has not called RingPoller.attach() for it) — this is
            a routine "nothing to poll yet" case, not an error.

        """
        if orpheus is None:  # pragma: no cover - orpheus absent
            return []
        if self._out_ring is None:
            out_path = self._paths.agent_ring_path(self.agent_id, direction="out")
            if not out_path.exists():
                return []
            self._out_ring = orpheus.OrpheusRing(
                str(out_path), "reader", False, _RING_SLOT_SIZE, _RING_SLOT_COUNT, "fifo"
            )
        return contact.poll_offers(self._out_ring)

    def send_on_channel(self, ring_path: str, payload: bytes) -> bool:
        """Write one message on an already-open channel ring. See contact.send()."""
        if orpheus is None:  # pragma: no cover - orpheus absent
            return False
        ring = orpheus.OrpheusRing(
            ring_path, "writer", False, _CHANNEL_SLOT_SIZE, _CHANNEL_SLOT_COUNT, "fifo"
        )
        return contact.send(ring, payload)

    def read_channel(self, ring_path: str) -> list[bytes]:
        """Drain every pending message on an already-open channel ring."""
        if orpheus is None:  # pragma: no cover - orpheus absent
            return []
        ring = orpheus.OrpheusRing(
            ring_path, "reader", False, _CHANNEL_SLOT_SIZE, _CHANNEL_SLOT_COUNT, "fifo"
        )
        messages: list[bytes] = []
        while True:
            raw = ring.peek(False)
            if raw is None:
                break
            messages.append(bytes(raw))
            ring.advance()
        return messages

    def close_channel(self, channel_id: str) -> bool:
        """Close a channel on demand. See beagle.beacon.contact.close_channel."""
        return contact.close_channel(self._rpc._client, channel_id)

    def list_channels(self) -> list[Channel]:
        """List every channel this agent is currently a party to."""
        return contact.list_channels(self._rpc._client, self.agent_id)

    def register_plan(self, plan_id: str, status: str) -> None:
        """Announce the plan this agent is executing (fire-and-forget)."""
        args = {"plan_id": plan_id, "status": status}

        def fallback() -> None:
            self._rpc._client.hset("plan:active", mapping={"owner": self.agent_id, **args})

        self._ring_or_socket("plan_status", args, fallback)

    def agent_info(self, agent_id: str) -> AgentRecord | None:
        """Full metadata for a specific agent (not necessarily this one)."""
        return self._rpc.whoami(agent_id)
