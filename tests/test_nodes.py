"""Tests for LangGraph node functions (Beagle v12.0)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from beagle.core.nodes import _make_prompt_builder, execute_goose_node


class TestMakePromptBuilder:
    """Tests for prompt builder creation."""

    def test_simple_substitution(self):
        builder = _make_prompt_builder("Research: {query}")
        result = builder({"query": "how does IPC work?"})
        assert "how does IPC work?" in result

    def test_multiple_substitutions(self):
        builder = _make_prompt_builder("Plan: {research_plan}\nQuery: {query}")
        state = {"query": "test", "research_plan": "step 1, step 2"}
        result = builder(state)
        assert "step 1, step 2" in result
        assert "test" in result

    def test_missing_vars_replaced_empty(self):
        builder = _make_prompt_builder("Data: {nonexistent}")
        result = builder({"query": "test"})
        assert "{nonexistent}" not in result

    def test_metadata_substitution(self):
        builder = _make_prompt_builder("Custom: {my_key}")
        state = {"query": "", "metadata": {"my_key": "my_value"}}
        result = builder(state)
        assert "my_value" in result

    def test_empty_state(self):
        builder = _make_prompt_builder("{query}")
        result = builder({})
        assert result == ""


class TestExecuteGooseNode:
    """Tests for execute_goose_node (mocked subprocess)."""

    @pytest.mark.asyncio
    async def test_returns_state_update_on_success(self):
        mock_answer = "The answer is 42"
        with patch(
            "beagle.core.nodes.execute_headless_goose",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = (
                mock_answer,
                f"<final_answer>{mock_answer}</final_answer>",
            )

            result = await execute_goose_node(
                state={"query": "test", "metadata": {}},
                skill_name="research-planner",
                prompt_builder=lambda s: "test prompt",
                output_key="research_plan",
            )

            assert "research_plan" in result
            assert result["research_plan"] == mock_answer
            assert "research-planner" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_returns_error_on_failure(self):
        with patch(
            "beagle.core.nodes.execute_headless_goose",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.side_effect = RuntimeError("subprocess crashed")

            result = await execute_goose_node(
                state={"query": "test", "metadata": {}},
                skill_name="research-planner",
                prompt_builder=lambda s: "test prompt",
                output_key="research_plan",
            )

            assert len(result["errors"]) > 0
            assert "crashed" in result["errors"][0]


# ── Fix 10: _make_prompt_builder warns on unknown vars ──────────────────────


class TestPromptBuilderUnknownVarWarning:
    """v13.12.5: verify that unknown {token}s emit a WARNING."""

    def test_unknown_var_emits_warning(self, caplog):
        """Pass a template with {undefined_var}, assert WARNING is emitted."""
        import logging

        caplog.set_level(logging.WARNING)
        builder = _make_prompt_builder("Hello {undefined_var}", node_name="test")
        builder({"query": "test"})
        assert "unresolved variable" in caplog.text.lower()
        assert "undefined_var" in caplog.text

    def test_known_var_no_warning(self, caplog):
        """Pass a template with only known vars, assert no WARNING."""
        import logging

        caplog.set_level(logging.WARNING)
        builder = _make_prompt_builder("Query: {query}", node_name="test")
        builder({"query": "test"})
        assert "unresolved variable" not in caplog.text.lower()


# ── Fix 11: execute_headless_goose(model=...) wired through ─────────────────


class TestExecuteHeadlessGooseModelPassthrough:
    """v13.12.5: model= param must reach run_goose as model_override=."""

    @pytest.mark.asyncio
    async def test_model_forwarded_to_run_goose(self):
        """Call execute_headless_goose(model='foo'), assert run_goose gets it."""
        from beagle.core import nodes as nodes_mod

        with patch(
            "beagle.utils.subprocess_pool.run_goose",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = ("answer", "raw")
            await nodes_mod.execute_headless_goose(
                prompt="test",
                system_directive="sys",
                node_name="test-node",
                model="qwen3.5:397b",
            )
            mock_run.assert_awaited_once()
            call_kwargs = mock_run.await_args.kwargs
            assert call_kwargs.get("model_override") == "qwen3.5:397b"


# ── Fix 4: structured error envelope ────────────────────────────────────────


class TestStructuredErrorEnvelope:
    """v1.0.2 (P-fix4): downstream consumers should be able to branch on
    the structured ``kind`` field instead of substring-matching the prose
    err_msg. The envelope must be JSON-serializable so it can flow through
    the MCP layer cleanly."""

    def test_envelope_has_required_keys(self):
        from beagle.core.nodes import _structured_error

        env = _structured_error(
            "fact-checker", "timeout", "fact-checker: Timeout after 150s", seconds=150
        )
        assert env["skill"] == "fact-checker"
        assert env["kind"] == "timeout"
        assert "Timeout" in env["message"]
        assert env["retryable"] is True
        assert env["details"]["seconds"] == 150

    def test_retryable_kinds(self):
        from beagle.core.nodes import _structured_error

        # transient
        assert _structured_error("a", "timeout", "m")["retryable"] is True
        assert _structured_error("a", "system", "m")["retryable"] is True
        # permanent
        assert _structured_error("a", "validation", "m")["retryable"] is False
        assert _structured_error("a", "permission", "m")["retryable"] is False
        assert _structured_error("a", "not_found", "m")["retryable"] is False
        assert _structured_error("a", "runtime", "m")["retryable"] is False

    def test_envelope_is_json_serializable(self):
        import json

        from beagle.core.nodes import _structured_error

        env = _structured_error("x", "timeout", "m", seconds=30, cause="boom")
        # round-trip
        assert json.loads(json.dumps(env)) == env
