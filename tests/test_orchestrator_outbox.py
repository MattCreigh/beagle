"""Tests for the D-01 fix: the orchestrator's outbox call.

The defect was that ``run()`` called ``await self._get_outbox()`` against a
method that does not exist on the instance — ``_get_outbox`` is a module-level
loader, so every workflow aborted at its first node with
``AttributeError: 'DAGOrchestrator' object has no attribute '_get_outbox'``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import beagle.core.autonomous_orchestrator as ao
from beagle.core.autonomous_orchestrator import DAGOrchestrator, _get_outbox

__all__ = ("_NoopBus",)


class _NoopBus:
    def publish(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_outbox_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Each test starts with a fresh outbox cache, then restores the real one."""
    monkeypatch.setattr(ao, "_outbox_client", None)
    yield
    # restore by deleting the attribute entirely so the module re-imports fresh
    # on the next test (matches what a real process start does).
    if "_outbox_client" in ao.__dict__:
        monkeypatch.delattr(ao, "_outbox_client", raising=False)


def test_get_outbox_returns_none_when_loader_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-01 sibling-guard: a False cache means "unavailable", not a sentinel."""
    monkeypatch.setattr(ao, "_outbox_client", False)
    assert _get_outbox() is None


def test_get_outbox_returns_none_on_importerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulated ImportError degrades to None instead of raising.

    ``__import__`` is called with up to five positional args by the import
    machinery, so the replacement must accept the full signature rather than
    being narrowed to ``(name)``.
    """
    real_import = __import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> Any:
        if name == "beagle.fault_recovery.outbox":
            raise ImportError("simulated: outbox unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    assert _get_outbox() is None
    # And the second call must still return None — the cache held False, so a
    # previous version of this loader returned False here.
    assert _get_outbox() is None


def test_dag_orchestrator_has_no_instance_outbox_attribute() -> None:
    """The original defect: the method was never on the class.

    This is the negative assertion that pins the bug — the fix must NOT add
    ``_get_outbox`` as an instance method, only stop awaiting it.
    """
    assert not hasattr(DAGOrchestrator, "_get_outbox")


def test_run_calls_module_get_outbox_not_instance_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call site must call the module function, never ``await self._get_outbox``.

    Two independent proofs, because doctrine forbids accepting either alone:

    1. Textual — ``inspect.getsource`` on the call site must name the module
       call and must not name the instance call. A refactor that renames the
       module function would break this, which is the point: the assertion
       names the invariant directly rather than trusting the fix to stick.
    2. Runtime — a spy on the module loader is invoked; if the code still did
       ``await self._get_outbox()`` the spy is bypassed and the call raises
       ``AttributeError`` before any node runs.
    """
    import asyncio
    import inspect

    source = inspect.getsource(ao.DAGOrchestrator._run_inner)
    assert "_get_outbox()" in source, "module-level outbox call missing"
    assert "self._get_outbox" not in source, (
        "run() still awaits an instance attribute that does not exist"
    )

    sentinel: Any = object()
    calls: list[Any] = []

    def _spy() -> Any:
        calls.append(sentinel)
        return None

    monkeypatch.setattr(ao, "_get_outbox", _spy)
    orchestrator = DAGOrchestrator(workflow_name="noop")
    monkeypatch.setattr(
        orchestrator,
        "state",
        type("S", (), {"completed_nodes": [], "query": "x"})(),
    )
    monkeypatch.setattr(orchestrator, "transitions", {})
    monkeypatch.setattr(orchestrator, "workflow_id", "test")
    monkeypatch.setattr(orchestrator, "start_node", "n")
    monkeypatch.setattr(orchestrator, "nodes", {})

    class _FakeNode:
        name = "n"

        async def execute(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("stop iteration")

    monkeypatch.setitem(orchestrator.nodes, "n", _FakeNode())

    async def _noop(*_a: object, **_k: object) -> Any:
        return None

    monkeypatch.setattr(orchestrator, "_run_startup", _noop)
    monkeypatch.setattr(orchestrator, "_diffadapt_routing", _noop)
    monkeypatch.setattr(ao, "_get_ctx_monitor", lambda: None)
    monkeypatch.setattr(ao, "_get_steering_injection", lambda: None)
    monkeypatch.setattr(ao, "get_event_bus", lambda: _NoopBus())

    class _FakeSteering:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def check(self) -> Any:
            return type("D", (), {"has_guidance": False})()

    monkeypatch.setattr(ao, "SteeringManager", _FakeSteering)
    # _run_inner reaches the outbox call before the first node.execute, so a
    # single iteration is enough to prove the call site was reached without
    # awaiting an instance attribute.
    with pytest.raises(RuntimeError, match="stop iteration"):
        asyncio.run(orchestrator._run_inner())
    assert calls == [sentinel], "module _get_outbox was never called"
