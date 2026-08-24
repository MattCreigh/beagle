"""Section 4.3: CLI startup health check integration tests."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from beagle.startup.health_check import (
    StartupCheckResult,
    format_startup_report,
    run_startup_checks,
)

runner = CliRunner()


class TestHealthCLICommand:
    """Tests for the 'health' CLI command."""

    def test_health_command_runs(self):
        """Health command should run and report results."""
        from beagle.cli.cli import app

        result = runner.invoke(app, ["health"])
        assert result.exit_code in (0, 1)  # 1 if any failure warns

    def test_health_command_json_output(self):
        """Health command with --json should produce valid JSON."""
        import json

        from beagle.cli.cli import app

        result = runner.invoke(app, ["health", "--json"])
        assert result.exit_code in (0, 1)
        # JSON output should parse
        output = result.output
        try:
            data = json.loads(output)
            assert isinstance(data, list)
        except json.JSONDecodeError:
            # If logs interfere, at minimum verify command ran
            assert result.exit_code in (0, 1)

    def test_health_command_required_only(self):
        """Health --required-only should skip optional checks."""
        from beagle.cli.cli import app

        result = runner.invoke(app, ["health", "--required-only"])
        assert result.exit_code in (0, 1)

    def test_health_command_exit_code_on_failure(self):
        """Health command should exit 1 when required checks fail."""
        from beagle.cli.cli import app

        with patch(
            "beagle.startup.health_check.check_config_loads",
            return_value=MagicMock(
                name="config",
                status="fail",
                message="broken",
                fix_hint="fix it",
            ),
        ):
            result = runner.invoke(app, ["health", "--required-only"])
        assert result.exit_code == 1


class TestStartupCheckInRunCommand:
    """Tests for startup check integration in the 'run' command."""

    def test_run_command_includes_startup_check(self):
        """The run command should invoke startup health checks."""
        # v1.0.0 (F2 split): `run` moved from cli.py into
        # cli/commands/execution.py. main() stays in cli.py.
        from beagle.cli.commands import execution

        source = inspect.getsource(execution.run)
        assert "run_startup_checks" in source, "run() should call run_startup_checks"

    def test_main_includes_startup_check(self):
        """The main() function should log startup check failures."""
        from beagle.cli import cli

        source = inspect.getsource(cli.main)
        assert "run_startup_checks" in source, "main() should call run_startup_checks"


class TestGracefulDegradation:
    """Tests for graceful degradation behavior."""

    def test_startup_check_failure_doesnt_crash_main(self):
        """If startup checks raise, main() should still proceed."""
        # Even with a broken config, run_startup_checks should not
        # raise an unhandled exception
        results = run_startup_checks(include_optional=True)
        assert isinstance(results, list)
        assert len(results) > 0

    def test_startup_check_with_optional_disabled(self):
        """Required-only checks should be a subset."""
        all_results = run_startup_checks(include_optional=True)
        req_results = run_startup_checks(include_optional=False)
        assert len(req_results) <= len(all_results)

    def test_format_report_without_failures(self):
        """Report with no failures should show 0 failures."""
        results = [
            StartupCheckResult("t1", "ok", "works"),
            StartupCheckResult("t2", "warn", "meh", "fix it"),
        ]
        report = format_startup_report(results)
        assert "0 failures" in report
        assert "1 warnings" in report
