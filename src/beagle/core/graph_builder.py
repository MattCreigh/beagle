"""Workflow graph builder — a leaf module that imports no module from core.

SP-7 (beagle-spotless-phase2): ``build_workflow_graph`` previously lived in
``core/graph.py`` and was imported by ``core/workflow_loader.py``. Because
``core.graph._run_workflow_impl`` lazily imports ``load_workflow_graph`` from
``core.workflow_loader``, the two formed a cycle:

    core.graph  ->  core.workflow_loader  ->  core.graph

Extracting the builder (and its pure helpers) here lets ``workflow_loader``
depend on this leaf, and ``core.graph`` re-exports the same builder — no cycle.

Layer order (directive SP-7):
    constants  <-  schema  <-  loader  <-  config  <-  services  <-  cli

This module sits at the ``services`` boundary: it composes LangGraph from
node/transition specs but imports no orchestrator module that imports it back.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from typing import Any

from langgraph.graph import StateGraph

from ..utils.prompt_builder import make_prompt_builder
from .state import BeagleState, OperationalMetadata

# langgraph's END is typed Any in its stubs; pin it to the str constant it
# actually is so circuit-breaker returns satisfy the no-any-return gate.
END: str = "__end__"

logger = logging.getLogger("Beagle.graph_builder")

# ── Circuit Breaker Constants ──────────────────────────────────────────────
MAX_ITERATIONS = 25  # Maximum graph iterations before forced termination
MAX_ERRORS = 3  # Maximum consecutive errors before forced termination

GRPO_N_TRAJECTORIES = 3
_MAX_GRPO_METADATA_KEYS = 50  # v0.3.0: cap trajectory metadata growth

# PYRSISTENT is disabled post-v13.19.4 (see graph.py module docstring).
PYRSISTENT_AVAILABLE = False
freeze = None
pmap = None
thaw = None


def _get_operational(state: dict[str, Any]) -> OperationalMetadata:
    """Extract OperationalMetadata from state, defaulting to a fresh instance."""
    raw = state.get("operational", {})
    if isinstance(raw, OperationalMetadata):
        return raw
    if isinstance(raw, dict):
        return OperationalMetadata(**raw)
    return OperationalMetadata()


def _increment_iteration(state: dict[str, Any]) -> dict[str, Any]:
    """Return a state update that increments the iteration counter."""
    op = _get_operational(state)
    return {
        "operational": {
            "iteration": op.iteration + 1,
            "error_count": op.error_count,
            "total_iterations": op.total_iterations + 1,
        }
    }


def _increment_error(state: dict[str, Any]) -> dict[str, Any]:
    """Return a state update that increments the error counter."""
    op = _get_operational(state)
    return {
        "operational": {
            "iteration": op.iteration,
            "error_count": op.error_count + 1,
            "total_iterations": op.total_iterations,
        }
    }


def _deep_fork_state(state: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy state dict to isolate mutable nested structures.

    The LangGraph state dict contains mutable containers (lists, dicts) that
    are shared by reference when using a shallow copy. Parallel GRPO
    trajectories that mutate nested keys would contaminate one another. This
    helper gives every trajectory a fully independent snapshot.

    Falls back to ``copy.deepcopy`` (PYRSISTENT is disabled post-v13.19.4).
    """
    try:
        return copy.deepcopy(state)
    except (TypeError, copy.Error) as e:
        logger.warning(f"deepcopy failed ({e}), performing manual fork of known fields")
        forked = dict(state)
        for key, val in list(forked.items()):
            if isinstance(val, dict):
                forked[key] = {
                    k: copy.deepcopy(v) if isinstance(v, dict | list) else v for k, v in val.items()
                }
            elif isinstance(val, list):
                forked[key] = [
                    copy.deepcopy(item) if isinstance(item, dict | list) else item for item in val
                ]
        return forked


async def _grpo_node(
    state: dict[str, Any],
    skill: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    output_key: str,
) -> dict[str, Any]:
    """Run GRPO: N parallel trajectories with a judge picking the best."""
    import asyncio as _asyncio
    import time as _time

    from .nodes import execute_goose_node

    prompt = prompt_builder(state)
    strategies = [
        "Conservative approach: minimal, safe changes that address the core problem",
        "Thorough approach: comprehensive coverage with all edge cases considered",
        "Novel approach: creative alternative solution that challenges assumptions",
    ]

    logger.info(f"[GRPO:{skill}] Launching {GRPO_N_TRAJECTORIES} trajectories")

    async def run_trajectory(idx: int) -> tuple[int, str, float]:
        start = _time.monotonic()
        trajectory_state = _deep_fork_state(state)
        trajectory_prompt = (
            f"{prompt}\n\n"
            f"[TRAJECTORY {idx + 1}/{GRPO_N_TRAJECTORIES}]\n"
            f"Strategy: {strategies[idx]}\n"
            f"Execute with this approach."
        )

        def _trajectory_prompt(_state: dict[str, Any]) -> str:
            return trajectory_prompt

        try:
            result = await execute_goose_node(
                trajectory_state,
                skill,
                _trajectory_prompt,
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

    _GRPO_TIMEOUT_SECONDS = 300  # 5 minutes per GRPO batch
    try:
        gathered = await _asyncio.wait_for(
            _asyncio.gather(
                *[run_trajectory(i) for i in range(GRPO_N_TRAJECTORIES)],
                return_exceptions=True,
            ),
            timeout=_GRPO_TIMEOUT_SECONDS,
        )
        results: list[tuple[int, str, float]] = [r for r in gathered if isinstance(r, tuple)]
    except TimeoutError:
        logger.error(f"[GRPO:{skill}] All trajectories timed out after {_GRPO_TIMEOUT_SECONDS}s")
        results = [(i, "", 0.0) for i in range(GRPO_N_TRAJECTORIES)]

    valid: list[tuple[int, str, float]] = []
    for r in results:
        if r[1].strip():
            valid.append(r)

    if not valid:
        logger.warning(f"[GRPO:{skill}] All trajectories empty, using empty result")
        return {"completed_nodes": [f"{skill}(grpo)"]}

    if len(valid) == 1:
        best_idx, best_output, _ = valid[0]
    else:
        best_idx, best_output = await _grpo_judge(skill, prompt, valid)

    logger.info(f"[GRPO:{skill}] Selected trajectory {best_idx + 1}/{len(valid)}")
    best_metadata = {
        **{f"{output_key}_trajectory_{i}": r for i, r, _ in valid},
        f"{output_key}": best_output,
        "grpo_selected_trajectory": best_idx,
        "grpo_trajectory_count": len(valid),
    }
    merged_metadata = {**state.get("metadata", {}), **best_metadata}
    if len(merged_metadata) > _MAX_GRPO_METADATA_KEYS:
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
    import re

    from .nodes import execute_goose_node

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
        match = re.search(r"\b([123])\b", judge_text[:20])
        if match:
            selected = int(match.group(1)) - 1
            if selected < len(valid_results):
                return selected, valid_results[selected][1]
    except (ValueError, IndexError, RuntimeError) as exc:
        logger.warning(f"[GRPO:{skill}] Judge failed: {exc}")

    return 0, valid_results[0][1]


async def _ensemble_node(
    state: dict[str, Any],
    skill: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    output_key: str,
) -> dict[str, Any]:
    """Run a panel-of-experts ensemble: N coding models compete, judge distils best."""
    from ..config.config import get_config
    from ..utils.ensemble import MultiModelEnsemble
    from .nodes import get_recipes_dir

    ensemble_cfg = get_config().ensemble
    panel_models = ensemble_cfg.panel_models
    judge_model = ensemble_cfg.judge_model
    timeout = ensemble_cfg.timeout_per_model

    prompt = prompt_builder(state)
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

    for r in result.responses:
        status = "SELECTED" if r.selected else "runner-up"
        logger.info(
            f"[Ensemble:{skill}] {r.model}: score={r.quality_score:.1f}, "
            f"latency={r.latency_seconds:.1f}s ({status})"
        )
    logger.info(f"[Ensemble:{skill}] Judge verdict: {result.judge_summary}")

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


def _scaled_synthesis_timeout(base: int) -> int:
    """Return a scaled timeout for synthesis-class nodes (WP-2 M15)."""
    # v13.22.4: placeholder — state is not available at graph build time. The
    # node_fn re-applies the scale at call time if state carries context.
    return base


def build_workflow_graph(
    nodes: list[dict[str, Any]],
    transitions: list[tuple[str, str, str | None]],
    _workflow_query: str = "",
    complexity: str = "normal",
) -> StateGraph:
    """Build a custom workflow graph from node specs and transitions.

    Args:
        nodes: List of node dicts with keys: name, skill_name, prompt_template, output_key
        transitions: List of (from_node, to_node, condition_field_or_none) tuples

    Returns:
        Configured StateGraph

    """
    from .nodes import execute_goose_node

    graph = StateGraph(BeagleState)

    for node_spec in nodes:
        name = node_spec["name"]
        skill = node_spec["skill_name"]
        template = node_spec["prompt_template"]
        output_key = node_spec.get("output_key", name)
        model_hint = node_spec.get("model")
        enable_grpo = node_spec.get("enable_grpo", False)
        enable_ensemble = node_spec.get("enable_ensemble", False)
        budget_weight = node_spec.get("budget_weight", 1.0)
        require_approval = node_spec.get("require_approval", False)
        prompt_builder = make_prompt_builder(template)

        _VALID_EXECUTORS = frozenset({"goose", "langchain_tool", "langchain_llm", "a2a_remote"})
        _executor = node_spec.get("executor", "goose")
        if _executor not in _VALID_EXECUTORS:
            logger.warning(
                f"[{name}] Invalid executor '{_executor}', falling back to 'goose'. "
                f"Valid executors: {sorted(_VALID_EXECUTORS)}"
            )
            _executor = "goose"
        _tool_name = node_spec.get("tool_name")
        _tool_method = node_spec.get("tool_method")
        _input_mapping = node_spec.get("input_mapping", {})
        _agent_url = node_spec.get("agent_url")
        _agent_name = node_spec.get("agent_name")

        try:
            from ..config.config import get_config as _get_config

            _default_timeout = _get_config().timeout.goose_default_seconds
        except (ImportError, AttributeError, KeyError, TypeError, ValueError):
            _default_timeout = 300

        _timeout = _default_timeout
        if skill == "synthesis-writer":
            _timeout = max(_default_timeout, _scaled_synthesis_timeout(_default_timeout))

        async def node_fn(
            state: dict[str, Any],
            _skill=skill,
            _pb=prompt_builder,
            _ok=output_key,
            _model_hint=model_hint,
            _enable_grpo=enable_grpo,
            _enable_ensemble=enable_ensemble,
            _bw=budget_weight,
            _require_approval=require_approval,
            _name=name,
            _executor=_executor,
            _tool_name=_tool_name,
            _tool_method=_tool_method,
            _input_mapping=_input_mapping,
            _agent_url=_agent_url,
            _agent_name=_agent_name,
            _timeout=_timeout,
        ) -> dict[str, Any]:
            if _require_approval:
                approval_granted = state.get("approval_granted", False)
                if not approval_granted:
                    logger.warning(
                        f"[HITL:{_name}] Phase requires approval but approval_granted=False. "
                        f"Set --approve-all flag or manually approve to proceed."
                    )
                    return {
                        _ok: f"[APPROVAL REQUIRED] Phase '{_name}' requires human approval. "
                        f"Run with --approve-all flag or set approval_granted=True in state.",
                        "completed_nodes": [f"{_name}(blocked)"],
                    }

            if _executor == "langchain_tool":
                from .nodes import execute_langchain_tool_node

                phase_spec = {
                    "name": _name,
                    "agent": _skill,
                    "tool_name": _tool_name or _skill,
                    "tool_method": _tool_method,
                    "input_mapping": _input_mapping,
                    "prompt_template": "",
                }
                result = await execute_langchain_tool_node(state, phase_spec, _ok)
                failure = result.get("tool_failure_flag")
                if failure and failure.get("escalate_to_goose"):
                    logger.warning(
                        f"[{_name}] LangChain tool failed, escalating "
                        f"to goose: {failure.get('error', 'unknown')}"
                    )
                    history = state.get("tool_failure_history", [])
                    history.append(failure)
                    return await execute_goose_node(
                        state, _skill, _pb, _ok, model_override=_model_hint, timeout=_timeout
                    )
                return result

            elif _executor == "langchain_llm":
                from .nodes import execute_langchain_llm_node

                phase_spec = {
                    "name": _name,
                    "agent": _skill,
                    "prompt_template": (
                        _pb.__code__.co_consts[1] if hasattr(_pb, "__code__") else ""
                    ),
                    "model": _model_hint,
                }
                prompt_text = _pb(state)
                phase_spec["_resolved_prompt"] = prompt_text
                return await execute_langchain_llm_node(
                    state, phase_spec, _ok, model_override=_model_hint
                )

            elif _executor == "a2a_remote":
                from ..bridges.a2a_client import get_a2a_client

                client = get_a2a_client()
                phase_spec = {
                    "name": _name,
                    "agent_url": _agent_url,
                    "agent_name": _agent_name or _skill,
                    "input_mapping": _input_mapping,
                }
                return await client.execute_as_node(state, phase_spec, _ok)

            if _enable_ensemble:
                return await _ensemble_node(state, _skill, _pb, _ok)
            if _enable_grpo:
                return await _grpo_node(state, _skill, _pb, _ok)
            return await execute_goose_node(
                state, _skill, _pb, _ok, model_override=_model_hint, timeout=_timeout
            )

        graph.add_node(name, node_fn)  # type: ignore[type-var]

    if nodes:
        graph.set_entry_point(nodes[0]["name"])

    added_from: set[str] = set()
    for from_node, to_node, condition_field in transitions:
        if condition_field:

            def make_condition(field: str, target: str):
                def cond(state: dict[str, Any]) -> str:
                    breaker = _check_circuit_breaker(state)
                    if breaker is not None:
                        return breaker
                    val = state.get(field, "")
                    if val and str(val).strip():
                        return target
                    return END

                return cond

            if from_node not in added_from:
                graph.add_conditional_edges(
                    from_node,
                    make_condition(condition_field, to_node),
                    {to_node: to_node, END: END},
                )
                added_from.add(from_node)
        else:
            if from_node not in added_from:
                graph.add_edge(from_node, to_node)
                added_from.add(from_node)

    if nodes:
        last_name = nodes[-1]["name"]
        if last_name not in added_from:
            graph.add_edge(last_name, END)

    return graph
