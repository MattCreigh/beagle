"""Coverage for the structured success_criteria evaluator."""

from __future__ import annotations

from beagle.core.orchestrator.success_criteria import evaluate


def test_free_text_criteria_are_informational():
    passed, failures = evaluate(
        criteria=["implementation_report is not empty"],
        state={"implementation_report": ""},
    )
    assert passed is True
    assert failures == []


def test_equals_predicate_fires():
    passed, failures = evaluate(
        criteria=[{"field": "verification_report.verdict", "equals": "PASS"}],
        state={"verification_report": {"verdict": "FAIL"}},
    )
    assert not passed
    assert "verification_report.verdict" in failures[0]


def test_equals_predicate_passes():
    passed, failures = evaluate(
        criteria=[{"field": "verification_report.verdict", "equals": "PASS"}],
        state={"verification_report": {"verdict": "PASS"}},
    )
    assert passed
    assert failures == []


def test_dotted_path_with_int():
    passed, _ = evaluate(
        criteria=[{"field": "verification_report.pytest.failed", "equals": 0}],
        state={"verification_report": {"pytest": {"failed": 3}}},
    )
    assert not passed


def test_contains_predicate():
    passed, _ = evaluate(
        criteria=[{"field": "cvcp_verdict", "contains": "PASS"}],
        state={"cvcp_verdict": "Overall verdict: PASS after 2 attempts"},
    )
    assert passed


def test_missing_field_is_failure():
    passed, _ = evaluate(
        criteria=[{"field": "nonexistent", "equals": "x"}],
        state={},
    )
    assert not passed


def test_json_embedded_in_string_is_parsed():
    """When an output_key holds a string containing a <final_answer> JSON
    block (the verification-gate agent's output shape), the dotted path
    should still resolve through it."""
    state = {
        "verification_report": '<final_answer>{"verdict": "PASS", "pytest": {"failed": 0}}</final_answer>',
    }
    passed, _ = evaluate(
        criteria=[
            {"field": "verification_report.verdict", "equals": "PASS"},
            {"field": "verification_report.pytest.failed", "equals": 0},
        ],
        state=state,
    )
    assert passed
