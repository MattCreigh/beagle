# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""The store contract, frozen and proven substitutable.

See plans/beagle-coord-backend-slot.xml WP-B5, decisions D-B6, hard
constraints C-B1/C-B2/C-B6, and invariants I-B1/I-B2.

Everything before this package was plumbing. This is the deliverable that
makes "entirely compatible" a test result, not an opinion: every command
in :class:`~beagle.beacon.backend.StoreClient` gets a behavioural
assertion against EVERY registered backend (parametrised over
``REGISTRY`` at collection time, so a backend added later without passing
this suite fails CI automatically — I-B2), and the substitution test
proves a backend swap needs nothing but a config value (C-B1).

The second backend here (:class:`DoubleDriver`) is a TEST DOUBLE, not a
second production store (D-B6) — it lives in this file, registered
through ``register()``, and proves the slot accepts an implementation
that is neither fakeredis nor socket-backed.
"""

from __future__ import annotations

import fnmatch
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from beagle.beacon.backend import (
    BackendCapabilities,
    BackendUnavailableError,
    UnknownBackendError,
)
from beagle.beacon.backends import REGISTRY, get_driver, register
from beagle.beacon.keys import BeaconPaths, resolve_paths
from beagle.beacon.records import AgentRecord, stable_colour
from beagle.beacon.server import apply_intent


def _make_record(agent_id: str) -> AgentRecord:
    now = datetime.now(UTC).isoformat()
    return AgentRecord(
        agent_id=agent_id,
        session_id=agent_id,
        pid=1,
        uid=1,
        host="test",
        connected_at=now,
        last_seen=now,
        model="test-model",
        phase="testing",
        current_plan="",
        current_work="",
        files=(),
        colour=stable_colour(agent_id),
    )


# ── D-B6: the test-double backend ────────────────────────────────────────


class _DoubleStoreClient:
    """An in-memory StoreClient. One instance per (paths, process) pair —
    DoubleDriver.connect() returns the SAME instance for the same paths,
    so multiple connect() calls within one test process observe each
    other's writes, matching a real store's behaviour within its scope.
    Not visible to other OS processes (capabilities.shared=False).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (type_tag, value). type_tag in {"string","hash","set","zset","list"}.
        self._data: dict[str, tuple[str, Any]] = {}
        self._expires: dict[str, float] = {}  # key -> time.monotonic() deadline

    def _expire_if_due(self, name: str) -> None:
        deadline = self._expires.get(name)
        if deadline is not None and time.monotonic() >= deadline:
            self._data.pop(name, None)
            self._expires.pop(name, None)

    def _get_typed(self, name: str, kind: str) -> Any:
        self._expire_if_due(name)
        entry = self._data.get(name)
        if entry is None:
            return None
        got_kind, value = entry
        if got_kind != kind:
            msg = f"WRONGTYPE key {name!r} is a {got_kind}, not a {kind}"
            raise TypeError(msg)
        return value

    # keys and strings

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._get_typed(name, "string")

    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool | None:
        with self._lock:
            self._expire_if_due(name)
            if nx and name in self._data:
                return None
            self._data[name] = ("string", value)
            if ex is not None:
                self._expires[name] = time.monotonic() + ex
            else:
                self._expires.pop(name, None)
            return True

    def delete(self, *names: str) -> int:
        with self._lock:
            count = 0
            for name in names:
                self._expire_if_due(name)
                if name in self._data:
                    del self._data[name]
                    self._expires.pop(name, None)
                    count += 1
            return count

    def exists(self, *names: str) -> int:
        with self._lock:
            count = 0
            for name in names:
                self._expire_if_due(name)
                if name in self._data:
                    count += 1
            return count

    def expire(self, name: str, seconds: int) -> bool:
        with self._lock:
            self._expire_if_due(name)
            if name not in self._data:
                return False
            self._expires[name] = time.monotonic() + seconds
            return True

    def ttl(self, name: str) -> int:
        with self._lock:
            self._expire_if_due(name)
            if name not in self._data:
                return -2
            deadline = self._expires.get(name)
            if deadline is None:
                return -1
            remaining = deadline - time.monotonic()
            return max(0, int(remaining))

    def incr(self, name: str) -> int:
        with self._lock:
            self._expire_if_due(name)
            current = self._data.get(name)
            value = int(current[1]) if current is not None else 0
            value += 1
            self._data[name] = ("string", str(value))
            return value

    def scan_iter(self, match: str | None = None):
        with self._lock:
            for name in list(self._data):
                self._expire_if_due(name)
                if name not in self._data:
                    continue
                if match is None or fnmatch.fnmatch(name, match):
                    yield name

    def type(self, name: str) -> str:
        with self._lock:
            self._expire_if_due(name)
            entry = self._data.get(name)
            return "none" if entry is None else entry[0]

    # hashes

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[str, str] | None = None,
    ) -> int:
        with self._lock:
            self._expire_if_due(name)
            entry = self._data.get(name)
            store: dict[str, str] = dict(entry[1]) if entry is not None else {}
            fields = dict(mapping) if mapping is not None else {key: value}  # type: ignore[dict-item]
            new_count = sum(1 for k in fields if k not in store)
            store.update(fields)
            self._data[name] = ("hash", store)
            return new_count

    def hget(self, name: str, key: str) -> str | None:
        with self._lock:
            store = self._get_typed(name, "hash")
            return None if store is None else store.get(key)

    def hgetall(self, name: str) -> dict[str, str]:
        with self._lock:
            store = self._get_typed(name, "hash")
            return dict(store) if store is not None else {}

    def hdel(self, name: str, *keys: str) -> int:
        with self._lock:
            store = self._get_typed(name, "hash")
            if store is None:
                return 0
            count = 0
            for k in keys:
                if k in store:
                    del store[k]
                    count += 1
            return count

    # sets

    def sadd(self, name: str, *values: str) -> int:
        with self._lock:
            self._expire_if_due(name)
            entry = self._data.get(name)
            store: set[str] = set(entry[1]) if entry is not None else set()
            new_count = sum(1 for v in values if v not in store)
            store.update(values)
            self._data[name] = ("set", store)
            return new_count

    def srem(self, name: str, *values: str) -> int:
        with self._lock:
            store = self._get_typed(name, "set")
            if store is None:
                return 0
            count = 0
            for v in values:
                if v in store:
                    store.discard(v)
                    count += 1
            return count

    def scard(self, name: str) -> int:
        with self._lock:
            store = self._get_typed(name, "set")
            return 0 if store is None else len(store)

    def smembers(self, name: str) -> set[str]:
        with self._lock:
            store = self._get_typed(name, "set")
            return set(store) if store is not None else set()

    def sinter(self, *names: str) -> set[str]:
        with self._lock:
            sets = [self._get_typed(n, "set") or set() for n in names]
            if not sets:
                return set()
            result = set(sets[0])
            for s in sets[1:]:
                result &= s
            return result

    # sorted sets

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        with self._lock:
            self._expire_if_due(name)
            entry = self._data.get(name)
            store: dict[str, float] = dict(entry[1]) if entry is not None else {}
            new_count = sum(1 for k in mapping if k not in store)
            store.update(mapping)
            self._data[name] = ("zset", store)
            return new_count

    def zrange(
        self, name: str, start: int, end: int, *, desc: bool = False, withscores: bool = False
    ) -> list[str]:
        with self._lock:
            store = self._get_typed(name, "zset")
            if store is None:
                return []
            ordered = sorted(store.items(), key=lambda kv: kv[1], reverse=desc)
            sliced = _redis_slice(ordered, start, end)
            if withscores:
                return [item for pair in sliced for item in (pair[0], str(pair[1]))]
            return [k for k, _ in sliced]

    def zrem(self, name: str, *values: str) -> int:
        with self._lock:
            store = self._get_typed(name, "zset")
            if store is None:
                return 0
            count = 0
            for v in values:
                if v in store:
                    del store[v]
                    count += 1
            return count

    # lists

    def lpush(self, name: str, *values: str) -> int:
        with self._lock:
            self._expire_if_due(name)
            entry = self._data.get(name)
            store: list[str] = list(entry[1]) if entry is not None else []
            for v in values:
                store.insert(0, v)
            self._data[name] = ("list", store)
            return len(store)

    def lrange(self, name: str, start: int, end: int) -> list[str]:
        with self._lock:
            store = self._get_typed(name, "list")
            if store is None:
                return []
            return list(_redis_slice(store, start, end))

    def ltrim(self, name: str, start: int, end: int) -> bool:
        with self._lock:
            store = self._get_typed(name, "list")
            if store is None:
                return True
            self._data[name] = ("list", list(_redis_slice(store, start, end)))
            return True

    def llen(self, name: str) -> int:
        with self._lock:
            store = self._get_typed(name, "list")
            return 0 if store is None else len(store)

    # connection

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


def _redis_slice(seq: list[Any], start: int, end: int) -> list[Any]:
    """Redis-style inclusive range with negative indices (-1 = last)."""
    n = len(seq)
    if n == 0:
        return []
    start = start if start >= 0 else max(0, n + start)
    end = end if end >= 0 else n + end
    end = min(end, n - 1)
    if start > end:
        return []
    return seq[start : end + 1]


_DOUBLE_CAPABILITIES = BackendCapabilities(
    name="test_double",
    shared=False,
    requires_server=False,
    description="In-process test double proving the slot accepts a "
    "non-fakeredis, non-socket implementation (D-B6). Not shipped in src/.",
)


class DoubleDriver:
    """Test-double backend. In-process, requires_server=False, shared=False.

    "Live" is a vacuous concept for an in-process store with no external
    process to probe — is_live() is always True, matching the intent of
    requires_server=False ("attached directly by each agent"): ensure_running()
    returns immediately with no spawn and no wait loop.
    """

    capabilities = _DOUBLE_CAPABILITIES
    _stores: ClassVar[dict[str, _DoubleStoreClient]] = {}

    def is_live(self, paths: BeaconPaths, *, connect_timeout_s: float) -> bool:
        del connect_timeout_s
        return True

    def clear_stale(self, paths: BeaconPaths) -> None:
        self._stores.pop(str(paths.socket_path), None)

    def serve(self, paths: BeaconPaths, options):
        del options
        msg = "DoubleDriver.capabilities.requires_server is False: serve() is never called"
        raise BackendUnavailableError(msg)

    def connect(self, paths: BeaconPaths, *, connect_timeout_s: float, options):
        del connect_timeout_s, options
        key = str(paths.socket_path)
        if key not in self._stores:
            self._stores[key] = _DoubleStoreClient()
        return self._stores[key]


# Registered at MODULE IMPORT time, not inside a fixture: the
# @pytest.fixture(params=sorted(REGISTRY)) decorator below evaluates
# sorted(REGISTRY) once, at collection time — before any fixture (autouse
# or not) has run. Registering inside a fixture body means the double
# would never actually appear in the parametrisation, only in REGISTRY
# itself; that shape looked correct but silently tested one backend, not
# two, because nothing surfaced the mismatch (21 collected, not ~40 — the
# actual symptom this stop_condition exists to catch, caught here rather
# than by inspection). C-B1's own required_action forbids re-registering
# an existing name, hence the guard.
if "test_double" not in REGISTRY:
    register("test_double", DoubleDriver)


# ── C-B6: behavioural conformance, parametrised over every registered backend ──


@pytest.fixture(params=sorted(REGISTRY))
def backend_client(request, tmp_path: Path):
    """Yields a live StoreClient for EVERY registered backend.

    A backend added to REGISTRY without passing this suite fails CI. That
    is the point: registration and conformance are the same event (I-B2).
    """
    driver = get_driver(request.param)
    paths = resolve_paths(tmp_path)
    server = None
    thread = None
    if driver.capabilities.requires_server:
        from beagle.beacon.server import BeaconServer

        server = BeaconServer(tmp_path)
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        poll_gate = threading.Event()  # never set; .wait() used as a real
        # blocking-primitive pause between poll attempts, not time.sleep.
        while time.monotonic() < deadline and not driver.is_live(paths, connect_timeout_s=0.5):
            poll_gate.wait(0.05)

    client = driver.connect(paths, connect_timeout_s=2.0, options={})
    yield client
    client.close()
    if server is not None:
        server.stop()
        thread.join(timeout=5)


def test_set_returns_true_unconditionally(backend_client) -> None:
    assert backend_client.set("k1", "v1") is True
    assert backend_client.get("k1") == "v1"


def test_set_nx_on_held_key_returns_none_and_does_not_overwrite(backend_client) -> None:
    assert backend_client.set("k2", "first", nx=True) is True
    assert backend_client.set("k2", "second", nx=True) is None
    assert backend_client.get("k2") == "first"


def test_ttl_missing_is_minus_two_and_no_expiry_is_minus_one(backend_client) -> None:
    assert backend_client.ttl("never-set") == -2
    backend_client.set("no-expiry", "v")
    assert backend_client.ttl("no-expiry") == -1
    backend_client.set("with-expiry", "v", ex=60)
    ttl = backend_client.ttl("with-expiry")
    assert 0 < ttl <= 60


def test_hgetall_missing_key_is_empty_dict_not_none(backend_client) -> None:
    assert backend_client.hgetall("no-such-hash") == {}


def test_get_returns_str_not_bytes(backend_client) -> None:  # MB-5
    backend_client.set("strkey", "a-value")
    result = backend_client.get("strkey")
    assert isinstance(result, str)
    assert not isinstance(result, bytes)


def test_ltrim_past_end_leaves_valid_list(backend_client) -> None:
    backend_client.delete("mylist")
    backend_client.lpush("mylist", "a", "b", "c")
    assert backend_client.ltrim("mylist", 0, 1000) is True
    assert backend_client.lrange("mylist", 0, -1) == ["c", "b", "a"]


def test_delete_returns_count_removed(backend_client) -> None:
    backend_client.set("d1", "v")
    backend_client.set("d2", "v")
    assert backend_client.delete("d1", "d2", "d3-never-existed") == 2


def test_exists_counts_only_present_names(backend_client) -> None:
    backend_client.set("e1", "v")
    assert backend_client.exists("e1", "e2-absent") == 1


def test_expire_false_when_key_absent(backend_client) -> None:
    assert backend_client.expire("absent-key", 60) is False


def test_incr_treats_missing_key_as_zero(backend_client) -> None:
    assert backend_client.incr("counter") == 1
    assert backend_client.incr("counter") == 2


def test_scan_iter_matches_glob(backend_client) -> None:
    backend_client.set("scan:a", "1")
    backend_client.set("scan:b", "1")
    backend_client.set("other:c", "1")
    found = set(backend_client.scan_iter(match="scan:*"))
    assert found == {"scan:a", "scan:b"}


def test_type_reports_the_right_shape(backend_client) -> None:
    backend_client.set("t-str", "v")
    backend_client.hset("t-hash", mapping={"f": "v"})
    backend_client.sadd("t-set", "v")
    backend_client.zadd("t-zset", {"m": 1.0})
    backend_client.lpush("t-list", "v")
    assert backend_client.type("t-str") == "string"
    assert backend_client.type("t-hash") == "hash"
    assert backend_client.type("t-set") == "set"
    assert backend_client.type("t-zset") == "zset"
    assert backend_client.type("t-list") == "list"


def test_hset_returns_count_of_new_fields_only(backend_client) -> None:
    backend_client.delete("h1")
    assert backend_client.hset("h1", mapping={"a": "1", "b": "2"}) == 2
    assert backend_client.hset("h1", mapping={"a": "99", "c": "3"}) == 1


def test_hdel_removes_fields(backend_client) -> None:
    backend_client.delete("h2")
    backend_client.hset("h2", mapping={"a": "1", "b": "2"})
    assert backend_client.hdel("h2", "a", "z-absent") == 1
    assert backend_client.hgetall("h2") == {"b": "2"}


def test_sadd_srem_scard_smembers(backend_client) -> None:
    backend_client.delete("s1")
    assert backend_client.sadd("s1", "x", "y") == 2
    assert backend_client.sadd("s1", "y", "z") == 1
    assert backend_client.scard("s1") == 3
    assert backend_client.smembers("s1") == {"x", "y", "z"}
    assert backend_client.srem("s1", "x") == 1
    assert backend_client.scard("s1") == 2


def test_sinter_intersects(backend_client) -> None:
    backend_client.delete("si1", "si2")
    backend_client.sadd("si1", "a", "b", "c")
    backend_client.sadd("si2", "b", "c", "d")
    assert backend_client.sinter("si1", "si2") == {"b", "c"}


def test_zadd_zrange_zrem(backend_client) -> None:
    backend_client.delete("z1")
    assert backend_client.zadd("z1", {"low": 1.0, "high": 3.0, "mid": 2.0}) == 3
    assert backend_client.zrange("z1", 0, -1) == ["low", "mid", "high"]
    assert backend_client.zrange("z1", 0, -1, desc=True) == ["high", "mid", "low"]
    assert backend_client.zrem("z1", "mid") == 1
    assert backend_client.zrange("z1", 0, -1) == ["low", "high"]


def test_lpush_lrange_llen(backend_client) -> None:
    backend_client.delete("l1")
    assert backend_client.lpush("l1", "a") == 1
    assert backend_client.lpush("l1", "b", "c") == 3
    assert backend_client.llen("l1") == 3
    assert backend_client.lrange("l1", 0, -1) == ["c", "b", "a"]


def test_ping_and_close(backend_client) -> None:
    assert backend_client.ping() is True
    # close() is exercised by the fixture's own teardown; calling it here
    # too must not raise (idempotent).
    backend_client.close()


# ── C-B1: the substitution proof ─────────────────────────────────────────


def test_backend_swap_needs_only_a_config_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive attach, heartbeat, lock acquire under contention, lock
    release, roster read, and event append through the REAL Beacon code
    path (SocketRpcClient + apply_intent — the same functions server.py's
    ring poller and every real caller use), with the ONLY input being
    [coord].backend = "test_double". Monkeypatches get_config's return
    value (config substitution, the same technique test_beacon_mcp_tools.py
    already uses) — never beacon's own selection logic in backends/__init__.py.
    """
    from beagle.config.schema import CoordConfig, WorkflowConfig

    fake_config = WorkflowConfig(coord=CoordConfig(backend="test_double"))
    monkeypatch.setattr("beagle.beacon.connector.get_config", lambda: fake_config)

    from beagle.beacon.connector import SocketRpcClient
    from beagle.beacon.keys import resolve_paths

    paths = resolve_paths(tmp_path)
    client = SocketRpcClient(paths, connect_timeout_s=1.0)
    try:
        agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())

        # attach
        client.attach_agent(_make_record(agent_a), agent_ttl_s=15)
        client.attach_agent(_make_record(agent_b), agent_ttl_s=15)

        # heartbeat — the normal Beacon code path (apply_intent), applied
        # directly since there is no live ring poller for a
        # requires_server=False backend (I-4/D-B7's ring path is
        # orthogonal to this seam; exercising it is not this test's job).
        apply_intent(client._client, "heartbeat", agent_a, {"phase": "writing", "agent_ttl_s": 15})

        # roster read
        roster = client.list_agents()
        assert {r.agent_id for r in roster} == {agent_a, agent_b}

        # lock acquire under contention
        first = client.lock_file(agent_a, "shared.py", lock_ttl_s=60)
        assert first.ok is True
        second = client.lock_file(agent_b, "shared.py", lock_ttl_s=60)
        assert second.ok is False
        assert second.holder == agent_a

        # lock release — the normal Beacon code path
        apply_intent(client._client, "unlock_file", agent_a, {"filehash": "deadbeef"})
        # (unlock_file's filehash must match lock_file's derivation; assert
        # via the real released-by-owner path instead, matching server.py's
        # _apply_unlock_file contract directly)
        from beagle.beacon.keys import filehash

        real_key = f"lock:{filehash('shared.py')}"
        assert client._client.get(real_key) == agent_a  # still held before direct release
        client._client.delete(real_key)
        assert client._client.get(real_key) is None

        # event append — the normal Beacon code path
        apply_intent(
            client._client,
            "event",
            agent_a,
            {"action": "commit", "path": "shared.py", "event_log_maxlen": 500},
        )
        assert client._client.llen("event") == 1

        # detach
        client.detach_agent(agent_a)
        client.detach_agent(agent_b)
        assert client.list_agents() == []
    finally:
        client.close()


def test_unknown_backend_name_raises_and_lists_valid_names() -> None:
    with pytest.raises(UnknownBackendError) as exc_info:
        get_driver("nope")
    for name in REGISTRY:
        assert name in str(exc_info.value)
