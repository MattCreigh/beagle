"""CLI interface for Goose Agentic Workflow (Beagle).

Provides commands for running, managing, and monitoring workflows.

v1.0.0 (F2 split): the command implementations live in
``cli/commands/{execution,workflows,runs,system,render}.py`` and are
registered flat on the root app below, so every command name, flag and
behaviour is unchanged from the pre-split monolith. (--help groups them by
module now; the old interleaved ordering is not reproducible once commands
live in separate modules.)
"""

from __future__ import annotations

import logging
import sys

try:
    import typer
except ImportError:
    print("Error: typer is required. Install with: pip install typer")
    sys.exit(1)

from .commands.checkpoint import checkpoint_app
from .commands.config import config_app
from .commands.coord import coord_app
from .commands.execution import execution_app
from .commands.render import render_app
from .commands.runs import runs_app
from .commands.slo import slo_app
from .commands.system import system_app
from .commands.workflows import workflows_app

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="goose-workflow",
    # v13.21: Was hardcoded "Beagle v13.7 - ..." which drifted from the actual
    # __version__ (13.19.5) for at least 12 minor releases. The package's
    # __version__ is the source of truth; we just label this as "Beagle" and
    # let the version command report the actual number.
    help="Beagle - Goose Agentic Workflow CLI",
    no_args_is_help=True,
    # v13.21.5: Enable `beagle --version` (delegates to the SSOT __version__).
    # When the user passes --version, typer prints "<app-name> <version>"
    # and exits. The version string is read from beagle.__version__
    # via the ``pretty_exceptions_show_locals=False`` + ``add_completion=False``
    # pattern below.
    add_completion=False,
)

# Command groups extracted from this module (F2 split). add_typer WITHOUT a
# name flattens each group's commands into the root namespace, so the CLI
# surface is byte-identical to the pre-split monolith.
app.add_typer(execution_app)
app.add_typer(workflows_app)
app.add_typer(runs_app)
app.add_typer(system_app)
app.add_typer(render_app)
app.add_typer(config_app, name="config")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(slo_app, name="slo")
app.add_typer(coord_app, name="coord")
# WP-6 (D-18): the frontends moved to beagle-plugin-pi / beagle-plugin-webui.
# Both commands now arrive through the beagle.frontends entry-point group and
# are mounted below by the plugin loader, after the built-ins.

# WP-3: frontend plugins are discovered through the ``beagle.frontends``
# entry-point group and mounted after the built-ins, so a plugin can never
# shadow a built-in command. Discovery never raises: a broken plugin is
# skipped with a warning and the CLI still starts.
from .plugin_loader import (
    mount_frontend_plugins,  # noqa: E402 — must stay below the add_typer calls: a plugin must never shadow a built-in
)

mount_frontend_plugins(app, lambda plugin_app, name: app.add_typer(plugin_app, name=name))


def _version_callback(value: bool) -> None:
    """Print the package version and exit when ``--version`` is passed."""
    if not value:
        return
    from beagle import __version__  # lazy: avoid top-of-file import cost

    typer.echo(f"beagle {__version__}")
    raise typer.Exit(code=0)


@app.callback()
def _main_options(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the Beagle package version and exit.",
    ),
) -> None:
    """Beagle CLI — global options.

    The `version` parameter is consumed by Typer's option-callback mechanism
    (the _version_callback above); its name generates the --version flag.
    The actual commands are registered as ``@app.command()`` below.
    This callback exists only to register the ``--version`` flag.
    """
    del version  # satisfy vulture — Typer injects via parameter name


def _bootstrap() -> None:
    """Non-fatal startup init shared by the CLI and the pi frontend path."""
    # Phase 4.3: Startup health check — log warnings, don't block
    try:
        from ..startup.health_check import run_startup_checks

        startup_results = run_startup_checks(include_optional=False)
        for r in startup_results:
            if r.status == "fail":
                logger.warning(
                    "Startup check [%s] FAILED: %s — %s",
                    r.name,
                    r.message,
                    r.fix_hint,
                )
    except (OSError, RuntimeError, ValueError, ImportError) as e:
        logger.debug(f"Startup health check skipped (non-fatal): {e}")

    # Auto-initialize Beagle on CLI start: syncs recipes→agents
    # so all agents are discoverable from the first command.
    try:
        from ..context.recipe_agent_bridge import on_beagle_init

        init_result = on_beagle_init()
        if init_result.get("added", 0) > 0:
            logger.info(
                f"Beagle init: {init_result['added']} agents registered, "
                f"{init_result.get('total_agents', 0)} total available"
            )
    except (OSError, RuntimeError, ValueError, ImportError) as e:
        logger.debug(f"Beagle init (non-fatal): {e}")


def main() -> None:
    """Main entry point — initializes Beagle and runs the CLI.

    With no subcommand, the default interactive frontend is launched (the
    vendored pi frontend when installed). Explicit subcommands (``beagle run``,
    …) dispatch to the normal CLI.
    """
    _bootstrap()

    # Bare ``beagle`` (no subcommand) launches the default frontend. typer's
    # ``no_args_is_help`` would otherwise print help; the frontend is the
    # intended out-of-the-box entry point.
    #
    # Resolved through the ``beagle.frontends`` entry-point group, never by
    # naming a frontend module: D-18 extracted the frontends to their own
    # distributions, so a hardcoded ``..frontends.pi.launcher`` import is a
    # dangling reference to a path that no longer exists. A missing frontend
    # falls through to the help surface rather than raising.
    if len(sys.argv) <= 1:
        from .plugin_loader import launch_frontend

        exit_code = launch_frontend("pi")
        if exit_code is not None:
            sys.exit(exit_code)

    app()


if __name__ == "__main__":
    main()
