"""Core context-management controller — context management is a CORE function.

This module is the canonical home for the two context-compaction entry points
that the goose auto-compaction plugin used to expose as shell-hook wrappers:

  - :func:`check_and_fold_context` — called after tool execution (PostToolUse)
    to fold at the pre-compact threshold before goose's own compaction.
  - :func:`enforce_post_final_answer_fold` — called unconditionally at the end
    of a session/workflow to write the rehydration sidecar.

Both delegate to the long-standing implementations in
``beagle.infrastructure.tools._impl`` and ``beagle.context.post_compaction_rehydration``.
This module exists so the controller surface is importable from core code (the
orchestrator finalize path, CLI shutdown, tests) without going through the MCP
server or the goose hook.

Configuration lives in the canonical config root, resolved by
``find_context_management_toml()`` (thresholds, skip-tool list, hook budgets) —
NOT hardcoded here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from beagle.infrastructure.tools._impl import (  # re-export
    check_and_fold_context,
    enforce_post_final_answer_fold,
)

__all__ = [
    "check_and_fold_context",
    "enforce_post_final_answer_fold",
    "load_context_management_config",
    "should_skip_tool",
]


def load_context_management_config() -> dict:
    """Load the context-management policy from the canonical config root.

    Returns:
        The parsed ``context_management`` TOML section, or an empty dict if
        the file is absent/unreadable (the controller degrades to defaults).

    The file lives in the canonical config root, resolved by
    ``find_context_management_toml()``.

    """
    try:
        from beagle.config._config_path import find_context_management_toml

        path = find_context_management_toml()
        if not path.is_file():
            return {}
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError, KeyError, TypeError, ImportError, tomllib.TOMLDecodeError):
        return {}


def should_skip_tool(tool_name: str, skip_pattern: str | None = None) -> bool:
    """Return True if a tool should be skipped by the auto-compaction hook.

    Args:
        tool_name: The tool name to test (e.g. "beagle_session_bootstrap").
        skip_pattern: A regex of tool names to skip. If None, loaded from
            ``context_management.toml [hook].skip_tools``.

    Returns:
        True if the tool matches the skip pattern.

    """
    import re

    if skip_pattern is None:
        cfg = load_context_management_config()
        skip_pattern = (cfg.get("hook") or {}).get("skip_tools") or ""
    if not skip_pattern:
        return False
    return re.search(skip_pattern, tool_name) is not None


def resolve_compact_threshold() -> float:
    """Return the pre-compact threshold from config (default 0.58).

    Returns:
        The configured ``pre_compact`` threshold.

    """
    cfg = load_context_management_config()
    thresholds = cfg.get("thresholds") or {}
    try:
        return float(thresholds.get("pre_compact", 0.58))
    except (TypeError, ValueError):
        return 0.58


def config_path() -> Path | None:
    """Return the resolved context_management.toml path (for diagnostics)."""
    try:
        from beagle.config._config_path import find_context_management_toml

        return find_context_management_toml()
    except (OSError, ImportError):
        return None
