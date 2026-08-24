"""B-1 regression locks — the auto-reingest path must not stampede.

Audit v13.22.1 found that `mcp_rag_server.rag_search` fires
`RAGStalenessTracker.trigger_reingest_async()` on every call, with three
guards missing:

  1. no in-flight marker — `can_reingest()` only advanced on *success*, so
     every search during a multi-minute reindex started another one;
  2. the auto path never acquired the swap lock that the manual MCP tool
     acquires, so a manual and an automatic hot-swap could interleave
     inside the destructive `swap_staged_to_live()` step;
  3. the throttle had been cut to 5s on the (false) premise that the
     incremental path was cheap.

These tests lock all three shut, plus the task-reference leak (B-26).
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time

import pytest

from beagle.context import rag_staleness as rs


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    """A fresh tracker with an isolated sidecar file."""
    rs.reset_staleness_tracker()
    monkeypatch.setattr(rs, "_MIN_REINGEST_INTERVAL", 300, raising=False)
    t = rs.get_staleness_tracker(staleness_file=str(tmp_path / "staleness.json"))
    t.mark_stale("test")
    yield t
    rs.reset_staleness_tracker()


# ── (a) concurrent triggers ──────────────────────────────────────────────


def test_second_trigger_is_rejected_while_one_is_in_flight(tracker, monkeypatch):
    """The second `trigger_reingest_async` must return None, not a task."""
    started = threading.Event()
    release = threading.Event()

    def _slow_hotswap(target):
        started.set()
        release.wait(timeout=10)
        return {"status": "ok", "mode": "full"}

    monkeypatch.setattr(tracker, "_sync_reingest", _slow_hotswap)

    async def _run():
        first = tracker.trigger_reingest_async("/tmp/x")
        assert first is not None, "first trigger should schedule a task"
        # Let the worker thread actually enter the reingest.
        await asyncio.to_thread(started.wait, 5)
        assert tracker.reingest_in_flight is True

        second = tracker.trigger_reingest_async("/tmp/x")
        third = tracker.trigger_reingest_async("/tmp/x")
        assert second is None, "a second concurrent reingest must be refused"
        assert third is None, "a third concurrent reingest must be refused"

        release.set()
        await first
        assert tracker.reingest_in_flight is False
        return True

    assert asyncio.run(_run()) is True


def test_slot_is_released_even_when_the_reingest_raises(tracker, monkeypatch):
    """A crashing reingest must not lock the tracker out permanently."""

    def _boom(target):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tracker, "_sync_reingest", _boom)

    async def _run():
        task = tracker.trigger_reingest_async("/tmp/x")
        assert task is not None
        result = await task
        assert result["status"] == "error"
        return tracker.reingest_in_flight

    assert asyncio.run(_run()) is False


# ── (b) a failing reingest still advances the throttle ───────────────────


def test_failed_reingest_advances_the_throttle(tracker, monkeypatch):
    """B-1: throttling keys off attempts, not just successes.

    Before the fix, `can_reingest()` looked only at `last_reingested_at`,
    which `mark_fresh()` sets on success. A reingest that always failed
    therefore never advanced the clock and was retried on every search.
    """
    monkeypatch.setattr(tracker, "_sync_reingest", lambda target: {"status": "failed"})

    async def _run():
        task = tracker.trigger_reingest_async("/tmp/x")
        assert task is not None
        await task

    asyncio.run(_run())

    # Never marked fresh …
    assert tracker.get_status()["reingest_count"] == 0
    assert tracker.get_status()["last_reingested_at"] == 0
    # … but the attempt was recorded, so we are throttled.
    assert tracker.get_status()["last_attempt_at"] > 0
    assert tracker.can_reingest() is False


def test_attempt_time_is_persisted_across_tracker_reload(tracker, tmp_path):
    """The attempt clock must survive a process restart, like the rest."""
    tracker.mark_attempt()
    path = str(tracker._file)
    rs.reset_staleness_tracker()
    reloaded = rs.get_staleness_tracker(staleness_file=path)
    assert reloaded.get_status()["last_attempt_at"] > 0
    assert reloaded.can_reingest() is False


# ── (c) hotswap_ingest serializes at the entry point ─────────────────────


def test_hotswap_ingest_is_reentrancy_safe_but_thread_exclusive(monkeypatch):
    """Concurrent hotswap_ingest calls: one runs, the other is skipped."""
    from beagle.infrastructure import hotswap_ingest as hi

    inside = threading.Event()
    release = threading.Event()
    results: dict[str, dict] = {}

    def _slow_locked(target_directory, **kwargs):
        inside.set()
        release.wait(timeout=10)
        return {"status": "ok", "mode": "full"}

    monkeypatch.setattr(hi, "_hotswap_ingest_locked", _slow_locked)

    def _first():
        results["first"] = hi.hotswap_ingest("/tmp/whatever")

    def _second():
        results["second"] = hi.hotswap_ingest("/tmp/whatever")

    t1 = threading.Thread(target=_first)
    t1.start()
    assert inside.wait(5), "first call never entered the critical section"

    t2 = threading.Thread(target=_second)
    t2.start()
    t2.join(timeout=5)
    assert results["second"]["status"] == "skipped"
    assert results["second"]["reason"] == "swap_in_progress"

    release.set()
    t1.join(timeout=5)
    assert results["first"]["status"] == "ok"


def test_swap_lock_is_the_same_object_in_both_modules():
    """B-1: the auto path and the MCP tool must share one lock."""
    from beagle.infrastructure import _locks, hotswap_ingest

    assert hotswap_ingest.SWAP_LOCK is _locks.SWAP_LOCK
    # mcp_rag_server imports fastmcp; skip if unavailable in this env.
    mcp = pytest.importorskip(
        "beagle.infrastructure.mcp_rag_server",
        reason="fastmcp not installed",
    )
    assert mcp._swap_lock is _locks.SWAP_LOCK


def test_swap_lock_is_reentrant_on_the_same_thread():
    """The MCP tool holds the lock and then calls hotswap_ingest()."""
    from beagle.infrastructure._locks import SWAP_LOCK

    assert SWAP_LOCK.acquire(blocking=False)
    try:
        assert SWAP_LOCK.acquire(blocking=False), "RLock must allow re-entry"
        SWAP_LOCK.release()
    finally:
        SWAP_LOCK.release()


# ── (d) the background task is not garbage-collectable ──────────────────


def test_background_task_survives_gc(tracker, monkeypatch):
    """B-26: asyncio keeps only a weak reference to a running task."""
    release = threading.Event()

    def _slow(target):
        release.wait(timeout=10)
        return {"status": "ok"}

    monkeypatch.setattr(tracker, "_sync_reingest", _slow)

    async def _run():
        task = tracker.trigger_reingest_async("/tmp/x")
        assert task is not None
        assert task in rs._BACKGROUND_TASKS
        del task
        gc.collect()
        assert len(rs._BACKGROUND_TASKS) == 1, "task was dropped after gc"
        (retained,) = tuple(rs._BACKGROUND_TASKS)
        assert not retained.done()
        release.set()
        await retained
        # Done-callback clears the registry so it cannot grow unbounded.
        await asyncio.sleep(0)
        assert retained not in rs._BACKGROUND_TASKS

    asyncio.run(_run())


# ── throttle default + env parsing ──────────────────────────────────────


def test_throttle_default_is_restored_to_300s():
    """B-1/B-5: 5s was justified by an incremental path that never ran."""
    import importlib

    mod = importlib.reload(rs)
    try:
        assert mod._MIN_REINGEST_INTERVAL == 300
    finally:
        importlib.reload(rs)


def test_garbage_env_int_does_not_break_import(monkeypatch, caplog):
    """B-31: a typo'd env var must not become an ImportError."""
    monkeypatch.setenv("BEAGLE_MIN_REINGEST_INTERVAL_SECONDS", "not-a-number")
    import importlib

    mod = importlib.reload(rs)
    try:
        assert mod._MIN_REINGEST_INTERVAL == 300
    finally:
        monkeypatch.delenv("BEAGLE_MIN_REINGEST_INTERVAL_SECONDS", raising=False)
        importlib.reload(rs)


# ── never-ingested is stale (B-24) ───────────────────────────────────────


def test_never_ingested_reports_stale(tmp_path):
    """B-24: the docstring always claimed this; the code did not."""
    rs.reset_staleness_tracker()
    t = rs.get_staleness_tracker(staleness_file=str(tmp_path / "fresh.json"))
    try:
        assert t.get_status()["last_reingested_at"] == 0
        assert t.is_stale is True
    finally:
        rs.reset_staleness_tracker()


def test_fresh_ingest_clears_staleness(tmp_path):
    rs.reset_staleness_tracker()
    t = rs.get_staleness_tracker(staleness_file=str(tmp_path / "fresh2.json"))
    try:
        t.mark_fresh(codebase_path="/tmp/x")
        assert t.is_stale is False
        assert t.get_status()["last_reingested_at"] == pytest.approx(time.time(), abs=5)
    finally:
        rs.reset_staleness_tracker()
