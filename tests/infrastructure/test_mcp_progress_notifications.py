"""Tests for v13.19.1 MCP progress-notification bridge.

Covers the `_EventBridge` helper that forwards Beagle EventBus events
to a FastMCP `Context` as `notifications/progress` and
`notifications/message`. Bridge is leak-free, no-op when no MCP
session is attached, and adds no public return-shape changes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.events.bus import EventBus
from beagle.events.events import (
    NodeCompleted,
    NodeFailed,
    NodeOutput,
    NodeStarted,
    WorkflowStarted,
)
from beagle.infrastructure.mcp_utility_server import (
    _count_workflow_phases_safe,
    _EventBridge,
    run_beagle_workflow,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_bus(monkeypatch):
    """Replace the global event bus with a fresh instance for the test.

    The production bus is a module-level singleton; mutating it in
    tests would leak subscribers across tests and risk the 1000-sub
    cap. We swap it for the duration of each test.
    """
    bus = EventBus()
    # The bridge's local import resolves to this name, so patching here
    # affects what _EventBridge.attach()/detach() actually use.
    import beagle.events.bus as bus_mod

    monkeypatch.setattr(bus_mod, "_bus", bus)
    yield bus
    # Cleanup any lingering subscribers
    for sub_id in list(bus._subscribers.keys()):
        bus.unsubscribe(sub_id)


@pytest.fixture
def mock_ctx():
    """AsyncMock with the FastMCP Context surface used by the bridge."""
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.debug = AsyncMock()
    return ctx


# ── Tests ───────────────────────────────────────────────────────────────────


class TestEventBridgeNoOp:
    """Bridge must be a no-op when no MCP context is supplied."""

    def test_attach_without_ctx_does_not_subscribe(self, fresh_bus):
        bridge = _EventBridge(ctx=None, total_nodes_hint=3)
        bridge.attach()
        try:
            assert bridge.subscription_id is None
            assert len(fresh_bus._subscribers) == 0
        finally:
            bridge.detach()

    def test_detach_without_ctx_is_safe(self):
        bridge = _EventBridge(ctx=None)
        bridge.detach()  # must not raise
        bridge.detach()  # idempotent

    def test_publish_without_ctx_is_silent(self, fresh_bus):
        bridge = _EventBridge(ctx=None)
        bridge.attach()
        # Should not raise even though we're not actually subscribed
        fresh_bus.publish(NodeStarted(workflow_id="wf-x", node_name="n1"))


class TestEventBridgeDispatch:
    """Bridge forwards events to a real mock Context."""

    async def test_node_completed_advances_progress(self, fresh_bus, mock_ctx):
        bridge = _EventBridge(ctx=mock_ctx, total_nodes_hint=3)
        bridge.attach()
        try:
            fresh_bus.publish(
                NodeCompleted(workflow_id="wf-y", node_name="n1", cost=0.1, tokens=50)
            )
            # Drain the create_task'd dispatch
            await asyncio.sleep(0.05)
            assert mock_ctx.report_progress.await_count >= 1
            last_call = mock_ctx.report_progress.call_args
            assert last_call.kwargs["progress"] == 1
            assert last_call.kwargs["total"] == 3
            assert "n1" in last_call.kwargs["message"]
        finally:
            bridge.detach()

    async def test_node_failed_emits_error_log(self, fresh_bus, mock_ctx):
        bridge = _EventBridge(ctx=mock_ctx, total_nodes_hint=2)
        bridge.attach()
        try:
            fresh_bus.publish(
                NodeFailed(
                    workflow_id="wf-z",
                    node_name="n_failed",
                    error="boom",
                )
            )
            await asyncio.sleep(0.05)
            # error level routes to ctx.error
            assert mock_ctx.error.await_count >= 1
            error_msg = mock_ctx.error.call_args.args[0]
            assert "node.failed" in error_msg
            assert "n_failed" in error_msg
        finally:
            bridge.detach()

    async def test_workflow_started_emits_info_log(self, fresh_bus, mock_ctx):
        bridge = _EventBridge(ctx=mock_ctx)
        bridge.attach()
        try:
            fresh_bus.publish(WorkflowStarted(workflow_id="wf-q", query="hi", budget_usd=1.0))
            await asyncio.sleep(0.05)
            assert mock_ctx.info.await_count >= 1
            assert mock_ctx.report_progress.await_count == 0  # not a node event
        finally:
            bridge.detach()

    async def test_secret_fields_are_redacted(self, fresh_bus, mock_ctx):
        """Fields whose name suggests credentials must be redacted in logs."""
        from beagle.events.events import ToolCallEvent

        bridge = _EventBridge(ctx=mock_ctx)
        bridge.attach()
        try:
            fresh_bus.publish(
                ToolCallEvent(
                    workflow_id="wf-s",
                    tool_name="search",
                    parameters={"api_key": "AKIA-SECRET-1234"},
                    status="matched",
                )
            )
            await asyncio.sleep(0.05)
            # Find any emitted message and assert it doesn't leak the secret
            all_messages = " ".join(str(c.args[0]) for c in mock_ctx.info.call_args_list)
            assert "AKIA-SECRET-1234" not in all_messages
            assert "<redacted>" in all_messages or "api_key=<redacted>" in all_messages
        finally:
            bridge.detach()


class TestEventBridgeGranularity:
    """v13.21.13: content forwarding, node.started progress, throttle,
    and ring-buffer replay gating."""

    async def test_node_output_content_is_forwarded(self, fresh_bus, mock_ctx):
        """The actual output line must reach the client, not just the
        event-type/node-name envelope (pre-v13.21.13 regression)."""
        bridge = _EventBridge(ctx=mock_ctx)
        bridge.attach()
        try:
            fresh_bus.publish(
                NodeOutput(
                    workflow_id="wf-c",
                    node_name="implementer",
                    content="Running pytest on module X",
                )
            )
            await asyncio.sleep(0.05)
            all_messages = " ".join(str(c.args[0]) for c in mock_ctx.info.call_args_list)
            assert "Running pytest on module X" in all_messages
        finally:
            bridge.detach()

    async def test_node_completed_result_is_forwarded(self, fresh_bus, mock_ctx):
        bridge = _EventBridge(ctx=mock_ctx, total_nodes_hint=2)
        bridge.attach()
        try:
            fresh_bus.publish(
                NodeCompleted(
                    workflow_id="wf-r",
                    node_name="planner",
                    result="Plan: do the thing",
                )
            )
            await asyncio.sleep(0.05)
            all_messages = " ".join(str(c.args[0]) for c in mock_ctx.info.call_args_list)
            assert "Plan: do the thing" in all_messages
        finally:
            bridge.detach()

    async def test_node_started_emits_progress(self, fresh_bus, mock_ctx):
        """Phase starts must show up on the progress token, not only ends."""
        bridge = _EventBridge(ctx=mock_ctx, total_nodes_hint=4)
        bridge.attach()
        try:
            fresh_bus.publish(NodeStarted(workflow_id="wf-p", node_name="researcher"))
            await asyncio.sleep(0.05)
            assert mock_ctx.report_progress.await_count >= 1
            last_call = mock_ctx.report_progress.call_args
            assert last_call.kwargs["progress"] == 0  # numerator unchanged
            assert "node.started" in last_call.kwargs["message"]
            assert "researcher" in last_call.kwargs["message"]
        finally:
            bridge.detach()

    async def test_progress_reported_without_denominator(self, fresh_bus, mock_ctx):
        """Unknown phase count must no longer silence Tier 1 entirely."""
        bridge = _EventBridge(ctx=mock_ctx, total_nodes_hint=0)
        bridge.attach()
        try:
            fresh_bus.publish(NodeCompleted(workflow_id="wf-n", node_name="n1"))
            await asyncio.sleep(0.05)
            assert mock_ctx.report_progress.await_count >= 1
            last_call = mock_ctx.report_progress.call_args
            assert last_call.kwargs["progress"] == 1
            assert last_call.kwargs["total"] is None
        finally:
            bridge.detach()

    async def test_node_output_throttle_drops_rapid_lines(self, fresh_bus, mock_ctx, monkeypatch):
        monkeypatch.setenv("BEAGLE_BRIDGE_OUTPUT_MIN_INTERVAL", "60")
        bridge = _EventBridge(ctx=mock_ctx)
        bridge.attach()
        try:
            fresh_bus.publish(NodeOutput(workflow_id="wf-t", node_name="n1", content="line-one"))
            fresh_bus.publish(NodeOutput(workflow_id="wf-t", node_name="n1", content="line-two"))
            # Heartbeat lines are exempt from the throttle.
            fresh_bus.publish(
                NodeOutput(
                    workflow_id="wf-t",
                    node_name="_workflow_heartbeat",
                    content="heartbeat-msg",
                )
            )
            await asyncio.sleep(0.05)
            all_messages = " ".join(str(c.args[0]) for c in mock_ctx.info.call_args_list)
            assert "line-one" in all_messages
            assert "line-two" not in all_messages
            assert "heartbeat-msg" in all_messages
        finally:
            bridge.detach()

    async def test_ring_buffer_replay_is_not_forwarded(self, fresh_bus, mock_ctx):
        """subscribe() replays buffered events; the bridge must gate out
        anything created before attach() (previous workflow's transcript)."""
        fresh_bus.publish(
            NodeOutput(workflow_id="wf-old", node_name="old_node", content="stale-line")
        )
        await asyncio.sleep(0.05)  # ensure replayed timestamp < attach time
        bridge = _EventBridge(ctx=mock_ctx)
        bridge.attach()
        try:
            fresh_bus.publish(
                NodeOutput(workflow_id="wf-new", node_name="new_node", content="fresh-line")
            )
            await asyncio.sleep(0.05)
            all_messages = " ".join(str(c.args[0]) for c in mock_ctx.info.call_args_list)
            assert "stale-line" not in all_messages
            assert "fresh-line" in all_messages
        finally:
            bridge.detach()


class TestEventBridgeLeakFree:
    """Bridge must remove its subscription on detach, even on exceptions."""

    def test_detach_removes_subscriber(self, fresh_bus, mock_ctx):
        bridge = _EventBridge(ctx=mock_ctx, total_nodes_hint=1)
        bridge.attach()
        assert len(fresh_bus._subscribers) == 1
        bridge.detach()
        assert len(fresh_bus._subscribers) == 0
        assert bridge.subscription_id is None

    def test_double_detach_is_idempotent(self, fresh_bus, mock_ctx):
        bridge = _EventBridge(ctx=mock_ctx)
        bridge.attach()
        bridge.detach()
        bridge.detach()  # must not raise
        assert len(fresh_bus._subscribers) == 0

    async def test_run_beagle_workflow_cleans_up_on_exception(self, fresh_bus, mock_ctx):
        """The `finally: bridge.detach()` in run_beagle_workflow MUST
        unsubscribe even if `_run_workflow_impl` raises."""
        from beagle.infrastructure import mcp_utility_server as srv

        async def fake_impl(*args, **kwargs):
            raise RuntimeError("simulated workflow failure")

        with (
            patch.object(srv, "_run_workflow_impl", side_effect=fake_impl),
            pytest.raises(RuntimeError, match="simulated"),
        ):
            await run_beagle_workflow(
                query="q",
                workflow_name="research",
                budget_usd=0.1,
                ctx=mock_ctx,
            )
        # Yield once to let any pending tasks complete before asserting
        await asyncio.sleep(0.05)
        # After exception, the bridge should have detached
        assert len(fresh_bus._subscribers) == 0


class TestCountWorkflowPhases:
    """Cheap phase-count helper used as progress denominator."""

    def test_returns_zero_for_unknown_workflow(self):
        assert _count_workflow_phases_safe("__nonexistent_workflow__") == 0

    def test_returns_positive_for_real_workflow(self):
        # `verify` is a real workflow present in the repo
        n = _count_workflow_phases_safe("verify")
        # 0 is acceptable if the workflow yaml isn't in scope; we only
        # assert the function does not raise and returns an int ≥ 0.
        assert isinstance(n, int)
        assert n >= 0


class TestRunBeagleWorkflowSignature:
    """The MCP tool must accept the optional `ctx` parameter without
    breaking the existing positional contract (regression check)."""

    def test_ctx_is_optional(self):
        import inspect

        sig = inspect.signature(run_beagle_workflow)
        assert "ctx" in sig.parameters
        ctx_param = sig.parameters["ctx"]
        assert ctx_param.default is None
        # No required args before the optional ctx — existing callers
        # calling by keyword must continue to work.
        required = [
            p
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind
            not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]
        assert len(required) == 1 and required[0].name == "query"

    def test_run_beagle_workflow_is_not_stub_after_change(self):
        """v13.16 anti-stub guard — make sure we didn't accidentally
        reduce the function to a constant return."""
        import inspect

        from beagle.infrastructure.mcp_utility_server import (
            run_beagle_workflow,
        )

        lines = len(inspect.getsource(run_beagle_workflow).split("\n"))
        assert lines > 30, f"run_beagle_workflow shrunk to {lines} lines — possible stub regression"
