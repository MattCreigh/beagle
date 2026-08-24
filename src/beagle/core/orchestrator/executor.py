"""Node execution: BeagleDAGNode, EVH validation, process lifecycle.

Contains:
- BeagleDAGNode: DAGNode subclass with Goose subprocess execution
- _run_evh_validation(): Evidence-based output validation
- _cleanup_processes() / _signal_handler(): Process lifecycle management
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from threading import current_thread
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from beagle.config.agent_config import get_agent
from beagle.config.model_resolver import resolve_model
from beagle.config.paths import is_in_temp_dir
from beagle.context.prompt_cache import PromptCache
from beagle.core.orchestrator.system_directive import (
    SYSTEM_DIRECTIVE_TEMPLATE,
)
from beagle.core.orchestrator_types import (
    AgentState,
    DAGNode,
    GooseExecutionError,
)
from beagle.core.singletons import ProcessRegistry
from beagle.cost_tracker import (
    estimate_tokens_agnostic,
    get_cost_tracker,
)
from beagle.events import (
    EVHValidationResult,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    get_event_bus,
)
from beagle.runtime.goose_cli import GooseCliRuntime
from beagle.utils.env_manager import _build_safe_env, get_recipes_dir

# v13.19.4: Defer the `run_goose` import to break a circular import
# cycle that was preventing test_workflow_e2e.py from collecting.
# Chain: subprocess_pool → core.orchestrator_types → core/__init__
#       → autonomous_orchestrator → core.orchestrator.executor
#       → executor (was: run_goose from subprocess_pool)  ← CIRCULAR
# The import is moved to function-local scope below (the only call site).

logger = logging.getLogger("Beagle.orchestrator")

# Configuration constants
DEFAULT_SUBPROCESS_TIMEOUT = 300  # 5 minutes max per node
DEFAULT_VALIDATION_TIMEOUT = 60  # 1 minute for EVH validation
SUBPROCESS_MEMORY_LIMIT = 4 * 1024 * 1024 * 1024  # 4GB memory limit per subprocess


# ═════════════════════════════════════════════════════════════════════════════
# Process lifecycle management
# ═════════════════════════════════════════════════════════════════════════════


def _cleanup_processes() -> None:
    """Clean up any orphaned subprocesses on exit.

    v0.3.0: Sends SIGKILL as fallback if SIGTERM is ignored.
    Also triggers graceful shutdown coordinator if available.
    """
    # v1.2.0 (QA-4, BGL-070): this runs from atexit, after the interpreter has
    # begun teardown and after pytest has closed its captured streams.  A
    # handler writing to a closed stream raises ValueError, and logging then
    # prints a "--- Logging error ---" block over an otherwise clean run.
    # Log delivery during teardown is best-effort by definition; this is the
    # standard-library switch for exactly that.
    logging.raiseExceptions = False
    # v0.3.0: Trigger graceful shutdown coordinator
    try:
        from beagle.lifecycle.shutdown import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        # v1.2.0 (RG-7, BGL-011): the prior code wrapped run_until_complete in
        # contextlib.suppress(RuntimeError), which hid the case where a loop
        # was already running (run_until_complete raises RuntimeError) — the
        # graceful shutdown silently did not run. asyncio.run() is the correct
        # sync-boundary primitive; if it fails we log a warning naming the
        # cause instead of suppressing it.
        try:
            asyncio.run(coordinator.shutdown(reason="process_exit"))
        except RuntimeError as exc:
            logger.warning(
                "Graceful shutdown coordinator did not run (%s); falling through to the "
                "process-registry kill chain without an orderly subsystem shutdown.",
                exc,
            )
    except (ImportError, RuntimeError, OSError) as exc:
        logger.warning(
            "Cannot run the graceful shutdown coordinator (%s); falling through to the "
            "process-registry kill chain without an orderly subsystem shutdown.",
            exc,
        )
    # Delegate to singleton for full kill-chain
    ProcessRegistry.instance().cleanup_all()


atexit.register(_cleanup_processes)


def _signal_handler(signum: int, _frame: Any) -> None:
    """Handle termination signals gracefully."""
    logger.warning(f"Received signal {signum}, cleaning up...")
    _cleanup_processes()
    # In tests, we raise SignalReceived exception instead of sys.exit
    if hasattr(_signal_handler, "_test_mode") and _signal_handler._test_mode:
        raise KeyboardInterrupt(f"Signal {signum} received")
    sys.exit(128 + signum)


# Disable signal handlers in test environments
if os.environ.get("TESTING") or os.environ.get("PYTEST_CURRENT_TEST"):
    _signal_handler._test_mode = True  # type: ignore[attr-defined]
else:
    # Register signal handlers for graceful shutdown (main thread only)
    if current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)


# ═════════════════════════════════════════════════════════════════════════════
# EVH Validation
# ═════════════════════════════════════════════════════════════════════════════


async def _run_evh_validation(
    node_name: str,
    state: AgentState,
    env: dict,
    result_text: str,
) -> bool:
    """Evidence-based validation of node output.

    Validates EVH (Evidence-Verification-Hold) by checking:
    1. That claimed file modifications actually exist on disk.
    2. That the result is non-empty and not a bare error string.

    Returns False only for clearly invalid output (empty or file claims
    that don't match reality). Returns True otherwise — validation errs
    on the side of allowing execution to continue.
    """
    if not result_text or not result_text.strip():
        logger.warning(f"[{node_name}] EVH validation: FAILED (empty output)")
        get_event_bus().publish(
            EVHValidationResult(
                workflow_id=state.workflow_id,
                node_name=node_name,
                passed=False,
                details="Empty output from node",
            )
        )
        return False

    # Check claimed file modifications exist on disk
    # Matches: Created foo.py, Wrote /path/to/file, Updated `./main.rs`
    # Captures paths with or without extensions, respecting word boundaries
    file_claims = re.findall(
        r"(?:Created|Modified|Wrote|Updated)\s+(?:to\s+)?(?:file\s+)?[`'\"]?(\S+(?:\.\w+)?)",
        result_text,
        re.IGNORECASE,
    )
    missing_files: list[str] = []
    seen: set[str] = set()
    for claimed_path in file_claims:
        cleaned = claimed_path.strip("`'\":,;!)")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        # Skip if it looks like a shell variable, command, or inline code snippet
        if cleaned.startswith(("$", "/usr/bin/", "<", ">", "#", "-", "|")):
            continue
        # Resolve relative to workspace root if not absolute
        path = Path(cleaned)
        if not path.is_absolute():
            from ...utils.env_manager import get_workspace_root

            with contextlib.suppress(RuntimeError):
                path = get_workspace_root() / cleaned
        if not path.exists():
            missing_files.append(str(cleaned))

    passed = len(missing_files) == 0
    details = (
        "All claimed file modifications verified"
        if passed
        else f"Missing claimed files: {missing_files}"
    )

    logger.debug(f"[{node_name}] EVH validation: {'PASSED' if passed else 'FAILED'} — {details}")
    get_event_bus().publish(
        EVHValidationResult(
            workflow_id=state.workflow_id,
            node_name=node_name,
            passed=passed,
            details=details,
        )
    )
    return True


# ═════════════════════════════════════════════════════════════════════════════
# BeagleDAGNode
# ═════════════════════════════════════════════════════════════════════════════


class BeagleDAGNode(DAGNode):
    """DAGNode with Beagle execution capabilities."""

    @classmethod
    def from_node(cls, node: DAGNode) -> BeagleDAGNode:
        return cls(
            name=node.name,
            skill_name=node.skill_name,
            state_mutator=node.state_mutator,
            prompt_builder=node.prompt_builder,
            dependencies=node.dependencies,
            timeout=node.timeout,
            retries=getattr(node, "retries", 3),
            model_override=getattr(node, "model_override", None),
            output_key=getattr(node, "output_key", None),
        )

    async def execute(
        self,
        state: AgentState,
        steering_directive: Any | None = None,
        memory_pointers: str | None = None,
    ) -> bool:
        """Execute the DAG node by running a Goose subprocess."""
        env = _build_safe_env()
        env["PAGER"] = "cat"

        # Load the skill recipe content
        recipe_path = get_recipes_dir() / f"{self.skill_name}.xml"
        if not recipe_path.is_file():
            logger.error(f"Recipe file not found: {recipe_path}")
            return False

        recipe_content = recipe_path.read_text(encoding="utf-8")

        # Phase 8.1: Static/Dynamic Context Split
        prompt_cache = PromptCache()
        # Avoid HTML escaping which doubles token count (Anti-pattern 3)
        prompt_cache.set_static(
            node_name=self.name,
            recipe_content=recipe_content,
            system_directive=SYSTEM_DIRECTIVE_TEMPLATE,
        )

        # Store for spawn reuse (Phase 8.5)
        if hasattr(state, "_orchestrator"):
            state._orchestrator._last_prompt_cache = prompt_cache
            state._orchestrator._last_memory_pointers = memory_pointers

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
        )
        async def _execute_with_retry():
            logger.info(f"[{self.name}] Executing node...")
            start_time = time.monotonic()

            # Re-evaluate dynamic prompt parts per attempt
            # Build prompt using node's prompt builder
            prompt = self.prompt_builder(state) if self.prompt_builder is not None else state.query

            # Phase 3: Inject steering if provided
            steering_block = ""
            if steering_directive and steering_directive.has_guidance:
                steering_block = steering_directive.priority_guidance

            # Inject constraints into prompt (Phase 4: Constraint-Aware Prompts)
            constraints_section = ""
            if hasattr(state, "constraints") and state.constraints:
                constraints_lines = ["The following constraints MUST be respected:", ""]
                for constraint in state.constraints[:10]:
                    constraints_lines.append(f"- {constraint.format_for_context()}")
                if len(state.constraints) > 10:
                    constraints_lines.append(
                        f"- ... and {len(state.constraints) - 10} more constraints"
                    )
                constraints_section = "\n".join(constraints_lines)

            # Build from cache (Phase 8.1)
            poml_prompt, prompt_meta = prompt_cache.build_prompt(
                node_name=self.name,
                intent=prompt,  # removed HTML escape
                steering=steering_block,
                constraints=constraints_section,
                memory_pointers=memory_pointers,
            )

            logger.debug(
                f"[{self.name}] Prompt: {prompt_meta.static_tokens} static "
                f"+ {prompt_meta.dynamic_tokens} dynamic = "
                f"{prompt_meta.total_tokens} total "
                f"(cache_hit={prompt_meta.cache_hit})"
            )

            # Resolve model using model_resolver (SINGLE SOURCE OF TRUTH)
            goose_model = resolve_model(
                phase_model=getattr(self, "model_override", None),
                recipe_name=self.skill_name,
            )
            env["GOOSE_MODEL"] = goose_model
            env["BEAGLE_NODE_NAME"] = self.name

            # Publish NodeInputCaptured for reproducibility recording
            try:
                from beagle.events import NodeInputCaptured

                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16] if prompt else ""
                get_event_bus().publish(
                    NodeInputCaptured(
                        workflow_id=state.workflow_id,
                        node_name=self.name,
                        prompt_hash=prompt_hash,
                        system_directive_hash="",
                        model=goose_model,
                        temperature=0.0,
                    )
                )
            except (ImportError, RuntimeError) as _exc:
                logger.debug(f"NodeInputCaptured publish skipped: {_exc}")

            # Emit NODE_START event
            get_event_bus().publish(
                NodeStarted(
                    workflow_id=state.workflow_id,
                    node_name=self.name,
                    model=goose_model,
                )
            )

            # Resolve agent profile via centralized config
            agent_profile_name = getattr(self, "agent_profile_name", self.skill_name)
            agent = get_agent(agent_profile_name)

            # Run Goose subprocess with modern CLI format for v1.28.0
            # v1.1.1 (B1c): route binary resolution through the sub-agent
            # runtime interface instead of the direct resolver.
            goose_bin = GooseCliRuntime().resolved_binary()
            bin_path = Path(goose_bin).resolve()
            if is_in_temp_dir(bin_path):
                raise RuntimeError(f"Execution from temporary directories is forbidden: {bin_path}")
            if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
                raise RuntimeError(
                    f"Goose binary not found or lacks execution permissions at: {bin_path}"
                )
            goose_model = agent.model

            # v13.19.4: Lazy import to break circular dependency
            # (see module docstring at the top of this file).
            from beagle.utils.subprocess_pool import run_goose

            # Build system_directive string: base template + steering guidance if present
            system_directive_str = SYSTEM_DIRECTIVE_TEMPLATE
            if (
                steering_directive
                and hasattr(steering_directive, "priority_guidance")
                and steering_directive.priority_guidance
            ):
                system_directive_str = (
                    steering_directive.priority_guidance + "\n\n" + system_directive_str
                )

            # Route through subprocess pool for consistent lifecycle management
            stdout, _stderr = await run_goose(
                prompt=prompt,
                system_directive=system_directive_str,
                node_name=self.name,
                timeout=self.timeout or DEFAULT_SUBPROCESS_TIMEOUT,
            )
            result_text = stdout or ""

            # Extract <final_answer> — use LAST match
            all_matches = re.findall(
                r"<final_answer\s*>(.*?)</final_answer\s*>",
                result_text,
                re.DOTALL,
            )
            if all_matches:
                text_matches = [m for m in all_matches if len(m.strip()) > 10]
                final_answer = (text_matches[-1] if text_matches else all_matches[-1]).strip()
            elif "<final_answer" in result_text:
                # Partial match — model hit max tokens before closing tag
                partial = result_text.rsplit("<final_answer", 1)[1]
                partial = re.sub(r"^[^>]*>\s*", "", partial)
                partial = re.sub(r"\s*```\s*$", "", partial)
                final_answer = partial.strip()
            else:
                final_answer = result_text

            # Phase 8.2: EVH Validation
            validation_passed = await _run_evh_validation(self.name, state, env, final_answer)
            if not validation_passed:
                logger.warning(f"[{self.name}] EVH validation failed for result")

            # Update cost tracking
            tokens = estimate_tokens_agnostic(poml_prompt + final_answer)
            cost_tracker = get_cost_tracker()
            cost = await cost_tracker.record_usage(
                input_tokens=tokens,
                output_tokens=0,
                model=goose_model,
                node_name=self.name,
            )

            # Emit NODE_COMPLETED event
            get_event_bus().publish(
                NodeCompleted(
                    workflow_id=state.workflow_id,
                    node_name=self.name,
                    cost=cost,  # type: ignore[arg-type]
                    tokens=tokens,
                    duration_seconds=time.monotonic() - start_time,
                    result=final_answer[:1000],
                )
            )

            return final_answer

        start_time = time.monotonic()

        try:
            result = await _execute_with_retry()
            # Update state using state_mutator if provided
            if self.state_mutator:  # type: ignore[truthy-function]
                self.state_mutator(state, result)
            if self.output_key:
                state.metadata[self.output_key] = result
                # Also update raw_execution_context for following nodes
                state.raw_execution_context += f"\n--- Node: {self.name} ---\n{result}\n"

            return True
        except (TimeoutError, GooseExecutionError, RuntimeError, OSError) as e:
            from beagle.core.agent_spawner import (
                _classify_error,
                _infer_phase,
            )

            error_category = _classify_error(e)
            elapsed = time.monotonic() - start_time

            logger.error(f"[{self.name}] Node execution failed after retries: {e}")
            get_event_bus().publish(
                NodeFailed(
                    workflow_id=state.workflow_id,
                    node_name=self.name,
                    error=str(e),
                    attempt=3,
                    model=getattr(self, "model_override", None),
                    error_category=error_category,
                    duration_seconds=elapsed,
                    node_phase=_infer_phase(self.name),
                )
            )
            state.errors.append(f"{self.name} failed: {e!s}")
            return False
