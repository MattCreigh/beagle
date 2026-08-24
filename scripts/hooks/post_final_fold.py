#!/usr/bin/env python3
"""Beagle post-final-answer fold hook — fires on Stop.

Context management is a CORE Beagle function (S8). This entrypoint is a
thin harness integration point: goose fires it on Stop so the core controller
writes the rehydration sidecar on EVERY exit path (direct edits, research,
incident), not just workflow finalize (which already calls it). It owns no
logic and no config — everything is delegated to
``beagle.context.compaction_controller``, whose policy lives in
the context-management TOML that ``find_context_management_toml()``
resolves.

The percentage is metadata only; the fold ALWAYS fires so the next session
can bootstrap cleanly.

This is NOT a plugin. The ``.agents/plugins/beagle-auto-compaction`` shell
wrapper was deleted in S8; the same capability now runs from core code.

Exit code: 0 always — a Stop hook must never veto the stop.
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


def _read_percentage() -> float:
    """Return the last-reported context-usage percentage (metadata only)."""
    percentage = 0.0
    report_path = Path.home() / ".beagle" / "context_report.json"
    try:
        if report_path.is_file():
            data = json.loads(report_path.read_text(encoding="utf-8"))
            percentage = float(data.get("percentage", 0.0) or 0.0)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    if percentage == 0.0:
        env_pct = os.environ.get("GOOSE_CONTEXT_PERCENTAGE", "")
        if env_pct:
            with contextlib.suppress(ValueError):
                percentage = float(env_pct.rstrip("%")) / 100.0

    return percentage


def _payload_context() -> tuple[str, str, str]:
    """Extract (workflow_id, query, project_dir) from the goose Stop payload."""
    workflow_id = "cli_session"
    query = ""
    project_dir = os.environ.get("PWD") or _default_project_dir()
    try:
        payload = sys.stdin.read()
        if payload.strip():
            data = json.loads(payload)
            workflow_id = str(data.get("session_id") or data.get("workflow_id") or workflow_id)
            query = str(data.get("query") or data.get("prompt") or "")
            payload_cwd = str(data.get("cwd") or data.get("project_dir") or "")
            if payload_cwd and Path(payload_cwd).is_dir():
                project_dir = payload_cwd
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return workflow_id, query, project_dir


def main() -> int:
    # v1.2.0 (CC-1, BGL-029): re-exec under an interpreter that can import
    # beagle. This must precede the stdin read in _payload_context().
    bootstrap = _load_bootstrap()
    if bootstrap is not None:
        bootstrap.ensure_beagle_interpreter("post_final_fold")

    from beagle.context.compaction_controller import enforce_post_final_answer_fold

    workflow_id, query, project_dir = _payload_context()
    percentage = _read_percentage()

    try:
        result = asyncio.run(
            enforce_post_final_answer_fold(
                workflow_id=workflow_id,
                query=query,
                completed_nodes=[],
                project_dir=project_dir,
                percentage=percentage,
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
        print(
            f"[Beagle Post-Final-Fold] status={parsed.get('status', 'unknown')} "
            f"action={parsed.get('action', '')} "
            f"sidecar_chars={parsed.get('sidecar_chars', 0)} workflow={workflow_id}",
            file=sys.stderr,
        )
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, ImportError) as exc:
        print(f"[Beagle Post-Final-Fold] Error (non-fatal): {exc}", file=sys.stderr)

    # Stop hooks are blocking — always allow the stop.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
