"""``webui`` — launch the Beagle web dashboard server.

Starts the real Beagle-backed ``aiohttp`` server that serves the vendored React
bundle and exposes live Beagle data (workflows, runs, cost, agents, RAG).
"""

from __future__ import annotations

import os

import typer

webui_app = typer.Typer(help="Serve the Beagle web dashboard (real Beagle-backed).")


@webui_app.command("webui")
def webui(
    port: int = typer.Option(
        int(os.environ.get("BEAGLE_WEBUI_PORT", "8080")),
        "--port",
        "-p",
        help="Port to bind the web dashboard on.",
    ),
    host: str = typer.Option(
        os.environ.get("BEAGLE_WEBUI_HOST", "0.0.0.0"),
        "--host",
        help="Host interface to bind. 0.0.0.0 for container access.",
    ),
) -> None:
    """Serve the Beagle web dashboard on the given host:port."""
    os.environ["BEAGLE_WEBUI_PORT"] = str(port)
    os.environ["BEAGLE_WEBUI_HOST"] = host

    from beagle.frontends.webui.server import main

    raise typer.Exit(main())
