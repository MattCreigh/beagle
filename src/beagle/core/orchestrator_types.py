"""Shared types and exceptions for Beagle workflow orchestration.

This module provides common data structures and exceptions to avoid circular
dependencies between orchestrator, graph, and node modules.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.orchestrator_types")


class GooseExecutionError(Exception):
    """Exception raised when a Goose subprocess execution fails."""

    pass


class SubprocessTimeoutError(GooseExecutionError):
    """Raised when a subprocess exceeds its timeout and is forcefully terminated."""

    pass


class CircuitBreakerOpenError(GooseExecutionError):
    """Raised when the circuit breaker is open."""

    def __init__(self, circuit_name: str, retry_after: float):
        self.circuit_name = circuit_name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker '{circuit_name}' is OPEN. Retry after {retry_after:.1f}s"
        )


@dataclass
class AgentPingMessage:
    """Standardized message format for agent-to-orchestrator communication."""

    type: str = ""  # "completion", "checkpoint", "error", "context_fold_request"
    agent_id: str = ""
    parent_workflow_id: str = ""
    status: str = ""  # "success", "failed", "partial"
    result: str = ""  # The agent's result/output
    research_path: str = ""  # Path to any research artifacts stored
    checkpoint_data: dict = field(default_factory=dict)  # State checkpoint
    tokens_used: int = 0
    cost_usd: float = 0.0
    error_message: str = ""
    continuation_token: str = ""  # Token for spawned agent to continue
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert message to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AgentPingMessage:
        """Create message from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentState:
    """Strongly typed state object passed through the DAG.

    Attributes:
        query: The user query being processed.
        research_plan: Generated investigation plan.
        raw_execution_context: Raw output from execution phase.
        verified_facts: Facts verified by the fact-checker.
        final_report: Final synthesized report.
        metadata: Additional metadata per node.
        errors: List of errors encountered.
        total_cost: Total cost in USD.
        total_tokens: Total tokens used.
        completed_nodes: List of completed node names.
        workflow_id: Unique workflow identifier.
        start_time: Workflow start timestamp.
        constraints: Active constraints from registry.
        constraint_registry: Reference to ConstraintRegistry.

    """

    query: str = ""
    research_plan: str = ""
    raw_execution_context: str = ""
    verified_facts: str = ""
    final_report: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # v13.22.4 (P3-4): explicit signal that the synthesis-writer node
    # produced an empty or structurally-invalid ``final_report``. The
    # MCP layer (mcp_utility_server._run_workflow_impl) reads this flag
    # to set status=synthesis_failed instead of completed_with_errors,
    # so downstream consumers (CI, monitoring) can distinguish a
    # synthesized-but-empty report from a legitimate report that simply
    # has unmentioned side errors. Set by autonomous_orchestrator.py
    # when the structural-failure guard fires.
    synthesis_failed: bool = False
    total_cost: float = 0.0
    total_tokens: int = 0
    completed_nodes: list[str] = field(default_factory=list)
    workflow_id: str = field(default_factory=lambda: str(time.time()))
    start_time: float = field(default_factory=time.time)
    constraints: list[Any] = field(default_factory=list)
    constraint_registry: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Convert state to dictionary for serialization."""
        return asdict(self)

    def context_size_tokens(self) -> int:
        """Estimate current context token count (4 chars ≈ 1 token).

        Counts ALL state fields that contribute to context size, not just the
        five primary string fields.  Missing fields caused compaction to never
        trigger because the estimate was far below real usage.
        """
        total_chars = len(self.raw_execution_context) + len(self.query)
        total_chars += len(self.research_plan) + len(self.verified_facts) + len(self.final_report)
        # Include serialized metadata, errors, constraints, and completed nodes
        # which all consume context window space during execution.
        for err in self.errors:
            total_chars += len(err)
        for node_name in self.completed_nodes:
            total_chars += len(node_name)
        for constraint in self.constraints:
            total_chars += len(str(constraint))
        # Metadata can hold large tool outputs and intermediate results
        try:
            import json as _json

            total_chars += len(_json.dumps(self.metadata, default=str))
        except (TypeError, ValueError):
            # Fallback: estimate from keys
            total_chars += sum(len(str(k)) + len(str(v)) for k, v in self.metadata.items())
        return total_chars // 4

    def should_compress_context(self, threshold: float = 0.0, max_tokens: int = 0) -> bool:
        """Check if context should be compressed based on utilization.

        Args:
            threshold: Fraction of max_tokens at which to compress (0.0-1.0).
                       0.0 (default) means read from config.context_threshold.compact.
            max_tokens: Maximum context window size in tokens.
                        0 (default) means read from config.context_threshold.max_tokens.
                        Negative values disable compression (always returns False).

        Returns:
            True if context exceeds the compression threshold.

        """
        if max_tokens < 0:
            return False
        from beagle.config.config import get_config

        cfg = get_config().context_threshold
        if threshold <= 0.0:
            threshold = cfg.compact
        if max_tokens == 0:
            max_tokens = cfg.max_tokens
        return self.context_size_tokens() >= int(max_tokens * threshold)

    def compress_context(self) -> str:
        """Compress raw_execution_context using structural skeleton extraction.

        For long contexts, replaces the full text with a structural skeleton
        (imports, class/function definitions, first/last lines), preserving
        the most important information while reducing token count.

        Compressed fold sidecars are stored via CompressedStore for later
        semantic retrieval.

        Returns:
            The compressed context (skeleton), or original if too short.

        """
        raw = self.raw_execution_context
        if len(raw) <= 10_000:
            return raw  # Too short to meaningfully compress

        try:
            from ..context.context_integration import ContextIntegration

            skeleton = ContextIntegration._build_skeleton(raw)

            # Try to store the full context as a fold for later retrieval
            try:
                from ..context.compressed_store import get_compressed_store

                store = get_compressed_store()
                fold_id = f"ctx_{self.workflow_id}_{len(self.completed_nodes)}"
                manifest = {
                    "fold_id": fold_id,
                    "seed": 0,
                    "n_chunks": max(1, len(raw) // 512),
                    "embedding_dim": 0,
                    "original_tokens": len(raw) // 4,
                    "compressed_tokens": len(skeleton) // 4,
                    "compression_ratio": len(skeleton) / max(len(raw), 1),
                    "created_at": time.time(),
                    "source": "agent_state_context_compression",
                    "workflow_id": self.workflow_id,
                }
                store.store_fold(manifest, raw.encode("utf-8"))
                # Store the fold_id so decompression can find it later
                self.metadata.setdefault("_compressed_fold_ids", []).append(fold_id)
            except ImportError as exc:
                logger.warning(
                    "Cannot import the fold sidecar store (%s); the compressed context "
                    "is not persisted, so it cannot be decompressed later.",
                    exc,
                )

            self.raw_execution_context = skeleton
            return skeleton
        except ImportError:
            # Fallback: simple head+tail truncation
            if len(raw) <= 10_000:
                return raw
            head = raw[:3000]
            tail = raw[-2000:]
            compressed = head + f"\n\n[... {len(raw) - 5000} chars compressed ...]\n\n" + tail
            self.raw_execution_context = compressed
            return compressed

    def as_markdown(self) -> str:
        """Render agent state as human-readable Markdown.

        Inspired by claw-code Markdown Rendering.
        """
        lines = [
            f"# Workflow State: {self.workflow_id}",
            "",
            f"**Query:** {self.query}",
            f"**Status:** {'SUCCESS' if not self.errors else 'FAILED'}",
            f"**Total Cost:** ${self.total_cost:.4f}",
            f"**Total Tokens:** {self.total_tokens:,}",
            "",
            "## Completed Nodes",
        ]

        if not self.completed_nodes:
            lines.append("_No nodes completed_")
        else:
            for node in self.completed_nodes:
                lines.append(f"- {node}")

        if self.errors:
            lines.extend(["", "## Errors"])
            for error in self.errors:
                lines.append(f"- {error}")

        if self.final_report:
            lines.extend(["", "## Final Report Summary"])
            # Show first 500 chars of report
            summary = self.final_report[:500] + ("..." if len(self.final_report) > 500 else "")
            lines.append(summary)

        return "\n".join(lines)


@dataclass
class DAGNode:
    """A node in the directed acyclic graph.

    Each node represents a step in the workflow with:
    - name: Identifier for the node
    - skill_name: Recipe/skill to invoke for execution
    - state_mutator: Function to update state after execution
    - prompt_builder: Function to construct prompts
    - dependencies: Nodes that must complete before this one
    - timeout: Maximum execution time in seconds
    - retries: Number of retry attempts on failure
    """

    name: str
    skill_name: str
    state_mutator: Callable[[Any, str], None] = None  # type: ignore[assignment]
    prompt_builder: Callable[[Any], str] = None  # type: ignore[assignment]
    dependencies: list[str] = field(default_factory=list)
    timeout: int = 600
    retries: int = 3
    max_retries: int = 3
    output_key: str | None = None

    # Execution state
    status: str = "pending"
    result: str = ""
    error: str = ""
    attempts: int = 0

    # Model configuration
    model_override: str | None = None
    provider_override: str | None = None

    # Context management
    context_compression: bool = True
    max_context_tokens: int = 0  # 0 = use config.context_threshold.max_tokens

    # Permission context (Claw-Code Step 1.3)
    permission_context: Any = None

    # Validation
    requires_validation: bool = False

    # Hard task support
    is_hard_task: bool = False

    def __post_init__(self) -> None:
        if self.max_retries is None:
            self.max_retries = self.retries

    def can_execute(self, completed_nodes: list[str]) -> bool:
        """Check if all dependencies have completed."""
        return all(dep in completed_nodes for dep in self.dependencies)

    def reset(self) -> None:
        """Reset node state for re-execution."""
        self.status = "pending"
        self.result = ""
        self.error = ""
        self.attempts = 0

    def to_dict(self) -> dict:
        """Serialize node to dict."""
        return {
            "name": self.name,
            "skill_name": self.skill_name,
            "dependencies": self.dependencies,
            "timeout": self.timeout,
            "retries": self.retries,
            "status": self.status,
            "result": self.result[:500] if self.result else "",
            "error": self.error[:200] if self.error else "",
            "attempts": self.attempts,
        }

    def as_markdown(self) -> str:
        """Render node status as Markdown."""
        status_emoji = {
            "pending": "⏳",
            "running": "⠋",
            "completed": "✅",
            "failed": "❌",
            "blocked": "🚫",
        }.get(self.status, "❓")

        lines = [
            f"### {status_emoji} Node: {self.name}",
            f"- **Skill:** `{self.skill_name}`",
            f"- **Status:** {self.status}",
            f"- **Attempts:** {self.attempts}/{self.max_retries}",
        ]

        if self.dependencies:
            lines.append(f"- **Depends on:** {', '.join(self.dependencies)}")

        if self.error:
            lines.append(f"- **Error:** `{self.error}`")

        return "\n".join(lines)
