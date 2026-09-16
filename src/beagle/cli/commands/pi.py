"""``pi`` — launch the vendored pi coding-agent frontend.

Beagle ships a prebuilt bundle of the ``earendil-works/pi`` TUI coding agent
as its default frontend. This command locates that bundle (in the wheel via
package-data, or in the repo source tree), spawns it, and — when requested —
starts Beagle's MCP server so pi can call Beagle workflows over MCP.

The bundle is self-contained: ``dist/bundle/cli.js`` has no external npm
dependencies, so it runs on any host with ``node`` (>=18).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

pi_app = typer.Typer()


_VENDOR_SUBPATH = Path("frontends") / "pi" / "vendor" / "pi-prebuild"


def _bundle_dir() -> Path:
    """Resolve the vendored pi bundle directory.

    The bundle ships as package-data, so it always lands next to the package
    module — in a source checkout and in an installed wheel alike. There is no
    second layout to search: the previous "source checkout" branch walked five
    parents to ``<repo>/beagle/frontends/...``, but that path does not exist and
    the first branch resolves in both cases, so the second was dead code that
    could never return (D-13).
    """
    bundle = Path(__file__).resolve().parent.parent.parent / _VENDOR_SUBPATH
    if (bundle / "dist" / "bundle" / "cli.js").is_file():
        return bundle
    typer.echo(
        "pi frontend bundle not found. Reinstall Beagle (the pi bundle ships in the wheel) "
        "or run from the source tree with vendor/pi-prebuild/dist present.",
        err=True,
    )
    raise typer.Exit(1)


def _mcp_server_script() -> Path | None:
    """Locate Beagle's MCP server entry point.

    Single resolver for the same reason as ``_bundle_dir``: the module lives at
    a fixed offset from this file, so the extra search branch was unreachable.
    """
    script = (
        Path(__file__).resolve().parent.parent.parent
        / "infrastructure"
        / "mcp_beagle_server.py"
    )
    return script if script.is_file() else None


@pi_app.command()
def pi(
    args: Annotated[
        list[str] | None,
        typer.Argument(help="Arguments forwarded to the pi CLI."),
    ] = None,
    mcp: Annotated[
        bool,
        typer.Option(
            "--mcp",
            help="Start Beagle's MCP server so pi can call Beagle workflows.",
        ),
    ] = False,
) -> None:
    """Launch the vendored pi coding agent frontend."""
    # D-12: the old signature was ``args: list[str] = typer.Argument(None)``,
    # which typer 0.26 rejects with "Value after * must be an iterable, not
    # NoneType" on a no-argument invocation. Declaring the default as None via
    # the annotated form makes a bare `beagle pi` valid.
    forwarded = list(args or [])
    node = shutil.which("node")
    if not node:
        # D-EXTRA-3: this message restated the Node.js floor as a literal
        # ">=18". Commit 625a03f established a single source for the floor —
        # engines.node of the vendored pi-prebuild manifest — for the exact
        # reason that three hardcoded copies had drifted apart. The launcher's
        # helper is the implementation; reuse it so both entry points quote the
        # same manifest value instead of two different literals.
        from beagle.frontends.pi.launcher import _required_node_version

        declared = _required_node_version()
        requirement = f" ({declared})" if declared else ""
        typer.echo(
            f"Node.js{requirement} is required to run the pi frontend. "
            "Install Node or add it to PATH.",
            err=True,
        )
        raise typer.Exit(1)

    bundle = _bundle_dir() / "dist" / "bundle" / "cli.js"

    env = dict(os.environ)
    if mcp:
        script = _mcp_server_script()
        if script is None:
            typer.echo("Beagle MCP server not found.", err=True)
            raise typer.Exit(1)
        # Expose the MCP server to pi via env so an extension can bridge it.
        env["BEAGLE_MCP_SERVER"] = str(script)

    cmd = [node, str(bundle), *forwarded]
    try:
        raise typer.Exit(subprocess.call(cmd, env=env))
    except KeyboardInterrupt:
        # B904: chain explicitly. The interrupt is not an error in the handler,
        # it is the cause of this exit; `from None` would hide it from a
        # traceback and `from exc` is not legal for a bare except.
        raise typer.Exit(130) from None
