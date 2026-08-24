"""CLI command modules.

Each module registers its commands on a shared Typer app instance.
Import the ``app`` from ``cli.cli`` and attach sub-commands via
``app.command()`` in each module's ``register`` function.
"""

from __future__ import annotations

__all__: list[str] = []
