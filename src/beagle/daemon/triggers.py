"""Trigger mapping for the Beagle daemon.

Matches codebase changes to specific workflows.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from .watcher import ChangeSet


@dataclass
class Trigger:
    """Definition of a daemon trigger."""

    name: str
    file_patterns: list[str]
    workflow: str
    mode: str = "audit"
    budget: float = 2.0
    cooldown_minutes: int = 60


class TriggerMatcher:
    """Matches ChangeSets against configured triggers."""

    def __init__(self, triggers: list[Trigger] | None = None):
        self.triggers = triggers or self._get_default_triggers()

    def match(self, changes: ChangeSet) -> list[Trigger]:
        """Find all triggers that match the given changes."""
        matched = []
        all_files = changes.changed_files + changes.new_files

        for trigger in self.triggers:
            is_match = False
            for pattern in trigger.file_patterns:
                if any(fnmatch.fnmatch(f, pattern) for f in all_files):
                    is_match = True
                    break

            if is_match:
                matched.append(trigger)

        return matched

    def _get_default_triggers(self) -> list[Trigger]:
        return [
            Trigger(
                name="security_check",
                file_patterns=["*.py"],
                workflow="security",
                budget=2.0,
            ),
            Trigger(
                name="architecture_review",
                file_patterns=["core/*.py", "infrastructure/*.py"],
                workflow="deep-planning",
                budget=3.0,
            ),
        ]
