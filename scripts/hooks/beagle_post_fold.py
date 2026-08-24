#!/usr/bin/env python3
"""Beagle post-fold hook — fires on every context-fold / pre-compact event.

A "fold" in goose's terminology is the auto-summarisation that
happens when the context window is approaching its token limit. The
Beagle doctrine says the session MUST NOT stop at this boundary — it
must continue executing the user's task. This hook enforces that:

  1. Mark the RAG as STALE (the codebase may have changed since
     the last full ingest, so the next rag_search call must
     trigger a hot-swap before serving results).
  2. Save a compaction checkpoint with the session's task
     context (workflow_id, query, completed_nodes, errors).
  3. Emit a rehydration prompt so the post-fold context window
     carries the Beagle system identity, the current task
     context, the project context, and the "do not stop, continue"
     directive.

Like ``beagle_bootstrap.py``, this hook is a thin orchestration
layer; all real work lives in:
  - ``rag_staleness.get_staleness_tracker()`` — staleness marker
  - ``post_compaction_rehydration.on_post_compaction`` — the
    rehydration prompt + checkpoint save

Exit code: 0, so a hook failure never blocks the fold. Set
``BEAGLE_HOOK_STRICT=1`` to exit 1 when a step fails, for CI or a
deliberate health check.

Stdout: single-line JSON with the summary dict, carrying a truthful
``status`` of ``ok`` / ``degraded`` / ``error``.

<invariant>
``status`` is DERIVED from the step results, never assigned a constant.
An earlier version set ``status = "ok"`` unconditionally, so a run where
every step died with ModuleNotFoundError still reported success — and
pre-commit reported "Passed". A hook that cannot fail is not a check.
The failing step names also go to stderr, which is what pre-commit
shows the user.
</invariant>

Idempotent: a second invocation just re-marks stale and re-emits
the rehydration prompt. The RAGStalenessTracker is the source of
truth for "have I been rehydrated recently?", so duplicate calls
do not cause a reingest storm.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _emit(payload: dict[str, Any]) -> None:
    """Print a single-line JSON object to stdout.

    Args:
        payload: The summary to serialise.
    """
    print(json.dumps(payload, default=str, sort_keys=True))


def _finalise(summary: dict[str, Any], failed: list[str], total_steps: int) -> int:
    """Derive a truthful status, emit the summary, and choose an exit code.

    Args:
        summary: The step results. Mutated in place with ``status`` and, when
            anything failed, ``failed_steps``.
        failed: Names of the steps that failed.
        total_steps: How many steps were attempted.

    Returns:
        0 normally; 1 when BEAGLE_HOOK_STRICT=1 and at least one step failed.
    """
    if not failed:
        summary["status"] = "ok"
    elif len(failed) >= total_steps:
        summary["status"] = "error"
    else:
        summary["status"] = "degraded"

    if failed:
        summary["failed_steps"] = failed
        print(
            f"beagle hook: {summary['status']} — failed step(s): {', '.join(failed)}",
            file=sys.stderr,
        )

    _emit(summary)
    return 1 if (failed and os.environ.get("BEAGLE_HOOK_STRICT") == "1") else 0


def main() -> int:
    """Mark the RAG stale and checkpoint the fold, reporting what happened.

    Returns:
        The process exit code. See the module docstring.
    """
    project_dir = os.environ.get("BEAGLE_PROJECT_ROOT") or str(_PROJECT_ROOT)
    failed: list[str] = []

    summary: dict[str, Any] = {
        "project_dir": project_dir,
        "marked_stale": False,
        "checkpoint_saved": False,
        "rehydration_emitted": False,
    }

    # Step 1: Mark RAG stale. The next rag_search call will detect
    # the staleness and trigger a hot-swap BEFORE serving results
    # (via the auto_hydrate path, or via trigger_reingest_async in
    # the fire-and-forget path).
    try:
        from beagle.context.rag_staleness import (
            get_staleness_tracker,
        )

        tracker = get_staleness_tracker()
        if not tracker.is_stale:
            tracker.mark_stale(reason="context_fold")
            summary["marked_stale"] = True
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        summary["marked_stale"] = False
        summary["mark_stale_error"] = repr(exc)
        failed.append("mark_stale")

    # Step 2: Save a compaction checkpoint with the current
    # session's task context. The on_post_compaction function
    # handles both the checkpoint save AND the rehydration
    # prompt; one call covers both.
    # Step 2 is opt-out, so it counts towards the total only when attempted.
    # Otherwise BEAGLE_SKIP_HYDRATION=1 plus a step-1 failure would read as
    # "error" (all steps failed) when only one step ever ran.
    attempted = 1
    if not os.environ.get("BEAGLE_SKIP_HYDRATION"):
        attempted = 2
        try:
            from beagle.context.post_compaction_rehydration import (
                on_post_compaction,
            )

            rehydr = on_post_compaction(project_dir=Path(project_dir))
            summary["checkpoint_saved"] = True
            summary["rehydration_emitted"] = bool(rehydr)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            summary["checkpoint_saved"] = False
            summary["rehydration_error"] = repr(exc)
            failed.append("checkpoint")
    else:
        summary["checkpoint_skipped"] = "BEAGLE_SKIP_HYDRATION"

    return _finalise(summary, failed, total_steps=attempted)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        _emit(
            {
                "status": "error",
                "error": repr(exc),
                "error_type": type(exc).__name__,
            }
        )
        sys.exit(0)
