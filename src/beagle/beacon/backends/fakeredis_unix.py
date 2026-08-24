# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""The fakeredis-over-unix-socket store, behind the BackendDriver protocol.

See plans/beagle-coord-backend-slot.xml WP-B3, decisions D-B1, D-B5, and
hard constraints C-B3/C-B4.

This module MOVES existing working code from the deleted
``beagle.beacon.store`` (parent plan WP-2, decisions D-02/D-03). It is
the only file under ``src/beagle/`` that names ``redis``/``fakeredis`` —
that is the whole point of the slot (C-B3): every caller reaches this
store through :class:`FakeredisUnixDriver`, never by constructing a
client directly.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

import redis
from fakeredis import FakeServer
from fakeredis._tcp_server import TCPFakeRequestHandler

from beagle.beacon.backend import BackendCapabilities, BackendUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from beagle.beacon.backend import StoreClient
    from beagle.beacon.keys import BeaconPaths

logger = logging.getLogger("Beagle.beacon.backends.fakeredis_unix")

_DEFAULT_SOCKET_MODE = 0o600

CAPABILITIES = BackendCapabilities(
    name="fakeredis_unix",
    shared=True,
    requires_server=True,
    description=(
        "fakeredis.FakeServer hosted on a unix domain socket, spoken to with "
        "redis-py. Parent plan (plans/beagle-beacon-coordination.xml) D-02, D-03."
    ),
)


class UnixFakeServer(socketserver.ThreadingUnixStreamServer):
    """MOVED VERBATIM from beagle/beacon/store.py. Do not rewrite.

    <invariant>
      get_request() MUST return a unique synthetic address per connection.
      fakeredis keys its per-connection client table on client_address, and
      ThreadingUnixStreamServer sets that to '' for every unix connection,
      so a constant address makes every agent alias one writer. Parent M-9.
    </invariant>

    Verified (parent plan measured fact M-8): two independent client
    processes connected to one instance observe each other's writes,
    ``SET ... NX`` mutual exclusion holds across processes, TTL expiry
    fires, and the store survives the client count reaching zero.
    """

    daemon_threads = True
    # fakeredis's own TCPFakeRequestHandler.handle() busy-spins (time.sleep(0),
    # which yields rather than actually sleeping) on every idle connection
    # while waiting for the next command. Each held-open connection (the ring
    # poller's own persistent client, in particular) is therefore a thread
    # that is almost always runnable, which under GIL contention can starve
    # the accept loop long enough for a fresh connect() to see the socket's
    # default 5-deep listen backlog as full and fail with EAGAIN. A much
    # larger backlog is a standard, low-risk mitigation: it does not fix the
    # busy-spin (that is fakeredis's own code, not ours to patch), but it
    # gives the kernel enough queue depth to absorb a burst of connects while
    # the accept loop is momentarily starved.
    request_queue_size = 128

    def __init__(self, path: str | Path, *, socket_mode: int = _DEFAULT_SOCKET_MODE) -> None:
        """Bind and start listening on a unix socket.

        Args:
            path: The socket file path. Must not already exist — the caller
                (spawn.py) is responsible for stale-socket detection and
                removal before construction.
            socket_mode: Permission bits applied to the socket file after
                bind. Default 0600 — only the owning user may connect.

        """
        self._shutdown_event = threading.Event()
        self._addr_ids = itertools.count(0)
        self.clients: dict[tuple[str, int], object] = {}

        old_umask = os.umask(0o077)
        try:
            super().__init__(str(path), TCPFakeRequestHandler)
        finally:
            os.umask(old_umask)

        os.chmod(path, socket_mode)

        self.fake_server = FakeServer(server_type="redis", version=(8, 0))
        self.client_ids = itertools.count(0)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        """Accept a connection and assign it a unique synthetic address.

        This override is the fix for M-9. Do not remove it and do not
        replace the per-connection counter with a constant — fakeredis's
        request handler uses the returned address as a dict key to find
        this connection's own :class:`FakeRedisConnection`, and a
        constant address makes every connection alias the same one.
        """
        conn, _real_addr = self.socket.accept()
        return conn, ("unix", next(self._addr_ids))

    def shutdown(self) -> None:
        """Stop serving and signal every request handler loop to exit."""
        self._shutdown_event.set()
        super().shutdown()


class FakeredisUnixDriver:
    """The BackendDriver implementation for fakeredis-over-unix-socket.

    A ``redis.Redis(..., decode_responses=True)`` client already satisfies
    the whole :class:`~beagle.beacon.backend.StoreClient` surface — no
    wrapper class is written or needed (D-B1's reason: a wrapper is a
    second implementation that drifts from the one redis-py actually
    provides).
    """

    capabilities = CAPABILITIES

    def is_live(self, paths: BeaconPaths, *, connect_timeout_s: float) -> bool:
        """PING over paths.socket_path.

        False for a socket file that exists but does not answer — that is
        a crashed Beacon, not a live one (concept spec section 9: "detect
        a dead process, not a vanished one"). This is the body of the
        deleted ``store.ping``, moved here unchanged (C-B4).
        """
        client = None
        try:
            client = redis.Redis(
                unix_socket_path=str(paths.socket_path),
                socket_connect_timeout=connect_timeout_s,
                socket_timeout=connect_timeout_s,
            )
            return bool(client.ping())
        except (redis.RedisError, OSError):
            return False
        finally:
            if client is not None:
                with contextlib.suppress(redis.RedisError, OSError):
                    client.close()

    def clear_stale(self, paths: BeaconPaths) -> None:
        """Unlink paths.socket_path if present. Idempotent."""
        if paths.socket_path.exists():
            logger.info(
                "removing stale Beacon socket at %s (no live process answered)",
                paths.socket_path,
            )
            paths.socket_path.unlink(missing_ok=True)

    def serve(self, paths: BeaconPaths, options: Mapping[str, str]) -> UnixFakeServer:
        """Bind the socket at socket_mode and return the UnixFakeServer.

        Raises:
            BackendUnavailableError: the store could not be created
                (address already in use, permission denied, ...).

        """
        del options  # fakeredis_unix takes no backend_options today
        try:
            return UnixFakeServer(paths.socket_path)
        except OSError as exc:
            msg = f"fakeredis_unix: could not bind {paths.socket_path}: {exc}"
            raise BackendUnavailableError(msg) from exc

    def connect(
        self, paths: BeaconPaths, *, connect_timeout_s: float, options: Mapping[str, str]
    ) -> StoreClient:
        """redis.Redis(unix_socket_path=..., decode_responses=True, ...).

        decode_responses=True is NOT optional — see D-B5 and MB-5: a
        client returning bytes makes a `get()` result compare unequal to
        a str agent id and silently no-ops a lock release.

        Raises:
            BackendUnavailableError: the store could not be reached.

        """
        del options  # fakeredis_unix takes no backend_options today
        try:
            client = redis.Redis(
                unix_socket_path=str(paths.socket_path),
                decode_responses=True,
                socket_connect_timeout=connect_timeout_s,
                socket_timeout=connect_timeout_s,
            )
            # redis-py's own stubs are wider than StoreClient (they type
            # every command generically over bytes | str | memoryview,
            # since decode_responses is a runtime flag their stubs don't
            # encode). decode_responses=True makes every return genuinely
            # str at runtime; the cast tells mypy what the stubs can't.
            return cast("StoreClient", client)
        except (redis.RedisError, OSError) as exc:
            msg = f"fakeredis_unix: could not connect to {paths.socket_path}: {exc}"
            raise BackendUnavailableError(msg) from exc
