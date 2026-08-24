"""Tests for multi-model ensemble executor.

Covers judge JSON parsing, heuristic scoring, and ensemble execution.
"""

from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beagle.utils.ensemble import (
    EnsembleResult,
    ModelResponse,
    MultiModelEnsemble,
    _parse_judge_json,
    _score_response,
)

# ── _parse_judge_json tests ──────────────────────────────────────────────────


class TestParseJudgeJson:
    """Tests for _parse_judge_json() — JSON extraction from judge output."""

    def test_valid_json(self):
        result = _parse_judge_json('{"ratings": [], "combined_answer": "x"}')
        assert result is not None
        assert result["combined_answer"] == "x"

    def test_wrapped_json(self):
        raw = 'Here is my analysis:\n{"ratings": [{"model": "a"}], "verdict": "good"}\nDone.'
        result = _parse_judge_json(raw)
        assert result is not None
        assert result["verdict"] == "good"

    def test_no_json(self):
        assert _parse_judge_json("No JSON content here at all") is None

    def test_malformed_json(self):
        assert _parse_judge_json('{"broken": ') is None

    def test_empty_string(self):
        assert _parse_judge_json("") is None

    def test_nested_json(self):
        raw = '{"ratings": [{"model": "a", "score": 5}], "combined_answer": "yes"}'
        result = _parse_judge_json(raw)
        assert result is not None
        assert len(result["ratings"]) == 1


# ── _score_response tests ────────────────────────────────────────────────────


def _make_response(final_answer: str = "", **kwargs) -> ModelResponse:
    return ModelResponse(
        model=kwargs.get("model", "test-model"),
        final_answer=final_answer,
        raw_stdout=kwargs.get("raw_stdout", ""),
        latency_seconds=kwargs.get("latency_seconds", 1.0),
    )


class TestScoreResponse:
    """Tests for _score_response() — heuristic scoring."""

    def test_long_response_with_code(self):
        answer = "Here is the analysis.\n" * 10 + "\ndef foo():\n    pass\n"
        score = _score_response(_make_response(answer))
        assert score >= 2.0  # length bonus + code bonus

    def test_short_response(self):
        score = _score_response(_make_response("Short."))
        assert score == 0.0  # short penalty clamps to 0

    def test_structured_response(self):
        answer = "# Header\n\n- point one\n- point two\n" + "Details. " * 30
        score = _score_response(_make_response(answer))
        assert score >= 1.0  # structured formatting bonus

    def test_empty_response(self):
        score = _score_response(_make_response(""))
        assert score == 0.0

    def test_medium_unstructured(self):
        answer = "This is a decent response with enough content. " * 5
        score = _score_response(_make_response(answer))
        assert score >= 1.0  # length bonus

    def test_score_never_negative(self):
        score = _score_response(_make_response("x"))
        assert score >= 0.0


# ── MultiModelEnsemble.run() tests ──────────────────────────────────────────


class TestMultiModelEnsembleRun:
    """Tests for ensemble execution with mocked goose calls."""

    @pytest.mark.asyncio
    async def test_all_models_succeed(self):
        responses = [
            ("Answer from model A. " * 15, "raw A"),
            ("Answer from model B. " * 15, "raw B"),
        ]

        async def mock_run_goose(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            idx = 0 if "model_a" in node_name else 1
            return responses[idx]

        ensemble = MultiModelEnsemble(models=["model_a", "model_b"], timeout_per_model=10)

        with patch(
            "beagle.utils.ensemble.run_goose",
            side_effect=mock_run_goose,
        ):
            result = await ensemble.run("test prompt", "test system")

        assert isinstance(result, EnsembleResult)
        assert len(result.responses) == 2
        assert result.combined_response  # non-empty

    @pytest.mark.asyncio
    async def test_one_model_fails(self):
        call_count = 0

        async def mock_run_goose(
            prompt,
            system_directive,
            node_name,
            timeout,
            *,
            readonly=False,
            model_override=None,
            provider_override=None,
        ):
            nonlocal call_count
            call_count += 1
            if "model_a" in node_name:
                raise RuntimeError("model A crashed")
            return ("Good answer. " * 20, "raw")

        ensemble = MultiModelEnsemble(models=["model_a", "model_b"], timeout_per_model=10)

        with patch(
            "beagle.utils.ensemble.run_goose",
            side_effect=mock_run_goose,
        ):
            result = await ensemble.run("test prompt", "test system")

        # model_a returns empty final_answer on failure, model_b succeeds
        assert len(result.responses) == 2
        assert result.combined_response

    @pytest.mark.asyncio
    async def test_all_models_fail(self):
        async def mock_run_goose(**_kw):
            raise RuntimeError("all crashed")

        ensemble = MultiModelEnsemble(models=["model_a", "model_b"], timeout_per_model=10)

        with patch(
            "beagle.utils.ensemble.run_goose",
            side_effect=mock_run_goose,
        ):
            # _call_model catches exceptions and returns empty ModelResponse
            # but if ALL have empty final_answer, run() still returns (no RuntimeError
            # unless asyncio.gather returns exceptions, which _call_model prevents)
            result = await ensemble.run("test prompt", "test system")
            # Heuristic fallback selects best of the empty responses
            assert isinstance(result, EnsembleResult)

    @pytest.mark.asyncio
    async def test_judge_failure_falls_back_to_heuristic(self):
        async def mock_run_goose(
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
                return ("not valid json at all", "raw")
            return ("Good detailed answer with code: def foo(): pass. " * 5, "raw")

        ensemble = MultiModelEnsemble(models=["model_a"], timeout_per_model=10)

        with patch(
            "beagle.utils.ensemble.run_goose",
            side_effect=mock_run_goose,
        ):
            result = await ensemble.run("test prompt", "test system")

        assert "Heuristic" in result.judge_summary


# ── Merged from test_ensemble_extra.py (v1.0.0 consolidation) ────────
# Hypothesis strategy for ModelResponse
@st.composite
def model_response_strategy(draw):
    model = draw(st.text(min_size=1))
    final_answer = draw(st.text(min_size=0, max_size=500))
    raw_stdout = draw(st.text())
    latency = draw(st.floats(min_value=0.1, max_value=120.0))
    return ModelResponse(
        model=model,
        final_answer=final_answer,
        raw_stdout=raw_stdout,
        latency_seconds=latency,
    )


@given(model_response_strategy())
def test_score_response_properties(response):
    score = _score_response(response)
    assert score >= 0.0  # Should never be negative due to max(0.0, score)
    assert isinstance(score, float)


def test_score_response_heuristics():
    # Length > 200 => +1.0
    # specific code snippet => +1.5
    # structured formatting => +1.0
    # Length < 50 => -2.0
    # < 2 dots => -1.0

    r1 = ModelResponse("m", "short", "", 1.0)
    # length 5 < 50 => -2.0
    # dots 0 < 2 => -1.0
    # total -3.0 => max(0, -3.0) = 0.0
    assert _score_response(r1) == 0.0

    ans2 = "This is a longer response. " * 20 + "def my_func():\n    pass\n" + "- item 1\n- item 2"
    r2 = ModelResponse("m", ans2, "", 1.0)
    score2 = _score_response(r2)
    # len > 200 (+1), code (+1.5), structured (+1), no penalties
    # score should be 3.5
    assert score2 == 3.5


def test_parse_judge_json():
    # Valid json block
    raw = 'Here is my judgment:\n```json\n{"selected_index": 1, "all_scores": []}\n```'
    res = _parse_judge_json(raw)
    assert res == {"selected_index": 1, "all_scores": []}

    # Invalid json
    raw_inv = "No json here."
    assert _parse_judge_json(raw_inv) is None

    # Malformed json
    raw_mal = '{"selected_index": 1, "all_scores": ['
    assert _parse_judge_json(raw_mal) is None
