# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""The coordination store contract: what a backend must be, not how it works.

See plans/beagle-coord-backend-slot.xml WP-B1, decisions D-B1, D-B2, D-B5,
and invariant I-B1/I-B4.

This module is the seam the whole slot exists to cut. It imports nothing
from ``redis``, nothing from ``fakeredis``, and nothing from
``beagle.beacon.store`` — a backend that reaches its store through shared
memory rather than a socket is a legal implementation of everything below.

Two protocols, not one (D-B2): :class:`StoreClient` is the data plane (the
24 frozen commands every caller under ``src/beagle/beacon`` uses).
:class:`BackendDriver` is the control plane (liveness, stale-state
clearing, server construction, client construction) — the transport-
specific lifecycle concerns that MB-9 found scattered through
``spawn.py`` and ``server.py``. Keeping them apart means a test double
implementing only :class:`StoreClient` never has to fake a subprocess.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from beagle.beacon.keys import BeaconPaths


class CoordBackendError(Exception):
    """Base for every backend-selection and backend-availability failure."""


class UnknownBackendError(CoordBackendError):
    """The configured ``[coord].backend`` names no registered backend.

    The message MUST list every registered name (C-B2) — there is no
    default and no fallback to fall back to silently.
    """


class BackendUnavailableError(CoordBackendError):
    """A registered backend could not start or could not be reached."""


# <invariant id="I-B3" name="disabled is not broken">
# [coord].enabled = false is a supported mode and produces no error. A
# configured backend that is unknown or fails to start is a defect and
# always raises one of the two exceptions above. The two are never
# handled by the same branch — see the logic block in the parent plan's
# <architecture> section ("When is a missing store a defect, and when is
# it a mode?").
# </invariant>
@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend can and cannot do, declared rather than probed.

    Args:
        name: The registry key this backend is registered under.
        shared: True if store state is visible to other processes (a
            coordination store that only one process can see coordinates
            nobody).
        requires_server: True if a server process must be spawned and
            reachable before connect() succeeds. A backend attached
            directly by every agent (e.g. over shared memory with no
            listener) sets this False, and spawn.py must not launch a
            subprocess for it (see BackendDriver.serve).
        description: A short human-readable summary for error messages
            and `beagle coord status` diagnostics.

    """

    name: str
    shared: bool
    requires_server: bool
    description: str


# <invariant id="I-B4" name="the transport never leaks into the contract">
# No name in StoreClient, and no argument it takes, refers to a socket, a
# port, RESP, redis, or fakeredis. A shared-memory backend that never
# opens a file descriptor is a legal implementation.
# </invariant>
@runtime_checkable
class StoreClient(Protocol):
    """The frozen store surface: 27 commands, str in, str out (D-B5).

    Every command that returns strings returns ``str``, never ``bytes`` —
    this is part of the contract, not an implementation detail, because a
    backend returning bytes makes a `get()` result compare unequal to a
    ``str`` agent id and silently no-ops a lock release (MB-5's exact
    defect). ``decode_responses=True`` semantics are load-bearing.

    Return types on the missing/empty case are part of the contract too —
    that case is what backends get wrong:

    - ``get`` / ``hget``: ``None``, never ``b""`` and never ``KeyError``.
    - ``hgetall``: ``{}`` for a missing key, never ``KeyError``.
    - ``smembers``: empty ``set()`` for a missing key.
    - ``scard``: ``0`` for a missing key.
    - ``lrange`` / ``zrange``: ``[]`` for a missing key.
    - ``ttl``: ``-2`` for a missing key, ``-1`` for a key with no expiry.
    - ``exists``: the count of names (among those given) that exist.
    - ``incr``: ``1`` on the first call for a key that did not exist.
    - ``set(..., nx=True)`` on an already-held key: ``None``, NOT
      ``False`` — matching redis-py's own return contract, since callers
      (e.g. archive.py's ``elect_flush_owner``) test truthiness against a
      value, not against a specific sentinel.

    This module is the contract only. Every method here is unimplemented
    (``...``) — see ``backends/fakeredis_unix.py`` (WP-B3) for the first,
    and so far only, real implementation.
    """

    # keys and strings

    def get(self, name: str) -> str | None:
        """Return the string value of ``name``, or None if it is unset."""
        ...

    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool | None:
        """Set ``name`` to ``value``.

        Args:
            name: The key to write.
            value: The value to store.
            ex: Expiry in seconds, or None for no expiry.
            nx: If True, only set when ``name`` does not already exist.

        Returns:
            True on an unconditional set. With ``nx=True``: True if this
            call created the key, None if the key already existed (the
            call was a no-op) — never False.

        """
        ...

    def delete(self, *names: str) -> int:
        """Delete every given name. Returns the count actually removed."""
        ...

    def exists(self, *names: str) -> int:
        """Return how many of the given names currently exist."""
        ...

    def expire(self, name: str, seconds: int) -> bool:
        """Set a TTL on an existing key. Returns False if it does not exist."""
        ...

    def ttl(self, name: str) -> int:
        """Return the TTL of ``name`` in seconds: -2 missing, -1 no expiry."""
        ...

    def incr(self, name: str) -> int:
        """Increment ``name`` by 1, treating a missing key as 0, and return it."""
        ...

    def scan_iter(self, match: str | None = None) -> Iterator[str]:
        """Iterate every key name, optionally filtered by a glob pattern."""
        ...

    def type(self, name: str) -> str:
        """Return the redis TYPE of ``name`` — "string", "hash", "set",
        "list", "zset", or "none" for a missing key. Used by a generic
        key-space walk (archive.py's snapshot_keyspace) that must dispatch
        on shape without knowing each key's origin in advance."""
        ...

    # hashes

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[str, str] | None = None,
    ) -> int:
        """Set one field (key/value) or many (mapping) on hash ``name``.

        Returns:
            The number of NEW fields added (fields that already existed
            and were only updated are not counted).

        """
        ...

    def hget(self, name: str, key: str) -> str | None:
        """Return one field of hash ``name``, or None if unset."""
        ...

    def hgetall(self, name: str) -> dict[str, str]:
        """Return every field of hash ``name``, or {} if it does not exist."""
        ...

    def hdel(self, name: str, *keys: str) -> int:
        """Delete fields from hash ``name``. Returns the count removed."""
        ...

    # sets

    def sadd(self, name: str, *values: str) -> int:
        """Add values to set ``name``. Returns the count newly added."""
        ...

    def srem(self, name: str, *values: str) -> int:
        """Remove values from set ``name``. Returns the count removed."""
        ...

    def scard(self, name: str) -> int:
        """Return the number of members of set ``name``, or 0 if unset."""
        ...

    def smembers(self, name: str) -> builtins.set[str]:
        """Return every member of set ``name``, or an empty set if unset.

        Return type is spelled `builtins.set[str]`, not `set[str]` — this
        Protocol also defines a method named `set`, which shadows the
        builtin type name for any bare `set[...]` annotation used
        elsewhere in this class body.
        """
        ...

    def sinter(self, *names: str) -> builtins.set[str]:
        """Return the intersection of the given sets (empty if any is unset)."""
        ...

    # sorted sets

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        """Add member/score pairs to sorted set ``name``.

        Returns the count of NEW members added (existing members whose
        score changed are not counted).
        """
        ...

    def zrange(
        self, name: str, start: int, end: int, *, desc: bool = False, withscores: bool = False
    ) -> list[str]:
        """Return a score-ordered slice [start, end] (inclusive) of ``name``."""
        ...

    def zrem(self, name: str, *values: str) -> int:
        """Remove members from sorted set ``name``. Returns the count removed."""
        ...

    # lists

    def lpush(self, name: str, *values: str) -> int:
        """Push values onto the head of list ``name``. Returns the new length."""
        ...

    def lrange(self, name: str, start: int, end: int) -> list[str]:
        """Return a slice [start, end] (inclusive) of list ``name``."""
        ...

    def ltrim(self, name: str, start: int, end: int) -> bool:
        """Trim list ``name`` to the slice [start, end] (inclusive)."""
        ...

    def llen(self, name: str) -> int:
        """Return the length of list ``name``, or 0 if it does not exist."""
        ...

    # connection

    def ping(self) -> bool:
        """Return True if the store answered. Never raises for "unreachable"."""
        ...

    def close(self) -> None:
        """Release this client's own resources. Idempotent."""
        ...


class ServerHandle(Protocol):
    """A running store server, owned by the Beacon subprocess.

    Mirrors the lifecycle methods ``BeaconServer`` already exposes
    (``backends/fakeredis_unix.py``'s ``UnixFakeServer``, moved from the
    deleted ``store.py``), so a driver's ``serve()`` can return the
    existing server object unchanged.
    """

    def serve_forever(self) -> None:
        """Block, serving store requests, until shutdown() is called."""
        ...

    def shutdown(self) -> None:
        """Stop serving. Safe to call from a thread other than the one
        blocked in serve_forever() — the caller (server.py's teardown
        path) depends on this."""
        ...

    def server_close(self) -> None:
        """Release the server's own resources (e.g. close the listening
        socket's file descriptor) once shutdown() has already stopped the
        serve loop. Distinct from shutdown(): shutdown() only unblocks
        serve_forever(); this is the second, separate step BeaconServer.stop()
        takes afterward — WP-B1's original Protocol omitted it even though
        the real caller (server.py) already called it, discovered wiring
        WP-B4 against this contract."""
        ...


class BackendDriver(Protocol):
    """Constructs and probes one backend's store (D-B2's control plane).

    A driver instance is stateless with respect to any one Beacon
    instance — every method takes the :class:`~beagle.beacon.keys.BeaconPaths`
    it operates on as an argument, so one driver instance can serve many
    working directories.
    """

    capabilities: BackendCapabilities

    def is_live(self, paths: BeaconPaths, *, connect_timeout_s: float) -> bool:
        """Return True when a store for ``paths`` is reachable and answering.

        This is a liveness probe against the world as it is right now —
        it makes no promise about a moment ago or a moment from now.
        """
        ...

    def clear_stale(self, paths: BeaconPaths) -> None:
        """Remove the on-disk remains of a store that no longer answers.

        Must be idempotent (safe to call when there is nothing to clear)
        and must never remove the state of a LIVE store — callers only
        reach this after ``is_live()`` has already returned False.
        """
        ...

    def serve(self, paths: BeaconPaths, options: Mapping[str, str]) -> ServerHandle:
        """Create the store server for ``paths``. Called in the Beacon
        subprocess only — never on an attaching agent's own process.

        Raises:
            BackendUnavailableError: the store could not be created.

        """
        ...

    def connect(
        self, paths: BeaconPaths, *, connect_timeout_s: float, options: Mapping[str, str]
    ) -> StoreClient:
        """Return a client connected to the store for ``paths``.

        The returned client is never shared across threads — callers
        construct one per caller, matching the existing
        ``SocketRpcClient``/``RingPoller`` pattern.

        Raises:
            BackendUnavailableError: the store could not be reached.

        """
        ...
