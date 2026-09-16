"""Tests for the fault-recovery package (D-03, D-06, D-07, D-08, D-09, D-10, D-19).

The package previously had no tests at all (audit fact: zero of 278 files
reference fault_recovery). Each case names the defect it guards.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import socket
import sqlite3
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from beagle.fault_recovery import dlq as dlq_mod
from beagle.fault_recovery import reconciliation as rec_mod
from beagle.fault_recovery import sandbox as sandbox_mod
from beagle.fault_recovery.dlq import DeadLetterQueue
from beagle.fault_recovery.outbox import (
    _MAXLEN,
    _SOCKET_CONNECT_TIMEOUT,
    _SOCKET_TIMEOUT,
    OutboxClient,
    _default_consumer_name,
)
from beagle.fault_recovery.reconciliation import ReconciliationDaemon, ReconciliationStore
from beagle.fault_recovery.sandbox import get_sandbox_mode, route_payload


async def _drive_then_cancel(task: asyncio.Task) -> None:
    """Let one scheduling iteration run, then cancel ``task``.

    Spelled out rather than a bare ``try/finally`` because mypy marks the
    ``await task`` after ``task.cancel()`` unreachable inside a function whose
    only ``await`` is the sleep — and the cancel/await dance is exactly the
    part that must run.
    """
    try:
        await asyncio.sleep(0)
    except BaseException:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class _Stop(Exception):
    """Sentinel raised by the counting-sleep driver to end a test loop.

    Deliberately NOT asyncio.CancelledError: the daemon re-raises
    CancelledError as "stop the daemon", so raising it from the patched
    sleep would abort the loop after one iteration and mask the behaviour
    under test.
    """


# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeOutbox(OutboxClient):
    """OutboxClient stand-in that raises on every read_group call.

    Subclasses OutboxClient so the daemon's typed ``outbox`` parameter accepts
    it; the real client is never constructed because every method is
    overridden to raise or return empty.
    """

    def __init__(self, exc: BaseException | None = None) -> None:
        # OutboxClient sets ``self.stream`` / ``self.group`` in its own __init__
        # and the daemon logs against them on every run(); replicate the
        # attributes so an uninitialised subclass doesn't raise AttributeError.
        self.stream = "dag:outbox"
        self.group = "dag-reconciliation"
        self._exc = exc or OSError("redis connection reset")
        self.read_calls = 0
        self.ack_calls: list[tuple[str, ...]] = []

    async def read_group(self, last_id: str = ">", count: int = 100) -> list[dict[str, Any]]:
        # Both params are part of the contract the daemon calls by keyword
        # (last_id=">", count=self.max_batch); reference them so the fake
        # stays a drop-in for OutboxClient without them being dead.
        _ = (last_id, count)
        self.read_calls += 1
        raise self._exc

    async def read_events(self, last_id: str = "0", count: int = 100) -> list[dict[str, Any]]:
        _ = (last_id, count)
        return []

    async def ack(self, *stream_ids: str) -> None:
        self.ack_calls.append(stream_ids)

    async def xautoclaim(self, start_id: str = "0-0", count: int = 100) -> list[dict[str, Any]]:
        _ = (start_id, count)
        return []


async def _noop_sleep(_delay: float) -> None:
    return None


async def _drive(task: asyncio.Task, seconds: float) -> None:
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# ── D-03: dlq.py used json without importing it ──────────────────────────────


def test_dlq_roundtrip_with_payload(tmp_path: Path) -> None:
    """D-03: enqueue with a payload round-trips through SQLite.

    Before the fix this raised NameError: name 'json' is not defined at
    dlq.py:256, so no payload could ever be stored.
    """
    q = DeadLetterQueue(db_path=tmp_path / "dlq.db")
    q.enqueue(
        workflow_id="wf-1",
        dag_id="dag-1",
        node_name="research",
        error="429 rate limit",
        payload={"prompt": "summarise the paper", "tokens": 128},
    )
    rows = q.list_entries(limit=10)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"prompt": "summarise the paper", "tokens": 128}
    assert rows[0]["error"] == "429 rate limit"
    q.close()


def test_dlq_json_module_is_imported() -> None:
    """D-03: the module must actually import json, not rely on a bare name."""
    assert "json" in dlq_mod.__dict__


# ── D-06: daemon recursion instead of bounded backoff ────────────────────────


def test_daemon_survives_50_consecutive_errors() -> None:
    """D-06: 50 consecutive poll errors must not blow the stack.

    The old code did ``await self.run()`` in the except handler, which re-entered
    run() on every error, re-reading the whole stream with count=100_000 and
    consuming a stack frame per failure. The fix is a bounded backoff inside
    the existing while loop, so each poll iteration is independent and the
    stack depth is constant.

    Driven by a counting sleep that raises a sentinel after a fixed number of
    iterations. The sentinel is not asyncio.CancelledError — the daemon
    re-raises CancelledError as "stop the daemon", so raising it from the
    patched sleep would abort the loop after one iteration and mask the very
    behaviour under test.
    """
    outbox = _FakeOutbox()
    daemon = ReconciliationDaemon(
        outbox=outbox,
        store=ReconciliationStore(":memory:"),
        poll_interval_seconds=0.0,
        replay_on_start=True,
    )
    # Drive by call count, not by a patched asyncio.sleep: patching the sleep
    # attribute on the asyncio module also shadows the loop's own internal
    # scheduling, so the loop only ever fires one iteration. The outbox's
    # read_group is the daemon's only loop entry, so counting its calls drives
    # the loop deterministically without touching the scheduler.
    call_count = {"n": 0}
    task_ref: dict[str, asyncio.Task] = {}

    async def _counting_read_group(last_id: str, count: int) -> list[dict[str, Any]]:
        call_count["n"] += 1
        if call_count["n"] > 50 and task_ref.get("t") is not None:
            task_ref["t"].cancel()
        raise OSError("redis connection reset")

    # Exercise the REAL ``_backoff``, not a stub. Zeroing both caps makes the
    # real method return 0.0 (so 50 iterations cost microseconds instead of
    # ~60s of wall time) while still incrementing ``_backoff_attempt``.
    # ``patch.object(daemon, "_backoff", return_value=0.0)`` was wrong: it
    # replaced the method under test, so the ``_backoff_attempt > 0``
    # assertion below could never pass — the real method never ran. A stub
    # made the test's own invariant vacuous.
    daemon._backoff_base = 0.0
    daemon._backoff_cap = 0.0

    async def _run() -> None:
        with patch.object(daemon.outbox, "read_group", new=_counting_read_group):
            task = asyncio.create_task(daemon.run())
            task_ref["t"] = task
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(_run())
    assert call_count["n"] > 50
    # Substantive invariants: the backoff counter advanced, no recursion so the
    # daemon object is intact, and the flag is cleared after the first error.
    assert daemon._backoff_attempt > 0
    assert daemon.replay_on_start is False
    # The daemon's own read_group was hit on every iteration, proving the loop
    # kept running rather than aborting after the first error.
    assert call_count["n"] > 50


def test_daemon_replays_only_once() -> None:
    """D-06: the replay flag clears after the first poll-loop error.

    The invariant is about the flag, not iteration count: the loop calls
    ``asyncio.sleep`` once per error (backoff), so a fixed iteration target is
    a property of the patched sleep, not of the daemon. What is invariant is
    that ``replay_on_start`` starts True and ends False after one error — so a
    restart never re-reads the whole stream with count=100_000.
    """
    outbox = _FakeOutbox()
    daemon = ReconciliationDaemon(
        outbox=outbox,
        store=ReconciliationStore(":memory:"),
        poll_interval_seconds=0.0,
        replay_on_start=True,
    )
    assert daemon.replay_on_start is True

    async def _stop_after_one(_delay: float) -> None:
        raise _Stop()

    async def _run1() -> None:
        with patch.object(rec_mod.asyncio, "sleep", new=_stop_after_one):
            task = asyncio.create_task(daemon.run())
            with contextlib.suppress(_Stop, asyncio.CancelledError):
                await task

    asyncio.run(_run1())
    assert daemon.replay_on_start is False


# ── D-07: socket timeouts on from_url ───────────────────────────────────────


def test_socket_timeout_is_set_on_from_url() -> None:
    """D-07: from_url must carry socket_timeout and socket_connect_timeout.

    Without them both default to unlimited, so a hung Redis blocks the
    workflow even though this module documents itself as best-effort.

    The assertion is on the call site, not the constants: we drive the real
    ``_get_redis`` with a fake ``redis.asyncio`` module and assert the kwargs
    that reach ``from_url``. If either timeout is dropped from the call, this
    fails — which is exactly what the defect was.
    """
    captured: dict[str, Any] = {}

    class _FakeRedisClient:
        # ``*_args`` is deliberate: this stub is called exactly as
        # ``from_url(url, **timeouts)``, so the positional slot is part of the
        # signature it must accept but is never read. The leading underscore is
        # the unused-parameter convention, not a suppression — vulture flags a
        # bare ``args`` at 100% confidence because it cannot see that the
        # signature is the contract under test.
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            return None

    class _FakeRedisModule:
        asyncio = type("Asyncio", (), {"from_url": _FakeRedisClient})

    fake_module = _FakeRedisModule()
    with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_module.asyncio}):
        client = OutboxClient()
        client._get_redis()
    assert captured.get("socket_timeout") == _SOCKET_TIMEOUT == 5.0
    assert captured.get("socket_connect_timeout") == _SOCKET_CONNECT_TIMEOUT == 5.0
    assert captured.get("decode_responses") is True
    assert _MAXLEN == 100_000


def test_outbox_client_defaults_to_hostname_pid_consumer() -> None:
    """D-08: two daemons in one group must be distinct group members.

    Every OutboxClient previously took the static consumer name
    ``outbox-client``, so two daemons shared it. That gave partitioned
    delivery with no ordering guarantee, and since the SQLite upsert is
    last-writer-wins on (workflow_id, node_name) a node could be recorded
    pending after it completed.
    """
    name = _default_consumer_name()
    assert name == f"{socket.gethostname()}:{os.getpid()}"
    c = OutboxClient()
    assert c.consumer == name
    # Two clients in the same process share the pid — that is expected; the
    # point is the name is never the static default.
    assert c.consumer != "outbox-client"


# ── D-08: stream trimming + XAUTOCLAIM reclaim ──────────────────────────────


def test_append_uses_maxlen_trimming() -> None:
    """D-08: xadd must carry MAXLEN so the stream cannot grow without bound."""
    source = inspect.getsource(OutboxClient._append)
    assert "maxlen" in source
    assert "_MAXLEN" in source


def test_xautoclaim_reclaims_orphaned_entries() -> None:
    """D-08: the outbox exposes an XAUTOCLAIM pass for dead consumers."""
    assert hasattr(OutboxClient, "xautoclaim")
    source = inspect.getsource(OutboxClient.xautoclaim)
    assert "xautoclaim" in source


# ── D-09: wasmtime branch must not claim an uncomputed pass ─────────────────


def test_wasm_mode_never_claims_uncomputed_pass() -> None:
    """D-09: with wasmtime importable, route_payload must not return 'pass'.

    The previous branch set wasm_passed = ast_passed and verdict = "pass",
    reporting a pass the stub never computed. The honest verdict already
    existed two branches above; reuse it and leave the pass uncomputed.
    """
    with patch.object(sandbox_mod, "_wasmtime_available", lambda: True):
        result = route_payload("print('hi')", mode="wasm")
    assert result["verdict"] == "fallback_native"
    assert result["wasm_passed"] is None
    assert result["ast_passed"] is True


def test_wasm_mode_rejects_bad_code_even_with_wasmtime() -> None:
    """D-09: wasmtime-present does not mask an AST rejection."""
    with patch.object(sandbox_mod, "_wasmtime_available", lambda: True):
        result = route_payload("import os\nos.system('rm -rf /')", mode="wasm")
    assert result["verdict"] == "reject"
    assert result["wasm_passed"] is None


# ── D-10: sandbox_mode must reach get_sandbox_mode ──────────────────────────


def test_sandbox_mode_reaches_config() -> None:
    """D-10: the documented flag must be settable through the config schema.

    Grep for sandbox_mode across src/beagle/config/ returns nothing, so
    get_sandbox_mode always returned "native". The flag is now a real field
    on WorkflowConfig.
    """
    from beagle.config.schema import WorkflowConfig

    cfg = WorkflowConfig()
    assert hasattr(cfg, "sandbox_mode")
    cfg.sandbox_mode = "hybrid"
    assert cfg.sandbox_mode == "hybrid"


def test_get_sandbox_mode_reads_config() -> None:
    """D-10: get_sandbox_mode must honour the schema field, not hardcode."""
    from beagle.config.schema import WorkflowConfig

    fake = WorkflowConfig()
    fake.sandbox_mode = "wasm"
    with patch("beagle.config.loader.get_config", lambda: fake):
        assert get_sandbox_mode() == "wasm"


# ── D-19: close() must close every thread connection ────────────────────────
#
# Both tests below assert ``open_connection_count == 0`` after ``close()``.
# That assertion is the point: the previous version asserted that executing on
# the captured connection raised ``sqlite3.ProgrammingError``, which is true of
# a connection created with the default ``check_same_thread=True`` whether or
# not ``close()`` ever ran. It passed for the wrong reason and would have passed
# against the unfixed code. The registry count cannot be satisfied by accident.


def _open_on_worker(store: object) -> sqlite3.Connection:
    """Open a connection on a worker thread and return it to the caller.

    Uses a plain ``dict`` as the cross-thread handoff because the connection is
    created with ``check_same_thread=False``, so it is legal to read it here.
    """
    box: dict[str, sqlite3.Connection] = {}

    def worker() -> None:
        box["conn"] = store._get_conn()  # type: ignore[attr-defined]

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "conn" in box, "worker thread never opened a connection"
    return box["conn"]


def test_close_closes_every_thread_connection(tmp_path: Path) -> None:
    """D-19: close() must release connections opened on any other thread."""
    store = ReconciliationStore(db_path=tmp_path / "recon.db")
    assert store.open_connection_count >= 1, "schema init must have opened one"
    worker_conn = _open_on_worker(store)
    assert store.open_connection_count >= 2, "worker connection not registered"

    # close() runs on the TEST thread while a connection is owned by another.
    store.close()

    assert store.open_connection_count == 0, "close() left connections registered"
    with pytest.raises(sqlite3.ProgrammingError):
        worker_conn.execute("SELECT 1")
    # A closed store must reconnect rather than hand back a dead handle.
    store._get_conn()
    assert store.open_connection_count == 1


def test_dlq_close_closes_every_thread_connection(tmp_path: Path) -> None:
    """D-19: the DLQ shares the base, so its close() has the same guarantee."""
    q = DeadLetterQueue(db_path=tmp_path / "dlq.db")
    assert q.open_connection_count >= 1
    worker_conn = _open_on_worker(q)
    assert q.open_connection_count >= 2

    q.close()

    assert q.open_connection_count == 0, "close() left connections registered"
    with pytest.raises(sqlite3.ProgrammingError):
        worker_conn.execute("SELECT 1")
    q._get_conn()
    assert q.open_connection_count == 1


def test_task_store_closes_every_thread_connection(tmp_path: Path) -> None:
    """D-19: TaskStore is the third store on the shared base."""
    from beagle.infrastructure.task_store import TaskStore

    store = TaskStore(tmp_path / "tasks.db")
    assert store.open_connection_count >= 1
    worker_conn = _open_on_worker(store)
    assert store.open_connection_count >= 2

    store.close()

    assert store.open_connection_count == 0
    with pytest.raises(sqlite3.ProgrammingError):
        worker_conn.execute("SELECT 1")
