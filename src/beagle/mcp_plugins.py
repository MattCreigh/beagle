"""MCP plugin discovery — auto-detect, never auto-activate.

beagle scans the ``beagle.mcp_plugins`` entry-point group at doctor/status
time and reports every installed plugin with its activation state read from
the PLUGIN's OWN TOML configuration. beagle itself never starts a plugin
server: each plugin owns its console script and config gate (informed
decision), e.g. ``beagle-openclaw-mcp`` for the openclaw plugin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.mcp_plugins")

_ENTRY_POINT_GROUP = "beagle.mcp_plugins"


@dataclass(slots=True)
class McpPluginInfo:
    """One detected MCP plugin."""

    name: str
    description: str = ""
    importable: bool = True
    enabled: bool | None = None  # None → plugin exposes no enablement probe
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def discover_mcp_plugins(*, rescan: bool = False) -> list[McpPluginInfo]:
    """Detect installed MCP plugins via entry points.

    Args:
        rescan: Force a fresh entry-point scan (default uses this call's
            fresh scan; entry-point metadata is cheap).

    Returns:
        One :class:`McpPluginInfo` per detected plugin. Detection failures
        are reported per-plugin and never raised.
    """
    try:
        from importlib.metadata import entry_points

        eps = list(entry_points(group=_ENTRY_POINT_GROUP))
    except Exception as exc:  # noqa: BLE001 - discovery must never crash callers
        logger.debug("mcp-plugin scan failed: %s", exc)
        return []

    plugins: list[McpPluginInfo] = []
    for ep in eps:
        info = McpPluginInfo(name=ep.name)
        try:
            module = ep.load()
            info.description = str(getattr(module, "PLUGIN_DESCRIPTION", ""))
            probe = getattr(module, "is_enabled", None)
            info.enabled = bool(probe()) if callable(probe) else None
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not hide others
            info.importable = False
            info.error = str(exc)
        plugins.append(info)
    return plugins


__all__ = ["McpPluginInfo", "discover_mcp_plugins"]
