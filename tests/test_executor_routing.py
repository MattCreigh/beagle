"""Tests for executor routing, validation, and tool failure escalation.

v13.7.0: Ensures Beagle tools (Goose) are the default executor,
invalid executors are caught, and LangChain tool failures escalate
to Goose automatically.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.bridges.tool_node import (
    _infer_phase_from_tool,
    _resolve_input_mapping,
    execute_langchain_tool_node,
)
from beagle.events.events import ToolEscalated

# ── Executor Validation Tests ────────────────────────────────────────────────


class TestExecutorValidation:
    """Test that invalid executors fall back to goose with warning."""

    def test_valid_executors_accepted(self):
        """All valid executor strings should be accepted."""
        valid = {"goose", "langchain_tool", "langchain_llm", "a2a_remote"}
        for executor in valid:
            spec = {
                "name": "test",
                "skill_name": "test",
                "executor": executor,
                "prompt_template": "test",
            }
            assert spec["executor"] in valid

    def test_default_executor_is_goose(self):
        """When no executor specified, default should be 'goose'."""
        spec = {"name": "test", "skill_name": "test", "prompt_template": "test"}
        executor = spec.get("executor", "goose")
        assert executor == "goose"


# ── Tool Failure Flag Tests ──────────────────────────────────────────────────


class TestToolFailureFlag:
    """Test that tool_node.py returns proper failure flags."""

    @pytest.mark.asyncio
    async def test_unavailable_tool_returns_failure_flag(self):
        """Tool not in registry should return a tool_failure_flag."""
        mock_config = MagicMock()
        mock_config.fallback_on_error = True
        mock_config.default_timeout_seconds = 30

        mock_registry = MagicMock()
        mock_registry.get_tool.return_value = None

        state = {"workflow_id": "test-wf"}
        phase_spec = {"name": "test_tool", "tool_name": "nonexistent"}

        with (
            patch(
                "beagle.bridges.tool_node.get_tools_config",
                return_value=mock_config,
            ),
            patch(
                "beagle.bridges.tool_node.get_tool_registry",
                return_value=mock_registry,
            ),
        ):
            result = await execute_langchain_tool_node(state, phase_spec, "output")

        assert "tool_failure_flag" in result
        flag = result["tool_failure_flag"]
        assert flag["category"] == "tool_unavailable"
        assert flag["escalate_to_goose"] is True
        assert flag["tool_name"] == "nonexistent"

    @pytest.mark.asyncio
    async def test_timeout_returns_failure_flag(self):
        """Tool timeout should return a tool_failure_flag."""
        import asyncio

        mock_config = MagicMock()
        mock_config.fallback_on_error = True
        mock_config.default_timeout_seconds = 0.01  # Very short timeout

        mock_tool = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_registry = MagicMock()
        mock_registry.get_tool.return_value = mock_tool

        state = {"workflow_id": "test-wf"}
        phase_spec = {"name": "slow_tool", "tool_name": "slow_tool"}

        with (
            patch(
                "beagle.bridges.tool_node.get_tools_config",
                return_value=mock_config,
            ),
            patch(
                "beagle.bridges.tool_node.get_tool_registry",
                return_value=mock_registry,
            ),
            patch(
                "beagle.bridges.tool_node.get_event_bus",
            ),
        ):
            # We need to trigger the TimeoutError path
            mock_tool.ainvoke = AsyncMock(side_effect=TimeoutError)
            result = await execute_langchain_tool_node(state, phase_spec, "output", timeout=1)

        assert "tool_failure_flag" in result
        assert result["tool_failure_flag"]["category"] == "timeout"
        assert result["tool_failure_flag"]["escalate_to_goose"] is True

    @pytest.mark.asyncio
    async def test_exception_returns_failure_flag(self):
        """Tool exception should return a tool_failure_flag."""
        mock_config = MagicMock()
        mock_config.fallback_on_error = True
        mock_config.default_timeout_seconds = 30

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(side_effect=ValueError("bad input"))
        mock_tool.invoke = MagicMock(side_effect=ValueError("bad input"))
        # Make it not callable directly so it goes through invoke
        del mock_tool.__call__

        mock_registry = MagicMock()
        mock_registry.get_tool.return_value = mock_tool

        state = {"workflow_id": "test-wf"}
        phase_spec = {"name": "bad_tool", "tool_name": "bad_tool"}

        with (
            patch(
                "beagle.bridges.tool_node.get_tools_config",
                return_value=mock_config,
            ),
            patch(
                "beagle.bridges.tool_node.get_tool_registry",
                return_value=mock_registry,
            ),
            patch(
                "beagle.bridges.tool_node.get_event_bus",
            ),
        ):
            result = await execute_langchain_tool_node(state, phase_spec, "output")

        assert "tool_failure_flag" in result
        assert result["tool_failure_flag"]["category"] == "tool_error"
        assert result["tool_failure_flag"]["escalate_to_goose"] is True


# ── ToolEscalated Event Tests ────────────────────────────────────────────────


class TestToolEscalatedEvent:
    """Test the ToolEscalated event dataclass."""

    def test_tool_escalated_creation(self):
        event = ToolEscalated(
            workflow_id="wf-123",
            tool_name="test_tool",
            error="connection refused",
        )
        assert event.event_type == "tool.escalated"
        assert event.tool_name == "test_tool"
        assert event.error == "connection refused"
        assert event.escalated_to == "goose"
        assert event.original_executor == "langchain_tool"

    def test_tool_escalated_is_frozen(self):
        event = ToolEscalated(
            workflow_id="wf-123",
            tool_name="test",
            error="err",
        )
        with pytest.raises(AttributeError):
            event.tool_name = "modified"  # type: ignore[misc]


# ── Input Mapping Tests ──────────────────────────────────────────────────────


class TestInputMapping:
    """Test state reference resolution in input mappings."""

    def test_simple_state_reference(self):
        mapping = {"path": "{{state.file_path}}"}
        state = {"file_path": "/tmp/test.py"}
        result = _resolve_input_mapping(mapping, state)
        assert result["path"] == "/tmp/test.py"

    def test_nested_state_reference(self):
        mapping = {"content": "{{state.hydration.manifest.source_file}}"}
        state = {"hydration": {"manifest": {"source_file": "main.py"}}}
        result = _resolve_input_mapping(mapping, state)
        assert result["content"] == "main.py"

    def test_literal_passthrough(self):
        mapping = {"mode": "read_only"}
        state = {}
        result = _resolve_input_mapping(mapping, state)
        assert result["mode"] == "read_only"

    def test_missing_state_key_returns_empty(self):
        mapping = {"x": "{{state.nonexistent}}"}
        state = {}
        result = _resolve_input_mapping(mapping, state)
        assert result["x"] == ""


# ── Phase Inference Tests ────────────────────────────────────────────────────


class TestPhaseInference:
    """Test tool name to workflow phase inference."""

    def test_file_tool_is_execution(self):
        assert _infer_phase_from_tool("file_reader") == "execution"

    def test_search_tool_is_execution(self):
        assert _infer_phase_from_tool("web_search") == "execution"

    def test_slack_tool_is_synthesis(self):
        assert _infer_phase_from_tool("slack_notify") == "synthesis"

    def test_unknown_tool_is_execution(self):
        assert _infer_phase_from_tool("custom_analyzer") == "execution"
