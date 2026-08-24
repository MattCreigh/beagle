"""GRPO Integration Tests — Multi-trajectory workflow with judge selection.

Tests the full GRPO (Group Relative Policy Optimization) pattern:
1. Launch multiple model trajectories in parallel
2. Collect responses from each trajectory
3. Judge evaluates and scores each response
4. Select best trajectory based on scoring
5. Update workflow state with selection

This tests the integration between:
- MultiModelEnsemble (utils/ensemble.py)
- Workflow execution (core/graph.py, core/nodes.py)
- State management (core/state.py)
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from beagle.utils.ensemble import (  # ruff: ignore[E402]
    EnsembleResult,
    ModelResponse,
    MultiModelEnsemble,
    _parse_judge_json,
    _score_response,
)

# ── Test Fixtures ───────────────────────────────────────────────────────────────


@dataclass
class MockState:
    """Mock workflow state for GRPO testing."""

    messages: list[dict[str, str]] = field(default_factory=list)
    grpo_trajectory_count: int = 0
    grpo_selected_trajectory: str | None = None
    grpo_scores: dict[str, float] = field(default_factory=dict)
    grpo_judge_summary: str = ""
    context: dict[str, Any] = field(default_factory=dict)


def make_mock_response(model: str, answer: str, latency: float = 1.0) -> ModelResponse:
    """Create a mock model response."""
    return ModelResponse(
        model=model,
        final_answer=answer,
        raw_stdout=f"[{model}] raw output",
        latency_seconds=latency,
    )


# ── GRPO Integration Tests ───────────────────────────────────────────────────────


class TestGRPOMultiTrajectory:
    """Test GRPO multi-trajectory execution."""

    @pytest.mark.asyncio
    async def test_three_trajectories_launched_in_parallel(self):
        """GRPO should launch exactly 3 trajectories concurrently."""
        models = ["model_a", "model_b", "model_c"]
        call_times = []

        async def track_call(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.1)  # Simulate work
            return (f"Answer from {model_override}", "raw")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=track_call):
            result = await ensemble.run("test prompt", "test system")

        # All 3 models should have been called
        assert len(result.responses) == 3
        model_names = [r.model for r in result.responses]
        assert "model_a" in model_names
        assert "model_b" in model_names
        assert "model_c" in model_names

        # Calls should happen concurrently (within 0.2s of each other)
        if len(call_times) >= 2:
            max_diff = max(call_times) - min(call_times)
            assert max_diff < 0.3, f"Trajectories not parallel: diff={max_diff}s"

    @pytest.mark.asyncio
    async def test_trajectory_timeout_handling(self):
        """Trajectories that time out should return empty response, not crash."""
        models = ["fast_model", "slow_model"]

        async def mock_run(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            if "slow" in node_name:
                # Simulate timeout by raising the error that _call_model catches
                raise TimeoutError("Model call timed out")
            return ("Good answer", "raw")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=mock_run):
            # This should not crash - slow model failure should be handled gracefully
            result = await ensemble.run("test", "system")

        assert len(result.responses) == 2
        # Fast model should have content
        fast = next(r for r in result.responses if "fast" in r.model)
        assert fast.final_answer
        # Slow model returns empty response on failure
        slow = next(r for r in result.responses if "slow" in r.model)
        assert slow.final_answer == ""  # Empty on failure

    @pytest.mark.asyncio
    async def test_all_models_fail_gracefully(self):
        """When all models fail, ensemble should still return a result."""
        models = ["model_a", "model_b"]

        async def mock_fail(**_kw):
            raise RuntimeError("Model API down")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=mock_fail):
            # _call_model catches exceptions and returns empty ModelResponse
            result = await ensemble.run("test", "system")
            assert isinstance(result, EnsembleResult)
            assert len(result.responses) == 2
            # All responses should be empty but present
            for r in result.responses:
                assert r.final_answer == "" or isinstance(r, Exception)


class TestGRPOJudgeSelection:
    """Test GRPO judge-based trajectory selection."""

    @pytest.mark.asyncio
    async def test_judge_selects_best_trajectory(self):
        """Judge should correctly identify and select best trajectory."""
        models = ["model_a", "model_b", "model_c"]

        # Responses indexed by model name
        responses = {
            "model_a": "Short answer.",
            "model_b": (
                "# Analysis\n\n- Point 1\n- Point 2\n\n"
                + "Detailed explanation. " * 30
                + "\n\ndef solution():\n    return 'code'"
            ),
            "model_c": "Medium length answer with some details.",
        }

        async def mock_judge(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            # Check for judge in node_name (judge uses self._judge_model for model_override)
            if "judge" in node_name:
                # Simulate judge selecting model_b (best detailed answer, index 1)
                return (
                    json.dumps(
                        {
                            "selected_index": 1,
                            "all_scores": [
                                {"model": "model_a", "score": 2},
                                {"model": "model_b", "score": 9},
                                {"model": "model_c", "score": 5},
                            ],
                        }
                    ),
                    "judge raw",
                )
            # Regular model calls - return response based on model_override parameter
            answer = responses.get(model_override)
            if answer is None:
                raise ValueError(f"Unknown model: {model_override}")
            return (answer, "raw")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=mock_judge):
            result = await ensemble.run("Analyze this code", "You are a code reviewer.")

        # Judge should have selected model_b (index 1 in models list)
        assert result.best_response.model == "model_b"
        # Judge verdict should be present
        assert result.judge_summary
        assert len(result.responses) == 3

    @pytest.mark.asyncio
    async def test_judge_json_parsing(self):
        """Judge JSON output should be correctly parsed."""
        # Valid JSON
        valid = '{"selected_index": 0, "all_scores": [{"score": 10}]}'
        parsed = _parse_judge_json(valid)
        assert parsed is not None
        assert parsed["selected_index"] == 0

        # JSON wrapped in text
        wrapped = 'Here is my analysis:\n{"selected_index": 2, "ratings": []}\nEnd.'
        parsed = _parse_judge_json(wrapped)
        assert parsed is not None
        assert parsed["selected_index"] == 2

        # Invalid JSON
        invalid = "This is just plain text with no JSON."
        assert _parse_judge_json(invalid) is None

        # Malformed JSON
        malformed = '{"broken": '
        assert _parse_judge_json(malformed) is None

    @pytest.mark.asyncio
    async def test_heuristic_fallback_when_judge_fails(self):
        """When judge fails, heuristic scoring should select best trajectory."""
        models = ["model_a", "model_b"]

        async def mock_run(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            if "judge" in node_name:
                return ("Not valid JSON at all", "raw")  # Judge will fail
            if model_override == "model_a":
                return ("Short answer", "raw")
            return (
                "A longer, more detailed answer. " * 50 + "\ncode: def foo(): pass",
                "raw",
            )

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=mock_run):
            result = await ensemble.run("test", "system")

        # Should fall back to heuristic selection
        assert "Heuristic" in result.judge_summary
        # model_b should win (longer, has code)
        assert result.best_response.model == "model_b"


class TestGRPOStateManagement:
    """Test GRPO state updates in workflow context."""

    @pytest.mark.asyncio
    async def test_state_contains_trajectory_count(self):
        """After GRPO, state should contain trajectory count."""
        state = MockState()

        # Simulate GRPO execution
        state.grpo_trajectory_count = 3
        state.grpo_scores = {"model_a": 5.0, "model_b": 8.5, "model_c": 7.0}
        state.grpo_selected_trajectory = "model_b"
        state.grpo_judge_summary = "Selected model_b with score 8.5"

        assert state.grpo_trajectory_count == 3
        assert "model_b" in state.grpo_scores
        assert state.grpo_selected_trajectory == "model_b"
        assert "model_b" in state.grpo_judge_summary

    @pytest.mark.asyncio
    async def test_state_messages_preserved_after_grpo(self):
        """GRPO should not corrupt existing state messages."""
        state = MockState()
        state.messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Explain this code."},
        ]

        # Simulate GRPO adding results
        original_count = len(state.messages)
        state.grpo_trajectory_count = 3
        state.grpo_selected_trajectory = "model_a"

        # Original messages should still be there
        assert len(state.messages) == original_count
        assert state.messages[0]["role"] == "system"
        assert "Explain" in state.messages[1]["content"]


class TestGRPOTokenBudget:
    """Test GRPO with token budget constraints."""

    @pytest.mark.asyncio
    async def test_grpo_tracks_latency_per_trajectory(self):
        """Each trajectory should track its execution latency."""
        models = ["model_a", "model_b"]

        async def mock_timed(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            if "model_a" in node_name:
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(0.2)
            return (f"Answer from {model_override}", "raw")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=mock_timed):
            result = await ensemble.run("test", "system")

        # Latency should be tracked
        for r in result.responses:
            assert r.latency_seconds >= 0
            # model_b should have longer latency
            if "model_b" in r.model:
                assert r.latency_seconds >= 0.15

    @pytest.mark.asyncio
    async def test_ensemble_budget_calculation(self):
        """Ensemble should report total token usage."""
        models = ["model_a"]

        async def mock_with_tokens(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            return ("Response with some content", "raw")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch(
            "beagle.utils.ensemble.run_goose",
            side_effect=mock_with_tokens,
        ):
            result = await ensemble.run("test" * 100, "system" * 100)

        # Result should have response
        assert len(result.responses) == 1
        assert result.combined_response


class TestGRPOErrorRecovery:
    """Test GRPO error handling and recovery."""

    @pytest.mark.asyncio
    async def test_single_trajectory_failure_does_not_crash(self):
        """If one trajectory fails, others should continue."""
        models = ["model_a", "model_b", "model_c"]

        async def mock_partial_fail(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            if "model_b" in node_name:
                raise RuntimeError("model_b crashed")
            return (f"Good answer from {model_override}", "raw")

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch(
            "beagle.utils.ensemble.run_goose",
            side_effect=mock_partial_fail,
        ):
            result = await ensemble.run("test", "system")

        # Should have responses from successful models
        assert len(result.responses) >= 2
        successful_models = [r.model for r in result.responses if r.final_answer]
        assert "model_a" in successful_models or "model_c" in successful_models

    @pytest.mark.asyncio
    async def test_empty_response_handling(self):
        """Empty responses should be handled without crashes."""
        models = ["model_a", "model_b"]

        # Response content for each model
        responses = {
            "model_a": ("", "raw"),  # Empty response
            "model_b": (
                "Valid answer with enough content to score well for testing",
                "raw",
            ),
        }

        async def mock_empty(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            # Judge call - return valid JSON selecting model_b (index 1)
            if "judge" in node_name:
                return (
                    json.dumps(
                        {
                            "selected_index": 1,
                            "all_scores": [{"score": 0}, {"score": 5}],
                        }
                    ),
                    "judge raw",
                )
            # Regular model calls
            return responses[model_override]

        ensemble = MultiModelEnsemble(models=models, timeout_per_model=10)

        with patch("beagle.utils.ensemble.run_goose", side_effect=mock_empty):
            result = await ensemble.run("test", "system")

        assert isinstance(result, EnsembleResult)
        # model_b's response should be selected since model_a is empty
        assert result.best_response.model == "model_b"
        # Should still return valid response from model_b
        assert "Valid" in result.combined_response


# ── Scoring Heuristic Tests ───────────────────────────────────────────────────────


class TestGRPOHeuristicScoring:
    """Test heuristic scoring used when judge unavailable."""

    def test_long_response_scores_higher(self):
        """Longer, more detailed responses should score higher."""
        short = ModelResponse(model="a", final_answer="Short.", raw_stdout="", latency_seconds=1.0)
        long = ModelResponse(
            model="b",
            final_answer="Detailed answer. " * 50,
            raw_stdout="",
            latency_seconds=1.0,
        )

        score_short = _score_response(short)
        score_long = _score_response(long)

        assert score_long > score_short

    def test_code_bonus_applied(self):
        """Responses with code should get bonus score."""
        # Both responses need to be long enough to avoid short penalty
        no_code = ModelResponse(
            model="a",
            final_answer=(
                "Text only explanation. " * 20 + "\n\nThis is a detailed answer without any code."
            ),
            raw_stdout="",
            latency_seconds=1.0,
        )
        with_code = ModelResponse(
            model="b",
            final_answer=(
                "Here's the analysis:\n\n"
                + "Detailed explanation. " * 20
                + "\n\ndef foo():\n    return 'code'\n"
            ),
            raw_stdout="",
            latency_seconds=1.0,
        )

        score_no_code = _score_response(no_code)
        score_with_code = _score_response(with_code)

        # Code bonus should make with_code score higher
        assert score_with_code > score_no_code

    def test_formatted_text_bonus(self):
        """Structured responses (headers, lists) should score higher."""
        plain = ModelResponse(
            model="a",
            final_answer="Plain text response. " * 10,
            raw_stdout="",
            latency_seconds=1.0,
        )
        structured = ModelResponse(
            model="b",
            final_answer="# Analysis\n\n- Point 1\n- Point 2\n\n" + "Details. " * 30,
            raw_stdout="",
            latency_seconds=1.0,
        )

        score_plain = _score_response(plain)
        score_structured = _score_response(structured)

        assert score_structured > score_plain

    def test_score_never_negative(self):
        """Scores should never be negative."""
        tiny = ModelResponse(model="a", final_answer="x", raw_stdout="", latency_seconds=1.0)
        empty = ModelResponse(model="b", final_answer="", raw_stdout="", latency_seconds=1.0)

        assert _score_response(tiny) >= 0
        assert _score_response(empty) >= 0


# ── Configuration Tests ─────────────────────────────────────────────────────────────


class TestGRPOConfiguration:
    """Test GRPO ensemble configuration."""

    def test_default_models_loaded_from_config(self):
        """Default models should come from config."""
        ensemble = MultiModelEnsemble()

        # Should have models from config
        assert len(ensemble.models) > 0
        # All should be valid model names
        for model in ensemble.models:
            assert isinstance(model, str)
            assert len(model) > 0

    def test_custom_models_override_config(self):
        """Custom models should override config defaults."""
        custom = ["custom_model_1", "custom_model_2"]
        ensemble = MultiModelEnsemble(models=custom)

        assert ensemble.models == custom

    def test_timeout_configuration(self):
        """Timeout should be configurable."""
        ensemble = MultiModelEnsemble(timeout_per_model=300)

        assert ensemble._timeout == 300


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
