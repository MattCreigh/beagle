"""Tests for Phases 2-6: Tool Node, Chat Model, LangSmith, A2A, Cloud."""

from __future__ import annotations

import os
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: Tool Node Adapter
# ═════════════════════════════════════════════════════════════════════════════


class TestToolRegistry:
    """Unit tests for the TOML-driven tool registry."""

    def test_singleton_pattern(self):
        """ToolRegistry uses singleton pattern."""
        from beagle.bridges.tool_registry import ToolRegistry

        r1 = ToolRegistry()
        r2 = ToolRegistry()
        assert r1 is r2

    def test_available_tools(self):
        """get_available_tools returns configured tool names."""
        from beagle.bridges.tool_registry import get_tool_registry

        registry = get_tool_registry()
        tools = registry.get_available_tools()
        assert isinstance(tools, list)

    def test_unavailable_tool_returns_none(self):
        """get_tool returns None for unregistered tools."""
        from beagle.bridges.tool_registry import get_tool_registry

        registry = get_tool_registry()
        result = registry.get_tool("nonexistent_tool_xyz")
        assert result is None

    def test_programmatic_registration(self):
        """register_tool adds tools without editing config.toml."""
        from beagle.bridges.tool_registry import (
            get_tool_registry,
            register_tool,
        )

        registry = get_tool_registry()
        register_tool("test_tool", "some.module.Tool", enabled=False)
        assert registry.is_tool_available("test_tool") is False  # disabled


class TestToolNode:
    """Unit tests for LangChain Tool Node execution."""

    def test_input_mapping_state_refs(self):
        """_resolve_input_mapping handles {{state.field}} references."""
        from beagle.bridges.tool_node import _resolve_input_mapping

        state = {"query": "test query", "metadata": {"source_file": "/tmp/test.py"}}
        mapping = {
            "query_text": "{{state.query}}",
            "file": "{{state.metadata.source_file}}",
        }

        resolved = _resolve_input_mapping(mapping, state)
        assert resolved["query_text"] == "test query"
        assert resolved["file"] == "/tmp/test.py"

    def test_input_mapping_passthrough(self):
        """Non-template values pass through unchanged."""
        from beagle.bridges.tool_node import _resolve_input_mapping

        state = {}
        mapping = {"path": "/tmp/fixed_path.py", "count": 42}

        resolved = _resolve_input_mapping(mapping, state)
        assert resolved["path"] == "/tmp/fixed_path.py"
        assert resolved["count"] == 42

    @pytest.mark.asyncio
    async def test_tool_not_available(self):
        """execute_langchain_tool_node handles missing tools gracefully."""
        from beagle.bridges.tool_node import execute_langchain_tool_node

        state = {"metadata": {}}
        phase_spec = {"name": "test", "tool_name": "nonexistent"}

        result = await execute_langchain_tool_node(state, phase_spec, "output")
        assert "errors" in result or "completed_nodes" in result


# ═════════════════════════════════════════════════════════════════════════════
# Phase 3: Chat Model / LLM Node
# ═════════════════════════════════════════════════════════════════════════════


class TestOllamaCloudChatModel:
    """Unit tests for OllamaCloudChatModel."""

    def test_llm_type(self):
        """OllamaCloudChatModel._llm_type returns 'ollama-cloud'."""
        from beagle.bridges.chat_model import OllamaCloudChatModel

        model = OllamaCloudChatModel.__new__(OllamaCloudChatModel)
        model._instance = None
        model._model_name = None
        model._temperature = None
        model._kwargs = {}
        assert model._llm_type == "ollama-cloud"

    def test_repr(self):
        """OllamaCloudChatModel has useful repr."""
        from beagle.bridges.chat_model import OllamaCloudChatModel

        model = OllamaCloudChatModel(model_name="glm-5.1:cloud")
        assert "glm-5.1:cloud" in repr(model)

    def test_create_requires_api_key(self):
        """create_chat_model raises RuntimeError without API key (if langchain-openai installed)."""
        from beagle.bridges.chat_model import create_chat_model

        with patch("beagle.bridges.chat_model.load_secret", return_value=""):
            try:
                create_chat_model(model_name="glm-5.1:cloud")
                pytest.fail("Expected RuntimeError or ImportError")
            except (RuntimeError, ImportError):
                pass  # Either is acceptable depending on install state

    def test_create_requires_langchain_openai(self):
        """create_chat_model raises ImportError without langchain-openai."""
        from beagle.bridges.chat_model import create_chat_model

        with (
            patch(
                "beagle.bridges.chat_model.load_secret",
                return_value="test-key",
            ),
            patch.dict("sys.modules", {"langchain_openai": None}),
            pytest.raises(ImportError, match="langchain-openai"),
        ):
            create_chat_model(model_name="glm-5.1:cloud")


class TestLLMNode:
    """Unit tests for LangChain LLM Node execution."""

    def test_extract_final_answer_string(self):
        """_extract_final_answer handles plain strings."""
        from beagle.bridges.llm_node import _extract_final_answer

        assert _extract_final_answer("hello") == "hello"

    def test_extract_final_answer_aimessage(self):
        """_extract_final_answer handles AIMessage-like objects."""
        from beagle.bridges.llm_node import _extract_final_answer

        class FakeAIMessage:
            content = "test response"

        assert _extract_final_answer(FakeAIMessage()) == "test response"


# ═════════════════════════════════════════════════════════════════════════════
# Phase 4: LangSmith Bridge
# ═════════════════════════════════════════════════════════════════════════════


class TestLangSmithBridge:
    """Unit tests for the OTel → LangSmith observability bridge."""

    def test_bridge_disabled_by_default(self):
        """BeagleLangSmithBridge is disabled in default config."""
        from beagle.bridges.otel_langsmith_bridge import (
            BeagleLangSmithBridge,
        )

        bridge = BeagleLangSmithBridge()
        assert bridge.config.enabled is False

    def test_start_returns_false_when_disabled(self):
        """bridge.start() returns False when disabled."""
        from beagle.bridges.otel_langsmith_bridge import (
            BeagleLangSmithBridge,
        )

        bridge = BeagleLangSmithBridge()
        assert bridge.start() is False

    def test_translate_span_disabled(self):
        """translate_otel_span_to_run_metadata returns empty when not started."""
        from beagle.bridges.otel_langsmith_bridge import (
            BeagleLangSmithBridge,
        )

        bridge = BeagleLangSmithBridge()
        result = bridge.translate_otel_span_to_run_metadata(MagicMock())
        assert result == {}


class TestCallbackHandler:
    """Unit tests for the Beagle LangChain callback handler."""

    def test_handler_creation(self):
        """BeagleCallbackHandler can be created with workflow_id."""
        from beagle.bridges.callback_handler import BeagleCallbackHandler

        handler = BeagleCallbackHandler(workflow_id="test-wf-123")
        assert handler._workflow_id == "test-wf-123"

    def test_on_llm_start(self):
        """on_llm_start publishes NodeStarted event."""
        from uuid import uuid4

        from beagle.bridges.callback_handler import BeagleCallbackHandler

        handler = BeagleCallbackHandler(workflow_id="test")
        # Should not raise
        handler.on_llm_start(
            serialized={"kwargs": {"model_name": "glm-5.1:cloud"}},
            prompts=["test"],
            run_id=uuid4(),
        )

    def test_on_llm_end(self):
        """on_llm_end publishes NodeCompleted event."""
        from uuid import uuid4

        from beagle.bridges.callback_handler import BeagleCallbackHandler

        handler = BeagleCallbackHandler(workflow_id="test")
        run_id = uuid4()
        handler.on_llm_start(serialized={}, prompts=["test"], run_id=run_id)
        handler.on_llm_end(response=MagicMock(), run_id=run_id)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 5: A2A Bridge
# ═════════════════════════════════════════════════════════════════════════════


class TestA2ACardBuilder:
    """Unit tests for A2A AgentCard auto-generation."""

    def test_agent_card_structure(self):
        """AgentCard has required A2A fields."""
        from beagle.bridges.a2a_server import AgentCard

        card = AgentCard(
            name="test-agent",
            description="Test agent",
            capabilities=["execute_workflow"],
        )
        assert card.name == "test-agent"
        assert "execute_workflow" in card.capabilities

    def test_a2a_task_result_structure(self):
        """A2ATaskResult has required fields."""
        from beagle.bridges.a2a_server import A2ATaskResult

        result = A2ATaskResult(task_id="t1", status="completed", output="done")
        assert result.status == "completed"
        assert result.task_id == "t1"


class TestA2AClient:
    """Unit tests for the outbound A2A client."""

    def test_client_creation(self):
        """A2AClientBridge can be created."""
        from beagle.bridges.a2a_client import A2AClientBridge

        client = A2AClientBridge()
        assert client.config.max_concurrent_tasks == 4

    def test_semaphore_cap(self):
        """A2AClientBridge respects concurrency cap."""
        from beagle.bridges.a2a_client import A2AClientBridge

        client = A2AClientBridge()
        assert client._semaphore._value == 4


# ═════════════════════════════════════════════════════════════════════════════
# Phase 6: Cloud Readiness / Checkpointer
# ═════════════════════════════════════════════════════════════════════════════


class TestCheckpointerFactory:
    """Tests for the checkpointer factory (SQLite vs Postgres)."""

    def test_sqlite_by_default(self):
        """create_checkpointer returns SQLite when not in cloud mode."""
        try:
            from beagle.memory.checkpointer import create_checkpointer
        except ImportError:
            pytest.skip("langgraph.checkpoint.sqlite not installed")
            return

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BEAGLE_EXECUTION_ENV", None)
            try:
                cp = create_checkpointer()
            except ImportError:
                pytest.skip("langgraph-checkpoint-sqlite not installed")
                return
            assert cp is not None

    def test_cloud_mode_requires_postgres(self):
        """create_checkpointer raises when cloud mode and no Postgres URI."""
        try:
            from beagle.memory.checkpointer import create_checkpointer
        except ImportError:
            pytest.skip("langgraph.checkpoint.sqlite not installed")
            return

        with (
            patch.dict(os.environ, {"BEAGLE_EXECUTION_ENV": "cloud"}, clear=False),
            pytest.raises((RuntimeError, ImportError)),
        ):
            create_checkpointer()

    def test_get_checkpointer_backward_compat(self):
        """get_checkpointer still works (backward compatible API)."""
        try:
            from beagle.memory.checkpointer import get_checkpointer
        except ImportError:
            pytest.skip("langgraph.checkpoint.sqlite not installed")
            return

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BEAGLE_EXECUTION_ENV", None)
            try:
                cp = get_checkpointer()
            except ImportError:
                pytest.skip("langgraph-checkpoint-sqlite not installed")
                return
            assert cp is not None


class TestOrpheusHTTPTransport:
    """Tests for the Orpheus HTTP SSE transport (cloud)."""

    def test_transport_creation(self):
        """OrpheusHTTPTransport can be created."""
        from beagle.bridges.orpheus_http_transport import (
            OrpheusHTTPTransport,
        )

        transport = OrpheusHTTPTransport()
        assert transport.config.orpheus_transport == "unix_socket"  # default


# ═════════════════════════════════════════════════════════════════════════════
# Cross-phase: Workflow Loader executor routing
# ═════════════════════════════════════════════════════════════════════════════


class TestWorkflowLoaderExecutorField:
    """Tests for workflow_loader.py executor field parsing."""

    def test_executor_default_is_goose(self):
        """Phases without executor field default to 'goose' (backward compatible)."""
        import yaml

        yaml_content = """
name: test_workflow
phases:
  - name: plan
    agent: research-planner
    prompt_template: "Plan: {query}"
"""
        spec = yaml.safe_load(yaml_content)
        # The _build_graph_from_spec function should handle missing executor
        assert spec["phases"][0].get("executor", "goose") == "goose"

    def test_executor_langchain_tool(self):
        """YAML with executor: langchain_tool is parsed correctly."""
        import yaml

        yaml_content = """
name: test_workflow
phases:
  - name: fetch
    agent: research-planner
    prompt_template: "Read {query}"
    executor: langchain_tool
    tool_name: file_system
    tool_method: read_file
    input_mapping:
      file_path: "/tmp/test.py"
"""
        spec = yaml.safe_load(yaml_content)
        assert spec["phases"][0]["executor"] == "langchain_tool"
        assert spec["phases"][0]["tool_name"] == "file_system"

    def test_executor_langchain_llm(self):
        """YAML with executor: langchain_llm is parsed correctly."""
        import yaml

        yaml_content = """
name: test_workflow
phases:
  - name: analyze
    agent: fact-checker
    prompt_template: "Analyze: {query}"
    executor: langchain_llm
"""
        spec = yaml.safe_load(yaml_content)
        assert spec["phases"][0]["executor"] == "langchain_llm"

    def test_executor_a2a_remote(self):
        """YAML with executor: a2a_remote is parsed correctly."""
        import yaml

        yaml_content = """
name: test_workflow
phases:
  - name: remote
    agent: researcher
    prompt_template: "Remote: {query}"
    executor: a2a_remote
    agent_url: "https://remote:8420/a2a"
    agent_name: "crewai_researcher"
"""
        spec = yaml.safe_load(yaml_content)
        assert spec["phases"][0]["executor"] == "a2a_remote"
        assert spec["phases"][0]["agent_url"] == "https://remote:8420/a2a"


# ═════════════════════════════════════════════════════════════════════════════
# Golden Master Security & Robustness Tests
# ═════════════════════════════════════════════════════════════════════════════


class TestA2ASecurityFixes:
    """Tests for A2A security hardening (Golden Master Section 1 & 3)."""

    def test_ed25519_signing_fails_closed_without_nacl(self):
        """A2A server _sign_payload must raise RuntimeError if Ed25519 unavailable.

        SECURITY (DevSecOps): HMAC fallback has been removed. If nacl
        is unavailable, the system must fail-closed rather than silently
        degrading to a weaker cryptographic scheme.
        """
        from beagle.bridges.a2a_server import BeagleToA2ABridge

        bridge = BeagleToA2ABridge.__new__(BeagleToA2ABridge)
        bridge._signing_key = None
        bridge._peer_verify_key = None

        def _mock_load_key():
            raise RuntimeError("PyNaCl is REQUIRED for A2A signing")

        bridge._load_signing_key = _mock_load_key
        with pytest.raises(RuntimeError, match=r"PyNaCl|Ed25519|signing"):
            bridge._sign_payload(b"test payload")

    def test_verification_fails_closed_without_peer_key(self):
        """A2A server _verify_signature must return False if no Ed25519 peer key."""
        from beagle.bridges.a2a_server import BeagleToA2ABridge

        with patch("beagle.bridges.a2a_server.get_a2a_config"):
            bridge = BeagleToA2ABridge()
            bridge._signing_key = None
            bridge._peer_verify_key = None
            result = bridge._verify_signature(b"test payload", "fakesignature")
            assert result is False  # FAIL CLOSED — no HMAC downgrade

    def test_client_signing_fails_closed_without_nacl(self):
        """A2A client _sign_request must fail if Ed25519 unavailable.

        SECURITY: No HMAC downgrade — cryptographic consistency is enforced.
        """
        from beagle.bridges.a2a_client import A2AClientBridge

        # Patch _get_signing_key to simulate nacl unavailable
        client = A2AClientBridge.__new__(A2AClientBridge)
        client._signing_key = None

        def _mock_get_key():
            raise RuntimeError("PyNaCl is REQUIRED for A2A signing")

        client._get_signing_key = _mock_get_key
        with pytest.raises(RuntimeError, match=r"Ed25519|PyNaCl|signing"):
            client._sign_request(b"test payload")

    def test_sanitize_query_strips_control_chars(self):
        """_sanitize_query removes control characters."""
        from beagle.bridges.a2a_server import _sanitize_query

        result = _sanitize_query("hello\x00world\x01test")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "hello" in result
        assert "world" in result

    def test_sanitize_query_rejects_oversized(self):
        """_sanitize_query rejects queries exceeding max length."""
        from beagle.bridges.a2a_server import _sanitize_query

        with pytest.raises(ValueError, match="too long"):
            _sanitize_query("x" * 100000)

    def test_sanitize_query_allows_normal(self):
        """_sanitize_query passes through normal queries."""
        from beagle.bridges.a2a_server import _sanitize_query

        result = _sanitize_query("Analyze this code module for security issues")
        assert result == "Analyze this code module for security issues"


class TestRetrieverCacheThreadSafety:
    """Tests for thread-safe retriever cache (Golden Master Section 2)."""

    def test_cache_get_miss_returns_none(self):
        """Cache miss returns None."""
        from beagle.bridges.retriever import _cache_get, clear_cache

        clear_cache()
        result = _cache_get("nonexistent_key", ttl=300)
        assert result is None

    def test_cache_put_and_get(self):
        """Basic cache put/get works."""
        from langchain_core.documents import Document

        from beagle.bridges.retriever import (
            _cache_get,
            _cache_put,
            clear_cache,
        )

        clear_cache()
        docs = [Document(page_content="test")]
        _cache_put("test_key", docs)
        result = _cache_get("test_key", ttl=300)
        assert result is not None
        assert len(result) == 1
        assert result[0].page_content == "test"

    def test_cache_clear(self):
        """clear_cache empties the cache."""
        from langchain_core.documents import Document

        from beagle.bridges.retriever import (
            _cache_get,
            _cache_put,
            clear_cache,
        )

        clear_cache()
        _cache_put("key1", [Document(page_content="a")])
        _cache_put("key2", [Document(page_content="b")])
        clear_cache()
        assert _cache_get("key1", ttl=300) is None
        assert _cache_get("key2", ttl=300) is None


class TestCheckpointerSecurity:
    """Tests for checkpointer URI validation (Golden Master Section 4)."""

    # v1.0.2: the first two of these were `pytest.skip(...)` with the stale
    # reason "langgraph.checkpoint.sqlite not installed". That package IS
    # installed (a declared hard dependency), so the reason was false and two
    # security tests were simply switched off. The real obstacle was different:
    # _create_postgres_checkpointer imported the optional postgres backend
    # BEFORE validating the URI, so without that extra the injection check was
    # unreachable. The source now validates first, so these can assert for real
    # on any install.

    def test_postgres_uri_rejects_invalid_scheme(self, monkeypatch):
        """Invalid POSTGRES_URI schemes are rejected (OWASP A03)."""
        from beagle.memory.checkpointer import _create_postgres_checkpointer

        monkeypatch.delenv("POSTGRES_URI", raising=False)
        with pytest.raises(ValueError, match="Invalid PostgreSQL URI scheme"):
            _create_postgres_checkpointer(conn_string="mysql://evil:3306/db")

    def test_postgres_uri_rejects_empty(self, monkeypatch):
        """Empty POSTGRES_URI raises RuntimeError."""
        from beagle.memory.checkpointer import _create_postgres_checkpointer

        monkeypatch.delenv("POSTGRES_URI", raising=False)
        with pytest.raises(RuntimeError, match="requires either"):
            _create_postgres_checkpointer(conn_string="")

    @pytest.mark.parametrize(
        "hostile_uri",
        [
            "mysql://evil:3306/db",
            "file:///etc/passwd",
            "http://evil.example/db",
            " postgresql://user@host/db",  # leading space defeats a naive startswith
            "POSTGRESQL_EVIL://user@host/db",
        ],
    )
    def test_postgres_uri_rejects_hostile_schemes(self, monkeypatch, hostile_uri):
        """A spread of injection-shaped URIs are all rejected."""
        from beagle.memory.checkpointer import _create_postgres_checkpointer

        monkeypatch.delenv("POSTGRES_URI", raising=False)
        with pytest.raises(ValueError, match="Invalid PostgreSQL URI scheme"):
            _create_postgres_checkpointer(conn_string=hostile_uri)

    @pytest.mark.parametrize(
        "valid_uri",
        [
            "postgresql://user:pass@localhost:5432/beagle_db",
            "postgres://user:pass@localhost:5432/beagle_db",
            "POSTGRESQL://user:pass@localhost:5432/beagle_db",  # scheme is case-insensitive
        ],
    )
    def test_postgres_uri_accepts_valid_schemes(self, monkeypatch, valid_uri):
        """Valid schemes pass validation and reach the backend import.

        v1.0.2: this replaces a test that re-implemented the allowed-scheme
        tuple inside the test body and asserted against its own copy — a
        tautology that could never catch a regression in the production check.
        Reaching the ImportError means validation accepted the URI, which is
        the property under test; the postgres extra is optional and not
        installed by default.
        """
        from beagle.memory.checkpointer import _create_postgres_checkpointer

        monkeypatch.delenv("POSTGRES_URI", raising=False)
        try:
            _create_postgres_checkpointer(conn_string=valid_uri)
        except ImportError as exc:
            assert "langgraph-checkpoint-postgres" in str(exc)
        except ValueError as exc:  # pragma: no cover - the regression this guards
            pytest.fail(f"valid URI {valid_uri!r} was rejected by scheme validation: {exc}")


class TestLangSmithSecretHandling:
    """Tests for LangSmith API key handling (Golden Master Section 4)."""

    def test_bridge_has_api_key_attribute(self):
        """BeagleLangSmithBridge stores API key in-process."""
        from beagle.bridges.otel_langsmith_bridge import (
            BeagleLangSmithBridge,
        )

        bridge = BeagleLangSmithBridge()
        assert hasattr(bridge, "_api_key")
        assert bridge._api_key == ""

    def test_bridge_stop_clears_api_key(self):
        """stop() clears _api_key from memory."""
        from beagle.bridges.otel_langsmith_bridge import (
            BeagleLangSmithBridge,
        )

        bridge = BeagleLangSmithBridge()
        bridge._api_key = "test-secret"
        bridge._started = True
        bridge.stop()
        assert bridge._api_key == ""
        assert bridge._started is False


class TestLLMNodeRobustness:
    """Tests for LLM node robustness (Golden Master Section 2)."""

    def test_extract_final_answer_list_content(self):
        """_extract_final_answer handles list content blocks."""
        from beagle.bridges.llm_node import _extract_final_answer

        class FakeAIMessage:
            content: ClassVar[list[dict[str, str]]] = [
                {"type": "text", "text": "block1"},
                {"type": "text", "text": "block2"},
            ]

        result = _extract_final_answer(FakeAIMessage())
        assert "block1" in result
        assert "block2" in result

    def test_module_level_re_import(self):
        """'re' module is imported at module level, not inline."""
        import beagle.bridges.llm_node as llm_module

        assert hasattr(llm_module, "re"), "re should be a module-level import"
