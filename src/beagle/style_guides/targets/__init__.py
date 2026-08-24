"""Render-target package for front-end directive emission (axis 1).

Each front end (goose, claude, pi, OpenClaw-over-MCP) consumes the rendered
directives in a different shape: a file (``.goosehints``, ``CLAUDE.md``), an
XML blob, or a payload returned over MCP. This package provides a single
``emit(scope, layers)`` interface so a new front end is a new target rather
than a new ``render_*`` method on the renderer.

The renderer stays pure and offline; a target that needs a network call
belongs in ``style_guides/tom_hydrator.py``, not here.
"""

from beagle.style_guides.targets.base import (
    EmitOptions,
    RenderTarget,
    TargetRegistry,
    emit,
)

__all__ = ["EmitOptions", "RenderTarget", "TargetRegistry", "emit"]
