"""Tests for Beagle output validation feedback loop.

Covers: ToolResult/ValidationResult dataclasses, ValidationRunner tool
discovery and output parsing, RegressionDetector finding conversion,
FeedbackLoop event emission order, format_feedback_prompt, and singletons.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from beagle.validation.analyzer import (
    Regression,
    RegressionDetector,
)
from beagle.validation.feedback import (
    FeedbackLoop,
    get_feedback_loop,
)
from beagle.validation.runner import (
    ToolResult,
    ValidationResult,
    ValidationRunner,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_tool_result(
    tool: str = "pytest",
    exit_code: int = 0,
    passed: bool = True,
    **overrides,
) -> ToolResult:
    defaults = {
        "tool": tool,
        "exit_code": exit_code,
        "passed": passed,
        "stdout": "",
        "stderr": "",
        "duration_seconds": 1.0,
        "summary": "all good",
        "findings_count": 0,
    }
    defaults.update(overrides)
    return ToolResult(**defaults)


def _make_validation_result(**overrides) -> ValidationResult:
    defaults = {
        "timestamp": time.time(),
        "workflow_id": "wf-test",
        "tool_results": (
            _make_tool_result("pytest"),
            _make_tool_result("ruff"),
        ),
        "total_findings": 0,
        "all_passed": True,
        "duration_seconds": 2.0,
        "files_checked": ("test.py",),
    }
    defaults.update(overrides)
    return ValidationResult(**defaults)


# ── ToolResult / ValidationResult ─────────────────────────────────────────


class TestToolResult:
    def test_create(self):
        tr = _make_tool_result()
        assert tr.tool == "pytest"
        assert tr.passed is True

    def test_frozen(self):
        tr = _make_tool_result()
        with pytest.raises(AttributeError):
            tr.tool = "ruff"  # type: ignore[misc]


class TestValidationResult:
    def test_create(self):
        vr = _make_validation_result()
        assert vr.all_passed is True
        assert len(vr.tool_results) == 2

    def test_frozen(self):
        vr = _make_validation_result()
        with pytest.raises(AttributeError):
            vr.all_passed = False  # type: ignore[misc]

    def test_with_failures(self):
        failed = _make_tool_result("pytest", exit_code=1, passed=False, findings_count=3)
        vr = _make_validation_result(
            tool_results=(failed,),
            all_passed=False,
            total_findings=3,
        )
        assert not vr.all_passed
        assert vr.total_findings == 3


# ── ValidationRunner output parsing ───────────────────────────────────────


class TestRunnerParsing:
    def test_parse_pytest_pass(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        stdout = "collected 42 items\n\n======= 42 passed in 1.5s ======="
        summary, count = runner._parse_pytest(stdout, "")
        assert "42 passed" in summary
        assert count == 0

    def test_parse_pytest_failures(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        stdout = "FAILED tests/test_a.py::test_x\n======= 3 failed, 39 passed in 2.0s ======="
        summary, count = runner._parse_pytest(stdout, "")
        assert "3 failed" in summary
        assert count == 3

    def test_parse_ruff_clean(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        stdout = "All checks passed!"
        _summary, count = runner._parse_ruff(stdout, "")
        assert count == 0

    def test_parse_ruff_errors(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        stdout = "file.py:1:1: E302 expected 2 blank lines\nFound 5 errors."
        _summary, count = runner._parse_ruff(stdout, "")
        assert count == 5

    def test_parse_mypy_clean(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        stdout = "Success: no issues found in 10 source files"
        _summary, count = runner._parse_mypy(stdout, "")
        assert count == 0

    def test_parse_mypy_errors(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        stdout = "file.py:10: error: Incompatible return value\nFound 2 errors in 1 file"
        _summary, count = runner._parse_mypy(stdout, "")
        assert count == 2


# ── ValidationRunner tool discovery ───────────────────────────────────────


class TestRunnerToolDiscovery:
    @pytest.mark.asyncio
    async def test_skips_missing_tools(self):
        runner = ValidationRunner.__new__(ValidationRunner)
        runner._workspace_root = None

        with patch("shutil.which", return_value=None):
            result = await runner.run_all(workflow_id="wf-1")
            # No tools found → empty results, all_passed=True
            assert result.all_passed is True
            assert len(result.tool_results) == 0


# ── Regression dataclass ─────────────────────────────────────────────────


class TestRegression:
    def test_create(self):
        r = Regression(
            category="test_failure",
            description="test_x now fails",
        )
        assert r.severity == "critical"
        assert r.previous_status == "passing"

    def test_frozen(self):
        r = Regression(category="lint_error", description="new error")
        with pytest.raises(AttributeError):
            r.category = "other"  # type: ignore[misc]


# ── RegressionDetector.to_findings ────────────────────────────────────────


class TestToFindings:
    def test_creates_findings_from_failures(self):
        detector = RegressionDetector()
        failed_pytest = _make_tool_result(
            "pytest",
            exit_code=1,
            passed=False,
            stdout="tests/test_a.py::test_x FAILED\n======= 1 failed in 0.5s =======",
            findings_count=1,
        )
        vr = _make_validation_result(
            tool_results=(failed_pytest,),
            all_passed=False,
            total_findings=1,
        )
        findings = detector.to_findings(vr)
        assert len(findings) >= 1

    def test_no_findings_on_clean_run(self):
        detector = RegressionDetector()
        vr = _make_validation_result()
        findings = detector.to_findings(vr)
        assert len(findings) == 0

    def test_severity_mapping(self):
        detector = RegressionDetector()
        failed = _make_tool_result(
            "ruff",
            exit_code=1,
            passed=False,
            stdout="file.py:1:1: E302 expected 2 blank lines\nFound 1 error.",
            findings_count=1,
        )
        vr = _make_validation_result(
            tool_results=(failed,),
            all_passed=False,
            total_findings=1,
        )
        findings = detector.to_findings(vr)
        if findings:
            assert findings[0].severity == "medium"


# ── FeedbackLoop event emission order ─────────────────────────────────────


class TestFeedbackLoopEvents:
    @pytest.mark.asyncio
    async def test_started_emitted_before_validation(self):
        """ValidationStarted must fire BEFORE tools run."""
        emission_order: list[str] = []

        loop = FeedbackLoop()

        original_emit_started = loop._emit_started

        def tracking_started(*args, **kwargs):
            emission_order.append("started")
            return original_emit_started(*args, **kwargs)

        async def mock_run_all(**kwargs):
            emission_order.append("run_all")
            return _make_validation_result()

        with (
            patch.object(loop, "_emit_started", side_effect=tracking_started),
            patch.object(loop._runner, "run_all", side_effect=mock_run_all),
            patch.object(loop, "_emit_completed"),
            patch.object(loop, "_persist_findings"),
            patch.object(loop._detector, "detect_regressions", return_value=[]),
        ):
            await loop.validate_and_feedback("wf-1")

        assert emission_order == ["started", "run_all"]

    @pytest.mark.asyncio
    async def test_completed_emitted_after_validation(self):
        """ValidationCompleted fires AFTER tools run."""
        loop = FeedbackLoop()

        with (
            patch.object(loop, "_emit_started"),
            patch.object(
                loop._runner,
                "run_all",
                new_callable=AsyncMock,
                return_value=_make_validation_result(),
            ),
            patch.object(loop, "_emit_completed") as mock_completed,
            patch.object(loop, "_persist_findings"),
            patch.object(loop._detector, "detect_regressions", return_value=[]),
        ):
            await loop.validate_and_feedback("wf-1")
            mock_completed.assert_called_once()


# ── FeedbackLoop.format_feedback_prompt ───────────────────────────────────


class TestFormatFeedbackPrompt:
    def test_includes_tool_failures(self):
        loop = FeedbackLoop()
        failed = _make_tool_result(
            "pytest",
            exit_code=1,
            passed=False,
            summary="3 failed, 39 passed",
            stdout="FAILED test_a.py::test_x\nFAILED test_b.py::test_y\n",
            findings_count=3,
        )
        result = _make_validation_result(
            tool_results=(failed,),
            all_passed=False,
            total_findings=3,
        )
        prompt = loop.format_feedback_prompt(result, [])
        assert "pytest" in prompt.lower()
        assert "3" in prompt

    def test_includes_regressions(self):
        loop = FeedbackLoop()
        result = _make_validation_result(all_passed=False, total_findings=1)
        regs = [
            Regression(
                category="test_failure",
                description="test_x regressed",
            )
        ]
        prompt = loop.format_feedback_prompt(result, regs)
        assert "regression" in prompt.lower()

    def test_empty_on_all_passed(self):
        loop = FeedbackLoop()
        result = _make_validation_result()
        prompt = loop.format_feedback_prompt(result, [])
        assert "passed" in prompt.lower() or prompt == ""


# ── Singleton ─────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_feedback_loop_same_instance(self):
        import beagle.validation.feedback as mod

        mod._feedback_loop = None
        f1 = get_feedback_loop()
        f2 = get_feedback_loop()
        assert f1 is f2
        mod._feedback_loop = None


# ── Validation events ─────────────────────────────────────────────────────


class TestValidationEvents:
    def test_validation_started(self):
        from beagle.events.events import ValidationStarted

        e = ValidationStarted(
            workflow_id="wf-1",
            tools=("pytest", "ruff"),
            files_count=5,
        )
        assert e.event_type == "validation.started"

    def test_validation_completed(self):
        from beagle.events.events import ValidationCompleted

        e = ValidationCompleted(
            workflow_id="wf-1",
            all_passed=False,
            total_findings=3,
            tool_summaries=("3 failed",),
            duration_seconds=2.5,
        )
        assert e.event_type == "validation.completed"
        assert not e.all_passed

    def test_regression_detected(self):
        from beagle.events.events import RegressionDetected

        e = RegressionDetected(
            workflow_id="wf-1",
            regression_count=2,
            categories=("test_failure",),
            description="tests regressed",
        )
        assert e.event_type == "validation.regression"
