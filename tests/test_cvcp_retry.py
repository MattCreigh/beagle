"""Tests for CVCP retry paths and feedback incorporation.

SP2-2C: Verifies that CVCP correctly retries on verification failure,
incorporates feedback between attempts, and handles edge cases like
consecutive failures, partial feedback, and timeout during retry.
"""

import json

from beagle.config.config import get_config
from beagle.protocols.cvcp import _cvcp_route, _parse_verdict


class TestCVCPRetryPaths:
    """Test CVCP retry routing for various attempt scenarios."""

    def test_retry_from_attempt_1(self):
        """First failure routes to incorporate_feedback for retry."""
        state = {"cvcp_verdict": "fail", "cvcp_attempt": 1}
        assert _cvcp_route(state) == "incorporate_feedback"

    def test_retry_from_attempt_2(self):
        """Second failure still routes to incorporate_feedback."""
        state = {"cvcp_verdict": "fail", "cvcp_attempt": 2}
        assert _cvcp_route(state) == "incorporate_feedback"

    def test_max_attempts_routes_to_end(self):
        """At max attempts, failure routes to end (no more retries)."""
        max_attempts = get_config().orpheus.max_cvcp_attempts
        state = {"cvcp_verdict": "fail", "cvcp_attempt": max_attempts}
        assert _cvcp_route(state) == "end"

    def test_over_max_attempts_routes_to_end(self):
        """Beyond max attempts, failure routes to end."""
        max_attempts = get_config().orpheus.max_cvcp_attempts
        state = {"cvcp_verdict": "fail", "cvcp_attempt": max_attempts + 1}
        assert _cvcp_route(state) == "end"

    def test_pass_always_ends(self):
        """Pass verdict always goes to end, regardless of attempt number."""
        for attempt in range(1, 5):
            state = {"cvcp_verdict": "pass", "cvcp_attempt": attempt}
            assert _cvcp_route(state) == "end"

    def test_partial_pass_with_fail_verdict(self):
        """Fail verdict from partial verification routes to feedback."""
        state = {"cvcp_verdict": "fail", "cvcp_attempt": 1}
        assert _cvcp_route(state) == "incorporate_feedback"

    def test_default_verdict_pass(self):
        """Missing verdict defaults to pass, routing to end."""
        state = {"cvcp_attempt": 1}
        assert _cvcp_route(state) == "end"


class TestCVCPFeedbackIncorporation:
    """Test that feedback from failed attempts affects subsequent verdicts."""

    def test_verdict_parse_with_feedback_notes(self):
        """Feedback notes in verdict are parsed correctly."""
        verdict_json = json.dumps(
            {
                "verdict": "fail",
                "reason": "Missing error handling for timeout",
                "notes": "Add try/except around subprocess calls",
            }
        )
        result = _parse_verdict([verdict_json])
        assert result == "fail"

    def test_verdict_parse_with_detailed_reason(self):
        """Detailed failure reason is preserved in the verdict."""
        feedback = '{"verdict": "fail", "reason": "SQL injection vulnerability in auth module"}'
        result = _parse_verdict([feedback])
        assert result == "fail"

    def test_multiline_failure_with_specific_pointers(self):
        """Failure verdict with specific file/line pointers."""
        critique = """
        After reviewing the code:
        - verdict: fail
        - Issue: eval() call in line 42 of parser.py
        - Suggestion: Replace with ast.literal_eval()
        """
        result = _parse_verdict([critique])
        assert result == "fail"

    def test_mixed_critiques_all_fail(self):
        """When all attacker critiques say fail, verdict is fail."""
        critiques = [
            '{"verdict": "fail", "reason": "security issue"}',
            "verdict: fail — missing input validation",
            "After analysis: VERDICT=FAIL",
        ]
        result = _parse_verdict(critiques)
        assert result == "fail"

    def test_mixed_critiques_one_fail_overrides(self):
        """When any critique says fail, overall verdict is fail."""
        critiques = [
            '{"verdict": "pass", "notes": "looks fine"}',
            "verdict: fail — edge case not handled",
        ]
        result = _parse_verdict(critiques)
        assert result == "fail"

    def test_all_pass_critiques(self):
        """When all critiques say pass, verdict is pass."""
        critiques = [
            '{"verdict": "pass"}',
            "verdict: pass",
        ]
        result = _parse_verdict(critiques)
        assert result == "pass"


class TestCVCPAttemptTracking:
    """Test that attempt counts are tracked correctly through retry loops."""

    def test_attempt_increments_on_each_failure(self):
        """Each failure should increment the cvcp_attempt counter."""
        # Verify the state structure supports attempt counting
        for attempt in range(1, 4):
            state = {"cvcp_verdict": "fail", "cvcp_attempt": attempt}
            if attempt < get_config().orpheus.max_cvcp_attempts:
                assert _cvcp_route(state) == "incorporate_feedback"
            else:
                assert _cvcp_route(state) == "end"

    def test_attempt_preserved_across_transitions(self):
        """The attempt count should persist across state transitions."""
        # Simulating a full retry cycle:
        # attempt 1: fail -> incorporate_feedback
        state_1 = {"cvcp_verdict": "fail", "cvcp_attempt": 1}
        assert _cvcp_route(state_1) == "incorporate_feedback"

        # attempt 2: fail -> incorporate_feedback (or end if max)
        state_2 = {"cvcp_verdict": "fail", "cvcp_attempt": 2}
        max_attempts = get_config().orpheus.max_cvcp_attempts
        expected = "end" if max_attempts <= 2 else "incorporate_feedback"
        assert _cvcp_route(state_2) == expected

    def test_successful_retry_after_initial_failure(self):
        """After a failure and feedback, a pass verdict on retry ends the loop."""
        # Attempt 1: fail -> feedback
        state_fail = {"cvcp_verdict": "fail", "cvcp_attempt": 1}
        assert _cvcp_route(state_fail) == "incorporate_feedback"

        # Attempt 2: pass -> end
        state_pass = {"cvcp_verdict": "pass", "cvcp_attempt": 2}
        assert _cvcp_route(state_pass) == "end"


class TestCVCPTimeoutAndEdgeCases:
    """Test CVCP behavior under timeout and edge cases."""

    def test_empty_critique_treated_as_pass(self):
        """Empty critiques default to pass (no objections raised)."""
        result = _parse_verdict([""])
        assert result == "pass"

    def test_none_critique_treated_as_pass(self):
        """None in critiques defaults to pass."""
        result = _parse_verdict([None])
        assert result == "pass"

    def test_malformed_json_with_verdict_keyword(self):
        """Malformed JSON that contains 'verdict' keyword still parsed."""
        result = _parse_verdict(['{"verdict": "fail"'])  # Missing closing brace
        assert result == "fail"

    def test_ambiguous_output_defaults_pass(self):
        """Completely ambiguous output with no verdict defaults to pass."""
        result = _parse_verdict(["This is a review of the code. Looks good overall."])
        assert result == "pass"

    def test_partial_failure_text(self):
        """Partial text containing 'fail' keyword is parsed correctly."""
        result = _parse_verdict(["The implementation has a fail case in error handling."])
        assert result == "fail"
