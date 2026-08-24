"""LangGraph graph builders for Beagle v13.

Provides StateGraph construction for standard workflows
with conditional edges and checkpointing support.

H-MEM Integration:
- Hydration node runs before execution nodes
- Context is pre-loaded from RAG, constraints, and manifests
- Ensures constraints survive compaction boundaries

Deep Fork Optimization:
- Uses pyrsistent for structural sharing in state forks
- Avoids expensive deepcopy operations for GRPO trajectories
- Immutable data structures prevent race conditions

v13.19.4 release notes (formerly inline at the PYRSISTENT_AVAILABLE
constant, moved to top-of-module per R5.3 doctrine):
- pyrsistent removed as a phantom dependency. The optional import
  was previously wrapped in try/except; we now always use the
  copy.deepcopy fallback (the same code path triggered when
  pyrsistent was unavailable). PYRSISTENT_AVAILABLE is preserved
  for any callers that introspect it (always returns False now).
- See CHANGELOG.md v13.19.4 for the full release notes.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from collections.abc import Callable
from typing import Any

from langgraph.graph import StateGraph

# langgraph's END is typed Any in its stubs; pin it to the str constant it
# actually is so circuit-breaker returns satisfy the no-any-return gate.
END: str = "__end__"

# v13.19.4: BEAGLE_SKIP_HYDRATION is the documented escape hatch for users
# (and tests) who want to bypass the on_session_start hydration hook.
# Setting BEAGLE_SKIP_HYDRATION=1 in the environment short-circuits the
# hydration call in the graph builder below. The check is intentionally
# performed at module import time so that downstream code can rely on
# the _BEAGLE_SKIP_HYDRATION flag throughout the session.
_BEAGLE_SKIP_HYDRATION = os.environ.get("BEAGLE_SKIP_HYDRATION", "0") == "1"


def is_hydration_skipped() -> bool:
    """Return True if BEAGLE_SKIP_HYDRATION=1 is set in the environment.

    The graph builder consults this flag to decide whether to invoke
    the on_session_start hydration hook before running a workflow.
    """
    return _BEAGLE_SKIP_HYDRATION


# Initialize logger FIRST before any usage
logger = logging.getLogger("Beagle.graph")

# PYRSISTENT_AVAILABLE is always False post-v13.19.4. See the module
# docstring for the full release-note context. Kept for any external
# callers that still introspect the flag.
PYRSISTENT_AVAILABLE = False
freeze = None  # type: ignore[assignment]
pmap = None  # type: ignore[assignment]
thaw = None  # type: ignore[assignment]

# Import event bus
from ..events import BeagleEvent, get_event_bus  # ruff: ignore[E402]

# Fixed absolute imports to relative
from .nodes import (  # ruff: ignore[E402]
    execute_goose_node,
    execution_node,
    planning_node,
    synthesis_node,
    verification_node,
)
from .state import (  # ruff: ignore[E402]
    BeagleState,
    OperationalMetadata,
    create_initial_state,
)

# ── Circuit Breaker Constants ──────────────────────────────────────────────

MAX_ITERATIONS = 25  # Maximum graph iterations before forced termination
MAX_ERRORS = 3  # Maximum consecutive errors before forced termination


def _get_operational(state: dict[str, Any]) -> OperationalMetadata:
    """Extract OperationalMetadata from state, defaulting to a fresh instance."""
    raw = state.get("operational", {})
    if isinstance(raw, OperationalMetadata):
        return raw
    if isinstance(raw, dict):
        return OperationalMetadata(**raw)
    return OperationalMetadata()


def _increment_iteration(state: dict[str, Any]) -> dict[str, Any]:
    """Return a state update that increments the iteration counter.

    Called by routers to track each graph traversal cycle.
    """
    op = _get_operational(state)
    return {
        "operational": {
            "iteration": op.iteration + 1,
            "error_count": op.error_count,
            "total_iterations": op.total_iterations + 1,
        }
    }


def _increment_error(state: dict[str, Any]) -> dict[str, Any]:
    """Return a state update that increments the error counter.

    Called when a node produces an error to track consecutive failures.
    """
    op = _get_operational(state)
    return {
        "operational": {
            "iteration": op.iteration,
            "error_count": op.error_count + 1,
            "total_iterations": op.total_iterations,
        }
    }


# H-MEM: Import hydration node for context pre-loading
try:
    from .hydration_node import hydration_node

    HYDRATION_AVAILABLE = True
except ImportError:
    HYDRATION_AVAILABLE = False
    logger.debug("Hydration node not available — context will not be pre-loaded")

# ── GRPO helper (inline, avoids importing the legacy standalone GRPO) ────────────

GRPO_N_TRAJECTORIES = 3
_MAX_GRPO_METADATA_KEYS = 50  # v0.3.0: cap trajectory metadata growth


def _deep_fork_state(state: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy state dict to isolate mutable nested structures.

    v13.4 Optimization: Uses pyrsistent for structural sharing when available.
    This provides:
    - O(1) fork operations via structural sharing
    - Immutable data structures prevent race conditions
    - Memory efficient - shared structure between forks
    - Thread-safe by design (immutable)

    The LangGraph state dict contains mutable containers (lists, dicts)
    that are shared by reference when using state.copy(deep=False) or
    dict(state).  Parallel GRPO trajectories that mutate nested keys
    (e.g. ``metadata``, ``errors``, ``fact_ledger``) would otherwise
    contaminate one another because only the top-level dict is cloned,
    not the values it points to.

    This helper ensures every trajectory receives a fully independent
    snapshot so that concurrent writes never race.

    Args:
        state: The workflow state dictionary to fork.

    Returns:
        A deep copy that is safe to mutate without affecting the original.

    """
    # Use pyrsistent for structural sharing if available (v13.4 optimization)
    if PYRSISTENT_AVAILABLE and freeze is not None:
        try:
            # Convert to persistent map (immutable, structural sharing)
            persistent_state = freeze(state)
            # Create a new fork - this is O(1) with structural sharing
            # The persistent map shares structure with the original
            forked_pmap = pmap(persistent_state)
            # Convert back to regular dict for LangGraph compatibility
            return thaw(forked_pmap)  # type: ignore[return-value]
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning(f"pyrsistent fork failed ({e}), falling back to deepcopy")

    # Fallback to copy.deepcopy for compatibility.  ``copy.deepcopy``
    # handles all standard Python containers correctly.  TypedDict
    # instances are just dicts at runtime, so this works transparently.
    #
    # SECURITY: Catch only serialization errors, not ALL exceptions.
    # A broad ``except Exception`` silently produces a partial shallow
    # copy that would corrupt sibling GRPO trajectories.
    try:
        return copy.deepcopy(state)
    except (TypeError, copy.Error) as e:
        # Only catch serialization/pickling errors — not KeyboardInterrupt,
        # SystemExit, MemoryError, etc.
        logger.warning(f"deepcopy failed ({e}), performing manual fork of known fields")
        forked = dict(state)  # shallow copy as base
        # Isolate every mutable nested structure explicitly
        for key, val in list(forked.items()):
            if isinstance(val, dict):
                forked[key] = {
                    k: copy.deepcopy(v) if isinstance(v, dict | list) else v for k, v in val.items()
                }
            elif isinstance(val, list):
                forked[key] = [
                    copy.deepcopy(item) if isinstance(item, dict | list) else item for item in val
                ]
            # Scalars (str, int, float, bool, None) are safe to share
        return forked


async def _grpo_node(
    state: dict[str, Any],
    skill: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    output_key: str,
) -> dict[str, Any]:
    """Run GRPO: N parallel trajectories with a judge picking the best.

    Inline version that avoids the unused legacy grpo.py. Runs
    GRPO_N_TRAJECTORIES parallel executions, then uses a synthesis agent
    to pick the best trajectory based on quality scoring.

    BUGFIX (v12.3): Each trajectory now receives a deep-forked copy of
    the state so that mutations to ``shared_workspace`` / ``metadata``
    / other nested dicts are fully isolated and cannot contaminate
    sibling trajectories.
    """
    import asyncio as _asyncio
    import time as _time

    prompt = prompt_builder(state)  # type: ignore[misc]
    state.get("_complexity", "normal")

    # Distinct strategies for each trajectory
    strategies = [
        "Conservative approach: minimal, safe changes that address the core problem",
        "Thorough approach: comprehensive coverage with all edge cases considered",
        "Novel approach: creative alternative solution that challenges assumptions",
    ]

    logger.info(f"[GRPO:{skill}] Launching {GRPO_N_TRAJECTORIES} trajectories")

    async def run_trajectory(idx: int) -> tuple[int, str, float]:
        start = _time.monotonic()
        # BUGFIX: Deep-fork state so mutations from this trajectory are
        # fully isolated from sibling trajectories.  A shallow copy
        # (dict(state)) would share ``metadata``, ``shared_workspace``,
        # and other nested mutables by reference, causing race conditions.
        trajectory_state = _deep_fork_state(state)

        # Per-trajectory prompt with distinct strategy
        trajectory_prompt = (
            f"{prompt}\n\n"
            f"[TRAJECTORY {idx + 1}/{GRPO_N_TRAJECTORIES}]\n"
            f"Strategy: {strategies[idx]}\n"
            f"Execute with this approach."
        )
        try:
            result = await execute_goose_node(
                trajectory_state,
                skill,
                lambda _s, _tp=trajectory_prompt: _tp,  # type: ignore[misc]
                f"{output_key}_trajectory_{idx}",
                model_override=trajectory_state.get("_model_override"),
            )
            latency = _time.monotonic() - start
            output = result.get("metadata", {}).get(f"{output_key}_trajectory_{idx}", "")
            return idx, output, latency
        except asyncio.CancelledError:
            raise
        except (RuntimeError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            logger.warning(f"[GRPO:{skill}] Trajectory {idx} failed: {exc}")
            return idx, "", 0.0

    # Run all trajectories in parallel with timeout to prevent hangs
    _GRPO_TIMEOUT_SECONDS = 300  # 5 minutes per GRPO batch
    try:
        results: list[tuple[int, str, float]] = await _asyncio.wait_for(
            _asyncio.gather(  # type: ignore[arg-type]
                *[run_trajectory(i) for i in range(GRPO_N_TRAJECTORIES)],
                return_exceptions=True,
            ),
            timeout=_GRPO_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.error(f"[GRPO:{skill}] All trajectories timed out after {_GRPO_TIMEOUT_SECONDS}s")
        results = [(i, "", 0.0) for i in range(GRPO_N_TRAJECTORIES)]

    # Filter valid results
    valid: list[tuple[int, str, float]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"[GRPO:{skill}] Trajectory raised exception: {r}")
        elif r[1].strip():
            valid.append(r)

    if not valid:
        logger.warning(f"[GRPO:{skill}] All trajectories empty, using empty result")
        return {"completed_nodes": [f"{skill}(grpo)"]}

    # Judge: use synthesis-writer to pick the best trajectory
    if len(valid) == 1:
        best_idx, best_output, _ = valid[0]
    else:
        best_idx, best_output = await _grpo_judge(skill, prompt, valid)

    logger.info(f"[GRPO:{skill}] Selected trajectory {best_idx + 1}/{len(valid)}")

    # Store trajectories in metadata for downstream access
    best_metadata = {
        **{f"{output_key}_trajectory_{i}": r for i, r, _ in valid},
        f"{output_key}": best_output,
        "grpo_selected_trajectory": best_idx,
        "grpo_trajectory_count": len(valid),
    }

    # v0.3.0: Cap metadata growth to prevent unbounded accumulation
    merged_metadata = {**state.get("metadata", {}), **best_metadata}
    if len(merged_metadata) > _MAX_GRPO_METADATA_KEYS:
        # Keep the newest keys (best_metadata) and trim oldest from existing
        keys_to_keep = list(merged_metadata)[-_MAX_GRPO_METADATA_KEYS:]
        merged_metadata = {k: merged_metadata[k] for k in keys_to_keep}

    return {
        output_key: best_output,
        "metadata": merged_metadata,
        "completed_nodes": [f"{skill}(grpo)"],
    }


async def _grpo_judge(
    skill: str,
    original_prompt: str,
    valid_results: list[tuple[int, str, float]],
) -> tuple[int, str]:
    """Select the best trajectory using a judge LLM call."""
    comparisons = "\n\n".join(
        f"--- TRAJECTORY {idx + 1} ({latency:.1f}s) ---\n{output[:3000]}"
        for idx, output, latency in valid_results
    )

    judge_prompt = (
        f"Evaluate these {len(valid_results)} execution trajectories for quality.\n\n"
        f"ORIGINAL TASK: {original_prompt[:500]}\n\n"
        f"{comparisons}\n\n"
        f"Select the BEST trajectory. Respond with ONLY the number (1, 2, or 3) "
        f"of the best trajectory, followed by a one-sentence reason."
    )

    try:
        result = await execute_goose_node(
            {},
            "synthesis-writer",
            lambda _: judge_prompt,
            "grpo_judge_output",
            model_override="nemotron-3-ultra:cloud",
        )
        judge_text = result.get("metadata", {}).get("grpo_judge_output", "1")
        # Parse first digit from response
        import re

        match = re.search(r"\b([123])\b", judge_text[:20])
        if match:
            selected = int(match.group(1)) - 1
            if selected < len(valid_results):
                return selected, valid_results[selected][1]
    except (ValueError, IndexError, RuntimeError) as exc:
        logger.warning(f"[GRPO:{skill}] Judge failed: {exc}")

    # Fallback to first valid
    return 0, valid_results[0][1]


async def _ensemble_node(
    state: dict[str, Any],
    skill: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    output_key: str,
) -> dict[str, Any]:
    """Run a panel-of-experts ensemble: N coding models compete, judge distils best.

    Uses MultiModelEnsemble from utils/ensemble.py. Panel models and judge
    are read from config.toml [ensemble] section. All execution is remote
    on Ollama Cloud — no local compute required.
    """
    # Fixed absolute imports
    from ..config.config import get_config
    from ..utils.ensemble import MultiModelEnsemble
    from .nodes import get_recipes_dir

    # v1.0.0: this read config.toml by hand — `config._raw["ensemble"]` with a
    # manual tomllib re-parse as backup — and carried its own inline model
    # literals. That bypassed EnsembleConfig, which loader.py already
    # populates from the very same [ensemble] table, so the panel was declared
    # in three places (here, config/schema.py, config/models.py) and all three
    # drifted apart. Pull from the config object: [ensemble] in config.toml is
    # the SSOT and every value below is configurable there.
    ensemble_cfg = get_config().ensemble

    panel_models = ensemble_cfg.panel_models
    judge_model = ensemble_cfg.judge_model
    timeout = ensemble_cfg.timeout_per_model

    prompt = prompt_builder(state)  # type: ignore[misc]

    # Load recipe as system directive
    recipe_path = get_recipes_dir() / f"{skill}.xml"
    system_directive = ""
    if recipe_path.exists():
        system_directive = recipe_path.read_text(encoding="utf-8")

    logger.info(
        f"[Ensemble:{skill}] Launching panel of {len(panel_models)} models: "
        f"{', '.join(panel_models)} | judge: {judge_model}"
    )

    ensemble = MultiModelEnsemble(
        models=panel_models,
        judge_model=judge_model,
        timeout_per_model=timeout,
    )

    result = await ensemble.run(
        prompt=prompt,
        system_directive=system_directive,
        judge_prompt=(
            "Focus on: correctness, security, edge case handling, and code quality. "
            "The combined answer should be production-ready code."
        ),
    )

    # Log per-model results
    for r in result.responses:
        status = "SELECTED" if r.selected else "runner-up"
        logger.info(
            f"[Ensemble:{skill}] {r.model}: score={r.quality_score:.1f}, "
            f"latency={r.latency_seconds:.1f}s ({status})"
        )
    logger.info(f"[Ensemble:{skill}] Judge verdict: {result.judge_summary}")

    # Store all model responses in metadata for transparency
    ensemble_metadata = {
        f"{output_key}_ensemble_{r.model.replace(':', '_')}": r.final_answer[:3000]
        for r in result.responses
    }
    ensemble_metadata[output_key] = result.combined_response
    ensemble_metadata[f"{output_key}_judge_verdict"] = result.judge_summary
    ensemble_metadata[f"{output_key}_best_model"] = result.best_response.model

    return {
        output_key: result.combined_response,
        "metadata": {**state.get("metadata", {}), **ensemble_metadata},
        "completed_nodes": [f"{skill}(ensemble)"],
    }


def _check_circuit_breaker(state: dict[str, Any]) -> str | None:
    """Check circuit breaker limits. Returns '__end__' if exceeded, None otherwise."""
    op = _get_operational(state)
    if op.total_iterations >= MAX_ITERATIONS:
        logger.error(
            f"[Circuit Breaker] MAX_ITERATIONS ({MAX_ITERATIONS}) exceeded — terminating graph. "
            f"total_iterations={op.total_iterations}"
        )
        return END
    if op.error_count >= MAX_ERRORS:
        logger.error(
            f"[Circuit Breaker] MAX_ERRORS ({MAX_ERRORS}) exceeded — terminating graph. "
            f"error_count={op.error_count}"
        )
        return END
    return None


def executor_router(state: dict[str, Any]) -> str:
    """Explicit router for execution node conditional edges.

    Checks circuit breakers first. If iteration/error limits are exceeded,
    terminates the graph (__end__). Otherwise routes to verification or
    synthesis based on execution context presence.
    """
    # Circuit breaker check
    breaker = _check_circuit_breaker(state)
    if breaker is not None:
        return breaker

    ctx = state.get("raw_execution_context", "")
    if not ctx or not str(ctx).strip():
        logger.info("[Graph] Execution context empty - routing to synthesis")
        return "synthesis"

    logger.info(f"[Graph] Execution context has {len(str(ctx))} chars - routing to verification")
    return "verification"


def reviewer_router(state: dict[str, Any]) -> str:
    """Explicit router for verification/review node conditional edges.

    After verification, routes to synthesis unless circuit breaker triggers.
    If verification produced errors, increments error counter.
    """
    # Circuit breaker check
    breaker = _check_circuit_breaker(state)
    if breaker is not None:
        return breaker

    # If verification produced new errors, track them
    errors = state.get("errors", [])
    if errors:
        op = _get_operational(state)
        if op.error_count >= MAX_ERRORS:
            logger.error("[Circuit Breaker] Error limit hit during review — terminating")
            return END

    return "synthesis"


def error_router(state: dict[str, Any], target_node: str = "planning") -> str:
    """Explicit router for error-handling conditional edges.

    Instead of unconditionally routing errors back to planner_node (which
    causes infinite loops), this router checks circuit breaker limits and
    terminates the graph if thresholds are exceeded.

    Args:
        state: The current graph state.
        target_node: The node to route to for retry (default: "planning").

    Returns:
        Target node name or END if circuit breaker triggered.

    """
    op = _get_operational(state)

    # Check total iterations
    if op.total_iterations >= MAX_ITERATIONS:
        logger.error(
            f"[Circuit Breaker] MAX_ITERATIONS ({MAX_ITERATIONS}) exceeded in error router — "
            f"terminating graph. total_iterations={op.total_iterations}"
        )
        return END

    # Check consecutive errors
    if op.error_count >= MAX_ERRORS:
        logger.error(
            f"[Circuit Breaker] MAX_ERRORS ({MAX_ERRORS}) exceeded in error router — "
            f"terminating graph. error_count={op.error_count}"
        )
        return END

    # Route to retry target
    logger.warning(
        f"[Error Router] Routing to {target_node} for retry "
        f"(iteration={op.total_iterations}, errors={op.error_count})"
    )
    return target_node


def build_research_graph(include_hydration: bool = True) -> StateGraph:
    """Build the standard research workflow graph with optional context hydration.

    Flow (with hydration): hydration → planning → execution → verification → synthesis
    Flow (without hydration): planning → execution → verification → synthesis

    H-MEM Integration: When include_hydration=True, the hydration node runs first
    to pre-load relevant context from RAG, constraints registry, and manifests.

    Args:
        include_hydration: If True, add hydration node before planning (default: True)

    """
    graph = StateGraph(BeagleState)

    # H-MEM: Add hydration node if available and requested
    if include_hydration and HYDRATION_AVAILABLE:
        graph.add_node("hydration", hydration_node)  # type: ignore[type-var]
        graph.add_node("planning", planning_node)  # type: ignore[type-var]
        graph.add_node("execution", execution_node)  # type: ignore[type-var]
        graph.add_node("verification", verification_node)  # type: ignore[type-var]
        graph.add_node("synthesis", synthesis_node)  # type: ignore[type-var]

        graph.set_entry_point("hydration")
        graph.add_edge("hydration", "planning")
        graph.add_edge("planning", "execution")
        # SECURITY: Use explicit executor_router (circuit breaker aware)
        # instead of bare lambda/_should_verify which had no iteration guard
        graph.add_conditional_edges(
            "execution",
            executor_router,
            {
                "verification": "verification",
                "synthesis": "synthesis",
                END: END,
            },
        )
        # SECURITY: Use reviewer_router for post-verification routing
        graph.add_conditional_edges(
            "verification",
            reviewer_router,
            {
                "synthesis": "synthesis",
                END: END,
            },
        )
        graph.add_edge("synthesis", END)
    else:
        graph.add_node("planning", planning_node)  # type: ignore[type-var]
        graph.add_node("execution", execution_node)  # type: ignore[type-var]
        graph.add_node("verification", verification_node)  # type: ignore[type-var]
        graph.add_node("synthesis", synthesis_node)  # type: ignore[type-var]

        graph.set_entry_point("planning")
        graph.add_edge("planning", "execution")
        # SECURITY: Use explicit executor_router (circuit breaker aware)
        graph.add_conditional_edges(
            "execution",
            executor_router,
            {
                "verification": "verification",
                "synthesis": "synthesis",
                END: END,
            },
        )
        # SECURITY: Use reviewer_router for post-verification routing
        graph.add_conditional_edges(
            "verification",
            reviewer_router,
            {
                "synthesis": "synthesis",
                END: END,
            },
        )
        graph.add_edge("synthesis", END)

    return graph


def build_workflow_graph(
    nodes: list[dict[str, Any]],
    transitions: list[tuple[str, str, str | None]],
    _workflow_query: str = "",
    complexity: str = "normal",
) -> StateGraph:
    """Build a custom workflow graph from node specs and transitions.

    SP-7: implementation lives in ``core/graph_builder.py`` (a leaf module that
    imports no module importing it back), breaking the
    core.graph <-> core.workflow_loader cycle. Re-exported here for backward
    compatibility.
    """
    from .graph_builder import build_workflow_graph as _build

    return _build(nodes, transitions, _workflow_query, complexity)


def _scaled_synthesis_timeout(base: int) -> int:
    """Return a scaled timeout for synthesis-class nodes (WP-2 M15).

    SP-7: implementation lives in core/graph_builder.py.
    """
    from .graph_builder import _scaled_synthesis_timeout as _scaled

    return _scaled(base)


async def run_workflow(
    query: str,
    workflow_name: str = "research",
    budget: float = 10.0,
    steering: str = "",
    thread_id: str | None = None,
    resume: bool = False,
    workflow_mode: str = "audit",
    approval_granted: bool = False,
) -> dict[str, Any]:
    """Compile and run a workflow graph.

    Args:
        query: User query to process
        workflow_name: Name of the workflow to run (resolved by CLI, passed here as path or name)
        budget: Maximum budget in USD
        steering: Optional steering prompt
        thread_id: Optional thread ID for checkpointing
        resume: If True, resume from checkpoint instead of starting fresh
        workflow_mode: One of "audit", "develop", "research". Controls file write permissions.
        approval_granted: If True, bypass all human-in-the-loop approval gates.

    Returns:
        Final state dict

    """
    # OpenTelemetry span for workflow execution (Phase 5)
    try:
        from opentelemetry import trace  # type: ignore[import-untyped,attr-defined]

        _graph_tracer = trace.get_tracer("beagle.graph", "13.4.0")
    except ImportError:
        from contextlib import nullcontext

        class _Nop:
            def start_as_current_span(self, *_a: Any, **_kw: Any) -> Any:
                return nullcontext()

        _graph_tracer = _Nop()  # type: ignore[assignment]

    with _graph_tracer.start_as_current_span("beagle.run_workflow") as wf_span:
        try:
            if hasattr(wf_span, "set_attribute"):
                wf_span.set_attribute("beagle.workflow", workflow_name)
                wf_span.set_attribute("beagle.budget", budget)
                wf_span.set_attribute("beagle.mode", workflow_mode)

            result = await _run_workflow_impl(
                query,
                workflow_name,
                budget,
                steering,
                thread_id,
                resume,
                workflow_mode,
                approval_granted,
            )
            return result
        except RuntimeError as e:
            if hasattr(wf_span, "set_status"):
                from opentelemetry.trace.status import StatusCode  # type: ignore[import-untyped]

                wf_span.set_status(StatusCode.ERROR, str(e))
            raise


async def _run_workflow_impl(
    query: str,
    workflow_name: str = "research",
    budget: float = 10.0,
    steering: str = "",
    thread_id: str | None = None,
    resume: bool = False,
    workflow_mode: str = "audit",
    approval_granted: bool = False,
) -> dict[str, Any]:
    """Implementation of run_workflow — separated for OTEL wrapping."""
    # Fixed absolute imports
    from pathlib import Path

    from ..config._config_path import find_metaprompts_dir
    from .workflow_loader import load_workflow_graph

    # Dispatch to the correct graph based on workflow_name. S5/S6: workflow
    # data is detached to the canonical config root, so resolve via
    # find_metaprompts_dir() — a wheel install has no workflow YAMLs in-package.
    metaprompts = find_metaprompts_dir().resolve()

    resolved_name = workflow_name.rstrip(".yaml").replace("-", "_")
    wf_path = Path(workflow_name)
    if wf_path.is_file():
        # Already an absolute path to a file
        graph_path = wf_path
    elif (metaprompts / f"{resolved_name}.yaml").exists():
        graph_path = metaprompts / f"{resolved_name}.yaml"
    elif (metaprompts / workflow_name).exists():
        # Try original name as a path under metaprompts/
        graph_path = metaprompts / workflow_name
    else:
        # Fall back to built-in research graph
        logger.warning(
            f"Workflow not found: {workflow_name} "
            f"(resolved: {resolved_name}), falling back to research graph"
        )
        graph_path = None

    from ..config.config import assess_task_complexity

    if graph_path:
        try:
            complexity = assess_task_complexity(query)
            # v13.22.4 (P2-3): thread steering_prompt through to the
            # workflow loader so requires_steering_mode validation can
            # fail loudly when the caller passes a conflicting steering.
            graph = load_workflow_graph(
                graph_path,
                workflow_query=query,
                complexity=complexity,
                steering_prompt=steering,
            )
            logger.info(f"Loaded workflow graph from: {graph_path} (complexity={complexity})")
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            logger.error(
                f"Failed to load workflow graph from {graph_path}: {exc}, "
                "falling back to research graph"
            )
            graph = build_research_graph()
    else:
        graph = build_research_graph()

    # Use checkpointer if available
    checkpointer_cm = None
    try:
        from ..memory.checkpointer import get_checkpointer

        checkpointer_cm = get_checkpointer()
    except (ImportError, RuntimeError) as e:
        logger.debug(f"Checkpointer not available: {e}")

    config = {"configurable": {"thread_id": thread_id or workflow_name}}

    if checkpointer_cm:
        async with checkpointer_cm as checkpointer:
            compiled = graph.compile(checkpointer=checkpointer)
            if resume and thread_id:
                result = await compiled.ainvoke(None, config)  # type: ignore[call-overload]
            else:
                initial = create_initial_state(
                    query=query,
                    workflow_id=workflow_name,
                    steering_prompt=steering,
                    workflow_mode=workflow_mode,
                    approval_granted=approval_granted,
                )
                result = await compiled.ainvoke(initial, config)  # type: ignore[call-overload]
    else:
        compiled = graph.compile(checkpointer=None)
        if resume and thread_id:
            result = await compiled.ainvoke(None, config)  # type: ignore[call-overload]
        else:
            initial = create_initial_state(
                query=query,
                workflow_id=workflow_name,
                steering_prompt=steering,
                workflow_mode=workflow_mode,
                approval_granted=approval_granted,
            )
            result = await compiled.ainvoke(initial, config)  # type: ignore[call-overload]

    # Log completion status
    completed = result.get("completed_nodes", [])
    errors = result.get("errors", [])
    logger.info(f"[Workflow] Result keys: {list(result.keys())}")
    logger.info(f"[Workflow] All errors: {errors}")
    logger.info(f"[Workflow] Completed {len(completed)} nodes: {completed}")
    if errors:
        logger.warning(f"[Workflow] Completed with {len(errors)} errors: {errors}")

    return result  # type: ignore[no-any-return]


async def stream_submit_message(
    query: str,
    workflow_name: str = "research",
    budget: float = 10.0,
    steering: str = "",
    thread_id: str | None = None,
    resume: bool = False,
    workflow_mode: str = "audit",
    approval_granted: bool = False,
):
    """Run a workflow and yield execution events in real-time.

    Inspired by claw-code unified streaming events.
    """
    queue = asyncio.Queue()  # type: ignore[var-annotated]
    bus = get_event_bus()

    # Subscribe to all events for this loop
    async def subscriber(event: BeagleEvent):
        await queue.put(event)

    await bus.subscribe(subscriber)  # type: ignore[arg-type,call-arg,misc]

    try:
        # Start workflow in a background task
        workflow_task = asyncio.create_task(
            run_workflow(
                query=query,
                workflow_name=workflow_name,
                budget=budget,
                steering=steering,
                thread_id=thread_id,
                resume=resume,
                workflow_mode=workflow_mode,
                approval_granted=approval_granted,
            )
        )

        while not workflow_task.done() or not queue.empty():
            try:
                # Wait for event or task completion
                if not queue.empty():
                    yield await queue.get()
                else:
                    await asyncio.sleep(0.1)
                    if workflow_task.done() and queue.empty():
                        break
            except asyncio.CancelledError:
                raise
            except (RuntimeError, TimeoutError, ValueError, KeyError, TypeError) as e:
                logger.error(f"Error in streaming loop: {e}")
                break

        # Final result check
        if workflow_task.done():
            await workflow_task
            # Final completion signal if needed

    finally:
        await bus.unsubscribe(subscriber)  # type: ignore[arg-type,func-returns-value,misc]
