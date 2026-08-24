"""Unified Session Model for Beagle workflow execution.

Provides a single source of truth for runtime state, execution history,
and contextual metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..events.events import BeagleEvent


@dataclass
class RoutedMatch:
    """Represents a successfully routed command or tool match."""

    kind: str  # "command", "tool", "skill"
    name: str
    score: float
    reason: str = ""


@dataclass
class RuntimeSession:
    """Unified session state tracking.

    Inspired by claw-code RuntimeSession.
    """

    workflow_id: str
    query: str
    start_time: float = field(default_factory=time.time)

    # State tracking
    history: list[dict[str, Any]] = field(default_factory=list)
    routed_matches: list[RoutedMatch] = field(default_factory=list)
    stream_events: list[BeagleEvent] = field(default_factory=list)

    # Metadata and persistence
    metadata: dict[str, Any] = field(default_factory=dict)
    persisted_path: str | None = None

    def add_event(self, event: BeagleEvent) -> None:
        """Add an execution event to the session log."""
        self.stream_events.append(event)

    def add_match(self, match: RoutedMatch) -> None:
        """Record a routing match decision."""
        self.routed_matches.append(match)

    def to_dict(self) -> dict[str, Any]:
        """Serialize session to dictionary."""
        return {
            "workflow_id": self.workflow_id,
            "query": self.query,
            "start_time": self.start_time,
            "history_len": len(self.history),
            "matches_count": len(self.routed_matches),
            "events_count": len(self.stream_events),
            "metadata": self.metadata,
        }

    def as_markdown(self) -> str:
        """Render session state as human-readable Markdown.

        Inspired by claw-code Markdown Rendering.
        """
        lines = [
            f"# Runtime Session: {self.workflow_id}",
            "",
            f"**Query:** {self.query}",
            f"**Started:** {time.ctime(self.start_time)}",
            "",
            "## Routed Matches",
        ]

        if not self.routed_matches:
            lines.append("_No matches recorded_")
        else:
            for m in self.routed_matches:
                lines.append(f"- [{m.kind.upper()}] **{m.name}** (score: {m.score:.2f})")
                if m.reason:
                    lines.append(f"  - _Reason: {m.reason}_")

        lines.extend(["", "## Execution History"])
        if not self.history:
            lines.append("_No history recorded_")
        else:
            for i, turn in enumerate(self.history):
                lines.append(f"### Turn {i + 1}")
                lines.append(f"**Action:** {turn.get('action', 'N/A')}")
                if "result" in turn:
                    res = str(turn["result"])
                    res_disp = res[:200] + "..." if len(res) > 200 else res
                    lines.append(f"**Result:** {res_disp}")
                lines.append("")

        return "\n".join(lines)
