"""Execute validation tools (pytest, ruff, mypy) and capture structured results."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.validation")


@dataclass(frozen=True)
class ToolResult:
    """Result from a single validation tool run."""

    tool: str  # "pytest", "ruff", "mypy"
    exit_code: int
    passed: bool
    stdout: str
    stderr: str
    duration_seconds: float
    summary: str  # e.g. "42 passed, 3 failed" or "Found 5 errors"
    findings_count: int  # Total issues found


@dataclass(frozen=True)
class ValidationResult:
    """Aggregated results from all validation tools."""

    timestamp: float
    workflow_id: str
    tool_results: tuple[ToolResult, ...]
    total_findings: int
    all_passed: bool
    duration_seconds: float
    files_checked: tuple[str, ...]  # Files that were validated

    @property
    def failures(self) -> tuple[str, ...]:
        """Human-readable messages for failed tools."""
        return tuple(f"{t.tool}: {t.summary}" for t in self.tool_results if not t.passed)

    @property
    def checks(self) -> tuple[dict[str, Any], ...]:
        """Structured summary of all checks run."""
        return tuple(
            {
                "tool": t.tool,
                "passed": t.passed,
                "exit_code": t.exit_code,
                "summary": t.summary,
                "findings_count": t.findings_count,
            }
            for t in self.tool_results
        )

    @property
    def coverage(self) -> float:
        """Code coverage fraction (1.0 if all passed, 0.0 otherwise)."""
        return 1.0 if self.all_passed else 0.0


class ValidationRunner:
    """Runs validation tools against the workspace."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        if workspace_root is None:
            from beagle.config.paths import get_workspace_root

            workspace_root = get_workspace_root()
        self._workspace_root = workspace_root

    async def run_all(
        self,
        workflow_id: str = "",
        changed_files: list[str] | None = None,
    ) -> ValidationResult:
        """Run all configured validation tools.

        If changed_files is provided, only validate those files.
        Otherwise validate the whole project.
        """
        start = time.monotonic()
        files_checked: tuple[str, ...] = ()
        if changed_files:
            files_checked = tuple(changed_files)

        # Run all tools concurrently
        tasks: list[asyncio.Task[ToolResult]] = []
        if shutil.which("pytest"):
            tasks.append(asyncio.create_task(self.run_pytest(test_paths=changed_files)))
        else:
            logger.info("pytest not found — skipping")

        if shutil.which("ruff"):
            tasks.append(asyncio.create_task(self.run_ruff(paths=changed_files)))
        else:
            logger.info("ruff not found — skipping")

        if shutil.which("mypy"):
            tasks.append(asyncio.create_task(self.run_mypy(paths=changed_files)))
        else:
            logger.info("mypy not found — skipping")

        if not tasks:
            logger.warning("No validation tools available")

        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_results: list[ToolResult] = []
        for r in results:
            if isinstance(r, ToolResult):
                tool_results.append(r)
            else:
                logger.error("Validation tool failed: %s", r)

        total_findings = sum(tr.findings_count for tr in tool_results)
        all_passed = all(tr.passed for tr in tool_results)
        duration = time.monotonic() - start

        return ValidationResult(
            timestamp=start,
            workflow_id=workflow_id,
            tool_results=tuple(tool_results),
            total_findings=total_findings,
            all_passed=all_passed,
            duration_seconds=duration,
            files_checked=files_checked,
        )

    async def run_pytest(
        self,
        test_paths: list[str] | None = None,
        timeout: int = 300,
    ) -> ToolResult:
        """Run pytest and capture results."""
        args: list[str] = ["--tb=short", "-q"]
        if test_paths:
            args.extend(test_paths)

        result = await self._run_tool("pytest", args, timeout)
        summary, count = self._parse_pytest(result.stdout, result.stderr)
        passed = result.exit_code == 0
        return ToolResult(
            tool="pytest",
            exit_code=result.exit_code,
            passed=passed,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration,
            summary=summary,
            findings_count=count,
        )

    async def run_ruff(
        self,
        paths: list[str] | None = None,
    ) -> ToolResult:
        """Run ruff check and capture results."""
        args: list[str] = ["check"]
        if paths:
            args.extend(paths)
        else:
            args.append(".")

        result = await self._run_tool("ruff", args, timeout=60)
        summary, count = self._parse_ruff(result.stdout, result.stderr)
        passed = result.exit_code == 0
        return ToolResult(
            tool="ruff",
            exit_code=result.exit_code,
            passed=passed,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration,
            summary=summary,
            findings_count=count,
        )

    async def run_mypy(
        self,
        paths: list[str] | None = None,
    ) -> ToolResult:
        """Run mypy and capture results."""
        args: list[str] = ["--no-error-summary"]
        if paths:
            args.extend(paths)
        else:
            args.append("beagle")

        result = await self._run_tool("mypy", args, timeout=120)
        summary, count = self._parse_mypy(result.stdout, result.stderr)
        passed = result.exit_code == 0
        return ToolResult(
            tool="mypy",
            exit_code=result.exit_code,
            passed=passed,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration,
            summary=summary,
            findings_count=count,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _run_tool(
        self,
        command: str,
        args: list[str],
        timeout: int,
    ) -> _SubprocessResult:
        """Run an external tool as a subprocess."""
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workspace_root),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            duration = time.monotonic() - start
            return _SubprocessResult(
                exit_code=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                duration=duration,
            )
        except TimeoutError:
            proc.kill()
            duration = time.monotonic() - start
            return _SubprocessResult(
                exit_code=-1,
                stdout="",
                stderr=f"Timeout after {timeout}s",
                duration=duration,
            )
        except FileNotFoundError:
            duration = time.monotonic() - start
            return _SubprocessResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command not found: {command}",
                duration=duration,
            )

    @staticmethod
    def _parse_pytest(stdout: str, stderr: str) -> tuple[str, int]:
        """Parse pytest output for summary line.

        Looks for the summary line in the last 5 lines of stdout,
        matching patterns like "42 passed, 3 failed".
        """
        lines = (stdout + "\n" + stderr).strip().splitlines()
        tail = lines[-5:] if len(lines) >= 5 else lines
        for line in reversed(tail):
            passed_match = re.search(r"(\d+) passed", line)
            if passed_match:
                failed_match = re.search(r"(\d+) failed", line)
                error_match = re.search(r"(\d+) error", line)
                _n_passed = int(passed_match.group(1))
                n_failed = int(failed_match.group(1)) if failed_match else 0
                n_errors = int(error_match.group(1)) if error_match else 0
                summary = line.strip()
                return summary, n_failed + n_errors
        return "pytest: no summary line found", 0

    @staticmethod
    def _parse_ruff(stdout: str, stderr: str) -> tuple[str, int]:
        """Parse ruff output for error count.

        Looks for "Found N error" in the last line.
        """
        lines = (stdout + "\n" + stderr).strip().splitlines()
        if not lines:
            return "ruff: no output", 0
        last_line = lines[-1]
        match = re.search(r"Found (\d+) error", last_line)
        if match:
            count = int(match.group(1))
            return last_line.strip(), count
        # If ruff reports clean or no output, zero errors
        if "All checks passed" in stdout or (not stdout.strip() and not stderr.strip()):
            return "ruff: clean", 0
        # Count lines of output as a fallback
        error_lines = [ln for ln in lines if ln.strip() and not ln.startswith("Found")]
        return last_line.strip(), len(error_lines)

    @staticmethod
    def _parse_mypy(stdout: str, stderr: str) -> tuple[str, int]:
        """Parse mypy output for error count.

        Looks for "Found N error" in the last line.
        """
        lines = (stdout + "\n" + stderr).strip().splitlines()
        for line in reversed(lines):
            match = re.search(r"Found (\d+) error", line)
            if match:
                count = int(match.group(1))
                return line.strip(), count
        return "mypy: no summary line found", 0


@dataclass
class _SubprocessResult:
    """Internal state for a subprocess result."""

    exit_code: int
    stdout: str
    stderr: str
    duration: float
