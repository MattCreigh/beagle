"""Tests for beagle.bridges A2A protocol — imports, AgentCard, AgentCardBuilder, sanitization."""

from __future__ import annotations

import pytest

from beagle.bridges.a2a_card_builder import (
    _infer_capabilities,
    _infer_input_schema,
    _infer_output_schema,
    build_agent_cards,
)
from beagle.bridges.a2a_server import (
    _MAX_QUERY_LENGTH,
    A2ATask,
    A2ATaskResult,
    AgentCard,
    BeagleToA2ABridge,
    _sanitize_query,
)

# ── AgentCard ──────────────────────────────────────────────────────────────


class TestAgentCard:
    """AgentCard dataclass creation and defaults."""

    def test_agent_card_creation_defaults(self):
        card = AgentCard(name="test-agent")
        assert card.name == "test-agent"
        assert card.description == ""
        assert card.version == "1.0.0"
        assert card.capabilities == []
        assert card.input_schema == {}
        assert card.output_schema == {}
        assert card.endpoint_url == ""

    def test_agent_card_full_creation(self):
        card = AgentCard(
            name="researcher",
            description="Research agent",
            version="2.0.0",
            capabilities=["execute_workflow"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            endpoint_url="http://localhost:8420/a2a/execute",
        )
        assert card.name == "researcher"
        assert card.version == "2.0.0"
        assert len(card.capabilities) == 1

    def test_agent_card_with_endpoint(self):
        card = AgentCard(name="audit", endpoint_url="http://localhost:8420/a2a/execute")
        assert "8420" in card.endpoint_url


# ── A2ATask ────────────────────────────────────────────────────────────────


class TestA2ATask:
    """A2ATask dataclass creation."""

    def test_a2a_task_defaults(self):
        task = A2ATask()
        assert task.task_id == ""
        assert task.agent_name == ""
        assert task.input == {}
        assert task.callback_url == ""

    def test_a2a_task_with_data(self):
        task = A2ATask(
            task_id="abc-123",
            agent_name="researcher",
            input={"query": "Analyze code"},
        )
        assert task.task_id == "abc-123"
        assert task.agent_name == "researcher"
        assert task.input["query"] == "Analyze code"


# ── A2ATaskResult ──────────────────────────────────────────────────────────


class TestA2ATaskResult:
    """A2ATaskResult dataclass creation."""

    def test_defaults(self):
        result = A2ATaskResult()
        assert result.status == "completed"
        assert result.error == ""
        assert result.agent_name == ""
        assert result.output is None

    def test_failed_result(self):
        result = A2ATaskResult(task_id="t1", status="failed", error="Timeout")
        assert result.status == "failed"
        assert result.error == "Timeout"


# ── _sanitize_query ─────────────────────────────────────────────────────────


class TestSanitizeQuery:
    """Input sanitization for A2A queries."""

    def test_normal_query_passes(self):
        result = _sanitize_query("Analyze the auth module")
        assert result == "Analyze the auth module"

    def test_control_chars_stripped(self):
        result = _sanitize_query("Hello\x00World\x01Test")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "Hello" in result
        assert "World" in result

    def test_newlines_preserved(self):
        result = _sanitize_query("Line1\nLine2\tIndented")
        assert "\n" in result
        assert "\t" in result

    def test_oversized_query_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            _sanitize_query("x" * (_MAX_QUERY_LENGTH + 1))

    def test_empty_query_passes(self):
        result = _sanitize_query("")
        assert result == ""


# ── AgentCardBuilder helper functions ──────────────────────────────────────


class TestInferCapabilities:
    """_infer_capabilities from agent profiles."""

    def test_develop_mode_capabilities(self):
        profile = {"mode": "develop", "model": "glm-5:cloud"}
        caps = _infer_capabilities(profile)
        assert "execute_workflow" in caps
        assert "code_generation" in caps
        assert "file_modification" in caps

    def test_audit_mode_capabilities(self):
        profile = {"mode": "audit"}
        caps = _infer_capabilities(profile)
        assert "execute_workflow" in caps
        assert "read_only_analysis" in caps

    def test_research_mode_capabilities(self):
        profile = {"mode": "research"}
        caps = _infer_capabilities(profile)
        assert "read_only_analysis" in caps

    def test_model_capability(self):
        profile = {"model": "glm-5:cloud"}
        caps = _infer_capabilities(profile)
        assert any("model:" in c for c in caps)

    def test_tools_capability(self):
        profile = {"tools": ["read_file", "write_file"]}
        caps = _infer_capabilities(profile)
        assert "tool_use" in caps
        assert any("tool:read_file" in c for c in caps)


class TestInferInputSchema:
    """_infer_input_schema always provides a query field."""

    def test_schema_has_query(self):
        profile = {}
        schema = _infer_input_schema(profile)
        assert "query" in schema["properties"]
        assert "required" in schema

    def test_schema_has_steering_prompt(self):
        profile = {}
        schema = _infer_input_schema(profile)
        assert "steering_prompt" in schema["properties"]


class TestInferOutputSchema:
    """_infer_output_schema returns a valid schema."""

    def test_schema_has_final_report(self):
        profile = {}
        schema = _infer_output_schema(profile)
        assert "final_report" in schema["properties"]

    def test_schema_has_completed_nodes(self):
        profile = {}
        schema = _infer_output_schema(profile)
        assert "completed_nodes" in schema["properties"]


# ── A2AClientBridge import ──────────────────────────────────────────────────


class TestA2AClientImport:
    """A2AClientBridge can be imported."""

    def test_import_a2a_client(self):
        from beagle.bridges.a2a_client import A2AClientBridge

        assert A2AClientBridge is not None

    def test_a2a_client_creation(self):
        from beagle.bridges.a2a_client import A2AClientBridge

        client = A2AClientBridge()
        assert client._discovery_cache == {}

    def test_get_a2a_client_singleton(self):
        from beagle.bridges.a2a_client import get_a2a_client

        c1 = get_a2a_client()
        c2 = get_a2a_client()
        assert c1 is c2


# ── build_agent_cards ──────────────────────────────────────────────────────


class TestBuildAgentCards:
    """build_agent_cards reads agents.toml."""

    def test_build_agent_cards_returns_list(self):
        cards = build_agent_cards()
        assert isinstance(cards, list)

    def test_build_agent_cards_items_are_agent_card(self):
        cards = build_agent_cards()
        for card in cards:
            assert isinstance(card, AgentCard)
            assert card.name
            assert card.endpoint_url


# ── BeagleToA2ABridge ────────────────────────────────────────────────────────


class TestBeagleToA2ABridge:
    """BeagleToA2ABridge creation."""

    def test_bridge_creation(self):
        bridge = BeagleToA2ABridge()
        assert bridge.config is not None
        assert bridge._app is None
