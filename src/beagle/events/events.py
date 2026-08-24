"""Event types for Beagle workflow orchestration.

Provides frozen dataclasses for all system events.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, kw_only=True)
class BeagleEvent:
    """Base class for all Beagle system events."""

    event_type: str
    workflow_id: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any:
        """Create an event instance from a dictionary, filtering unknown keys."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in fields}
        return cls(**filtered_data)


@dataclass(frozen=True, kw_only=True)
class WorkflowStarted(BeagleEvent):
    event_type: str = "workflow.started"
    query: str = ""
    budget_usd: float = 0.0
    mode: str = "audit"
    nodes: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class WorkflowCompleted(BeagleEvent):
    event_type: str = "workflow.completed"
    success: bool = True
    total_cost_usd: float = 0.0
    budget_usd: float = 0.0
    total_tokens: int = 0
    duration_seconds: float = 0.0
    completed_nodes: int = 0
    errors: int = 0


@dataclass(frozen=True, kw_only=True)
class NodeStarted(BeagleEvent):
    event_type: str = "node.started"
    node_name: str
    model: str = ""
    # v13.19.4: skill_name identifies which skill/plugin this node is
    # executing under. Used by RunRecorder and downstream telemetry
    # for per-skill cost and duration attribution.
    skill_name: str = ""


@dataclass(frozen=True, kw_only=True)
class ToolCallEvent(BeagleEvent):
    """Fired when a tool is matched or executed."""

    event_type: str = "tool.call"
    tool_name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    status: str = "matched"  # "matched", "running", "completed", "failed", "blocked"
    result: str | None = None


@dataclass(frozen=True, kw_only=True)
class NodeOutput(BeagleEvent):
    """Streaming output line from a running node."""

    event_type: str = "node.output"
    node_name: str
    stream_type: str = "stdout"  # stdout or stderr
    content: str


@dataclass(frozen=True, kw_only=True)
class NodeCompleted(BeagleEvent):
    event_type: str = "node.completed"
    node_name: str
    result: str = ""  # First 200 chars
    cost: float = 0.0
    tokens: int = 0  # total (input + output); kept for existing consumers
    # v13.22.3 (2026-07-28): the input/output split is available at every
    # emit site but was being collapsed into `tokens` alone. The tracking
    # recorder writes NodeRun.input_tokens / NodeRun.output_tokens as
    # separate DB columns, so the collapse left it with nothing faithful to
    # record. Emitters that know the split MUST pass it; `tokens` stays as
    # the total so nothing that already reads it breaks.
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    # v13.19.4: success flag distinguishes successful completion from
    # completion-with-warnings or completion-with-degraded-output. The
    # presence of `success=False` does NOT mean the node raised; it means
    # the node produced output that the recorder should mark for review.
    success: bool = True


@dataclass(frozen=True, kw_only=True)
class NodeFailed(BeagleEvent):
    """Event emitted when a workflow node fails after retries.

    Provides structured debugging context including the model used,
    error classification, stderr output, execution duration, and
    the workflow phase where the failure occurred.

    Attributes:
        node_name: Name of the failed node.
        error: Human-readable error message.
        attempt: Which retry attempt failed (1-based).
        model: Model identifier used during the failed attempt, if known.
        error_category: Classification of the error (timeout, ratelimit,
            validation, system, unknown). Useful for automated alert routing.
        stderr_snippet: Last ~500 chars of stderr output for debugging.
            None if no stderr was captured.
        duration_seconds: Wall-clock time of the failed attempt, if measured.
        node_phase: Workflow phase of the node (planning, execution,
            verification, synthesis, or unknown).

    """

    event_type: str = "node.failed"
    node_name: str
    error: str
    attempt: int = 1
    model: str | None = None
    error_category: str | None = None
    stderr_snippet: str | None = None
    duration_seconds: float | None = None
    node_phase: str | None = None


@dataclass(frozen=True, kw_only=True)
class ToolEscalated(BeagleEvent):
    """Emitted when a LangChain tool fails and execution is escalated to Goose.

    v13.7.0: Enables observability into tool routing failures and
    automatic escalation decisions.
    """

    event_type: str = "tool.escalated"
    tool_name: str
    error: str
    escalated_to: str = "goose"
    original_executor: str = "langchain_tool"


@dataclass(frozen=True, kw_only=True)
class NodeSkipped(BeagleEvent):
    event_type: str = "node.skipped"
    node_name: str
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class BudgetWarning(BeagleEvent):
    event_type: str = "budget.warning"
    current_cost: float
    threshold: float


@dataclass(frozen=True, kw_only=True)
class ContextWarning(BeagleEvent):
    event_type: str = "context.warning"
    utilization: float = 0.0
    threshold: float = 0.0
    node_name: str = ""
    current_tokens: int = 0
    max_tokens: int = 0


@dataclass(frozen=True, kw_only=True)
class SteeringReceived(BeagleEvent):
    event_type: str = "steering.received"
    source: str = ""  # "file", "api", "tui"


@dataclass(frozen=True, kw_only=True)
class EVHValidationResult(BeagleEvent):
    event_type: str = "evh.result"
    node_name: str
    passed: bool = True
    details: str = ""


@dataclass(frozen=True, kw_only=True)
class AutoDreamCompleted(BeagleEvent):
    event_type: str = "autodream.completed"
    pruned: int = 0
    merged: int = 0
    refreshed: int = 0
    index_tokens_before: int = 0
    index_tokens_after: int = 0


@dataclass(frozen=True, kw_only=True)
class AutoDreamPruned(BeagleEvent):
    event_type: str = "autodream.pruned"
    count: int = 0


@dataclass(frozen=True, kw_only=True)
class AutoDreamMerged(BeagleEvent):
    event_type: str = "autodream.merged"
    count: int = 0


@dataclass(frozen=True, kw_only=True)
class AutoDreamRefreshed(BeagleEvent):
    event_type: str = "autodream.refreshed"
    count: int = 0
    new_pointers: int = 0


@dataclass(frozen=True, kw_only=True)
class DaemonStarted(BeagleEvent):
    event_type: str = "daemon.started"
    tick_interval: int = 30
    max_daily_cost: float = 5.0


@dataclass(frozen=True, kw_only=True)
class DaemonStopped(BeagleEvent):
    event_type: str = "daemon.stopped"
    total_workflows_run: int = 0
    total_cost_usd: float = 0.0


@dataclass(frozen=True, kw_only=True)
class DaemonChangeDetected(BeagleEvent):
    event_type: str = "daemon.change_detected"
    changed_files: int = 0
    affected_modules: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class DaemonTriggered(BeagleEvent):
    event_type: str = "daemon.triggered"
    trigger_name: str = ""
    workflow: str = ""


@dataclass(frozen=True, kw_only=True)
class DaemonDeferred(BeagleEvent):
    event_type: str = "daemon.deferred"
    trigger_name: str = ""
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class DaemonIdleStart(BeagleEvent):
    event_type: str = "daemon.idle_start"
    idle_seconds: int = 0


@dataclass(frozen=True, kw_only=True)
class HealthCheckCompleted(BeagleEvent):
    """Emitted after each periodic health check cycle."""

    event_type: str = "health.check.completed"
    health_score: float = 1.0
    rss_mb: float = 0.0
    fd_count: int = 0
    circuits_open: int = 0
    pool_active: int = 0
    degraded_systems: tuple[str, ...] = ()
    critical_systems: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class HealthDegraded(BeagleEvent):
    """Emitted when health score drops below the degraded threshold."""

    event_type: str = "health.degraded"
    health_score: float = 0.0
    degraded_systems: tuple[str, ...] = ()
    critical_systems: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class HealthCritical(BeagleEvent):
    """Emitted when health score drops below the critical threshold."""

    event_type: str = "health.critical"
    health_score: float = 0.0
    critical_systems: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class HealthRecovered(BeagleEvent):
    """Emitted when health score recovers above the degraded threshold."""

    event_type: str = "health.recovered"
    health_score: float = 1.0
    previous_score: float = 0.0


@dataclass(frozen=True, kw_only=True)
class ShutdownStarted(BeagleEvent):
    """Emitted when a coordinated shutdown begins."""

    event_type: str = "lifecycle.shutdown.started"
    reason: str = "unknown"
    restart_planned: bool = False


@dataclass(frozen=True, kw_only=True)
class ShutdownCompleted(BeagleEvent):
    """Emitted when a coordinated shutdown finishes."""

    event_type: str = "lifecycle.shutdown.completed"
    duration_seconds: float = 0.0
    steps_completed: int = 0
    steps_failed: int = 0


@dataclass(frozen=True, kw_only=True)
class RestartTriggered(BeagleEvent):
    """Emitted when a graceful restart is triggered."""

    event_type: str = "lifecycle.restart.triggered"
    reason: str = "unknown"
    restart_count: int = 0
    checkpoint_saved: bool = False


@dataclass(frozen=True, kw_only=True)
class CheckpointRestored(BeagleEvent):
    """Emitted when a checkpoint is restored at startup."""

    event_type: str = "lifecycle.checkpoint.restored"
    checkpoint_age_seconds: float = 0.0
    restart_count: int = 0
    previous_reason: str = ""


@dataclass(frozen=True, kw_only=True)
class ValidationStarted(BeagleEvent):
    """Emitted when a validation run begins."""

    event_type: str = "validation.started"
    tools: tuple[str, ...] = ()  # ("pytest", "ruff", "mypy")
    files_count: int = 0


@dataclass(frozen=True, kw_only=True)
class ValidationCompleted(BeagleEvent):
    """Emitted when a validation run finishes."""

    event_type: str = "validation.completed"
    all_passed: bool = True
    total_findings: int = 0
    tool_summaries: tuple[str, ...] = ()  # ("pytest: 42 passed", "ruff: clean")
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class RegressionDetected(BeagleEvent):
    """Emitted when a regression is detected by comparing current vs historical results."""

    event_type: str = "validation.regression"
    regression_count: int = 0
    categories: tuple[str, ...] = ()  # ("test_failure", "lint_error")
    description: str = ""


@dataclass(frozen=True, kw_only=True)
class NodeInputCaptured(BeagleEvent):
    """Emitted before a node executes, capturing full input for replay."""

    event_type: str = "node.input.captured"
    node_name: str
    prompt_hash: str = ""  # SHA-256 of prompt (not full prompt — too large)
    system_directive_hash: str = ""
    model: str = ""
    temperature: float = 0.0


@dataclass(frozen=True, kw_only=True)
class ReplayStarted(BeagleEvent):
    event_type: str = "replay.started"
    original_workflow_id: str = ""
    seed: str = ""


@dataclass(frozen=True, kw_only=True)
class ReplayCompleted(BeagleEvent):
    event_type: str = "replay.completed"
    original_workflow_id: str = ""
    match: bool = True  # Did replay produce same outputs?
    differences: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class DegradationChanged(BeagleEvent):
    """Emitted when the system degradation level changes."""

    event_type: str = "degradation.changed"
    previous_level: str = "normal"
    current_level: str = "normal"
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class BudgetExhausted(BeagleEvent):
    """Emitted when a workflow or tenant budget is exhausted."""

    event_type: str = "budget.exhausted"
    current_cost: float = 0.0
    budget: float = 0.0
    tenant_id: str = "default"


@dataclass(frozen=True, kw_only=True)
class RAGStale(BeagleEvent):
    """Emitted when the RAG index is detected stale and reingestion is triggered.

    WP-5 M2: this class was previously imported lazily in
    ``src/context/rag_staleness.py`` but did not exist, so the import always
    failed and the event was never published. Defining it here restores the
    liveness signal for subscribers (e.g. the workflow heartbeat bridge).
    """

    event_type: str = "rag.stale"
    trigger: str = "auto"
    codebase_path: str = ""
