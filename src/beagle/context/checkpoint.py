"""Compaction checkpoint: snapshot and restore.

AUTO-GENERATED from context_compaction_hook.py decomposition — DO NOT HAND-EDIT.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("Beagle.context.checkpoint")


@dataclass
class CompactionCheckpoint:
    """State saved before compaction for recovery.

    H-MEM v13 Enhancement:
    - Memory trace: Semantic summary of reasoning chain
    - VFS archival: Large tool outputs archived to virtual file system
    """

    timestamp: datetime
    current_task: str
    iteration: int
    total_iterations: int
    files_modified: list[str] = field(default_factory=list)
    pending_commits: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    # Constraint extraction
    extracted_constraints: list[dict] = field(default_factory=list)
    # Knowledge extraction
    extracted_knowledge: list[dict] = field(default_factory=list)
    # Session memory (Phase 3)
    session_episodes: list[dict] = field(default_factory=list)
    session_id: str = ""
    # H-MEM v13: Memory trace and VFS archival
    memory_trace: str = ""  # Semantic summary of reasoning chain
    archived_outputs: dict[str, str] = field(default_factory=dict)  # URI -> content key mapping
    # v13.7.0: Tool routing state for rehydration continuity
    tool_preferences: dict[str, str] = field(default_factory=dict)  # node -> executor
    model_overrides: dict[str, str] = field(default_factory=dict)  # node -> model
    fallback_directives: list[str] = field(default_factory=list)
    tool_failure_history: list[dict] = field(default_factory=list)
    # Adaptive chunking state (v13.7.1)
    compaction_count: int = 0
    compaction_history: list[dict[str, Any]] = field(default_factory=list)
    # TurboQuant fold ID (Phase 10)
    fold_id: str = ""

    def to_json(self) -> str:
        """Serialize the compaction checkpoint to a JSON string."""
        return json.dumps(
            {
                "timestamp": self.timestamp.isoformat(),
                "current_task": self.current_task,
                "iteration": self.iteration,
                "total_iterations": self.total_iterations,
                "files_modified": self.files_modified,
                "pending_commits": self.pending_commits,
                "next_steps": self.next_steps,
                "extracted_constraints": self.extracted_constraints,
                "extracted_knowledge": self.extracted_knowledge,
                "session_episodes": self.session_episodes,
                "session_id": self.session_id,
                "memory_trace": self.memory_trace,
                "archived_outputs": self.archived_outputs,
                "tool_preferences": self.tool_preferences,
                "model_overrides": self.model_overrides,
                "fallback_directives": self.fallback_directives,
                "tool_failure_history": self.tool_failure_history,
                "compaction_count": self.compaction_count,
                "compaction_history": self.compaction_history,
                "fold_id": self.fold_id,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, data: str) -> CompactionCheckpoint:
        """Deserialize a CompactionCheckpoint from a JSON string."""
        d = json.loads(data)
        return cls(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            current_task=d["current_task"],
            iteration=d["iteration"],
            total_iterations=d["total_iterations"],
            files_modified=d.get("files_modified", []),
            pending_commits=d.get("pending_commits", []),
            next_steps=d.get("next_steps", []),
            extracted_constraints=d.get("extracted_constraints", []),
            extracted_knowledge=d.get("extracted_knowledge", []),
            session_episodes=d.get("session_episodes", []),
            session_id=d.get("session_id", ""),
            memory_trace=d.get("memory_trace", ""),
            archived_outputs=d.get("archived_outputs", {}),
            tool_preferences=d.get("tool_preferences", {}),
            model_overrides=d.get("model_overrides", {}),
            fallback_directives=d.get("fallback_directives", []),
            tool_failure_history=d.get("tool_failure_history", []),
            compaction_count=d.get("compaction_count", 0),
            compaction_history=d.get("compaction_history", []),
            fold_id=d.get("fold_id", ""),
        )
