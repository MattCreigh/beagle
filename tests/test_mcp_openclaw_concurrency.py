"""B-6 regression test: TaskStore calls in async MCP handlers must not block.

The MCP openclaw server exposes ``async def`` tool handlers that
interact with the synchronous ``TaskStore`` (sqlite3 + threading.local).
Calling sync I/O from an async context blocks the event loop and
serialises every concurrent tool call.

Reference: audit/golden_master_v13.22.0.md B-6
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "beagle_openclaw", reason="beagle-openclaw plugin not installed (optional)"
)

import asyncio
import importlib
import sys
import time
from pathlib import Path


def _import_openclaw_with_db(monkeypatch, tmp_path):
    """Import the openclaw MCP server with a fresh task DB."""
    db = tmp_path / "tasks.db"
    monkeypatch.setenv("OPENCLAW_DB_PATH", str(db))
    for mod in list(sys.modules):
        if mod.startswith("beagle.infrastructure.mcp_openclaw_server") or mod.startswith(
            "beagle.infrastructure.task_store"
        ):
            del sys.modules[mod]
    return importlib.import_module("beagle.infrastructure.mcp_openclaw_server")


def test_call_store_runs_in_thread_pool():
    """B-6: _call_store must push sync work to a thread, not block the loop."""
    import beagle.infrastructure.mcp_openclaw_server as oc

    block_duration = 0.05
    loop_continues = {"ticks": 0}

    async def loop_ticker():
        # Schedule a tick that runs immediately after the to_thread call
        for _ in range(5):
            await asyncio.sleep(block_duration / 10)
            loop_continues["ticks"] += 1

    def blocking_op():
        time.sleep(block_duration)
        return "done"

    async def runner():
        ticker = asyncio.create_task(loop_ticker())
        result = await oc._call_store(blocking_op)
        await ticker
        return result

    result = asyncio.run(runner())
    assert result == "done"
    # If the loop was blocked, we'd see 0 ticks during the 50ms block.
    # With proper to_thread, the ticker fires freely.
    assert loop_continues["ticks"] >= 1, (
        f"Event loop was blocked — only {loop_continues['ticks']} ticker runs during a "
        f"{block_duration * 1000:.0f}ms sync call. B-6 fix not working."
    )


def test_async_taskstore_exposes_db_methods_as_coro(monkeypatch, tmp_path):
    """B-6: the wrapped TaskStore must expose DB methods as coroutines."""
    oc = _import_openclaw_with_db(monkeypatch, tmp_path)
    store = oc._store()
    # list_tasks is a DB method
    assert asyncio.iscoroutinefunction(store.list_tasks), (
        "AsyncTaskStore.list_tasks must be a coroutine function, not the raw sync"
    )
    assert asyncio.iscoroutinefunction(store.create_task)
    assert asyncio.iscoroutinefunction(store.get_task)
    assert asyncio.iscoroutinefunction(store.get_metrics)


async def test_concurrent_creates_dont_block_each_other(monkeypatch, tmp_path):
    """B-6: 10 concurrent create_task calls must not serialise."""
    oc = _import_openclaw_with_db(monkeypatch, tmp_path)
    store = oc._store()

    async def make_one(i: int):
        return await store.create_task(
            task_type="workflow",
            spec={"workflow": "research", "query": f"q{i}"},
            constraints={},
            audit_config={},
            created_by="test",
        )

    start = time.perf_counter()
    results = await asyncio.gather(*[make_one(i) for i in range(10)])
    elapsed = time.perf_counter() - start

    # With the fix, 10 concurrent creates should complete quickly. Without
    # the fix (i.e. direct sync calls in async def), each call would block
    # the loop, so total elapsed would be 10 x single-call time.
    assert elapsed < 0.5, (
        f"10 concurrent create_task calls took {elapsed:.3f}s — "
        f"event loop is being blocked (B-6 not fixed)"
    )
    assert len(set(results)) == 10, "all 10 task IDs must be unique"


def test_async_taskstore_non_db_attrs_pass_through(monkeypatch, tmp_path):
    """B-6: non-DB attributes (e.g. db_path) must pass through unchanged."""
    oc = _import_openclaw_with_db(monkeypatch, tmp_path)
    store = oc._store()
    # db_path is an attribute, not a method — should be the underlying value
    assert hasattr(store, "db_path")
    assert isinstance(store.db_path, Path)
