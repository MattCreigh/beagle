"""Beagle v12.0 — DAGOrchestrator facade.

This module is the backward-compatible entry point for the orchestrator.
Implementation is split across ``core/orchestrator/``:

- ``system_directive.py`` — SYSTEM_DIRECTIVE_TEMPLATE, model fallbacks
- ``executor.py`` — BeagleDAGNode, EVH validation, process lifecycle
- ``state_manager.py`` — CompressedAgentState, KV pool, call tracking

The DAGOrchestrator class itself remains here (the main run loop has
deep coupling to state, events, and context that resists further extraction).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beagle.context.context_integration import get_context_integration
from beagle.context.context_window import get_context_manager

# Import context caching (Phase 8.1)
from beagle.context.prompt_cache import PromptCache
from beagle.core.a2a_integration import (
    configure_a2a,
    is_a2a_enabled,
    sign_delegation,
    verify_agent_result,
)

# ── Re-exports from orchestrator package (backward compat) ───────────────────
from beagle.core.orchestrator.executor import (
    DEFAULT_SUBPROCESS_TIMEOUT,
    DEFAULT_VALIDATION_TIMEOUT,
    SUBPROCESS_MEMORY_LIMIT,
    BeagleDAGNode,
    _cleanup_processes,
    _run_evh_validation,
    _signal_handler,
    get_recipes_dir,
)
from beagle.core.orchestrator.state_manager import (
    CompressedAgentState,
    CompressedKVPool,
    _add_process,
    _remove_process,
    cleanup_agent_call_counter,
    get_agent_call_count,
    get_kv_pool,
    increment_agent_call,
    ping_orchestrator,
    reset_agent_call_counter,
    set_orchestrator_channel,
)
from beagle.core.orchestrator.system_directive import (
    DEFAULT_MAX_NESTED_AGENTS,
    ENHANCED_MODES,
    MODEL_FALLBACKS,
    SYSTEM_DIRECTIVE_TEMPLATE,
)
from beagle.core.orchestrator_types import (
    AgentPingMessage,
    AgentState,
    DAGNode,
    GooseExecutionError,
)
from beagle.core.state import trim_state_lists

# Imports used by DAGOrchestrator
from beagle.cost_tracker import (
    reset_cost_tracker,
)

# Event bus — only events used directly by DAGOrchestrator
from beagle.events import (
    AutoDreamCompleted,
    NodeSkipped,
    SteeringReceived,
    WorkflowCompleted,
    WorkflowStarted,
    get_event_bus,
)
from beagle.security import validate_query_async
from beagle.steering import SteeringManager
from beagle.utils.env_manager import (
    load_env_file,
)
from beagle.utils.file_writer import staged_write

logger = logging.getLogger("Beagle.orchestrator")

# ── Lazy-optional module caches (avoid hot-loop re-import hangs) ────────────
_steering_injection: Any | None = None
_ctx_monitor_cls: Any | None = None
_guardian_mod: Any | None = None
_post_compaction_mod: Any | None = None
_rag_staleness_mod: Any | None = None
_outbox_client: Any | None = None  # lazily-created fault-recovery OutboxClient


def _get_steering_injection() -> Any | None:
    global _steering_injection
    if _steering_injection is not None:
        return _steering_injection
    try:
        from beagle.steering.injection import inject_steering

        _steering_injection = inject_steering
    except ImportError:
        _steering_injection = False  # sentinel: unavailable
    return _steering_injection if _steering_injection is not False else None


def _get_ctx_monitor() -> Any | None:
    global _ctx_monitor_cls
    monitor: Any = _ctx_monitor_cls
    if monitor is not None and monitor is not False:
        return monitor()
    try:
        from beagle.context.context_compaction_hook import get_monitor

        _ctx_monitor_cls = get_monitor
        monitor = get_monitor
        return monitor()
    except ImportError:
        _ctx_monitor_cls = False  # sentinel
    return None


def _get_guardian_mod() -> Any | None:
    global _guardian_mod
    if _guardian_mod is not None:
        return _guardian_mod
    try:
        from beagle.guardian import (
            ApprovalDecision,
            GuardianAction,
            RiskLevel,
            get_guardian,
        )

        # SimpleNamespace, NOT type(...)(): attribute access on an instance
        # of a class-based namespace turns a plain function into a BOUND
        # METHOD and injects `self`, so `get_guardian()` raised
        # "takes 0 positional arguments but 1 was given". The surrounding
        # `except Exception` swallowed that TypeError at debug level, so the
        # v0.3.0 Guardian approval check silently never ran — every node
        # executed unapproved. Narrowing that catch is what surfaced it.
        # The now-removed orchestrator/dag.py twin already used
        # SimpleNamespace for exactly this reason; the fix had only ever
        # landed in that dead copy, never here.
        _guardian_mod = SimpleNamespace(
            ApprovalDecision=ApprovalDecision,
            GuardianAction=GuardianAction,
            RiskLevel=RiskLevel,
            get_guardian=get_guardian,
        )
    except ImportError:
        _guardian_mod = False
    return _guardian_mod if _guardian_mod is not False else None


def _get_outbox() -> Any | None:
    """Lazily create and return the fault-recovery OutboxClient.

    Best-effort: returns ``None`` if the fault_recovery package is unavailable.
    The client is created once and reused for the life of the process.
    """
    global _outbox_client
    if _outbox_client is not None:
        return _outbox_client
    try:
        from beagle.fault_recovery.outbox import OutboxClient

        _outbox_client = OutboxClient()
        return _outbox_client
    except ImportError:
        _outbox_client = False
    return None


def _get_post_compaction_mod() -> Any | None:
    global _post_compaction_mod
    if _post_compaction_mod is not None:
        return _post_compaction_mod
    try:
        from beagle.context.post_compaction_rehydration import (
            on_post_compaction,
            save_compaction_checkpoint_for_orchestrator,
        )

        # SimpleNamespace for the same reason as _get_guardian_mod above:
        # both attributes here are plain functions, so a class-instance
        # namespace bound them and injected `self`. Every
        # `save_compaction_checkpoint_for_orchestrator(self)` /
        # `on_post_compaction(...)` call raised TypeError into a broad catch,
        # so post-compaction rehydration silently never ran either.
        _post_compaction_mod = SimpleNamespace(
            on_post_compaction=on_post_compaction,
            save_compaction_checkpoint_for_orchestrator=save_compaction_checkpoint_for_orchestrator,
        )
    except ImportError:
        _post_compaction_mod = False
    return _post_compaction_mod if _post_compaction_mod is not False else None


def _get_rag_staleness_mod() -> Any | None:
    global _rag_staleness_mod
    if _rag_staleness_mod is not None:
        return _rag_staleness_mod
    try:
        from beagle.context.rag_staleness import get_staleness_tracker

        _rag_staleness_mod = get_staleness_tracker
    except ImportError:
        _rag_staleness_mod = False
    return _rag_staleness_mod if _rag_staleness_mod is not False else None


# Public API exports
# NOTE: the underscore names below are deliberate backward-compat re-exports
# from the orchestrator package (F1 split) — declared in __all__ so vulture
# treats them as public API rather than dead imports.
__all__ = [
    "DEFAULT_MAX_NESTED_AGENTS",
    "DEFAULT_SUBPROCESS_TIMEOUT",
    "DEFAULT_VALIDATION_TIMEOUT",
    "ENHANCED_MODES",
    "MODEL_FALLBACKS",
    "SUBPROCESS_MEMORY_LIMIT",
    "SYSTEM_DIRECTIVE_TEMPLATE",
    "AgentPingMessage",
    "AgentState",
    "BeagleDAGNode",
    "CompressedAgentState",
    "CompressedKVPool",
    "DAGNode",
    "DAGOrchestrator",
    "GooseExecutionError",
    "_add_process",
    "_cleanup_processes",
    "_remove_process",
    "_run_evh_validation",
    "_signal_handler",
    "cleanup_agent_call_counter",
    "get_agent_call_count",
    "get_kv_pool",
    "get_output_dir",
    "get_recipes_dir",
    "get_workspace_root",
    "increment_agent_call",
    "ping_orchestrator",
    "reset_agent_call_counter",
    "set_orchestrator_channel",
]


def _write_text_sync(path: Path, content: str) -> None:
    """Blocking text write, for ``asyncio.to_thread`` inside async nodes.

    ASYNC230: ``open()`` in an async function blocks the event loop; the
    report/stub fallback writes below route through this helper instead.
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def get_workspace_root() -> Path:
    """Get workspace root from environment or default.

    Delegates to the canonical implementation in env_manager.
    All modules should use this single source of truth.

    Returns:
        Path to the workspace root directory.

    """
    from ..utils.env_manager import get_workspace_root as _get_ws

    return _get_ws()


def get_output_dir() -> Path:
    """Get the output directory for reports.

    Creates the directory if it doesn't exist.

    Returns:
        Path to the output directory.

    """
    # State, not assets — see get_output_dir() in utils/env_manager.py.
    # workspace_root resolves into site-packages under a wheel install.
    from beagle.config.paths import get_data_root

    output_dir = get_data_root() / "ai" / "analysis_reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


# Load environment variables for headless execution using safe allowlist

load_env_file()


# BeagleDAGNode and _run_evh_validation moved to core/orchestrator/executor.py


class DAGOrchestrator:
    """Beagle Stateful Orchestrator using Directed Acyclic Graphs."""

    def __init__(
        self,
        budget_usd: float = 10.0,
        workflow_id: str | None = None,
        workflow_name: str = "",
        model: str = "minimax-m3:cloud",
        permission_context: Any | None = None,
        enable_constraints: bool = True,
    ):
        """Initialize orchestrator with budget and model.

        Args:
            budget_usd: Total spending limit for the workflow.
            workflow_id: Optional workflow ID for checkpointing/resume.
            workflow_name: Optional workflow filename stem (e.g.
                ``"self-improvement"``) recorded into the replay manifest
                for fidelity. Defaults to empty; callers should set it.
            model: Model for cost tracking and context management.
            permission_context: ToolPermissionContext for granular tool safety.
            enable_constraints: Whether to load and enforce system constraints.

        """
        self.state = AgentState()
        self.enable_constraints = enable_constraints
        self.nodes: dict[str, DAGNode] = {}
        self.transitions: dict[str, list[tuple[Callable[[AgentState], bool], str]]] = {}
        self.start_node: str | None = None
        self.budget_usd = budget_usd
        self.model = model
        # v13.22.4 (P2-2): workflow_name captured into the replay
        # manifest at start_recording time.
        self._workflow_name: str = workflow_name
        # Strong references to fire-and-forget spawn tasks (RUF006). Without
        # retention the event loop may garbage-collect a task mid-execution.
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Claw-Code: Permission Context
        from beagle.permission_context import (
            DEFAULT_PERMISSION_CONTEXT,
        )

        self.permission_context = permission_context or DEFAULT_PERMISSION_CONTEXT

        # Initialize context-aware cost tracker
        self.cost_tracker = reset_cost_tracker(budget_usd, model)

        # Generate workflow ID if not provided (needed for logging)
        self.workflow_id = workflow_id or str(uuid.uuid4())
        self.state.workflow_id = self.workflow_id

        # Initialize context window manager
        self.context_manager = get_context_manager(model=model)
        self.context_manager.start_node("DAGOrchestrator-Init")

        # Initialize context integration for enhanced context management
        self._context_integration = get_context_integration(
            auto_compress_threshold=0.70,
            file_size_limit=500_000,
        )
        logger.info(
            f"[{self.workflow_id}] Context integration initialized (70% threshold, 500KB limit)"
        )

        # Initialize agent communication channel
        self._agent_channel: asyncio.Queue | None = None
        self._agent_messages: deque[dict] = deque(maxlen=100)

        # Spawned agent tracking
        self._spawned_agents: dict[str, dict] = {}
        self._agent_results: dict[str, str] = {}

        # Initialize A2A integration (config-driven)
        a2a_config = getattr(self, "_a2a_config", None)
        if a2a_config:
            configure_a2a(
                enabled=a2a_config.get("enabled", True),
                require_signatures=a2a_config.get("require_signatures", False),
            )

    def _refresh_constraints(self) -> None:
        """Load constraints from infrastructure registry into agent state."""
        if not self.enable_constraints:
            logger.info("Constraints disabled for this workflow")
            return

        try:
            from beagle.infrastructure.constraint_registry import (
                ConstraintRegistry,
            )

            registry = ConstraintRegistry()
            registry.load()
            self.state.constraints = registry.get_active()
            logger.info(
                f"Loaded {len(self.state.constraints)} active constraints into workflow state"
            )
        except ImportError:
            logger.warning(
                "Constraint registry not available - running without constraint awareness"
            )

    async def _start_agent_channel(self) -> None:
        """Initialize the agent communication channel."""
        if self._agent_channel is None:
            self._agent_channel = asyncio.Queue(maxsize=100)
            await set_orchestrator_channel(self._agent_channel)
            logger.info(f"[Orchestrator] Agent channel initialized for workflow {self.workflow_id}")

    async def _process_agent_pings(self, timeout: float = 0.1) -> list[dict]:
        """Process any pending agent ping messages.

        Args:
            timeout: How long to wait for each message.

        Returns:
            List of processed messages.

        """
        messages = []  # type: ignore[var-annotated]
        if self._agent_channel is None:
            return messages

        while not self._agent_channel.empty():
            try:
                msg = await asyncio.wait_for(self._agent_channel.get(), timeout=timeout)
                messages.append(msg)
                self._agent_messages.append(msg)

                # Track spawned agent results
                msg_type = msg.get("type")
                if msg_type == "spawn_completion":
                    agent_id = msg.get("agent_id", "")
                    # A2A: Verify agent result authenticity
                    if is_a2a_enabled() and not verify_agent_result(msg):
                        logger.warning(
                            f"[Orchestrator] A2A verification failed for "
                            f"agent {agent_id} — rejecting result"
                        )
                        continue
                    self._spawned_agents[agent_id] = msg
                    if msg.get("result"):
                        self._agent_results[agent_id] = msg["result"]
                    logger.info(f"[Orchestrator] Received spawn completion from {agent_id}")

                elif msg_type == "node_completion":
                    node_name = msg.get("agent_id", "")
                    logger.info(f"[Orchestrator] Received node completion from {node_name}")

            except TimeoutError:
                break
            except ImportError as e:
                logger.error(f"[Orchestrator] Error processing agent ping: {e}")

        return messages

    async def _wait_for_agent_ping(
        self,
        agent_id: str,
        timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
    ) -> dict | None:
        """Wait for a specific agent to ping completion.

        Args:
            agent_id: The agent ID to wait for.
            timeout: Maximum time to wait.

        Returns:
            The agent's completion message or None if timeout.

        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            # Process any pending messages
            await self._process_agent_pings(timeout=0.1)

            # Check if we have the result
            if agent_id in self._spawned_agents:
                return self._spawned_agents[agent_id]

            # Small sleep to prevent busy-waiting
            await asyncio.sleep(0.5)

        logger.warning(f"[Orchestrator] Timeout waiting for agent {agent_id}")
        return None

    def get_spawned_result(self, agent_id: str) -> str | None:
        """Get the result from a spawned agent.

        Args:
            agent_id: The agent ID to look up.

        Returns:
            The agent's result or None.

        """
        return self._agent_results.get(agent_id)

    async def _spawn_agent(
        self, parent_state: AgentState, intent: str, steering_block: str = ""
    ) -> str:
        """Spawn a subagent using ForkContext for cache reuse.

        Args:
            parent_state: State of the parent node.
            intent: Task for the subagent.
            steering_block: Optional priority guidance.

        Returns:
            The spawned agent ID.

        """
        from beagle.context.fork_context import ForkContext

        agent_id = f"spawn-{uuid.uuid4().hex}"
        logger.info(f"[Orchestrator] Spawning subagent {agent_id}...")

        # A2A: Sign the delegation message for cryptographic authenticity
        if is_a2a_enabled():
            _delegation = sign_delegation(
                workflow_id=self.workflow_id,
                agent_id=agent_id,
                task_description=intent,
            )
            logger.debug(f"[Orchestrator] A2A delegation signed for {agent_id}")

        # Create fork context (Phase 8.5)
        # Reuse prompt_cache and memory_pointers from current node execution context
        # (Note: In a stateless call this might need more plumbing)
        memory_index = getattr(self, "_last_memory_pointers", "")

        # Build fork
        fork = ForkContext.from_parent(
            parent_cache=getattr(self, "_last_prompt_cache", PromptCache()),
            parent_state=parent_state,
            parent_memory=memory_index,
            fork_id=agent_id,
        )

        # Build prompt from fork (reuses parent's cached static content)
        spawn_prompt = fork.build_prompt(
            intent=intent,
            steering=steering_block,
        )

        # Execute in background. The task reference MUST be retained
        # (RUF006): an unreferenced task can be garbage-collected mid-run.
        # (The previous ignore-comment here named a rule that does not
        # exist — "asyncio-dangling-task" — so it silenced nothing.)
        _spawned_task = asyncio.create_task(
            self._execute_spawned_agent(
                prompt=spawn_prompt,
                agent_id=agent_id,
                fork=fork,
                parent_state=parent_state,
            )
        )
        self._background_tasks.add(_spawned_task)
        _spawned_task.add_done_callback(self._background_tasks.discard)

        return agent_id

    async def _execute_spawned_agent(
        self, prompt: str, agent_id: str, fork: Any, parent_state: AgentState
    ) -> None:
        """Internal execution of a spawned agent."""
        try:
            # We reuse execute_headless_goose from nodes.py
            from beagle.core.nodes import execute_headless_goose

            # Simple system directive for spawns
            system_directive = "You are a subagent. Fulfill the intent and return a final answer."

            final_answer, _raw_stdout = await execute_headless_goose(
                prompt=prompt,
                system_directive=system_directive,
                node_name=f"spawn-{agent_id}",
                timeout=DEFAULT_SUBPROCESS_TIMEOUT,
            )

            # Merge fork observations back
            fork.add_observation(final_answer[:1000])

            # Update local tracking
            self._spawned_agents[agent_id] = {
                "status": "completed",
                "result": final_answer,
                "observations": fork.get_scratchpad(),
            }
            self._agent_results[agent_id] = final_answer

            # Signal orchestrator channel
            await ping_orchestrator(
                {
                    "type": "spawn_completion",
                    "agent_id": agent_id,
                    "result": final_answer,
                    "success": True,
                }
            )

        except (
            RuntimeError,
            ValueError,
            TimeoutError,
            OSError,
        ) as e:  # catch: NARROWED  # RATIONALE=spawn execution/timeout/IO failures. CancelledError deliberately propagates.
            logger.error(f"[Orchestrator] Spawned agent {agent_id} failed: {e}")
            await ping_orchestrator(
                {
                    "type": "spawn_completion",
                    "agent_id": agent_id,
                    "error": str(e),
                    "success": False,
                }
            )

    def add_node(self, node: DAGNode, is_start: bool = False) -> None:
        """Add a node to the DAG.

        Args:
            node: The DAGNode to add.
            is_start: Whether this is the start node.

        """
        # Convert to BeagleDAGNode
        beagle_node = BeagleDAGNode.from_node(node)
        self.nodes[beagle_node.name] = beagle_node
        self.transitions[beagle_node.name] = []
        if is_start:
            self.start_node = beagle_node.name

    def add_transition(
        self,
        from_node: str,
        to_node: str,
        condition: Callable[[AgentState], bool] | None = None,
    ) -> None:
        """Add a transition between two nodes.

        Args:
            from_node: The name of the source node.
            to_node: The name of the target node.
            condition: Optional condition for the transition.

        """
        if from_node not in self.nodes:
            raise ValueError(f"Source node '{from_node}' not found in DAG.")
        if to_node not in self.nodes and to_node != "END":
            raise ValueError(f"Target node '{to_node}' not found in DAG.")

        cond = condition or (lambda _: True)
        self.transitions[from_node].append((cond, to_node))

    async def _diffadapt_routing(self, query: str) -> str:
        """Determine task complexity for routing (Phase 2: Reflex Arc)."""
        ql = query.lower().strip()

        # Hardcoded trivial keywords
        TRIVIAL_ONLY = frozenset(
            {
                "ping",
                "status",
                "hello",
                "hi",
                "hey",
                "bye",
                "thanks",
                "thank you",
                "help",
                "what can you do",
                "list commands",
                "show help",
                "version",
                "about",
            }
        )

        # Check for exact match or command prefix only
        is_trivial = False
        for kw in TRIVIAL_ONLY:
            if ql == kw or ql.startswith(kw + " ") or ql.startswith(kw + "\n"):
                is_trivial = True
                break

        if not is_trivial:
            logger.info("⚡ [DiffAdapt] Query requires full workflow execution")
            return "HARD"

        # For trivial queries, use skill library cache check
        if ENHANCED_MODES:
            try:
                from beagle.core.skill_library import SkillLibrary

                skill_lib = SkillLibrary()
                matched = await skill_lib.search_skills(query)
                if matched and matched[0].use_count > 3:
                    logger.info(
                        f"🎯 [Skill Match] Found '{matched[0].name}' (uses: {matched[0].use_count})"
                    )
                    logger.info("⚡ [Cached Path] Using proven skill route")
                    return "EASY"
            except (
                ImportError,
                AttributeError,
                KeyError,
                OSError,
            ) as e:  # catch: NARROWED  # RATIONALE=optional skill library unavailable or schema drift
                logger.debug(f"Skill lookup failed: {e}")

        logger.info("⚡ [Reflex Arc] Query is trivial command")
        return "EASY"

    async def run(self, initial_query: str) -> AgentState:
        """Execute the DAG workflow.

        Args:
            initial_query: The user query to process.

        Returns:
            Final AgentState after execution.

        """
        if not self.start_node:
            logger.error("No start node defined for DAG.")
            return self.state

        # Validate the query before processing
        is_valid, error = await validate_query_async(initial_query)
        if not is_valid:
            logger.error(f"Query validation failed: {error}")
            self.state.errors.append(f"Invalid query: {error}")
            return self.state

        self.state.query = initial_query

        # Inject self into state for node/tool access (needed for spawn cache reuse)
        self.state._orchestrator = self  # type: ignore[attr-defined]

        # Emit WorkflowStarted event
        get_event_bus().publish(
            WorkflowStarted(
                workflow_id=self.workflow_id,
                query=self.state.query,
                budget_usd=self.budget_usd,
                mode=self.state.workflow_mode if hasattr(self.state, "workflow_mode") else "audit",
                nodes=list(self.nodes.keys()),
                metadata={"model": self.model},
            )
        )

        try:
            return await self._run_inner()
        finally:
            # v0.3.0: Always clean up agent counter, even on crash
            await cleanup_agent_call_counter(self.workflow_id)

    async def _run_inner(self) -> AgentState:
        """Inner execution loop, separated for try/finally cleanup in run().

        Decomposed into four phases for readability and testability:
          1. ``_run_startup()`` — checkpoint restore, health monitor,
             constraint refresh, replay recorder, agent channel.
          2. Pre-loop routing — DiffAdapt complexity classification
             and early-exit on trivial queries.
          3. The main DAG loop (inlined — the loop body *is* the
             orchestration logic and is not extracted).
          4. ``_run_finalize()`` — WorkflowCompleted event, cost
             report, replay manifest, autoDream consolidation.
        """
        await self._run_startup()

        # ── Pre-loop routing ──────────────────────────────────────────────

        complexity = await self._diffadapt_routing(self.state.query)
        if complexity == "EASY":
            # Check if there's substantive content
            query_len = len(self.state.query.strip())
            if query_len > 50:
                logger.info(
                    f"Reflex Arc: Query ({query_len} chars) may have "
                    "substance, running full workflow..."
                )
            else:
                logger.info("Reflex Arc: Query is trivial command. Bypassing deep deliberation...")
                logger.info("System Status: Nominal.")
                return self.state

        # Initialize steering manager — steering state (steer_api.json,
        # steer.md) is runtime state, so it anchors to the writable data
        # root, not the package install tree (see paths.py contract).
        from beagle.config.paths import get_data_root

        steering_manager = SteeringManager(get_data_root(), self.workflow_id)

        # Initialize memory index (Phase 8.2)
        from beagle.memory.memory_index import MemoryIndex

        memory_index = MemoryIndex(get_data_root())
        memory_pointers = memory_index.get_semantic_layer()

        current_node_name = self.start_node

        while current_node_name:
            # Phase 3: Check for steering guidance between nodes
            directive = steering_manager.check()
            if directive.has_guidance:
                get_event_bus().publish(
                    SteeringReceived(workflow_id=self.workflow_id, source=directive.source)
                )

                # 1. Handle budget override
                if directive.budget_override_usd is not None:
                    logger.info(
                        f"Steering: Budget override to ${directive.budget_override_usd:.2f}"
                    )
                    self.budget_usd = directive.budget_override_usd
                    self.cost_tracker.budget_usd = directive.budget_override_usd

                # 2. Handle node skipping
                if current_node_name in directive.skip_nodes:
                    logger.info(f"Steering: Skipping node {current_node_name}")
                    get_event_bus().publish(
                        NodeSkipped(
                            workflow_id=self.workflow_id,
                            node_name=current_node_name,
                            reason="Skipped via steering",
                        )
                    )
                    # Move to next node if possible
                    self.state.completed_nodes.append(current_node_name)
                    next_node_name = None
                    for condition, target_node in self.transitions[current_node_name]:
                        if condition(self.state):
                            next_node_name = target_node
                            break
                    current_node_name = next_node_name
                    continue

                # v0.3.0: Inject steering guidance into state for prompt influence
                if directive.priority_guidance:
                    _inject = _get_steering_injection()
                    if _inject and self.state.raw_execution_context:
                        self.state.raw_execution_context = _inject(
                            self.state.raw_execution_context, directive
                        )
                        logger.info(
                            f"[{self.workflow_id}] Steering guidance "
                            f"injected from {directive.source}"
                        )

            # Proactive context monitoring — check BEFORE each node so
            # checkpoints are saved while there is still room to compact.
            try:
                _ctx_monitor = _get_ctx_monitor()
                if _ctx_monitor:
                    _ctx_status = _ctx_monitor.check_before_work(
                        iteration=len(self.state.completed_nodes),
                        task_name=current_node_name,
                    )
                    if _ctx_status.is_critical:
                        logger.critical(
                            f"[{self.workflow_id}] Context CRITICAL at "
                            f"{_ctx_status.percentage:.0%} — saving checkpoint"
                        )
                        _ctx_monitor.save_checkpoint()
                    elif _ctx_status.should_compact:
                        logger.warning(
                            f"[{self.workflow_id}] Context at "
                            f"{_ctx_status.percentage:.0%} — "
                            f"checkpoint ready for compaction"
                        )
                        _ctx_monitor.save_checkpoint()
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
                ValueError,
            ) as _exc:  # catch: NARROWED  # RATIONALE=monitor API drift or checkpoint-save I/O on a best-effort path; monitoring must never abort the workflow
                logger.debug(
                    "[%s] best-effort context monitoring failed: %s",
                    self.workflow_id,
                    _exc,
                )

            # Check budget before each node
            if not self.cost_tracker.check_budget():
                logger.error(
                    f"Budget exceeded: "
                    f"${self.cost_tracker.total_cost_usd:.4f} / "
                    f"${self.budget_usd:.2f}"
                )
                self.state.errors.append("Budget exceeded - workflow halted")
                return self.state

            node = self.nodes[current_node_name]
            logger.info(f"Transitioning to Node: {node.name}")

            # v0.3.0: Guardian approval check before execution
            try:
                _gmod = _get_guardian_mod()
                if _gmod:
                    guardian = _gmod.get_guardian()
                    action = _gmod.GuardianAction(
                        action_type="workflow_node",
                        description=f"Execute workflow node: {node.name}",
                        details={
                            "node_name": node.name,
                            "skill_name": getattr(node, "skill_name", ""),
                            "workflow_id": self.workflow_id,
                        },
                        risk_level=_gmod.RiskLevel.MEDIUM
                        if getattr(node, "require_approval", False)
                        else _gmod.RiskLevel.LOW,
                    )
                    result = guardian.check_approval(action)
                    if result.decision == _gmod.ApprovalDecision.DENIED:
                        logger.warning(f"[{self.workflow_id}] Guardian DENIED node {node.name}")
                        self.state.errors.append(f"Guardian denied execution of node {node.name}")
                        # Skip to next node
                        self.state.completed_nodes.append(node.name)
                        next_node_name = None
                        for condition, target_node in self.transitions.get(current_node_name, []):
                            if condition(self.state):
                                next_node_name = target_node
                                break
                        current_node_name = next_node_name
                        continue
            except (
                ImportError,
                AttributeError,
                KeyError,
                OSError,
            ) as e:  # catch: NARROWED  # RATIONALE=guardian module unavailable or state-schema drift
                logger.debug(f"Guardian check skipped: {e}")

            # Phase 1 (fault-recovery hardening): write the node's in-flight
            # state to the Redis Streams WAL BEFORE execution and a completion
            # event AFTER. Both are best-effort — an outbox failure must never
            # break the workflow (it is a hardening aid, not a hard dependency).
            _outbox = await self._get_outbox()
            if _outbox is not None:
                try:
                    await _outbox.write_pending(
                        workflow_id=self.workflow_id,
                        node_name=node.name,
                        dag_id=self._workflow_name or self.workflow_id,
                        state_snapshot={
                            "status": "running",
                            "skill_name": getattr(node, "skill_name", ""),
                        },
                    )
                except Exception as _oe:  # broad catch: outbox is best-effort
                    logger.debug(
                        "[%s] Outbox pending write failed (node %s): %s",
                        self.workflow_id,
                        node.name,
                        _oe,
                    )

            success = await node.execute(  # type: ignore[attr-defined]
                self.state,
                steering_directive=directive,
                memory_pointers=memory_pointers,
            )
            if not success:
                # Non-stop execution: log failure but CONTINUE to next node.
                # A single node failure does NOT abort the entire workflow.
                # The error is recorded in state.errors for downstream nodes
                # and the final synthesis report.
                logger.error(
                    f"[{self.workflow_id}] Node {node.name} failed — "
                    f"recording error and continuing workflow (non-stop mode)"
                )
                # State already has the error appended by node.execute()

            # Phase 1 (fault-recovery hardening): record completion in the
            # WAL after the node finishes. Best-effort — never breaks the loop.
            if _outbox is not None:
                try:
                    await _outbox.write_completed(
                        workflow_id=self.workflow_id,
                        node_name=node.name,
                        dag_id=self._workflow_name or self.workflow_id,
                        state_snapshot={
                            "status": "completed" if success else "failed",
                            "skill_name": getattr(node, "skill_name", ""),
                        },
                    )
                except Exception as _oe:  # broad catch: outbox is best-effort
                    logger.debug(
                        "[%s] Outbox completed write failed (node %s): %s",
                        self.workflow_id,
                        node.name,
                        _oe,
                    )

            # 3. Handle stop_after_node
            if directive.has_guidance and directive.stop_after_node == current_node_name:
                logger.info(f"Halting workflow after node {current_node_name}")
                return self.state

            # Track completed nodes for checkpointing
            self.state.completed_nodes.append(node.name)

            # Mark safe compaction point after each node completes
            try:
                _ctxm = _get_ctx_monitor()
                if _ctxm:
                    _ctxm.mark_safe_point(f"completed_{node.name}")
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
            ) as _exc:  # catch: NARROWED  # RATIONALE=monitor unavailable or checkpoint-write failure on a best-effort path
                logger.debug(
                    "best-effort safe-point marking after node %s failed: %s",
                    node.name,
                    _exc,
                )

            # v0.3.0: Trim unbounded state lists between nodes
            trim_state_lists(self.state.__dict__)

            # Phase 8.3: Context compression after each node
            # Check if accumulated context exceeds threshold and compress if needed
            # Post-compaction rehydration: always re-inject Beagle identity and
            # task context after compression so the session never stops.
            if node.context_compression and self.state.should_compress_context():
                pre_size = len(self.state.raw_execution_context)
                self.state.compress_context()
                post_size = len(self.state.raw_execution_context)
                if post_size < pre_size:
                    logger.info(
                        f"[{self.workflow_id}] Context compressed after {node.name}: "
                        f"{pre_size} → {post_size} chars "
                        f"({post_size / max(pre_size, 1):.0%} of original)"
                    )

                    # Post-compaction rehydration: FULL rehydration with checkpoint,
                    # constraints, project context, tool routing state, and resume
                    # directives. The lightweight version was losing critical context.
                    _pcm = _get_post_compaction_mod()
                    if _pcm:
                        try:
                            # Record compaction for adaptive chunking and capture
                            # orchestrator state for checkpoint tool routing fields
                            _ctxm = _get_ctx_monitor()
                            if _ctxm:
                                _ctxm.record_compaction(
                                    context_size=pre_size,
                                    node_name=node.name,
                                )
                                _ctxm.register_orchestrator_state(self)

                            # Save checkpoint BEFORE rehydrating so we capture
                            # current state including tool routing and progress
                            checkpoint = _pcm.save_compaction_checkpoint_for_orchestrator(self)

                            # Inject compaction count into checkpoint for rehydration
                            _ctxm2 = _get_ctx_monitor()
                            if _ctxm2:
                                checkpoint.compaction_count = _ctxm2._compaction_count
                                checkpoint.compaction_history = _ctxm2._compaction_history[-10:]

                            rehydration = _pcm.on_post_compaction(
                                checkpoint=checkpoint,
                                workflow_id=self.workflow_id,
                                query=self.state.query,
                                completed_nodes=self.state.completed_nodes,
                                errors=self.state.errors,
                            )
                            self.state.raw_execution_context = (
                                rehydration + "\n\n" + self.state.raw_execution_context
                            )

                            # Re-inject constraints that were lost during compaction
                            self._refresh_constraints()

                            # Trigger RAG reingestion if stale (compaction marks it stale)
                            _tracker_cls = _get_rag_staleness_mod()
                            if _tracker_cls:
                                tracker = _tracker_cls()
                                if tracker.is_stale and tracker.can_reingest():
                                    logger.info(
                                        f"[{self.workflow_id}] RAG stale after "
                                        "compaction, triggering reingestion"
                                    )
                                    await tracker.trigger_reingest_if_stale()

                            logger.info(
                                f"[{self.workflow_id}] Full post-compaction rehydration: "
                                f"+{len(rehydration)} chars (checkpoint + constraints "
                                f"+ project context + resume directive)"
                            )
                        except (
                            ImportError,
                            AttributeError,
                            KeyError,
                            OSError,
                        ) as e:  # catch: NARROWED  # RATIONALE=rehydration module/checkpoint unavailable or state-schema drift
                            logger.debug(f"Post-compaction rehydration failed: {e}")
                    else:
                        logger.debug("post_compaction_rehydration module not available")

            # Agentic Context Engineering (ACE): Append Delta State
            self.state.metadata[node.name] = {
                "status": "SUCCESS",
                "timestamp": time.time(),
            }

            # Evaluate transitions
            next_node_name = None
            for condition, target_node in self.transitions[node.name]:
                if condition(self.state):
                    next_node_name = target_node
                    break

            current_node_name = next_node_name

        logger.info("Beagle DAG Execution Complete.")

        # Reset compaction counter on successful completion — next run starts fresh
        try:
            _compaction_state = Path.home() / ".beagle" / "compaction_state.json"
            if _compaction_state.exists():
                _compaction_state.unlink()
            _progress_file = Path.home() / ".beagle" / "progress.md"
            if _progress_file.exists():
                _progress_file.unlink()
        except OSError as _exc:  # catch: NARROWED  # RATIONALE=exists/unlink are pure filesystem ops; a missing or locked file must not fail a completed workflow
            logger.debug("best-effort compaction/progress cleanup failed: %s", _exc)

        await self._run_post_workflow_validation()

        # Log cost summary from tracker
        summary = self.cost_tracker.get_summary()
        logger.info(f"Total Tokens: {summary['total_tokens']:,}")
        logger.info(f"Total Cost: ${summary['total_cost_usd']:.6f}")
        logger.info(f"Budget Remaining: ${summary['budget_remaining_usd']:.6f}")
        if summary["node_costs"]:
            logger.info("Per-Node Costs:")
            for node_name, cost in summary["node_costs"].items():
                logger.info(f"  {node_name}: ${cost:.6f}")

        # Output final report (guard against empty synthesis)
        # v13.21: Tighten the empty-synthesis guard. Previously this only
        # checked for whitespace; it now also rejects reports that are just
        # a leaked prompt template (the synthesis-writer echoing its own
        # "INCIDENT POSTMORTEM" header back without content) by checking
        # for the absence of structural markers (headings, citations,
        # non-empty body lines). The error is appended to state.errors
        # AND a synthesis_failed flag is set so the MCP layer at
        # mcp_utility_server.py can return status=completed_with_errors
        # instead of status=completed.
        output_dir = get_output_dir()
        final_path = output_dir / "beagle_final_report.md"
        report_content = getattr(self.state, "final_report", "") or ""
        # v13.22.5: mirror the report into the workspace project tree
        # (<workspace>/ai/analysis_reports) so it is visible where the
        # workflow ran, not only under data_root. Best-effort — the data_root
        # primary is always written.
        try:
            from beagle.utils.env_manager import get_output_dir_mirror

            _mirror = get_output_dir_mirror()
            if _mirror is not None and _mirror != output_dir:
                _mirror = _mirror / "beagle_final_report.md"
                # staged_write is used below for the primary; mirror here is
                # a lightweight copy after the primary write succeeds.
        except (ImportError, AttributeError, ValueError):  # catch: NARROWED
            _mirror = None
        # Structural-content check: at least 3 non-empty body lines OR
        # at least one citation (`path.py:NNN` or `file:N`).
        import re as _re_v1321

        _nonempty_lines = [ln for ln in report_content.splitlines() if ln.strip()]
        _has_citation = bool(
            _re_v1321.search(r"[A-Za-z0-9_./-]+\.(?:py|yaml|yml|md|json|toml):\d+", report_content)
        )
        _synthesis_has_content = len(_nonempty_lines) >= 3 and (
            len(report_content) > 200 or _has_citation
        )
        if report_content.strip() and _synthesis_has_content:
            result = staged_write(final_path, report_content)
            if result.success:
                logger.info(f"Final Artifact persisted to: {final_path}")
            else:
                logger.error(
                    f"[{self.workflow_id}] Staged write FAILED for final report: {result.error}"
                )
                # Fallback: write directly if lint fails (reports are markdown, not code)
                await asyncio.to_thread(_write_text_sync, final_path, report_content)
                logger.info(f"Final Artifact persisted to: {final_path} (direct fallback)")
            # v13.22.5: mirror to the workspace project tree (best-effort).
            if _mirror is not None and _mirror != final_path:
                try:
                    await asyncio.to_thread(_write_text_sync, _mirror, report_content)
                    logger.info(f"Final Artifact mirrored to: {_mirror}")
                except OSError as _mirror_err:  # catch: NARROWED  # filesystem op
                    logger.warning(f"Report mirror failed to {_mirror}: {_mirror_err}")
        else:
            # v13.21: Mark synthesis as failed and surface a structured error.
            # This is the silent-failure-mode fix — previously the orchestrator
            # would write an empty report file and the MCP layer would report
            # status=completed, hiding the failure from CI and callers.
            _synthesis_failure_reason = (
                "Synthesis node produced empty or near-empty final_report. "
                "Possible causes: (1) synthesis-writer echoed its own prompt "
                "template back as the artifact (no upstream content to "
                "consolidate), (2) an upstream phase failed without an error "
                "being appended, (3) the model cold-started and produced a "
                "degenerate completion."
            )
            if not report_content.strip():
                _synthesis_failure_reason = (
                    "Synthesis node produced empty final_report. "
                    "Check synthesis node execution. No report file written."
                )
            logger.error(
                f"[{self.workflow_id}] SYNTHESIS FAILURE — {_synthesis_failure_reason} "
                f"nonempty_lines={len(_nonempty_lines)}, has_citation={_has_citation}, "
                f"len={len(report_content)}"
            )
            self.state.errors.append(
                f"Synthesis node produced empty final_report: {_synthesis_failure_reason}"
            )
            # v13.22.4 (P3-4): set the explicit synthesis_failed flag so
            # the MCP layer at mcp_utility_server.py can return
            # status=synthesis_failed (a distinct terminal status from
            # ``completed_with_errors``) instead of treating the echoed
            # prompt as a legitimate report.
            self.state.synthesis_failed = True
            # v13.21: Write a stub report so the file is at least present for
            # debugging. The stub includes the failure reason and the empty
            # content the model produced, which is useful for post-mortem.
            try:
                stub = (
                    f"# Beagle Workflow Report — SYNTHESIS FAILED\n\n"
                    f"**Workflow**: {self.workflow_id}\n"
                    f"**Reason**: {_synthesis_failure_reason}\n\n"
                    f"## Empty content from synthesis-writer\n\n"
                    f"```\n{report_content[:1000]}\n```\n"
                )
                final_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(_write_text_sync, final_path, stub)
                logger.info(f"[{self.workflow_id}] Wrote synthesis-failure stub to {final_path}")
            except OSError as stub_exc:  # catch: NARROWED  # RATIONALE=mkdir + file write are filesystem ops; a stub-write failure must not mask the real synthesis failure being reported
                logger.warning(
                    f"[{self.workflow_id}] Failed to write synthesis-failure stub: {stub_exc}"
                )

        # Final phase: emit events, write reports, consolidate memory.
        await self._run_finalize(summary, output_dir, final_path)
        return self.state

    # ── _run_inner phase helpers (extracted for readability) ─────────────

    async def _run_post_workflow_validation(self) -> None:
        """Run post-workflow validation feedback loop if configured."""
        try:
            from beagle.config.config import get_config
            from beagle.validation.feedback import run_validation

            config = get_config()
            val_config = getattr(config, "validation", None)
            run_after = getattr(val_config, "run_after_workflow", True)
            if run_after:
                logger.info(f"[{self.workflow_id}] Running post-workflow validation...")
                val_result = await run_validation(
                    workflow_id=self.workflow_id,
                )
                if val_result.failures:
                    logger.warning(
                        f"[{self.workflow_id}] Validation found {len(val_result.failures)} issues"
                    )
                    for f in val_result.failures[:5]:
                        self.state.errors.append(f"validation: {f}")
                else:
                    logger.info(f"[{self.workflow_id}] Validation passed")
        except ImportError as exc:
            logger.warning(
                "[%s] Cannot import post-workflow validation (%s); "
                "the workflow result is not validated.",
                self.workflow_id,
                exc,
            )
        except (AttributeError, RuntimeError, ValueError, OSError) as e:
            logger.warning(f"[{self.workflow_id}] Post-workflow validation failed: {e}")

    async def post_workflow_cleanup(self) -> None:
        """Alias for post-workflow validation and cleanup."""
        await self._run_post_workflow_validation()

    async def _run_startup(self) -> None:
        """Phase 1 of :meth:`_run_inner` — startup, restore, monitor.

        Restores from a checkpoint if a previous run was interrupted,
        starts the health monitor, refreshes persisted constraints into
        state, starts the replay recorder (if enabled), and opens the
        agent communication channel.

        All steps are best-effort: each is wrapped in a broad-catch
        with a debug log because none of them is required for a
        successful workflow — they're observability / continuity aids.
        """
        logger.info("Initiating Stateful DAG Orchestrator (Beagle v12.0)")
        logger.info(f"Workflow ID: {self.workflow_id}")
        logger.info(f"Budget: ${self.budget_usd:.2f}")
        if len(self.state.query) > 100:
            logger.info(f"Target: '{self.state.query[:100]}...'")
        else:
            logger.info(f"Target: '{self.state.query}'")

        # v0.3.0: Restore from checkpoint if previous run was interrupted
        try:
            from beagle.lifecycle.restore import restore_from_checkpoint

            restored = await restore_from_checkpoint()
            if restored:
                logger.info(f"[{self.workflow_id}] Restored from previous checkpoint")
        except ImportError as exc:
            logger.warning(
                "[%s] Cannot import checkpoint restore (%s); "
                "an interrupted previous run will not be resumed.",
                self.workflow_id,
                exc,
            )
        except (
            AttributeError,
            KeyError,
            OSError,
        ) as e:  # catch: NARROWED  # RATIONALE=checkpoint state drift or unreadable file
            logger.debug(f"Checkpoint restore skipped: {e}")

        # v0.3.0: Start health monitor for continuous observability
        try:
            from beagle.health import get_health_monitor

            health_monitor = get_health_monitor()
            await health_monitor.start()
            logger.info(f"[{self.workflow_id}] Health monitor started")
        except ImportError as exc:
            logger.warning(
                "[%s] Cannot import the health monitor (%s); "
                "the run proceeds without continuous health observability.",
                self.workflow_id,
                exc,
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"Health monitor start failed: {e}")

        # Load persisted constraints into state (Phase 4)
        self._refresh_constraints()

        # Start reproducibility recorder if config is enabled
        try:
            from beagle.reproducibility import get_replay_recorder

            # v13.22.4 (P2-2): populate workflow_name / beagle_version /
            # config_snapshot at recording time so the manifest is
            # replayable across upgrades. Config snapshot is best-effort
            # — fall back to {} if the config layer isn't initialised yet.
            try:
                import dataclasses

                from beagle.config.config import get_config

                _cfg_snapshot = dataclasses.asdict(get_config())
            except (ImportError, AttributeError, RuntimeError, ValueError) as _cfg_exc:
                logger.debug(
                    f"Reproducibility recorder: config snapshot unavailable "
                    f"({_cfg_exc.__class__.__name__}); recording empty snapshot."
                )
                _cfg_snapshot = {}

            from beagle.constants import PACKAGE_VERSION as _BEAGLE_VERSION

            recorder = get_replay_recorder()
            recorder.start_recording(
                workflow_id=self.workflow_id,
                workflow_name=getattr(self, "_workflow_name", "") or "",
                query=self.state.query,
                beagle_version=_BEAGLE_VERSION,
                config_snapshot=_cfg_snapshot,
            )
            logger.info(
                f"[{self.workflow_id}] Reproducibility recorder started "
                f"(beagle_version={_BEAGLE_VERSION}, "
                f"workflow_name={getattr(self, '_workflow_name', '') or ''!r})"
            )
        except ImportError as exc:
            logger.warning(
                "[%s] Cannot import the reproducibility recorder (%s); "
                "this run will not be replayable.",
                self.workflow_id,
                exc,
            )
        except (
            AttributeError,
            KeyError,
            OSError,
        ) as e:  # catch: NARROWED  # RATIONALE=recorder API drift or state-dir write failure
            logger.debug(f"Reproducibility recorder start skipped: {e}")

        # Initialize agent communication channel
        await self._start_agent_channel()

    async def _run_finalize(
        self,
        summary: dict[str, Any],
        output_dir: Path,
        final_path: Path,
    ) -> None:
        """Phase 4 of :meth:`_run_inner` — emit, report, consolidate.

        Writes the cost report, emits the ``WorkflowCompleted`` event,
        saves the replay manifest, and runs autoDream consolidation.
        All sub-steps are best-effort (broad-catch with a debug log) so
        that a failure in finalization never blocks the workflow's
        return value.
        """
        # Write cost report
        cost_report_path = output_dir / f"cost_report_{self.workflow_id}.txt"
        result = staged_write(cost_report_path, self.cost_tracker.format_report())
        if result.success:
            logger.info(f"Cost report saved to: {cost_report_path}")
        else:
            # Fallback: direct write for non-code files
            await asyncio.to_thread(
                _write_text_sync, cost_report_path, self.cost_tracker.format_report()
            )
            logger.info(f"Cost report saved to: {cost_report_path} (direct fallback)")

        # Emit WorkflowCompleted event
        # wall-clock-ok: state.start_time is a checkpointed field, so a
        # resumed workflow's duration spans a process restart — monotonic
        # is not comparable across restarts, wall clock is required.
        _completed_wall = time.time()
        get_event_bus().publish(
            WorkflowCompleted(
                workflow_id=self.workflow_id,
                success=not bool(self.state.errors),
                total_cost_usd=summary["total_cost_usd"],
                budget_usd=self.budget_usd,
                total_tokens=summary["total_tokens"],
                duration_seconds=_completed_wall - self.state.start_time,
                completed_nodes=len(self.state.completed_nodes),
                errors=len(self.state.errors),
            )
        )

        # Save replay manifest for reproducibility
        try:
            from beagle.reproducibility import get_replay_recorder

            recorder = get_replay_recorder()
            if recorder.is_recording:
                manifest = recorder.stop_recording()
                if manifest:
                    from beagle.config.paths import get_data_root

                    manifest_path = (
                        get_data_root() / "replays" / f"{self.workflow_id}_manifest.json"
                    )
                    manifest.save(manifest_path)
                    logger.info(f"[{self.workflow_id}] Replay manifest saved: {manifest_path}")
        except ImportError as exc:
            logger.warning(
                "[%s] Cannot import the reproducibility recorder (%s); "
                "no replay manifest is written for this run.",
                self.workflow_id,
                exc,
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
        ) as e:  # catch: NARROWED  # RATIONALE=manifest.save() is file I/O + JSON serialisation
            logger.debug(f"Reproducibility manifest save skipped: {e}")

        # Phase 8.4: autoDream post-run consolidation
        try:
            from beagle.memory.autodream import AutoDream

            dreamer = AutoDream(get_workspace_root())
            # Run in background or wait? Instructions say "Completion path"
            # We'll use asyncio.run or similar if we are in an async context (which we are)
            report = await dreamer.consolidate()
            logger.info(
                f"[autoDream] Consolidated: pruned={report.pruned_count}, "
                f"merged={report.merged_count}, refreshed={report.refreshed_count}, "
                f"index={report.index_tokens_before}→{report.index_tokens_after} tokens"
            )
            get_event_bus().publish(
                AutoDreamCompleted(
                    workflow_id=self.workflow_id,
                    pruned=report.pruned_count,
                    merged=report.merged_count,
                    refreshed=report.refreshed_count,
                    index_tokens_before=report.index_tokens_before,
                    index_tokens_after=report.index_tokens_after,
                )
            )
        except (
            ImportError,
            AttributeError,
            RuntimeError,
            OSError,
            ValueError,
        ) as e:  # catch: NARROWED  # RATIONALE=optional autodream module unavailable or index-write failure; post-run consolidation must never fail the workflow
            logger.warning(f"[autoDream] Consolidation failed (non-fatal): {e}")

        # v13.21.13: Runtime-side enforcement of <post_final_answer_fold
        # required="true"/> from the Top-of-Mind doctrine. Without this hook,
        # the fold is model-cooperative: the session-end model must remember
        # to call check_and_fold_context, and on providers that prefer
        # narrating over tool-calling (deepseek observed in
        # Projects/Skylon_Ecosystem/skylon 2026-06-12), the fold is silently
        # skipped. The next session then has no rehydration sidecar and
        # re-bootstraps from scratch. enforce_post_final_answer_fold()
        # ALWAYS writes the sidecar, regardless of the reported percentage
        # or which model drove the session.
        try:
            from beagle.context.post_compaction_rehydration import (
                enforce_post_final_answer_fold,
            )

            fold_result = enforce_post_final_answer_fold(
                workflow_id=self.workflow_id,
                query=self.state.query,
                completed_nodes=self.state.completed_nodes,
                project_dir=get_workspace_root(),
                percentage=0.0,
            )
            logger.info(
                f"[{self.workflow_id}] Post-final-answer fold enforced: "
                f"action={fold_result.get('action')} "
                f"sidecar_chars={fold_result.get('sidecar_chars')} "
                f"fold_id={fold_result.get('fold_id') or '(pending)'} "
                f"status={fold_result.get('status')}"
            )
        except (ImportError, RuntimeError, OSError, ValueError, AttributeError) as e:
            # Finalize must never fail because the post-fold hook had a
            # problem — log and continue. The sidecar write is best-effort
            # but the durable copy is in progress.xml.
            logger.warning(
                f"[{self.workflow_id}] Post-final-answer fold enforcement failed (non-fatal): {e}"
            )


# Public alias per Beagle entry point contract
AutonomousOrchestrator = DAGOrchestrator
