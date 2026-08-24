"""Tests for autonomous_orchestrator module.

Tests DAG orchestration, agent spawning, context folding,
and cost tracking integration.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.core.autonomous_orchestrator import (
    BeagleDAGNode,
    DAGOrchestrator,
    cleanup_agent_call_counter,
    get_agent_call_count,
    increment_agent_call,
    reset_agent_call_counter,
)
from beagle.core.orchestrator_types import AgentState, DAGNode

sys.path.insert(0, str(Path(__file__).parent.parent))

from beagle.core.autonomous_orchestrator import (
    AgentPingMessage,
    CompressedKVPool,
    get_kv_pool,
    get_output_dir,
    get_recipes_dir,
    get_workspace_root,
)
from beagle.core.orchestrator_types import GooseExecutionError
from beagle.core.state import BeagleState


class TestAgentPingMessage:
    """Test agent ping message handling."""

    def test_ping_message_to_dict(self):
        """Test serialization to dict."""
        msg = AgentPingMessage(
            agent_id="test-agent-1",
            parent_workflow_id="wf-123",
            status="completed",
            result="Success",
        )
        data = msg.to_dict()
        assert data["agent_id"] == "test-agent-1"
        assert data["parent_workflow_id"] == "wf-123"
        assert data["status"] == "completed"

    def test_ping_message_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "agent_id": "agent-2",
            "parent_workflow_id": "wf-789",
            "status": "failed",
            "result": None,
            "type": "completion",
        }
        msg = AgentPingMessage.from_dict(data)
        assert msg.agent_id == "agent-2"
        assert msg.status == "failed"


class TestGooseExecutionError:
    """Test custom execution error."""

    def test_error_creation(self):
        """Test creating execution error."""
        error = GooseExecutionError("Node failed: timeout")
        assert "Node failed" in str(error) or "timeout" in str(error)


class TestCompressedKVPool:
    """Test compressed key-value pool for state sharing."""

    def test_pool_creation(self):
        """Test creating a KV pool."""
        pool = CompressedKVPool()
        assert pool._pool == {}

    def test_pool_put_and_get(self):
        """Test storing and retrieving from pool."""
        pool = CompressedKVPool()
        cache_id = "wf-node1"
        data = b"compressed_state_data"

        pool.put(cache_id, data)
        retrieved = pool.get(cache_id)

        assert retrieved == data

    def test_pool_get_unknown(self):
        """Test retrieving unknown key returns None."""
        pool = CompressedKVPool()
        assert pool.get("nonexistent") is None

    def test_kv_pool_singleton(self):
        """Test get_kv_pool returns singleton."""
        pool1 = get_kv_pool()
        pool2 = get_kv_pool()
        assert pool1 is pool2  # Same instance


class TestDAGOrchestrator:
    """Test DAG orchestrator functionality."""

    def _make_orchestrator(self, **kwargs):
        """Create a DAGOrchestrator with mocked dependencies."""
        from unittest.mock import MagicMock, patch

        mock_ctx_mgr = MagicMock()
        mock_ctx_mgr.start_node = MagicMock()
        mock_ctx_int = MagicMock()
        mock_cost = MagicMock()
        mock_cost.budget_usd = kwargs.get("budget_usd", 10.0)

        with (
            patch(
                "beagle.core.autonomous_orchestrator.get_context_manager",
                return_value=mock_ctx_mgr,
            ),
            patch(
                "beagle.core.autonomous_orchestrator.get_context_integration",
                return_value=mock_ctx_int,
            ),
            patch(
                "beagle.core.autonomous_orchestrator.reset_cost_tracker",
                return_value=mock_cost,
            ),
        ):
            return DAGOrchestrator(**kwargs)

    def test_orchestrator_creation(self):
        """Test creating an orchestrator."""
        orchestrator = self._make_orchestrator(workflow_id="test-wf-001")
        assert orchestrator.workflow_id == "test-wf-001"

    def test_orchestrator_with_budget(self):
        """Test orchestrator with budget."""
        orchestrator = self._make_orchestrator(budget_usd=10.0, workflow_id="budget-test")
        assert orchestrator.budget_usd == 10.0


class TestDAGNode:
    """Test DAGNode execution."""

    # v1.0.2: both tests below used to wrap construction in
    # `except TypeError: pytest.skip("BeagleDAGNode signature changed")`.
    # A signature change is precisely the regression these tests exist to
    # catch, and that handler converted it into a silent skip — the suite would
    # have gone green on the exact break it was written to detect. The
    # constructor is part of the public contract; if it changes, these must
    # fail loudly so the change is a deliberate decision.

    def test_node_creation(self):
        """Test creating a node."""
        node = BeagleDAGNode(
            name="test_node",
            skill_name="test-skill",
            state_mutator=lambda _state, _result: None,
            prompt_builder=lambda _state: "test prompt",
        )
        assert node.name == "test_node"
        assert node.skill_name == "test-skill"

    def test_node_has_execute_method(self):
        """Test BeagleDAGNode has async execute method."""
        node = BeagleDAGNode(
            name="exec_node",
            skill_name="test-skill",
            state_mutator=lambda _state, _result: None,
            prompt_builder=lambda _state: "test",
        )
        assert hasattr(node, "execute")
        assert asyncio.iscoroutinefunction(node.execute)


class TestAgentCallCounter:
    """Test agent call counting (async functions)."""

    @pytest.mark.asyncio
    async def test_call_counter_functions_exist(self):
        """Test that call counter functions are async."""
        import inspect

        assert inspect.iscoroutinefunction(increment_agent_call)
        assert inspect.iscoroutinefunction(get_agent_call_count)
        assert inspect.iscoroutinefunction(reset_agent_call_counter)
        # Also verify invocation returns an awaitable without leaking coroutines
        coro = increment_agent_call("test")
        assert inspect.iscoroutine(coro)
        await coro
        await reset_agent_call_counter("test")


class TestPathHelpers:
    """Test path helper functions."""

    def test_get_workspace_root(self):
        """Test workspace root detection."""
        root = get_workspace_root()
        assert root is not None
        assert isinstance(root, Path)

    def test_get_recipes_dir(self):
        """Test recipes directory."""
        recipes_dir = get_recipes_dir()
        assert recipes_dir is not None
        assert "recipes" in str(recipes_dir)

    def test_get_output_dir(self):
        """Test output directory."""
        output_dir = get_output_dir()
        assert output_dir is not None
        assert isinstance(output_dir, Path)


class TestStateImport:
    """Test state module imports."""

    def test_beagle_state_is_typeddict(self):
        """Test BeagleState is a TypedDict."""

        # BeagleState is a TypedDict subclass
        assert BeagleState is not None

        # Create instance as dict
        state: BeagleState = {"query": "Test query"}
        assert state["query"] == "Test query"

    def test_state_with_workflow_id(self):
        """Test state with workflow ID."""
        state: BeagleState = {
            "query": "Test",
            "workflow_id": "wf-test",
        }
        assert state["workflow_id"] == "wf-test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── Merged from test_orchestrator_extra.py (v1.0.0 consolidation) ────
def _autodream_stub() -> MagicMock:
    """Stub for ``beagle.memory.autodream`` honouring its async contract.

    ``AutoDream.consolidate`` is ``async def`` and the orchestrator awaits it.
    A bare ``MagicMock()`` module returns a non-awaitable from
    ``consolidate()``, so the await raises
    ``TypeError: object MagicMock can't be used in 'await' expression``. That
    used to disappear into a broad ``except Exception`` on the consolidation
    path; with the catch narrowed it surfaces, so the stub must match the real
    signature rather than rely on the exception being swallowed.
    """
    module = MagicMock()
    module.AutoDream.return_value.consolidate = AsyncMock(return_value=None)
    return module


@pytest.mark.asyncio
async def test_agent_call_counters():
    wf_id = "test_wf_id"
    await reset_agent_call_counter(wf_id)
    assert await get_agent_call_count(wf_id) == 0
    val = await increment_agent_call(wf_id)
    assert val == 1
    assert await get_agent_call_count(wf_id) == 1
    await cleanup_agent_call_counter(wf_id)
    assert await get_agent_call_count(wf_id) == 0


@pytest.mark.asyncio
@patch("beagle.core.autonomous_orchestrator.validate_query_async")
async def test_orchestrator_run_trivial_query(mock_validate):
    mock_validate.return_value = (True, "")
    orch = DAGOrchestrator(budget_usd=10.0, workflow_id="wf1")
    node = DAGNode(name="test_node", skill_name="test_skill", state_mutator=lambda s, r: None)
    orch.add_node(node, is_start=True)

    state = await orch.run("ping")
    # Reflex Arc should bypass for "ping" since it's trivial
    assert state.query == "ping"


@pytest.mark.asyncio
@patch.dict(
    "sys.modules",
    {
        "beagle.memory.memory_index": MagicMock(),
        "beagle.memory.autodream": _autodream_stub(),
    },
)
@patch("beagle.core.autonomous_orchestrator.validate_query_async")
@patch(
    "beagle.core.autonomous_orchestrator.BeagleDAGNode.execute",
    new_callable=AsyncMock,
)
async def test_orchestrator_run_full_workflow(mock_execute, mock_validate):
    mock_validate.return_value = (True, "")
    mock_execute.return_value = True
    orch = DAGOrchestrator(budget_usd=10.0, workflow_id="wf2")

    node1 = DAGNode(name="node1", skill_name="skill1", state_mutator=lambda s, r: None)
    node2 = DAGNode(name="node2", skill_name="skill2", state_mutator=lambda s, r: None)

    orch.add_node(node1, is_start=True)
    orch.add_node(node2)
    orch.add_transition("node1", "node2")

    state = await orch.run("this is a complex query that requires full workflow execution")

    assert mock_execute.call_count == 2
    assert "node1" in state.completed_nodes
    assert "node2" in state.completed_nodes


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_beagle_dag_node_execute(mock_exec):
    process_mock = AsyncMock()

    class MockStream:
        def __init__(self, data):
            self.data = data
            self.index = 0

        async def readline(self):
            if self.index < len(self.data):
                val = self.data[self.index]
                self.index += 1
                return val
            return b""

    process_mock.stdout = MockStream([b"<final_answer>result</final_answer>\n"])
    process_mock.stderr = MockStream([])
    process_mock.stdin = AsyncMock()
    process_mock.returncode = 0
    process_mock.wait.return_value = 0
    mock_exec.return_value = process_mock

    node = DAGNode(name="n1", skill_name="s1", state_mutator=lambda s, r: None, output_key="out1")
    beagle_node = BeagleDAGNode.from_node(node)

    state = AgentState(workflow_id="wf1", query="query")

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value="recipe content"),
        patch.dict(os.environ, {"GOOSE_BIN": "/usr/local/bin/goose"}),
        patch("os.access", return_value=True),
        patch.object(Path, "is_file", return_value=True),
    ):
        res = await beagle_node.execute(state)
        assert res is True
        assert state.metadata["out1"] == "result"


@pytest.mark.asyncio
@patch.dict(
    "sys.modules",
    {
        "beagle.memory.memory_index": MagicMock(),
        "beagle.memory.autodream": _autodream_stub(),
    },
)
@patch("beagle.core.autonomous_orchestrator.validate_query_async")
@patch(
    "beagle.core.autonomous_orchestrator.BeagleDAGNode.execute",
    new_callable=AsyncMock,
)
async def test_orchestrator_budget_exceeded(mock_execute, mock_validate):
    mock_validate.return_value = (True, "")
    mock_execute.return_value = True
    orch = DAGOrchestrator(budget_usd=0.000001, workflow_id="wf3")
    orch.cost_tracker.check_budget = MagicMock(return_value=False)

    node = DAGNode(name="node1", skill_name="skill1", state_mutator=lambda s, r: None)
    orch.add_node(node, is_start=True)

    from unittest.mock import PropertyMock

    with patch(
        "beagle.cost_tracker.ContextAwareCostTracker.total_cost_usd",
        new_callable=PropertyMock,
        return_value=100.0,
    ):
        state = await orch.run("complex query")

    assert "Budget exceeded - workflow halted" in state.errors
    assert mock_execute.call_count == 0
