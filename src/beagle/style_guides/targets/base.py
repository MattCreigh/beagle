"""Render-target interface for front-end directive emission (axis 1).

A :class:`RenderTarget` knows how to deliver the rendered doctrine to one
front-end consumer. ``emit`` is the single entry point the CLI and the
renderer use; each target implements the delivery (write a file, return a
string, return an MCP payload). Targets are pure and offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class EmitOptions:
    """Options controlling how a target emits a directive.

    Attributes:
        scope: Directory the directive is scoped to (``None`` = global).
        layers: Ordered layer names to include (global → directory → task).
        target_dir: Where file targets write their artefact.

    """

    scope: Path | None = None
    layers: tuple[str, ...] = ("global", "directory", "task")
    target_dir: Path | None = None


class RenderTarget(Protocol):
    """Protocol for a front-end directive emitter.

    A target converts the rendered doctrine into the shape its front end
    consumes. It must be constructible with no side effects and must not
    make network calls.
    """

    name: str

    def emit(self, content: str, options: EmitOptions) -> str:
        """Deliver the rendered content to the front end.

        Args:
            content: The rendered directive text.
            options: Emission options (scope, layers, target dir).

        Returns:
            A human-readable status string describing what was emitted
            (e.g. the path written, or a payload marker for MCP targets).

        """


# Built-in target registry. Keys are the names used by the CLI
# ``--target`` flag (``goosehints``, ``claude_md``, ``top_of_mind_xml``,
# ``mcp_resource``). Imported lazily so importing this module never pulls
# the renderer or the CLI.
_BUILTIN_TARGETS: dict[str, RenderTarget] = {}


def register(name: str, target: RenderTarget) -> None:
    """Register a render target by name.

    Args:
        name: The CLI-facing name of the target.
        target: The target instance.

    """
    _BUILTIN_TARGETS[name] = target


class TargetRegistry:
    """Registry of available render targets."""

    @staticmethod
    def get(name: str) -> RenderTarget:
        """Resolve a target by name.

        Args:
            name: Target name (``goosehints``, ``claude_md``,
                ``top_of_mind_xml``, ``mcp_resource``).

        Returns:
            The target instance.

        Raises:
            KeyError: When the name is not registered.

        """
        # Lazy import to avoid a module-load cycle with render.py.
        if not _BUILTIN_TARGETS:
            from beagle.style_guides.targets.file_targets import (
                claude_md_target,
                goosehints_target,
                top_of_mind_xml_target,
            )
            from beagle.style_guides.targets.mcp_target import mcp_resource_target

            register("goosehints", goosehints_target)
            register("claude_md", claude_md_target)
            register("top_of_mind_xml", top_of_mind_xml_target)
            register("mcp_resource", mcp_resource_target)
        if name not in _BUILTIN_TARGETS:
            raise KeyError(
                f"unknown render target {name!r}; available: " + ", ".join(sorted(_BUILTIN_TARGETS))
            )
        return _BUILTIN_TARGETS[name]

    @staticmethod
    def names() -> list[str]:
        """List registered target names.

        Returns:
            The sorted list of available target names.

        """
        if not _BUILTIN_TARGETS:
            TargetRegistry.get("goosehints")  # force lazy registration
        return sorted(_BUILTIN_TARGETS)


def emit(target: str, content: str, options: EmitOptions) -> str:
    """Emit rendered content through a named target.

    Args:
        target: The target name.
        content: The rendered directive text.
        options: Emission options.

    Returns:
        The target's status string.

    """
    return TargetRegistry.get(target).emit(content, options)
