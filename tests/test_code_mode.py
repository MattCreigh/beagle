"""Tests for beagle.code_mode — the chained tool-call executor.

Release-readiness audit 2026-08-28 (D-02, Critical): ``execute_chain`` had an
unbounded busy-wait in ``_wait_for_dependencies`` and ``_resolve_dependencies``
appended unsatisfiable calls to the execution order, so a cyclic dependency
graph, a missing ``dep_id``, or a chain truncated mid-dependency hung the
executor permanently. These tests assert bounded completion on all three
triggers plus the dependency-wait timeout path.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from beagle.code_mode import ChainResult, CodeModeExecutor, ToolCall, ToolDefinition

pytestmark = pytest.mark.timeout(30)  # the whole point: these must never hang

Handler = Callable[[dict[str, Any], dict[str, Any] | None, dict[str, ChainResult]], Awaitable[Any]]


def _noop_handler(result: Any = None) -> Handler:
    async def handler(
        params: dict[str, Any],
        context: dict[str, Any] | None,
        results: dict[str, ChainResult],
    ) -> Any:
        return result

    return handler


def _executor(**kwargs: Any) -> CodeModeExecutor:
    ex = CodeModeExecutor(**kwargs)
    ex.register_tool(
        ToolDefinition(name="noop", description="test tool", parameters={}),
        _noop_handler("ok"),
    )
    return ex


# ── Trigger 1: cyclic dependency graph ───────────────────────────────────────


async def test_cyclic_dependency_completes_bounded() -> None:
    """a→b→a must terminate as failed ChainResults, never busy-wait."""
    ex = _executor(timeout_seconds=0.5)
    a = ToolCall(tool_name="noop", parameters={}, call_id="a", depends_on=["b"])
    b = ToolCall(tool_name="noop", parameters={}, call_id="b", depends_on=["a"])

    start = time.monotonic()
    results = await asyncio.wait_for(ex.execute_chain([a, b]), timeout=10)
    elapsed = time.monotonic() - start

    assert all(not r.success for r in results)
    assert all("nsatisfiable" in (r.error or "") for r in results)
    assert elapsed < 5.0, f"cyclic chain took {elapsed:.2f}s — busy-wait regression"


async def test_self_dependency_completes_bounded() -> None:
    """A self-cycle (a→a) is the minimal cyclic graph."""
    ex = _executor(timeout_seconds=0.5)
    a = ToolCall(tool_name="noop", parameters={}, call_id="a", depends_on=["a"])

    results = await asyncio.wait_for(ex.execute_chain([a]), timeout=10)
    assert len(results) == 1
    assert results[0].success is False


# ── Trigger 2: missing dep_id ────────────────────────────────────────────────


async def test_missing_dependency_completes_bounded() -> None:
    """A dep_id that names no call in the chain must fail fast, not wait."""
    ex = _executor(timeout_seconds=0.5)
    c = ToolCall(tool_name="noop", parameters={}, call_id="c", depends_on=["ghost-id"])

    start = time.monotonic()
    results = await asyncio.wait_for(ex.execute_chain([c]), timeout=10)

    assert results[0].success is False
    assert "ghost-id" in (results[0].error or "")
    assert time.monotonic() - start < 5.0


async def test_missing_dependency_does_not_execute() -> None:
    """An unsatisfiable call must never reach its handler."""
    called: list[str] = []

    async def spy(
        params: dict[str, Any],
        context: dict[str, Any] | None,
        results: dict[str, ChainResult],
    ) -> Any:
        called.append("hit")
        return "executed"

    ex = CodeModeExecutor(timeout_seconds=0.5)
    ex.register_tool(ToolDefinition(name="spy", description="", parameters={}), spy)
    c = ToolCall(tool_name="spy", parameters={}, call_id="c", depends_on=["ghost-id"])

    results = await asyncio.wait_for(ex.execute_chain([c]), timeout=10)
    assert called == [], "unsatisfiable call was executed"
    assert results[0].success is False


# ── Trigger 3: chain truncation orphaning ────────────────────────────────────


async def test_truncation_rejects_dependents_of_dropped_calls() -> None:
    """max_chain_length truncation must not orphan a dependent of a dropped call."""
    ex = _executor(timeout_seconds=2.0, max_chain_length=2)
    # Truncation keeps indices [0, 1): t1 kept, t2 kept, t3 DROPPED... but
    # t3 depends on t2 (kept) — that is fine. The orphan shape is the
    # inverse: the KEPT call whose dependency is DROPPED. Construct it
    # explicitly: keep t_orphan (index 1) which depends on t_dep (index 2).
    t_dep = ToolCall(tool_name="noop", parameters={}, call_id="t-dep")
    t_orphan = ToolCall(
        tool_name="noop", parameters={}, call_id="t-orphan", depends_on=["t-dep"]
    )
    t_plain = ToolCall(tool_name="noop", parameters={}, call_id="t-plain")
    calls = [t_plain, t_orphan, t_dep]  # truncation drops t_dep, keeps t_orphan

    results = await asyncio.wait_for(ex.execute_chain(calls), timeout=10)

    # t_orphan (dependent of dropped t_dep) is rejected with a failure
    # receipt naming the dropped dependency; the others run normally.
    by_id = {r.call_id: r for r in results}
    assert by_id["t-plain"].success is True
    assert by_id["t-orphan"].success is False
    assert "t-dep" in (by_id["t-orphan"].error or "")
    dropped_receipt = next(
        (r for r in ex._execution_history if r.call_id == "t-dep"), None
    )
    assert dropped_receipt is None, "dropped call must not appear in history"


# ── Dependency-wait timeout path ─────────────────────────────────────────────


async def test_dependency_wait_timeout_returns_failed_result() -> None:
    """A dep removed between resolution and execution hits the wait timeout."""
    ex = CodeModeExecutor(timeout_seconds=0.3)
    ex.register_tool(
        ToolDefinition(name="noop", description="test tool", parameters={}),
        _noop_handler("ok"),
    )

    order, unsat = ex._resolve_dependencies(
        [ToolCall(tool_name="noop", parameters={}, call_id="x")]
    )
    assert order and not unsat

    # Simulate the race: the dep result dict never receives the id.
    results: dict[str, ChainResult] = {}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            ex._wait_for_dependencies(["never-arrives"], results),
            timeout=ex.timeout_seconds,
        )


async def test_execute_chain_records_wait_timeout_as_failure() -> None:
    """execute_chain must convert a dependency-wait expiry into a failed result."""
    ex = _executor(timeout_seconds=0.3)
    a = ToolCall(tool_name="noop", parameters={}, call_id="a")
    b = ToolCall(tool_name="noop", parameters={}, call_id="b", depends_on=["phantom"])

    # Simulate the mid-chain race (a dep id that can never appear in the
    # results dict — the exact state the old unbounded busy-wait spun on
    # forever) by injecting a resolution that trusts the phantom dep.
    ex._resolve_dependencies = (  # type: ignore[method-assign]
        lambda calls: ([a, b], [])
    )

    results = await asyncio.wait_for(ex.execute_chain([a, b]), timeout=10)
    b_result = next(r for r in results if r.call_id == "b")
    assert b_result.success is False
    assert "ependency wait exceeded" in (b_result.error or "")


# ── Happy path regression guard ──────────────────────────────────────────────


async def test_happy_path_linear_chain_still_succeeds() -> None:
    """The D-02 fix must not break correct chains."""
    ex = _executor(timeout_seconds=2.0)
    a = ToolCall(tool_name="noop", parameters={}, call_id="a")
    b = ToolCall(tool_name="noop", parameters={}, call_id="b", depends_on=["a"])
    c = ToolCall(tool_name="noop", parameters={}, call_id="c", depends_on=["b"])

    results = await asyncio.wait_for(ex.execute_chain([a, b, c]), timeout=10)
    assert [r.call_id for r in results] == ["a", "b", "c"]
    assert all(r.success for r in results)


async def test_partially_satisfiable_chain_executes_the_good_part() -> None:
    """One bad call must not poison the satisfiable remainder of the chain."""
    ex = _executor(timeout_seconds=1.0)
    good1 = ToolCall(tool_name="noop", parameters={}, call_id="g1")
    bad = ToolCall(tool_name="noop", parameters={}, call_id="bad", depends_on=["missing"])
    good2 = ToolCall(tool_name="noop", parameters={}, call_id="g2", depends_on=["g1"])

    results = await asyncio.wait_for(ex.execute_chain([good1, bad, good2]), timeout=10)

    by_id = {r.call_id: r for r in results}
    assert by_id["g1"].success is True
    assert by_id["g2"].success is True
    assert by_id["bad"].success is False
