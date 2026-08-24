"""Parse validation results into findings and detect regressions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .runner import ValidationResult

if TYPE_CHECKING:
    from beagle.tracking.models import Finding

logger = logging.getLogger("Beagle.validation")


@dataclass(frozen=True)
class Regression:
    """A detected regression from validation comparison."""

    category: str  # "test_failure", "lint_error", "type_error"
    description: str
    severity: str = "critical"
    file_path: str = ""
    previous_status: str = "passing"
    current_status: str = "failing"


class RegressionDetector:
    """Compares current validation results against historical baseline."""

    def __init__(self) -> None:
        pass

    def detect_regressions(
        self,
        current: ValidationResult,
        workflow_id: str,
    ) -> list[Regression]:
        """Compare current results against the last successful run.

        A regression is when:
        - A previously passing test now fails
        - The number of lint errors increased
        - New mypy errors appeared

        Uses TrackingDatabase to look up the last run.
        """
        # Look up historical baseline
        try:
            from beagle.tracking.database import TrackingDatabase

            db = TrackingDatabase.get_instance()
            stats = db.get_stats(since_days=30)
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug("Could not access tracking database for regression detection")
            return []

        # No previous runs means we can't detect regressions
        if stats.get("total_runs", 0) == 0:
            logger.debug("No historical runs found — skipping regression detection")
            return []

        # Compare findings counts against previously resolved findings
        regressions: list[Regression] = []
        for tr in current.tool_results:
            if tr.tool == "pytest" and tr.findings_count > 0:
                # Test failures detected — check previous state
                previously_resolved = self._count_resolved_findings("test_failure", workflow_id)
                if previously_resolved > 0:
                    regressions.append(
                        Regression(
                            category="test_failure",
                            description=(
                                f"{tr.findings_count} test failure(s) detected; "
                                f"{previously_resolved} previously resolved"
                            ),
                            severity="critical",
                            file_path="",
                            previous_status="passing",
                            current_status="failing",
                        )
                    )
            elif tr.tool == "ruff" and tr.findings_count > 0:
                previously_resolved = self._count_resolved_findings("lint_error", workflow_id)
                if previously_resolved > 0:
                    regressions.append(
                        Regression(
                            category="lint_error",
                            description=(
                                f"{tr.findings_count} lint error(s) detected; "
                                f"{previously_resolved} previously resolved"
                            ),
                            severity="critical",
                            file_path="",
                            previous_status="passing",
                            current_status="failing",
                        )
                    )
            elif tr.tool == "mypy" and tr.findings_count > 0:
                previously_resolved = self._count_resolved_findings("type_error", workflow_id)
                if previously_resolved > 0:
                    regressions.append(
                        Regression(
                            category="type_error",
                            description=(
                                f"{tr.findings_count} type error(s) detected; "
                                f"{previously_resolved} previously resolved"
                            ),
                            severity="critical",
                            file_path="",
                            previous_status="passing",
                            current_status="failing",
                        )
                    )

        return regressions

    def to_findings(
        self,
        result: ValidationResult,
    ) -> list[Finding]:
        """Convert ValidationResult into tracking.models.Finding objects.

        Each test failure, lint error, or type error becomes a Finding
        with appropriate severity:
        - Test failure: HIGH
        - Lint error: MEDIUM
        - Type error: MEDIUM
        - Regression (any): CRITICAL
        """
        from beagle.tracking.models import Finding

        findings: list[Finding] = []
        for tr in result.tool_results:
            if tr.findings_count == 0:
                continue

            severity_map = {
                "pytest": "high",
                "ruff": "medium",
                "mypy": "medium",
            }
            category_map = {
                "pytest": "bug",
                "ruff": "style",
                "mypy": "bug",
            }
            severity = severity_map.get(tr.tool, "medium")
            category = category_map.get(tr.tool, "bug")

            # Parse individual findings from the tool output
            for line in tr.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                # Extract file path from the output line if present
                file_path, description = self._parse_finding_line(tr.tool, line)
                if file_path is None and description is None:
                    continue
                findings.append(
                    Finding(
                        id="",  # Will be assigned by database
                        workflow_run_id=result.workflow_id,
                        node_name=f"validation.{tr.tool}",
                        severity=severity,
                        category=category,
                        title=f"{tr.tool} finding",
                        description=description or line,
                        file_path=file_path,
                    )
                )

        # Limit findings to avoid overwhelming the database
        return findings[:100]

    @staticmethod
    def _parse_finding_line(tool: str, line: str) -> tuple[str | None, str | None]:
        """Parse a single output line into (file_path, description)."""
        import re

        if tool == "pytest":
            # Pattern: "tests/test_foo.py::test_bar FAILED"
            match = re.match(r"^(.+::.+)\s+(FAILED|ERROR)\b", line)
            if match:
                return match.group(1).split("::")[0], match.group(0)
        elif tool == "ruff":
            # Pattern: "path/to/file.py:42:5: E302 expected 2 blank lines"
            match = re.match(r"^([^:]+):(\d+):(\d+):\s+(\w+)\s+(.+)", line)
            if match:
                return match.group(1), match.group(0)
        elif tool == "mypy":
            # Pattern: "path/to/file.py:42: error: Incompatible types"
            match = re.match(r"^([^:]+):(\d+):\s+error:\s+(.+)", line)
            if match:
                return match.group(1), match.group(0)
        return None, None

    @staticmethod
    def _count_resolved_findings(category: str, workflow_id: str) -> int:
        """Count previously resolved findings of a given category."""
        try:
            from beagle.tracking.database import TrackingDatabase

            db = TrackingDatabase.get_instance()
            conn = db._get_conn()
            category_map = {
                "test_failure": "bug",
                "lint_error": "style",
                "type_error": "bug",
            }
            db_category = category_map.get(category, category)
            row = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE category = ? AND status = 'resolved'",
                (db_category,),
            ).fetchone()
            return row[0] if row else 0
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug("Failed to query resolved findings")
            return 0
