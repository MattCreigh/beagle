"""Hydration Hook — Lightweight startup hook integrating auto-hydration
and CLAUDE.md updating into the goose session lifecycle.

This is a thin orchestration layer — business logic lives in:

    - auto_hydration.auto_hydrate_sync  (RAG reingestion)
    - claude_md_updater.update_claude_md (CLAUDE.md refresh)
    - rag_staleness.RAGStalenessTracker  (stale/fresh tracking)

Integration points:

    on_session_start()  → called when a new goose instance starts
    on_session_end()    → called when a goose instance shuts down
    quick_hydration_check() → fast staleness check without reingestion
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .auto_hydration import AutoHydrationConfig, HydrationResult, auto_hydrate_sync
from .claude_md_updater import update_claude_md
from .rag_staleness import get_staleness_tracker

logger = logging.getLogger("Beagle.HydrationHook")


def _resolve_project_root() -> str:
    """Lazily resolve project root via canonical env_manager."""
    from ..utils.env_manager import get_workspace_root

    return str(get_workspace_root())


_DEFAULT_PROJECT_ROOT = os.environ.get("BEAGLE_PROJECT_ROOT") or _resolve_project_root()


# ── Session Start ──────────────────────────────────────────────────────────────


def on_session_start(
    project_dir: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Called when a new goose session starts.

    Runs auto-hydration to ensure RAG data is fresh, then updates
    CLAUDE.md if hydration succeeded or was already fresh.
    Also syncs recipes→agents so goose can discover Beagle agents.

    Args:
        project_dir: Override project root. Defaults to
            BEAGLE_PROJECT_ROOT env var.
        force: Force reingestion even if RAG data is fresh.

    Returns:
        Summary dict with hydration, CLAUDE.md, and agent sync status.

    """
    effective_dir = project_dir or _DEFAULT_PROJECT_ROOT
    logger.info(f"[HydrationHook] on_session_start: project_dir={effective_dir}, force={force}")

    result: dict[str, Any] = {
        "project_dir": effective_dir,
        "hydration": {},
        "claude_md": {},
        "agent_sync": {},
    }

    # Step 0: Sync recipes→agents (before hydration, so agents are discoverable)
    try:
        from .recipe_agent_bridge import on_beagle_init

        agent_result = on_beagle_init()
        result["agent_sync"] = agent_result
        logger.info(
            f"[HydrationHook] Agent sync: {agent_result.get('added', 0)} added, "
            f"{agent_result.get('total_agents', 0)} total agents"
        )
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        result["agent_sync"] = {"error": str(e)}
        logger.warning(f"[HydrationHook] Agent sync failed (non-fatal): {e}")

    # Step 1: Run auto-hydration.
    #
    # v13.22.3: use fire_and_forget=True. The session-start hook
    # should not block goose on a 17-minute ingest. The reingest
    # is scheduled as a background task; the next rag_search call
    # (or a poll of the staleness tracker) reports its status.
    # The blocking path is still used by dag.py:881 and :1057
    # (the orchestrator's "do the next workflow step" path) where
    # the caller wants the reingest result before proceeding.
    config = AutoHydrationConfig(project_dir=effective_dir, force=force, fire_and_forget=True)
    try:
        hydration: HydrationResult = auto_hydrate_sync(config)
        result["hydration"] = {
            "status": hydration.status,
            "chunks_created": hydration.chunks_created,
            "files_processed": hydration.files_processed,
            "kuzu_nodes": hydration.kuzu_nodes,
            "kuzu_edges": hydration.kuzu_edges,
            "elapsed_seconds": round(hydration.elapsed_seconds, 2),
            "errors": hydration.errors,
            "reingest_task": hydration.reingest_task,
        }
        logger.info(
            f"[HydrationHook] Hydration result: status={hydration.status}, "
            f"nodes={hydration.kuzu_nodes}, edges={hydration.kuzu_edges}"
        )
    except Exception as e:  # broad catch intentional
        result["hydration"] = {"status": "error", "errors": [str(e)]}
        logger.error(f"[HydrationHook] Hydration failed: {e}", exc_info=True)
        # Hydration failed — skip CLAUDE.md update
        result["claude_md"] = {"updated": False, "reason": "hydration_failed"}
        return result

    # Step 2: Update CLAUDE.md if hydration succeeded or was fresh
    hydration_status = result["hydration"].get("status", "error")
    if hydration_status in ("fresh", "reingested"):
        try:
            md_result = update_claude_md(project_dir=effective_dir, force=force)
            result["claude_md"] = md_result
            logger.info(
                f"[HydrationHook] CLAUDE.md update: updated={md_result.get('updated', False)}"
            )
        except Exception as e:  # broad catch intentional
            result["claude_md"] = {"updated": False, "reason": str(e)}
            logger.error(f"[HydrationHook] CLAUDE.md update failed: {e}", exc_info=True)
    else:
        result["claude_md"] = {
            "updated": False,
            "reason": f"hydration_status={hydration_status}",
        }
        logger.warning(
            f"[HydrationHook] Skipping CLAUDE.md update: hydration status={hydration_status}"
        )

    return result


# ── Session End ────────────────────────────────────────────────────────────────


def on_session_end(
    session_summary: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Called when a goose session ends.

    Marks RAG as stale (files may have changed during the session)
    and extracts learnings from the session summary.

    Args:
        session_summary: Text summary of the completed session.
        project_dir: Override project root for staleness tracking.

    Returns:
        Summary dict with staleness and learnings status.

    """
    effective_dir = project_dir or _DEFAULT_PROJECT_ROOT
    logger.info(
        f"[HydrationHook] on_session_end: project_dir={effective_dir}, "
        f"summary_len={len(session_summary)}"
    )

    result: dict[str, Any] = {
        "project_dir": effective_dir,
        "staleness_marked": False,
        "learnings_logged": False,
    }

    # Step 1: Mark RAG as stale — files may have changed during the session
    try:
        tracker = get_staleness_tracker()
        tracker.mark_stale(reason="session_end")
        result["staleness_marked"] = True
        logger.info("[HydrationHook] RAG marked stale (session ended)")
    except Exception as e:  # broad catch intentional
        result["staleness_error"] = str(e)
        logger.error(f"[HydrationHook] Failed to mark RAG stale: {e}", exc_info=True)

    # Step 2: Extract and log learnings from session summary
    try:
        learnings = _extract_learnings(session_summary)
        result["learnings_logged"] = True
        result["learnings_count"] = len(learnings)
        for learning in learnings:
            logger.info(f"[HydrationHook] Learning: {learning}")
    except Exception as e:  # broad catch intentional
        result["learnings_error"] = str(e)
        logger.error(f"[HydrationHook] Failed to extract learnings: {e}", exc_info=True)

    return result


# ── Quick Check ─────────────────────────────────────────────────────────────────


def quick_hydration_check() -> dict[str, Any]:
    """Fast check without reingestion — reads staleness status only.

    Useful for health-checks or dashboard endpoints that need to know
    RAG freshness without triggering a potentially expensive reingestion.

    Returns:
        Dict with staleness status, ages, and CLAUDE.md existence.

    """
    logger.debug("[HydrationHook] Running quick_hydration_check")

    try:
        tracker = get_staleness_tracker()
        status = tracker.get_status()
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.error(f"[HydrationHook] Failed to get staleness status: {e}")
        status = {}

    # Determine project dir from staleness record or default
    project_dir = status.get("codebase_path", _DEFAULT_PROJECT_ROOT)
    claude_md_path = Path(project_dir) / "CLAUDE.md"

    return {
        "stale": status.get("stale", False),
        "staleness_age": status.get("staleness_age_seconds", 0.0),
        "reingest_count": status.get("reingest_count", 0),
        "project_dir": project_dir,
        "claude_md_exists": claude_md_path.exists(),
    }


# ── Internal Helpers ────────────────────────────────────────────────────────────


def _extract_learnings(session_summary: str) -> list[str]:
    """Extract key learnings from a session summary.

    Looks for lines that start with actionable markers (learned:, note:, TODO:)
    or contain key phrases. Returns a list of extracted learning strings.

    This is intentionally simple — the heavy lifting for learning extraction
    belongs in the context compaction system, not in this hook.
    """
    if not session_summary or not session_summary.strip():
        return []

    learnings: list[str] = []
    markers = ("learned:", "note:", "todo:", "learning:", "insight:")

    for line in session_summary.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith(markers):
            # Capture the content after the marker
            for marker in markers:
                if lower.startswith(marker):
                    content = stripped[len(marker) :].strip()
                    if content:
                        learnings.append(content)
                    break

    return learnings
