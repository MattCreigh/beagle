"""CLAUDE.md Updater — Keeps the project CLAUDE.md file fresh and accurate.

Scans the project structure and updates CLAUDE.md with current metadata:
module counts, file counts, key directories, and recent changes.

This is called by the hydration hook after a successful reingestion so that
AI assistants always have up-to-date project context.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.ClaudeMdUpdater")

# ── Configuration ──────────────────────────────────────────────────────────────


def _resolve_project_root() -> str:
    """Lazily resolve project root via canonical env_manager."""
    from ..utils.env_manager import get_workspace_root

    return str(get_workspace_root())


_DEFAULT_PROJECT_ROOT = os.environ.get("BEAGLE_PROJECT_ROOT") or _resolve_project_root()

_CLAUDE_MD_FILENAME = "CLAUDE.md"

# Maximum age (seconds) before CLAUDE.md is considered stale
_STALE_THRESHOLD_SECONDS = int(os.environ.get("BEAGLE_CLAUDE_MD_STALE_SECONDS", str(7 * 86400)))

# Sections that we inject / update within CLAUDE.md
_METADATA_MARKER_START = "<!-- Beagle METADATA START -->"
_METADATA_MARKER_END = "<!-- Beagle METADATA END -->"


# ── Internal Helpers ──────────────────────────────────────────────────────────


def _scan_project_stats(project_dir: Path) -> dict[str, Any]:
    """Walk the project tree and gather lightweight statistics.

    Returns a dict with file counts, line counts, and key directory list.
    """
    stats: dict[str, Any] = {
        "python_files": 0,
        "total_files": 0,
        "total_lines": 0,
        "directories": [],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    skip_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
    }

    for root, dirs, files in os.walk(project_dir, topdown=True):
        # Prune skipped directories in-place
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        rel_root = Path(root).relative_to(project_dir)
        if str(rel_root) == ".":
            continue
        # Record top-level package directories (depth == 1)
        if len(rel_root.parts) == 1:
            stats["directories"].append(rel_root.parts[0])

        for fname in files:
            stats["total_files"] += 1
            fpath = Path(root) / fname
            if fname.endswith(".py"):
                stats["python_files"] += 1
                with (
                    contextlib.suppress(OSError),
                    open(fpath, encoding="utf-8", errors="ignore") as f,
                ):
                    stats["total_lines"] += sum(1 for _ in f)

    stats["directories"].sort()
    return stats


def _build_metadata_block(stats: dict[str, Any]) -> str:
    """Build the Beagle metadata block to inject into CLAUDE.md."""
    lines = [
        _METADATA_MARKER_START,
        f"<!-- Updated: {stats['timestamp']} -->",
        f"<!-- Python files: {stats['python_files']} -->",
        f"<!-- Total files: {stats['total_files']} -->",
        f"<!-- Total lines (Python): {stats['total_lines']} -->",
        f"<!-- Key dirs: {', '.join(stats['directories'])} -->",
        _METADATA_MARKER_END,
    ]
    return "\n".join(lines)


def _inject_metadata(content: str, metadata_block: str) -> str:
    """Replace or append the Beagle metadata block in CLAUDE.md content.

    If the markers already exist, replaces everything between them.
    If not, appends the block at the end.
    """
    if _METADATA_MARKER_START in content and _METADATA_MARKER_END in content:
        start_idx = content.index(_METADATA_MARKER_START)
        end_idx = content.index(_METADATA_MARKER_END) + len(_METADATA_MARKER_END)
        return content[:start_idx] + metadata_block + content[end_idx:]
    else:
        # Append at end of file
        separator = "\n\n" if content and not content.endswith("\n\n") else ""
        return content + separator + metadata_block + "\n"


# ── Public API ────────────────────────────────────────────────────────────────


def update_claude_md(
    project_dir: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Update CLAUDE.md with current project metadata.

    Scans the project directory, gathers statistics, and injects an
    auto-generated metadata block into CLAUDE.md. If CLAUDE.md doesn't
    exist, it is created with a minimal skeleton.

    Args:
        project_dir: Path to the project root. Defaults to
            BEAGLE_PROJECT_ROOT or auto-detection.
        force: If True, update even if CLAUDE.md is recent.

    Returns:
        Dict with keys:
            - updated (bool): Whether the file was modified.
            - path (str): Absolute path to CLAUDE.md.
            - reason (str): Why update was skipped (if not updated).
            - stats (dict): Project scan statistics.

    """
    project_path = Path(project_dir) if project_dir else Path(_DEFAULT_PROJECT_ROOT)
    result: dict[str, Any] = {
        "updated": False,
        "path": str(project_path / _CLAUDE_MD_FILENAME),
        "reason": "",
        "stats": {},
    }

    # Step 1: Scan project
    logger.info(f"[ClaudeMdUpdater] Scanning project: {project_path}")
    stats = _scan_project_stats(project_path)
    result["stats"] = stats

    claude_md_path = project_path / _CLAUDE_MD_FILENAME

    # Step 2: Check staleness (skip if recently updated, unless forced)
    if claude_md_path.exists() and not force:
        md_age = (
            # wall-clock-ok: compares against a persisted timestamp
            time.time() - claude_md_path.stat().st_mtime
        )
        if md_age < _STALE_THRESHOLD_SECONDS:
            result["reason"] = (
                f"CLAUDE.md is only {md_age / 3600:.1f}h old "
                f"(threshold: {_STALE_THRESHOLD_SECONDS / 3600:.1f}h)"
            )
            logger.info(f"[ClaudeMdUpdater] Skipped: {result['reason']}")
            return result

    # Step 3: Read existing content or create skeleton
    if claude_md_path.exists():
        try:
            content = claude_md_path.read_text(encoding="utf-8")
        except OSError as e:
            result["reason"] = f"Failed to read CLAUDE.md: {e}"
            logger.error(f"[ClaudeMdUpdater] {result['reason']}")
            return result
    else:
        logger.info("[ClaudeMdUpdater] CLAUDE.md not found — creating skeleton")
        content = (
            "# CLAUDE.md — Project Context\n"
            "\n"
            "> Auto-generated project context for AI assistants.\n"
            "\n"
            "## Project Structure\n"
            "\n"
            "_Populate with your project overview._\n"
        )

    # Step 4: Inject metadata block
    metadata_block = _build_metadata_block(stats)
    updated_content = _inject_metadata(content, metadata_block)

    # Step 5: Write back
    try:
        claude_md_path.write_text(updated_content, encoding="utf-8")
        result["updated"] = True
        logger.info(f"[ClaudeMdUpdater] Updated {claude_md_path}")
    except OSError as e:
        result["reason"] = f"Failed to write CLAUDE.md: {e}"
        logger.error(f"[ClaudeMdUpdater] {result['reason']}")

    return result
