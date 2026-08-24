#!/usr/bin/env python3
"""Beagle auto-compaction hook — fires on PostToolUse.

Context management is a CORE Beagle function (S8). This entrypoint is a
thin harness integration point: goose fires it on PostToolUse so the core
controller folds at the pre-compact threshold BEFORE goose's own compaction.
It owns no logic and no config — everything is delegated to
``beagle.context.compaction_controller``, whose policy lives in
the context-management TOML that ``find_context_management_toml()``
resolves.

This is NOT a plugin. The ``.agents/plugins/beagle-auto-compaction`` shell
wrapper was deleted in S8; the same capability now runs from core code.

Exit code: 0 always — a hook failure must never block the tool stream.
It is never a SILENT 0: every inert path writes a stderr diagnostic
(v1.2.0, CC-1, BGL-029).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path


def _load_bootstrap():
    """Load the sibling interpreter bootstrap by path.

    The hook may be running under an interpreter that cannot import beagle,
    so the bootstrap cannot live in the package. It sits beside this file.

    Returns:
        The loaded module, or None when it is absent.
    """
    path = Path(__file__).resolve().parent / "_hook_bootstrap.py"
    spec = importlib.util.spec_from_file_location("_hook_bootstrap", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_percentage() -> tuple[float, int, int]:
    """Return (percentage, used_tokens, max_tokens) from the best source.

    Source priority:
    1. ``read_session_usage()`` — the live goose CLI session's total_tokens
       over the declared GOOSE_CONTEXT_LIMIT.  This is the number the harness
       itself displays.
    2. The context report file (~/.beagle/context_report.json).
    3. The GOOSE_CONTEXT_PERCENTAGE / GOOSE_CONTEXT_MAX env vars.  No released
       goose version sets either, so this branch is an override, not a primary
       source.

    When every source fails, returns (0.0, 0, 0) so ``main()`` skips the fold
    rather than folding at 0.0 percent.
    """
    # Source 1: the live goose session store.
    session_failure = ""
    try:
        from beagle.context.session_usage import read_session_usage

        usage = read_session_usage()
        if usage is not None:
            return usage.percentage, usage.used_tokens, usage.max_tokens
        session_failure = "read_session_usage() returned None"
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        session_failure = f"read_session_usage() raised {exc}"

    # Source 2: the context report file.
    percentage = 0.0
    max_tokens = 128000
    used_tokens = 0
    report_failure = ""

    report_path = Path.home() / ".beagle" / "context_report.json"
    try:
        if report_path.is_file():
            data = json.loads(report_path.read_text(encoding="utf-8"))
            percentage = float(data.get("percentage", 0.0) or 0.0)
            max_tokens = int(data.get("max_tokens", max_tokens) or max_tokens)
            used_tokens = int(data.get("used_tokens", 0) or 0)
        else:
            report_failure = f"context_report.json absent at {report_path}"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report_failure = f"context_report.json unreadable: {exc}"

    # Source 3: env overrides.  No released goose sets these; they are an
    # override, not a primary source.
    if percentage == 0.0:
        env_pct = os.environ.get("GOOSE_CONTEXT_PERCENTAGE", "")
        env_max = os.environ.get("GOOSE_CONTEXT_MAX", "")
        if env_pct:
            with contextlib.suppress(ValueError):
                percentage = float(env_pct.rstrip("%")) / 100.0
        if env_max:
            with contextlib.suppress(ValueError):
                max_tokens = int(env_max)
        if percentage and not used_tokens:
            used_tokens = int(max_tokens * percentage)

    # Both primary sources failed and no env override answered.  Report the
    # failure and return a sentinel (percentage < 0) so main() skips the fold
    # rather than folding at 0.0 percent — 0.0 means "no fold needed", which
    # is the exact failure this directive repairs.
    if percentage == 0.0:
        print(
            f"[Beagle Auto-Compact] usage source unavailable: {session_failure}; {report_failure}",
            file=sys.stderr,
        )
        return -1.0, 0, 0

    return percentage, used_tokens, max_tokens


def _default_project_dir() -> str:
    """Return the repository root that contains this hook.

    v1.2.0 (CC-1, BGL-009): the previous default was a literal absolute path
    to one developer's checkout. That is a host-coupling violation and it is
    wrong on any other machine. The hook lives at
    ``<repo>/scripts/hooks/<name>.py``, so the root is two parents up.

    Returns:
        An absolute path to the repository root.
    """
    return str(Path(__file__).resolve().parents[2])


def _tool_name() -> str:
    """Extract the tool_name from the goose hook payload (stdin)."""
    try:
        payload = sys.stdin.read()
        if payload.strip():
            data = json.loads(payload)
            return str(data.get("tool_name") or data.get("tool") or "unknown")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return "unknown"


def main() -> int:
    # v1.2.0 (CC-1, BGL-029): re-exec under an interpreter that can import
    # beagle. This must precede the stdin read — os.execv keeps fd 0 open,
    # but only unconsumed bytes survive.
    bootstrap = _load_bootstrap()
    if bootstrap is not None:
        bootstrap.ensure_beagle_interpreter("auto_compact")

    # Cheap / read-only tools never trigger a fold; the controller decides.
    from beagle.context.compaction_controller import (
        check_and_fold_context,
        should_skip_tool,
    )

    tool = _tool_name()
    # Delegate the skip decision to the controller's config (no logic here).
    if should_skip_tool(tool):
        return 0

    percentage, used_tokens, max_tokens = _read_percentage()
    # Sentinel: no usage source answered.  Skip the fold — folding at 0.0
    # percent would be a false "no fold needed".
    if percentage < 0:
        return 0
    query = "Beagle auto-compaction check after tool execution"
    project_dir = os.environ.get("PWD") or _default_project_dir()

    try:
        result = asyncio.run(
            check_and_fold_context(
                percentage=percentage,
                used_tokens=used_tokens,
                max_tokens=max_tokens,
                query=query,
                project_dir=project_dir,
            )
        )
        parsed: dict = {}
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                parsed = {"raw": result}
        elif isinstance(result, dict):
            parsed = result
        action = parsed.get("action", "")
        # v1.2.0 (CC-1, BGL-036): report EVERY invocation, not only the ones
        # that fold. A control loop that speaks only when it acts cannot be
        # distinguished from a control loop that never runs — which is
        # exactly how 2679 failed invocations went unnoticed.
        print(
            f"[Beagle Auto-Compact] tool={tool} percentage={percentage:.1%} "
            f"used={used_tokens} max={max_tokens} action={action or 'none'}",
            file=sys.stderr,
        )
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, ImportError) as exc:
        print(f"[Beagle Auto-Compact] Error: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
