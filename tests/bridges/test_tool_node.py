"""Tests for beagle.bridges.tool_registry and tool_node — Tool registry and input resolution."""

from __future__ import annotations

from beagle.bridges.tool_node import (
    _infer_phase_from_tool,
    _resolve_input_mapping,
)
from beagle.bridges.tool_registry import (
    ToolRegistry,
    get_tool_registry,
    register_tool,
)

# ── ToolRegistry ───────────────────────────────────────────────────────────


class TestToolRegistry:
    """ToolRegistry singleton creation and methods."""

    def test_registry_is_singleton(self):
        r1 = ToolRegistry()
        r2 = ToolRegistry()
        assert r1 is r2

    def test_get_tool_registry_returns_tool_registry(self):
        registry = get_tool_registry()
        assert isinstance(registry, ToolRegistry)

    def test_get_tool_registry_returns_same_instance(self):
        r1 = get_tool_registry()
        r2 = get_tool_registry()
        assert r1 is r2

    def test_get_available_tools_returns_list(self):
        registry = get_tool_registry()
        tools = registry.get_available_tools()
        assert isinstance(tools, list)

    def test_is_tool_available_for_nonexistent_tool(self):
        registry = get_tool_registry()
        assert registry.is_tool_available("nonexistent_tool_xyz") is False

    def test_reset_clears_cache(self):
        registry = get_tool_registry()
        registry._tools["test_key"] = "test_value"
        registry.reset()
        assert registry._tools == {}

    def test_get_nonexistent_tool_returns_none(self):
        registry = get_tool_registry()
        registry.reset()
        result = registry.get_tool("nonexistent_tool_xyz")
        assert result is None


# ── register_tool function ─────────────────────────────────────────────────


class TestRegisterTool:
    """register_tool adds entries to the config registry."""

    def test_register_tool_import(self):
        assert callable(register_tool)


# ── _infer_phase_from_tool ────────────────────────────────────────────────


class TestInferPhaseFromTool:
    """_infer_phase_from_tool logic for phase name heuristics."""

    def test_file_tool_infers_execution(self):
        assert _infer_phase_from_tool("file_system") == "execution"

    def test_search_tool_infers_execution(self):
        assert _infer_phase_from_tool("web_search") == "execution"

    def test_sql_tool_infers_execution(self):
        assert _infer_phase_from_tool("sql_database") == "execution"

    def test_slack_tool_infers_synthesis(self):
        assert _infer_phase_from_tool("slack_notify") == "synthesis"

    def test_email_tool_infers_synthesis(self):
        assert _infer_phase_from_tool("email_sender") == "synthesis"

    def test_unknown_tool_infers_execution(self):
        assert _infer_phase_from_tool("custom_thing") == "execution"

    def test_read_tool_infers_execution(self):
        assert _infer_phase_from_tool("read_file_tool") == "execution"

    def test_git_tool_infers_execution(self):
        assert _infer_phase_from_tool("git_ops") == "execution"


# ── _resolve_input_mapping ───────────────────────────────────────────────


class TestResolveInputMapping:
    """_resolve_input_mapping resolves {{state.field}} templates."""

    def test_static_value_passthrough(self):
        result = _resolve_input_mapping({"file_path": "/tmp/test.py"}, {})
        assert result["file_path"] == "/tmp/test.py"

    def test_state_reference_resolved(self):
        state = {"query": "Analyze code"}
        result = _resolve_input_mapping({"query": "{{state.query}}"}, state)
        assert result["query"] == "Analyze code"

    def test_nested_state_reference(self):
        state = {"metadata": {"source": "config.toml"}}
        result = _resolve_input_mapping({"source": "{{state.metadata.source}}"}, state)
        assert result["source"] == "config.toml"

    def test_missing_state_key_returns_empty_string(self):
        state = {}
        result = _resolve_input_mapping({"q": "{{state.nonexistent}}"}, state)
        assert result["q"] == ""

    def test_non_template_string_passthrough(self):
        result = _resolve_input_mapping({"key": "plain value"}, {})
        assert result["key"] == "plain value"

    def test_non_string_value_passthrough(self):
        result = _resolve_input_mapping({"count": 42}, {})
        assert result["count"] == 42

    def test_empty_input_mapping(self):
        result = _resolve_input_mapping({}, {"query": "test"})
        assert result == {}

    def test_inline_substitution(self):
        """Template with text around {{state.X}} resolves via fallback."""
        state = {"name": "test_workflow"}
        result = _resolve_input_mapping({"msg": "Running {{state.name}} now"}, state)
        assert "test_workflow" in result["msg"]


# ── execute_langchain_tool_node import ─────────────────────────────────────


class TestToolNodeImport:
    """Verify execute_langchain_tool_node can be imported."""

    def test_import(self):
        from beagle.bridges.tool_node import execute_langchain_tool_node

        assert callable(execute_langchain_tool_node)
