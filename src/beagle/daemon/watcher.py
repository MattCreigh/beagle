"""Codebase watcher for the Beagle daemon.

Detects changes via git polling and filesystem events.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from beagle.config.paths import resolve_executable

logger = logging.getLogger("Beagle.daemon.watcher")


@dataclass
class ChangeSet:
    """Detected changes since last check."""

    changed_files: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    commit_message: str = ""
    commit_hash: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_files or self.new_files or self.deleted_files)

    def affected_modules(self) -> set[str]:
        """Extract top-level module names from changed paths."""
        modules = set()
        for f in self.changed_files + self.new_files:
            parts = Path(f).parts
            if len(parts) > 0:
                modules.add(parts[0])
        return modules


class Watcher:
    """Watches workspace for code changes."""

    def __init__(self, workspace_root: Path | str):
        self.workspace_root = Path(workspace_root)
        self._last_hash: str | None = self._get_git_head()

    def check(self) -> ChangeSet:
        """Perform a check for changes."""
        current_hash = self._get_git_head()
        changes = ChangeSet(commit_hash=current_hash or "")

        if current_hash and current_hash != self._last_hash:
            logger.info(f"Git change detected: {self._last_hash} -> {current_hash}")
            changes = self._get_git_diff(self._last_hash, current_hash)
            self._last_hash = current_hash

        return changes

    def _get_git_head(self) -> str | None:
        """Get current HEAD hash."""
        try:
            result = subprocess.run(
                [resolve_executable("git"), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                timeout=30,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except subprocess.TimeoutExpired:
            logger.warning("git rev-parse HEAD timed out after 30s")
            return None
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("git rev-parse HEAD failed: %s", exc)
            return None

    def _get_git_diff(self, old_hash: str | None, new_hash: str) -> ChangeSet:
        """Get files changed between two hashes."""
        if not old_hash:
            return ChangeSet(commit_hash=new_hash)

        try:
            result = subprocess.run(
                [resolve_executable("git"), "diff", "--name-status", old_hash, new_hash],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                timeout=30,
            )

            changes = ChangeSet(commit_hash=new_hash)
            for line in result.stdout.splitlines():
                if not line:
                    continue
                status, path = line.split(None, 1)
                if status == "M":
                    changes.changed_files.append(path)
                elif status == "A":
                    changes.new_files.append(path)
                elif status == "D":
                    changes.deleted_files.append(path)

            return changes
        except subprocess.TimeoutExpired:
            logger.warning("git diff timed out after 30s")
            return ChangeSet(commit_hash=new_hash)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"Failed to get git diff: {e}")
            return ChangeSet(commit_hash=new_hash)
