"""Tests for rehydration with tool routing state preservation.

v13.7.0: Ensures CompactionCheckpoint captures tool preferences
and the rehydration prompt includes tool routing state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from beagle.context.context_compaction_hook import (
    CompactionCheckpoint,
)
from beagle.context.post_compaction_rehydration import (
    build_rehydration_prompt,
)

# ── CompactionCheckpoint Serialization ───────────────────────────────────────


class TestCheckpointToolFields:
    """Test tool routing fields in CompactionCheckpoint."""

    def _make_checkpoint(self, **overrides) -> CompactionCheckpoint:
        defaults = {
            "timestamp": datetime.now(UTC),
            "current_task": "test task",
            "iteration": 3,
            "total_iterations": 10,
        }
        defaults.update(overrides)
        return CompactionCheckpoint(**defaults)

    def test_default_tool_fields_are_empty(self):
        cp = self._make_checkpoint()
        assert cp.tool_preferences == {}
        assert cp.model_overrides == {}
        assert cp.fallback_directives == []
        assert cp.tool_failure_history == []

    def test_tool_preferences_roundtrip(self):
        prefs = {"fetch_context": "langchain_tool", "analyze": "langchain_llm"}
        cp = self._make_checkpoint(tool_preferences=prefs)

        json_str = cp.to_json()
        restored = CompactionCheckpoint.from_json(json_str)

        assert restored.tool_preferences == prefs

    def test_model_overrides_roundtrip(self):
        overrides = {"planner": "glm-5.1:cloud", "coder": "qwen3.5:397b"}
        cp = self._make_checkpoint(model_overrides=overrides)

        json_str = cp.to_json()
        restored = CompactionCheckpoint.from_json(json_str)

        assert restored.model_overrides == overrides

    def test_fallback_directives_roundtrip(self):
        directives = ["glm-5.1:cloud", "qwen3.5:397b", "minimax-m2.7"]
        cp = self._make_checkpoint(fallback_directives=directives)

        json_str = cp.to_json()
        restored = CompactionCheckpoint.from_json(json_str)

        assert restored.fallback_directives == directives

    def test_tool_failure_history_roundtrip(self):
        failures = [
            {"tool_name": "sql_tool", "error": "connection refused", "category": "tool_error"},
            {"tool_name": "web_fetch", "error": "timeout", "category": "timeout"},
        ]
        cp = self._make_checkpoint(tool_failure_history=failures)

        json_str = cp.to_json()
        restored = CompactionCheckpoint.from_json(json_str)

        assert len(restored.tool_failure_history) == 2
        assert restored.tool_failure_history[0]["tool_name"] == "sql_tool"

    def test_backward_compatible_deserialization(self):
        """Old checkpoints without tool fields should deserialize with defaults."""
        old_json = json.dumps(
            {
                "timestamp": "2026-04-20T10:00:00+00:00",
                "current_task": "old task",
                "iteration": 1,
                "total_iterations": 5,
            }
        )
        cp = CompactionCheckpoint.from_json(old_json)
        assert cp.tool_preferences == {}
        assert cp.model_overrides == {}
        assert cp.fallback_directives == []
        assert cp.tool_failure_history == []


# ── Rehydration Prompt Tool State ────────────────────────────────────────────


class TestRehydrationToolState:
    """Test that rehydration prompt includes tool routing state."""

    def _make_checkpoint(self, **overrides) -> CompactionCheckpoint:
        defaults = {
            "timestamp": datetime.now(UTC),
            "current_task": "audit codebase",
            "iteration": 5,
            "total_iterations": 10,
            "session_id": "wf-test-123",
        }
        defaults.update(overrides)
        return CompactionCheckpoint(**defaults)

    def test_rehydration_includes_tool_routing_state(self):
        cp = self._make_checkpoint(
            tool_preferences={"fetch": "langchain_tool"},
            model_overrides={"planner": "glm-5.1:cloud"},
            tool_failure_history=[
                {"tool_name": "sql_tool", "error": "timeout", "category": "timeout"}
            ],
        )
        prompt = build_rehydration_prompt(checkpoint=cp)

        assert "<tool_routing_state>" in prompt
        assert "fetch: executor=langchain_tool" in prompt
        assert "planner: model=glm-5.1:cloud" in prompt
        assert "sql_tool: timeout" in prompt

    def test_rehydration_omits_tool_state_when_empty(self):
        cp = self._make_checkpoint()
        prompt = build_rehydration_prompt(checkpoint=cp)

        assert "<tool_routing_state>" not in prompt

    def test_rehydration_includes_system_identity(self):
        prompt = build_rehydration_prompt(query="test task")

        assert "<system_identity>" in prompt
        assert "Beagle" in prompt
        assert "NEVER stop execution" in prompt

    def test_rehydration_includes_task_context(self):
        cp = self._make_checkpoint()
        prompt = build_rehydration_prompt(checkpoint=cp)

        assert "<task_context>" in prompt
        assert "audit codebase" in prompt

    def test_rehydration_includes_resume_directive(self):
        cp = self._make_checkpoint()
        prompt = build_rehydration_prompt(checkpoint=cp)

        assert "<resume_directive>" in prompt
        assert "Do NOT stop executing" in prompt

    def test_rehydration_without_checkpoint(self):
        prompt = build_rehydration_prompt(
            workflow_id="wf-fallback",
            query="fallback query",
        )
        assert "fallback query" in prompt
        assert "<system_identity>" in prompt

    def test_fallback_directives_in_rehydration(self):
        cp = self._make_checkpoint(
            fallback_directives=["glm-5.1:cloud", "qwen3.5:397b"],
            tool_preferences={"x": "langchain_tool"},  # Need at least one
        )
        prompt = build_rehydration_prompt(checkpoint=cp)

        assert "Fallback chain:" in prompt
        assert "glm-5.1:cloud" in prompt

    def test_failure_history_truncated_to_3(self):
        failures = [{"tool_name": f"tool_{i}", "error": f"err_{i}"} for i in range(10)]
        cp = self._make_checkpoint(tool_failure_history=failures)
        prompt = build_rehydration_prompt(checkpoint=cp)

        # Should only show last 3
        assert "tool_7" in prompt
        assert "tool_8" in prompt
        assert "tool_9" in prompt
        # First ones should not appear individually (only count)
        assert "Recent tool failures (10)" in prompt
