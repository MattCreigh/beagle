# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""The Beacon server: owns the store, drains agent rings, serves socket RPC.

See plans/beagle-beacon-coordination.xml WP-4, decisions D-01, D-04, and
invariant I-1.

Two write paths land here (decision D-04):

- The RPC path is the store itself, reached through the
  :class:`~beagle.beacon.backend.StoreClient` seam
  (plans/beagle-coord-backend-slot.xml). The shipped
  ``fakeredis_unix`` backend's client speaks the redis protocol directly
  to :class:`~beagle.beacon.backends.fakeredis_unix.UnixFakeServer`;
  there is no separate custom RPC layer for synchronous operations,
  because fakeredis already gives every synchronous op (SET NX,
  HGETALL, ...) for free.
- The ring path is drained by :class:`RingPoller`, running on its own thread,
  and applied to the SAME store via a driver-resolved client
  (:func:`apply_intent`).

Invariant I-1 (read-your-writes) requires that a ring write from agent A be
visible before A's next RPC observes the store. :class:`RingPoller` uses
``orpheus.OrpheusRingPoller.wait()`` as a low-latency wake (not a coarse
polling sleep) so a write is drained in microseconds — far faster than the
hundreds of microseconds a round-trip RPC call costs (measured fact M-2) —
making the ordering hold in practice without a custom acknowledgement
protocol.

Operator override, 2026-08-21: orpheus is a REQUIRED runtime dependency
(reverses the original decision D-06, which imported it behind try/except
and fell back to the socket for every op when it was absent). It is declared
in pyproject.toml's dependencies and resolved via [tool.uv.sources] from the
local skylon_ecosystem/orpheus wheel. A missing orpheus is now a hard
ImportError at module load, not a silent degradation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# orpheus / beacon_lib are the optional proprietary ring transport. Without
# them the server still serves the store over its socket (RPC path); only the
# ring fast-path drain is disabled.
try:
    import orpheus
    from beacon_lib.codec import decode_signal
except ImportError:  # pragma: no cover - exercised only when the wheel is absent
    orpheus = None
    decode_signal = None

from beagle.beacon import contact
from beagle.beacon.archive import elect_flush_owner, flush_archive
from beagle.beacon.backend import ServerHandle, StoreClient
from beagle.beacon.backends import get_driver
from beagle.beacon.keys import BeaconPaths, resolve_paths
from beagle.config.config import get_config

logger = logging.getLogger("Beagle.beacon.server")

_RING_SLOT_SIZE = 4096
_RING_SLOT_COUNT = 64
_HEARTBEAT_INTERVAL_S = 5
# A safety-net cap, not a poll period. OrpheusRingPoller.wait() is an OS-level
# blocking wait (futex/pipe wake, per orpheus_lib's own docs), so a write on
# an ALREADY-REGISTERED ring wakes it in microseconds regardless of this
# value. The cap only bounds the ONE case that value doesn't cover: a newly
# attached agent's ring is not yet in the poller's OS wait set, so its first
# write is picked up on the next loop iteration, which happens at most this
# long after attach if nothing else wakes the poller first. 100ms bounds that
# one-time registration latency without turning steady-state drains (the
# ones invariant I-1 depends on) into a coarse periodic poll.
_POLLER_WAIT_MS = 100
_RING_FILE_MODE = 0o600
# R3 (concept spec section 3): grace period after the roster empties before
# Beacon tears itself down. Absorbs a client reconnect after a transport
# blip. Not yet config-driven (no other value in this module is either —
# see the [coord] section sketch in the plan for the eventual home).
_GRACE_TTL_S = 20.0
_TEARDOWN_ELECTOR_ID = "beacon-server-teardown"


def get_peer_credentials(conn: socket.socket) -> tuple[int, int]:
    """Return (pid, uid) for the process on the other end of a unix socket.

    Uses ``SO_PEERCRED``, a kernel-attested credential — the connecting
    process cannot forge it by claiming a different pid or uid. Beacon binds
    an agent's identity to this, never to a client-supplied id (WP-4
    instruction 3).

    Args:
        conn: A connected AF_UNIX socket.

    Returns:
        (pid, uid) of the peer process, as reported by the kernel.

    """
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", creds)
    return pid, uid


def apply_intent(client: StoreClient, op: str, agent_id: str, args: dict[str, Any]) -> None:
    """Apply one decoded ring intent to the store.

    Args:
        client: A redis client already connected to this Beacon's store.
        op: One of beacon_lib.codec.SIGNAL_OPS (formerly beagle-local RING_OPS).
        agent_id: The agent that sent the intent.
        args: The intent's arguments.

    Raises:
        ValueError: op is not a recognised ring operation. Should not happen
            for an intent that already passed decode_signal's own allowlist
            check, but is asserted here too as a defence against a caller
            constructing an intent by hand.

    """
    if op == "heartbeat":
        _apply_heartbeat(client, agent_id, args)
    elif op == "event":
        _apply_event(client, "event", agent_id, args)
    elif op == "commit":
        _apply_event(client, "commit", agent_id, args)
    elif op == "checkpoint":
        _apply_event(client, "checkpoint", agent_id, args)
    elif op == "plan_status":
        _apply_plan_status(client, agent_id, args)
    elif op == "unlock_file":
        _apply_unlock_file(client, agent_id, args)
    elif op == "touch_file":
        _apply_touch_file(client, agent_id, args)
    else:
        msg = f"apply_intent: unrecognised op {op!r}"
        raise ValueError(msg)


def _apply_heartbeat(client: StoreClient, agent_id: str, args: dict[str, Any]) -> None:
    agent_ttl_s = int(args.pop("agent_ttl_s", 15))
    key = f"agent:{agent_id}"
    if args:
        client.hset(key, mapping={k: str(v) for k, v in args.items()})
    client.expire(key, agent_ttl_s)
    client.sadd("agent:list", agent_id)


def _apply_event(client: StoreClient, kind: str, agent_id: str, args: dict[str, Any]) -> None:
    maxlen = int(args.pop("event_log_maxlen", 500))
    record = json.dumps({"kind": kind, "agent_id": agent_id, **args}, separators=(",", ":"))
    client.lpush("event", record)
    client.ltrim("event", 0, maxlen - 1)


def _apply_plan_status(client: StoreClient, agent_id: str, args: dict[str, Any]) -> None:
    payload = {"owner": agent_id, **{k: str(v) for k, v in args.items()}}
    client.hset("plan:active", mapping=payload)


def _apply_unlock_file(client: StoreClient, agent_id: str, args: dict[str, Any]) -> None:
    filehash = args.get("filehash")
    if not filehash:
        logger.warning("unlock_file intent from %s missing filehash, dropped", agent_id)
        return
    key = f"lock:{filehash}"
    holder = client.get(key)
    if holder == agent_id:
        client.delete(key)
    elif holder is not None:
        logger.debug(
            "unlock_file from %s ignored: lock held by %s, not this agent", agent_id, holder
        )


def _apply_touch_file(client: StoreClient, agent_id: str, args: dict[str, Any]) -> None:
    path = args.get("path")
    if not path:
        return
    key = f"agent:{agent_id}"
    current = client.hget(key, "files") or ""
    files = [f for f in current.split(",") if f]
    if path not in files:
        files.append(path)
    client.hset(key, "files", ",".join(files))


class RingPoller:
    """Drains every attached agent's ingress ring into the store.

    One thread, one orpheus.OrpheusRingPoller multiplexing every attached
    agent's ``<id>.in.ring``. Attaching and detaching agents is safe to call
    from any thread while the poller is running.
    """

    def __init__(
        self,
        paths: BeaconPaths,
        store_client_factory: Callable[[], StoreClient],
        *,
        on_teardown: Callable[[], None] | None = None,
        grace_ttl_s: float = _GRACE_TTL_S,
    ) -> None:
        """Args:
        paths: This Beacon instance's filesystem paths.
        store_client_factory: A zero-arg callable returning a fresh redis
            client connected to this Beacon's own store. Called once, on
            the poller thread, so the client is never shared across threads.
        on_teardown: Called once, from this poller's own thread, when R3
            teardown fires (roster empty for longer than grace_ttl_s after
            having had at least one member). Must be safe to call from a
            thread other than the one that started serve_forever() — it is
            expected to stop the store, not join this poller thread (that
            would deadlock: this IS that thread).
        grace_ttl_s: Seconds the roster may sit empty before teardown.
        """
        self._paths = paths
        self._store_client_factory = store_client_factory
        self._on_teardown = on_teardown
        self._grace_ttl_s = grace_ttl_s
        self._rings: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def attach(self, agent_id: str) -> None:
        """Start draining agent_id's ingress ring, and provision its out-ring.

        The agent (beagle.beacon.connector, WP-5) creates its own IN-ring
        file as the writer, with reset=True, when it starts up — that is the
        ring's one-time provisioning. This side opens the SAME file as a
        reader with reset=False: resetting here would race an already-open
        writer and wipe out its mapping. The ring file must therefore
        already exist by the time attach() is called for an agent_id.

        The OUT-ring (WP-5B) is the opposite: Beacon is the writer, so this
        side provisions it (reset=True, role="writer") — a channel offer
        pushed by beagle.beacon.contact._push_offer opens the SAME file as
        a writer with reset=False, and the agent's own connector opens it
        as a reader with reset=False, mirroring the in-ring convention.
        """
        in_path = self._paths.agent_ring_path(agent_id, direction="in")
        if orpheus is None:  # pragma: no cover - orpheus absent
            # Ring transport unavailable; keep the store socket as the only
            # path. Do not provision ring files.
            return
        ring = orpheus.OrpheusRing(
            str(in_path), "reader", False, _RING_SLOT_SIZE, _RING_SLOT_COUNT, "fifo"
        )
        with contextlib.suppress(OSError):
            in_path.chmod(_RING_FILE_MODE)

        out_path = self._paths.agent_ring_path(agent_id, direction="out")
        out_ring = orpheus.OrpheusRing(
            str(out_path), "writer", True, _RING_SLOT_SIZE, _RING_SLOT_COUNT, "fifo"
        )
        del out_ring
        out_path.chmod(_RING_FILE_MODE)

        with self._lock:
            self._rings[agent_id] = ring

    def detach(self, agent_id: str) -> None:
        """Stop draining agent_id's ring and remove its ring files (in and out)."""
        with self._lock:
            self._rings.pop(agent_id, None)
        self._paths.agent_ring_path(agent_id, direction="in").unlink(missing_ok=True)
        self._paths.agent_ring_path(agent_id, direction="out").unlink(missing_ok=True)

    def drain_one(self, agent_id: str, client: StoreClient) -> int:
        """Drain every currently-pending slot for one agent's ring right now.

        Used both by the background thread and synchronously by the RPC
        path (invariant I-1: drain the caller's own ring before serving its
        RPC).

        Returns:
            The number of intents applied.

        """
        with self._lock:
            ring = self._rings.get(agent_id)
        if ring is None or decode_signal is None:  # pragma: no cover - orpheus absent
            return 0
        applied = 0
        while True:
            raw = ring.peek(False)
            if raw is None:
                break
            try:
                op, sender_id, args = decode_signal(bytes(raw))
                apply_intent(client, op, sender_id, args)
            except ValueError as exc:
                logger.warning("dropping malformed ring intent from %s: %s", agent_id, exc)
            finally:
                ring.advance()
            applied += 1
        return applied

    def _run(self) -> None:
        client = self._store_client_factory()
        try:
            self._run_loop(client)
        finally:
            # Do not leave this connection open once the poller stops: an
            # idle fakeredis connection busy-spins in its own handler thread
            # (backends/fakeredis_unix.py's UnixFakeServer docstring), so a poller that started
            # and stopped many times in one process (as tests do) would
            # otherwise accumulate one spinning thread per instantiation.
            client.close()

    def _run_loop(self, client: StoreClient) -> None:
        ring_transport = orpheus is not None
        poller = orpheus.OrpheusRingPoller() if ring_transport else None
        registered: set[str] = set()
        last_sweep = time.monotonic()
        has_had_members = False
        empty_since: float | None = None
        while not self._stop.is_set():
            with self._lock:
                current_ids = set(self._rings)
                if poller is not None:
                    for agent_id in current_ids - registered:
                        poller.add_ring(self._rings[agent_id])
                        registered.add(agent_id)
                    registered &= current_ids
                agent_ids = list(self._rings)

            if poller is not None:
                poller.wait(timeout_ms=_POLLER_WAIT_MS)
            else:
                # No ring transport: keep the loop ticking so teardown and
                # channel-sweep still run.
                time.sleep(_POLLER_WAIT_MS / 1000.0)

            for agent_id in agent_ids:
                self.drain_one(agent_id, client)

            # WP-5B / D-11: sweep dead-party and stale-offer channels "on
            # every heartbeat tick" — throttled to _HEARTBEAT_INTERVAL_S
            # rather than every poller wake, since sweep_channels scans the
            # whole chan:* keyspace and the poller wakes far more often than
            # any heartbeat actually fires.
            now = time.monotonic()
            if now - last_sweep >= _HEARTBEAT_INTERVAL_S:
                contact.sweep_channels(client, self._paths)
                last_sweep = now

            # R3: last-detach teardown. members(d) reads agent:list directly
            # (never self._rings — WP-5B's out-ring is the only thing that
            # calls RingPoller.attach(), so self._rings is not the roster).
            # The grace clock is this process's own time.monotonic(), never
            # a cross-process wall-clock timestamp (doctrine: monotonic for
            # elapsed time). It starts only once beacon:ever_attached is set
            # (connector.py's attach_agent()) — a permanent store marker,
            # not a sampled scard() observation: an attach immediately
            # followed by a detach can complete inside one poll interval, so
            # a sampler can miss the roster ever being non-empty even though
            # it genuinely was. Checked once and cached — after the first
            # True it can never become false, so there is nothing more to
            # learn from asking again.
            if not has_had_members:
                has_had_members = bool(client.exists("beacon:ever_attached"))

            if client.scard("agent:list") == 0:
                if has_had_members:
                    if empty_since is None:
                        empty_since = now
                    elif now - empty_since > self._grace_ttl_s:
                        self._teardown(client)
                        return
            else:
                empty_since = None

    def _teardown(self, client: StoreClient) -> None:
        """R3: grace elapsed with the roster empty. Archive (a safety net —
        the departing agent's own CoordSession.detach() already did the
        primary flush; elect_flush_owner's NX makes a second attempt here a
        no-op in the common case), then stop serving and remove the socket
        — the concept spec's "tomb" state.
        """
        if elect_flush_owner(client, _TEARDOWN_ELECTOR_ID):
            try:
                flush_archive(client, self._paths)
            except OSError:
                logger.exception(
                    "teardown safety-net archive flush failed for %s", self._paths.socket_path
                )
        logger.info(
            "beacon teardown: roster empty for over %ss, stopping %s",
            self._grace_ttl_s,
            self._paths.socket_path,
        )
        self._stop.set()
        if self._on_teardown is not None:
            self._on_teardown()

    def start(self) -> None:
        """Start the background draining thread."""
        self._thread = threading.Thread(target=self._run, name="beacon-ring-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to exit and wait for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class BeaconServer:
    """Owns the store and the ring poller for one working directory."""

    def __init__(self, workdir: Path | str, *, grace_ttl_s: float = _GRACE_TTL_S) -> None:
        self.paths = resolve_paths(workdir)
        self.store: ServerHandle | None = None
        self.poller: RingPoller | None = None
        self._grace_ttl_s = grace_ttl_s
        coord = get_config().coord
        self._driver = get_driver(coord.backend)
        self._connect_timeout_s = coord.connect_timeout_s
        self._backend_options = coord.backend_options

    def _store_client(self) -> StoreClient:
        return self._driver.connect(
            self.paths,
            connect_timeout_s=self._connect_timeout_s,
            options=self._backend_options,
        )

    def start(self) -> None:
        """Bind the store socket and start the ring poller thread.

        Does NOT write any store keys here: nothing services the socket
        until serve_forever() is running (on this thread or another), so a
        client.set() call made from inside start() connects successfully
        (the kernel accepts into the listen backlog) but then hangs reading
        the response until its own timeout — there is no request-handler
        thread yet to read the command and reply. meta:dir and friends are
        written by the first real attach, once the server loop is live.
        """
        self.store = self._driver.serve(self.paths, self._backend_options)
        self.poller = RingPoller(
            self.paths,
            self._store_client,
            on_teardown=self._handle_teardown,
            grace_ttl_s=self._grace_ttl_s,
        )
        self.poller.start()

    def _handle_teardown(self) -> None:
        """R3 fired on the poller thread. ONLY unblocks serve_forever() —
        socketserver.shutdown() is documented-safe to call from a thread
        other than the one running serve_forever(). Does nothing else: once
        this returns, serve_forever() unblocks on the MAIN thread and
        main() proceeds straight to its own return, which ends the process
        — a daemon thread mid-cleanup at that point is simply killed. The
        rest of teardown (server_close, socket unlink) therefore happens in
        stop(), called from main() after serve_forever() returns, which is
        guaranteed to run to completion before the process can exit. Does
        NOT call self.poller.stop() either, for the same reason C-0 doesn't
        apply here — that thread is the caller of this method, and joining
        your own thread deadlocks.
        """
        if self.store is not None:
            self.store.shutdown()

    def serve_forever(self) -> None:
        """Block, serving store requests until stop() is called from another thread."""
        if self.store is None:
            msg = "BeaconServer.start() must be called before serve_forever()"
            raise RuntimeError(msg)
        self.store.serve_forever()

    def stop(self) -> None:
        """Stop the poller and the store, and remove the socket file.

        Always called from the thread that owns serve_forever() (main(),
        after it returns) — never from the poller thread — so it is safe to
        run to completion, unlike the teardown callback (see
        _handle_teardown).
        """
        if self.poller is not None:
            self.poller.stop()
        if self.store is not None:
            self.store.shutdown()
            self.store.server_close()
        with contextlib.suppress(OSError):
            self.paths.socket_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m beagle.beacon.server <workdir>`."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m beagle.beacon.server <workdir>", file=sys.stderr)
        return 2
    server = BeaconServer(argv[0])
    server.start()
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
