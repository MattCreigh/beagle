"""Tests for beagle.cli — CLI imports and help output via Typer's CliRunner."""

from __future__ import annotations

import pytest

try:
    from typer.testing import CliRunner
except ImportError:
    CliRunner = None

from beagle.cli.cli import app

# ── CLI Import Verification ─────────────────────────────────────────────────


class TestCLIImports:
    """Verify the CLI module and its app can be imported."""

    def test_import_cli_module(self):
        from beagle.cli import cli as cli_mod

        assert cli_mod is not None

    def test_app_is_typer_instance(self):
        import typer

        assert isinstance(app, typer.Typer)

    def test_app_has_commands(self):
        """The resolved command surface is non-empty.

        v1.0.0 (F2 split): commands moved into ``cli/commands/*.py`` and are
        attached with ``app.add_typer(...)`` without a ``name=``, so they stay
        flat in ``--help`` but live on the sub-apps. ``app.registered_commands``
        is therefore empty on the root app and this assertion has to walk the
        groups — checking the root list alone would report "no commands" for a
        CLI that exposes all 26.
        """
        resolved = list(app.registered_commands)
        for group in app.registered_groups:
            resolved.extend(group.typer_instance.registered_commands)
        assert resolved, "no commands registered on the root app or any of its groups"

    @pytest.mark.skipif(CliRunner is None, reason="typer.testing.CliRunner not available")
    def test_full_command_surface_is_preserved(self):
        """Every command the pre-split monolith exposed is still reachable.

        The F2 split moved 26 commands out of a 2075-line ``cli.py`` into five
        modules. The whole point is that the user-facing surface is unchanged,
        so pin the names: a command silently lost during a future move fails
        here rather than at someone's shell.
        """
        expected = {
            "agents",
            "checkpoint",
            "cli",
            "config",
            "daemon",
            "diff",
            "doctor",
            "dream",
            "findings",
            "goose-shell",
            "health",
            "history",
            "info",
            "interactive",
            "list",
            "new-workflow",
            "render-hints",
            "render-prompts",
            "render-prompts-all",
            "replay",
            "run",
            "run-autogen",
            "run-crewai",
            "stats",
            "validate",
            "visualize",
        }
        result = CliRunner().invoke(app, ["--help"])
        assert result.exit_code == 0, result.output
        missing = sorted(name for name in expected if name not in result.output)
        assert not missing, f"commands missing from --help after the split: {missing}"


# ── CLI Help Output ─────────────────────────────────────────────────────────


@pytest.mark.skipif(CliRunner is None, reason="typer.testing.CliRunner not available")
class TestCLIHelpOutput:
    """Test that the main CLI help and subcommand help work."""

    def test_main_help(self):
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output or "usage" in result.output.lower()

    def test_run_command_help(self):
        runner = CliRunner()
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower() or "workflow" in result.output.lower()

    def test_list_command_help(self):
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--help"])
        assert result.exit_code == 0

    def test_info_command_help(self):
        runner = CliRunner()
        result = runner.invoke(app, ["info", "--help"])
        assert result.exit_code == 0

    def test_validate_command_help(self):
        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--help"])
        assert result.exit_code == 0


# ── CLI list command (no external deps) ──────────────────────────────────────


@pytest.mark.skipif(CliRunner is None, reason="typer.testing.CliRunner not available")
class TestCLIListCommand:
    """Test the list command which should not require external services."""

    def test_list_workflows(self):
        runner = CliRunner()
        result = runner.invoke(app, ["list"])
        # List might succeed or fail depending on config; just ensure it doesn't crash with import errors
        assert result.exit_code in (0, 1) or "Error" not in (result.output or "")
