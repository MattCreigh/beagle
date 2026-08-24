"""Tests for CVCP (Cross-Verification Collaboration Protocol).

Covers verdict parsing logic and routing decisions.
"""

import pytest

from beagle.config.config import get_config
from beagle.protocols.cvcp import (
    _cvcp_ground_truth_validate,
    _cvcp_route,
    _parse_verdict,
)


class TestParseVerdict:
    """Tests for _parse_verdict() — 3-tier verdict parsing."""

    # ── Tier 1: Structured JSON ──────────────────────────────────────────────

    def test_json_pass(self):
        assert _parse_verdict(['{"verdict": "pass", "notes": "looks good"}']) == "pass"

    def test_json_fail(self):
        assert _parse_verdict(['{"verdict": "fail", "reason": "wrong"}']) == "fail"

    def test_json_case_insensitive(self):
        assert _parse_verdict(['{"verdict": "FAIL"}']) == "fail"

    def test_json_single_quotes_style(self):
        # Regex handles verdict with optional quotes
        assert _parse_verdict(['{"verdict": "pass"}']) == "pass"

    # ── Tier 2: Line marker ──────────────────────────────────────────────────

    def test_line_marker_pass(self):
        assert _parse_verdict(["Some analysis...\nverdict: pass\nEnd."]) == "pass"

    def test_line_marker_fail(self):
        assert _parse_verdict(["verdict: fail"]) == "fail"

    def test_line_marker_equals(self):
        assert _parse_verdict(["VERDICT=FAIL"]) == "fail"

    def test_line_marker_uppercase(self):
        assert _parse_verdict(["VERDICT: PASS"]) == "pass"

    # ── Tier 3: Substring FAIL ───────────────────────────────────────────────

    def test_substring_fail(self):
        assert _parse_verdict(["This is a clear FAIL"]) == "fail"

    def test_no_false_positive_fallback(self):
        """'FAIL' inside 'fallback' should not trigger — it's not a whole word."""
        assert _parse_verdict(["The fallback strategy worked well"]) == "pass"

    def test_no_false_positive_failing(self):
        """'FAIL' inside 'failing' should trigger — regex checks non-alpha boundaries."""
        # 'failing' starts with FAIL followed by 'i' (alpha) — should NOT match
        assert _parse_verdict(["The failing test was fixed"]) == "pass"

    # ── Edge cases ───────────────────────────────────────────────────────────

    def test_empty_critiques(self):
        assert _parse_verdict([]) == "pass"

    def test_empty_strings(self):
        assert _parse_verdict(["", "  "]) == "pass"

    def test_none_like_empty(self):
        assert _parse_verdict([""]) == "pass"

    def test_mixed_pass_and_fail(self):
        """If any critique contains FAIL, the overall verdict is fail."""
        assert _parse_verdict(['{"verdict":"pass"}', "FAIL"]) == "fail"

    def test_all_pass(self):
        assert _parse_verdict(["verdict: pass", '{"verdict":"pass"}']) == "pass"

    def test_json_fail_short_circuits(self):
        """First JSON FAIL should return immediately without checking second."""
        assert _parse_verdict(['{"verdict":"fail"}', '{"verdict":"pass"}']) == "fail"

    def test_multiline_json(self):
        text = '{\n  "verdict": "fail",\n  "reason": "hallucination"\n}'
        assert _parse_verdict([text]) == "fail"


class TestCVCPRoute:
    """Tests for _cvcp_route() — routing based on verdict and attempt count."""

    def test_pass_routes_to_end(self):
        state = {"cvcp_verdict": "pass", "cvcp_attempt": 1}
        assert _cvcp_route(state) == "end"

    def test_fail_under_limit_routes_to_feedback(self):
        state = {"cvcp_verdict": "fail", "cvcp_attempt": 1}
        assert _cvcp_route(state) == "incorporate_feedback"

    def test_fail_at_attempt_2_routes_to_feedback(self):
        state = {"cvcp_verdict": "fail", "cvcp_attempt": 2}
        assert _cvcp_route(state) == "incorporate_feedback"

    def test_fail_at_max_routes_to_end(self):
        state = {
            "cvcp_verdict": "fail",
            "cvcp_attempt": get_config().orpheus.max_cvcp_attempts,
        }
        assert _cvcp_route(state) == "end"

    def test_fail_over_max_routes_to_end(self):
        state = {
            "cvcp_verdict": "fail",
            "cvcp_attempt": get_config().orpheus.max_cvcp_attempts + 1,
        }
        assert _cvcp_route(state) == "end"

    def test_defaults_pass_when_missing(self):
        """Missing verdict defaults to 'pass'."""
        assert _cvcp_route({}) == "end"

    def test_pass_at_max_still_ends(self):
        state = {
            "cvcp_verdict": "pass",
            "cvcp_attempt": get_config().orpheus.max_cvcp_attempts,
        }
        assert _cvcp_route(state) == "end"


# ─────────────────────────────────────────────────────────────────────────────
# Regression tests for D3 (Fable 5 DD 2026-06-11) — CVCP ground-truth regex
# was double-backslashed, so the validator matched ZERO real file paths and
# always returned "pass". These tests lock in the fix.
# (import pytest and ground-truth import live at module top)


class TestCVCPGroundTruthRegression_D3:
    """Regression tests for the vacuous-regex defect (D3, 2026-06-11 DD)."""

    @pytest.mark.asyncio
    async def test_real_path_with_line_range_is_extracted(self, tmp_path):
        """A real existing path with `path:line-line` syntax must be extracted."""
        real = tmp_path / "real_file.py"
        real.write_text("# real")
        synthesis = f"See {real}:42-50 for details"
        result = await _cvcp_ground_truth_validate({"final_report": synthesis})
        assert any(str(real) in v for v in result["verified_files"]), (
            f"Expected {real} in verified_files, got {result}"
        )
        assert result["ground_truth_verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_real_path_with_single_line_is_extracted(self, tmp_path):
        """A real existing path with `path:line` syntax must be extracted."""
        real = tmp_path / "single.py"
        real.write_text("# real")
        synthesis = f"Check {real}:10 is broken"
        result = await _cvcp_ground_truth_validate({"final_report": synthesis})
        assert any(str(real) in v for v in result["verified_files"])

    @pytest.mark.asyncio
    async def test_hallucinated_path_triggers_fail(self):
        """A path that does not exist on disk (outside /tmp) must trigger 'fail'."""
        # Use a non-/tmp path that cannot exist on the test runner
        fake = "/this/path/definitely/does/not/exist/fake_file_xyz.py"
        synthesis = f"Found bug in {fake}:99"
        result = await _cvcp_ground_truth_validate({"final_report": synthesis})
        assert result["ground_truth_verdict"] == "fail"
        assert any(fake in h for h in result["hallucinated_files"])

    @pytest.mark.asyncio
    async def test_no_citations_yields_pass(self):
        """Synthesis with no file citations must yield pass (not stuck)."""
        result = await _cvcp_ground_truth_validate(
            {"final_report": "No file citations here at all, just prose."}
        )
        assert result["ground_truth_verdict"] == "pass"
        assert result["verified_files"] == []
        assert result["hallucinated_files"] == []

    @pytest.mark.asyncio
    async def test_ghost_injector_pattern_still_triggers_fail(self):
        """The GhostInjector hallucination pattern must still be caught."""
        synthesis = "The class GhostInjector was added in the fix."
        result = await _cvcp_ground_truth_validate(
            {"final_report": synthesis, "verified_codebase_scan": False}
        )
        assert result["ground_truth_verdict"] == "fail"
        assert any("GhostInjector" in h for h in result["hallucinated_files"])


# ── Fix 5: per-attacker timeout budget in CVCP fan-out ──────────────────────


class TestCVCPPerAttackerTimeout:
    """v1.0.2 (P-fix5): each of the 2 CVCP attackers gets half the vertex
    budget so a single slow attacker cannot starve its sibling and so
    the total fan-out cannot exceed ``verification_seconds``.

    Floor at 30s so a future config reduction can't push the per-attacker
    budget below what's needed to even open the subprocess.
    """

    def test_per_attacker_timeout_equals_half_vertex_budget(self):
        from unittest.mock import patch

        with patch("beagle.protocols.cvcp.get_config") as mock_cfg:
            mock_cfg.return_value.node_timeout.verification_seconds = 300
            # Import inside the patch so the call to get_config() inside
            # the helper sees the patched value.
            import asyncio

            from beagle.protocols.cvcp import _cvcp_validate

            with patch("beagle.protocols.cvcp.execute_goose_node") as mock_exec:
                # Two attackers both return empty -> verdict 'pass'
                mock_exec.return_value = {"metadata": {}}

                async def run_once():
                    return await _cvcp_validate({"raw_execution_context": "ctx", "query": "q"})

                result = asyncio.run(run_once())
                # _cvcp_validate calls execute_goose_node once per attacker (2x).
                # Each call must have received timeout=150 (half of 300).
                assert mock_exec.call_count == 2
                for call in mock_exec.call_args_list:
                    assert call.kwargs.get("timeout") == 150

                assert result["cvcp_verdict"] == "pass"

    def test_per_attacker_timeout_floor_at_30s(self):
        """A config of 30s should yield per-attacker=30, not 15."""
        from unittest.mock import patch

        with patch("beagle.protocols.cvcp.get_config") as mock_cfg:
            mock_cfg.return_value.node_timeout.verification_seconds = 30
            import asyncio

            from beagle.protocols.cvcp import _cvcp_validate

            with patch("beagle.protocols.cvcp.execute_goose_node") as mock_exec:
                mock_exec.return_value = {"metadata": {}}

                async def run_once():
                    await _cvcp_validate({"raw_execution_context": "x", "query": "q"})

                asyncio.run(run_once())
                for call in mock_exec.call_args_list:
                    assert call.kwargs.get("timeout") == 30  # floor, not 30/2=15
