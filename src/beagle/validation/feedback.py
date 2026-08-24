"""Connect validation results to self-improvement workflow, closing the loop."""

from __future__ import annotations

import logging
import threading

from beagle.tracking.models import Finding

from .analyzer import Regression, RegressionDetector
from .runner import ValidationResult, ValidationRunner

logger = logging.getLogger("Beagle.validation")


class FeedbackLoop:
    """Connects validation results to self-improvement workflow.

    After a workflow produces code:
    1. Run validation (tests, lints, types)
    2. Detect regressions
    3. If failures found, emit events and store findings
    4. Optionally trigger self-improvement workflow to fix issues
    """

    def __init__(
        self,
        auto_fix: bool = False,
        max_fix_attempts: int = 3,
    ) -> None:
        self._runner = ValidationRunner()
        self._detector = RegressionDetector()
        self._fix_attempts = 0
        self._auto_fix = auto_fix
        self._max_fix_attempts = max_fix_attempts

    async def validate_and_feedback(
        self,
        workflow_id: str,
        changed_files: list[str] | None = None,
    ) -> ValidationResult:
        """Run the full validation feedback cycle.

        1. Run validation tools
        2. Convert to findings and persist
        3. Detect regressions
        4. Emit events (ValidationCompleted, RegressionDetected)
        5. Return results for caller to act on
        """
        # 1. Emit ValidationStarted before running tools
        self._emit_started(workflow_id, changed_files)

        # 2. Run validation
        result = await self._runner.run_all(
            workflow_id=workflow_id,
            changed_files=changed_files,
        )

        # 3. Persist findings
        findings = self._detector.to_findings(result)
        self._persist_findings(findings, workflow_id)

        # 4. Detect regressions
        regressions = self._detector.detect_regressions(result, workflow_id)

        # 5. Emit completion events
        self._emit_completed(result, regressions, workflow_id)

        # 5. Auto-fix if configured
        if self._auto_fix and not result.all_passed and self._fix_attempts < self._max_fix_attempts:
            self._fix_attempts += 1
            logger.info(
                "Auto-fix attempt %d/%d for workflow %s",
                self._fix_attempts,
                self._max_fix_attempts,
                workflow_id,
            )
            prompt = self.format_feedback_prompt(result, regressions)
            logger.info("Auto-fix prompt generated (%d chars)", len(prompt))
            # The actual self-improvement trigger is handled
            # by the caller who receives the result.

        return result

    def format_feedback_prompt(
        self,
        result: ValidationResult,
        regressions: list[Regression],
    ) -> str:
        """Format validation failures into a prompt for self-improvement.

        Returns a structured prompt string that can be passed as the
        `query` to the self-improvement workflow.
        """
        sections: list[str] = []

        # Header
        total_failures = result.total_findings
        if total_failures == 0:
            return "All validation passed — no fixes needed."

        parts: list[str] = []
        for tr in result.tool_results:
            if tr.findings_count > 0:
                parts.append(f"{tr.tool}: {tr.findings_count} issues")
        header = f"Fix {total_failures} validation issue(s)"
        if parts:
            header += " (" + ", ".join(parts) + ")"
        header += f" introduced in workflow {result.workflow_id}:"
        sections.append(header)

        # Tool details
        for tr in result.tool_results:
            if tr.findings_count == 0:
                continue
            lines = tr.stdout.strip().splitlines()
            # Deduplicate and limit
            seen: set[str] = set()
            for line in lines:
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                if len(seen) >= 20:
                    break
            for line in sorted(seen):
                sections.append(f" - {line}")

        # Regressions
        if regressions:
            sections.append("")
            sections.append("Regressions detected:")
            for reg in regressions:
                sections.append(f" - [{reg.severity.upper()}] {reg.category}: {reg.description}")

        return "\n".join(sections)

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _persist_findings(findings: list[Finding], workflow_id: str) -> None:
        """Persist findings to the tracking database."""
        try:
            from beagle.tracking.database import TrackingDatabase

            db = TrackingDatabase.get_instance()
            for finding in findings:
                db.insert_finding(finding)
            logger.info("Persisted %d findings for workflow %s", len(findings), workflow_id)
        except Exception:  # broad catch intentional
            # What: persistence to TrackingDatabase is best-effort. Why: a DB
            # failure must never block the validation gate. Recovery: log at
            # WARNING and continue; the findings are still in the in-memory
            # result that the caller received.
            logger.warning("Failed to persist findings", exc_info=True)

    @staticmethod
    def _emit_started(
        workflow_id: str,
        changed_files: list[str] | None,
    ) -> None:
        """Emit ValidationStarted event before tools run."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import (
                ValidationStarted,
            )

            bus = get_event_bus()
            bus.publish(
                ValidationStarted(
                    workflow_id=workflow_id,
                    tools=("pytest", "ruff", "mypy"),
                    files_count=len(changed_files) if changed_files else 0,
                )
            )
        except Exception:  # broad catch intentional
            # What: emit ValidationStarted event to the bus. Why: telemetry
            # must never block the validation gate. Recovery: log at DEBUG
            # and continue; the validation tools will still execute.
            logger.debug("Failed to emit ValidationStarted", exc_info=True)

    @staticmethod
    def _emit_completed(
        result: ValidationResult,
        regressions: list[Regression],
        workflow_id: str,
    ) -> None:
        """Emit ValidationCompleted and RegressionDetected events."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import (
                RegressionDetected,
                ValidationCompleted,
            )

            bus = get_event_bus()

            tool_summaries = tuple(tr.summary for tr in result.tool_results)
            bus.publish(
                ValidationCompleted(
                    workflow_id=workflow_id,
                    all_passed=result.all_passed,
                    total_findings=result.total_findings,
                    tool_summaries=tool_summaries,
                    duration_seconds=result.duration_seconds,
                )
            )

            if regressions:
                categories = tuple(r.category for r in regressions)
                desc = "; ".join(r.description for r in regressions)
                bus.publish(
                    RegressionDetected(
                        workflow_id=workflow_id,
                        regression_count=len(regressions),
                        categories=categories,
                        description=desc,
                    )
                )
        except Exception:  # broad catch intentional
            # What: emit ValidationCompleted + RegressionDetected events. Why:
            # an event-bus outage must never block the validation report.
            # Recovery: log at DEBUG; the in-memory result is still returned
            # to the caller.
            logger.debug("Failed to emit validation events", exc_info=True)


# ── Module-level singleton ──────────────────────────────────────────────────────

_feedback_loop: FeedbackLoop | None = None
_feedback_lock = threading.Lock()


def get_feedback_loop() -> FeedbackLoop:
    """Get the global feedback loop singleton, configured from config.toml."""
    global _feedback_loop
    with _feedback_lock:
        if _feedback_loop is None:
            # v0.3.0: Load config values instead of hardcoded defaults
            auto_fix = False
            max_fix_attempts = 3
            try:
                from beagle.config.config import get_config

                config = get_config()
                val_config = getattr(config, "validation", None)
                if val_config:
                    auto_fix = getattr(val_config, "auto_fix", False)
                    max_fix_attempts = getattr(val_config, "max_fix_attempts", 3)
            except (ImportError, OSError, AttributeError, KeyError, TypeError, ValueError) as exc:
                # Reading the [validation] section must not stop the feedback loop
                # from starting; the safe defaults declared above are used instead.
                logger.warning(
                    "Cannot read the [validation] configuration (%s); starting the "
                    "feedback loop with auto_fix=%s and max_fix_attempts=%s.",
                    exc,
                    auto_fix,
                    max_fix_attempts,
                )
            _feedback_loop = FeedbackLoop(
                auto_fix=auto_fix,
                max_fix_attempts=max_fix_attempts,
            )
    return _feedback_loop


async def run_validation(
    workflow_id: str = "",
    changed_files: list[str] | None = None,
) -> ValidationResult:
    """Convenience: run full validation cycle."""
    return await get_feedback_loop().validate_and_feedback(workflow_id, changed_files)
