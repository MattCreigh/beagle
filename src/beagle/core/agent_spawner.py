"""Beagle v13.0 - Agent Spawner: Subprocess management, EVH validation, and DAG node execution.

Extracted from autonomous_orchestrator.py for maintainability.
Handles goose subprocess lifecycle, agent spawning, result parsing,
and Evidence Validation Hook (EVH) checks.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from beagle.config.model_resolver import resolve_model
from beagle.config.paths import is_in_temp_dir
from beagle.context.prompt_cache import PromptCache
from beagle.core.orchestrator_types import (
    AgentState,
    DAGNode,
    GooseExecutionError,
    SubprocessTimeoutError,
)
from beagle.cost_tracker import (
    estimate_tokens_agnostic,
    get_cost_tracker,
)
from beagle.events import (
    EVHValidationResult,
    NodeCompleted,
    NodeFailed,
    NodeOutput,
    NodeStarted,
    get_event_bus,
)
from beagle.runtime.goose_cli import GooseCliRuntime
from beagle.security import validate_python_code_ast
from beagle.utils.env_manager import _build_safe_env
from beagle.utils.subprocess_pool import _terminate_process_group

logger = logging.getLogger("Beagle.agent_spawner")


# ── Error Classification ──────────────────────────────────────────────────────

_ERROR_CATEGORY_MAP: dict[type, str] = {
    TimeoutError: "timeout",
    asyncio.TimeoutError: "timeout",
    ConnectionError: "connection",
    FileNotFoundError: "system",
    PermissionError: "system",
    OSError: "system",
    ValueError: "validation",
    TypeError: "validation",
    RuntimeError: "runtime",
}


def _classify_error(exc: Exception) -> str:
    """Classify an exception into a structured error category.

    Maps common exception types to semantic categories for alert routing
    and analytics. Falls back to 'unknown' for unclassified exceptions.

    Args:
        exc: The exception to classify.

    Returns:
        One of: timeout, ratelimit, validation, system, connection, runtime, unknown

    """
    for exc_type, category in _ERROR_CATEGORY_MAP.items():
        if isinstance(exc, exc_type):
            return category

    # Check for rate-limiting patterns in the error message
    error_msg = str(exc).lower()
    if "rate" in error_msg and "limit" in error_msg:
        return "ratelimit"
    if "429" in error_msg:
        return "ratelimit"
    if "quota" in error_msg:
        return "ratelimit"

    return "unknown"


def _infer_phase(node_name: str) -> str:
    """Infer the workflow phase from a node name.

    Args:
        node_name: Name of the node (e.g., 'research-planner', 'fact-checker').

    Returns:
        One of: planning, execution, verification, synthesis, unknown

    """
    name_lower = node_name.lower()
    if any(kw in name_lower for kw in ("plan", "research", "deep-plan", "architect")):
        return "planning"
    # Check verification BEFORE execution — "code-validator" contains "cod"
    # but is a verification node, not an execution node.
    if any(kw in name_lower for kw in ("verif", "check", "audit", "valid", "cvcp")):
        return "verification"
    if any(kw in name_lower for kw in ("execut", "search", "code-", "implement", "fix")):
        return "execution"
    if any(kw in name_lower for kw in ("synth", "report", "summar", "writ")):
        return "synthesis"
    return "unknown"


# ── Configuration Constants ────────────────────────────────────────────────────

DEFAULT_MAX_NESTED_AGENTS = 3
DEFAULT_SUBPROCESS_TIMEOUT = 300  # 5 minutes max per node
DEFAULT_VALIDATION_TIMEOUT = 60  # 1 minute for EVH validation
SUBPROCESS_MEMORY_LIMIT = 4 * 1024 * 1024 * 1024  # 4GB per subprocess

SYSTEM_DIRECTIVE_TEMPLATE = """\
CRITICAL 0A: If a required file does not exist, CREATE IT with appropriate content. \
Do NOT skip or delegate file creation. If you need to write to a file that doesn't exist, \
create the parent directories and write the file. If you need to read a test file that \
doesn't exist, create it with appropriate boilerplate content first. This applies to all \
file types: Python modules, test files, configuration files, documentation, etc.

CRITICAL 0: You are running in a headless, non-interactive CI/CD pipeline. \
NEVER use the ask_user TOOL. If you lack information, make safe autonomous assumptions.

CRITICAL 1: YOU ARE ALREADY A SUBAGENT INSIDE THE DAGOrchestrator. \
DO NOT invoke autonomous_orchestrator.py, run_agents_parallel.py, or any gemini CLI commands. \
Fulfill the intent directly using your file and grep tools.

CRITICAL 2: NEVER use run_shell_command with 'cat << EOF' or 'echo' to write multi-line scripts. \
Use write_file and replace tools exclusively.

CRITICAL 3: Output brief 1-sentence status updates before each action. \
Never output conversational filler.

CRITICAL 4: When complete, signal completion via:
   A) Wrap response in <final_answer> tags (preferred)
   B) Write to file and ping orchestrator channel
   C) Both - final_answer AND store artifacts

CRITICAL 5: AGENT CAPABILITIES:
   - CONTEXT FOLDING: Compress state to prevent overflow
   - AGENT SPAWNING: Spawn identical agents for subtasks
   - ORCHESTRATOR PING: Signal completion with results

CRITICAL 6: Store research in /tmp/agent_research_<id>/

CRITICAL 7: PREFERRED TOOLS FOR CODE INTERACTION:
   - code_search: Structured regex search (use instead of grep/rg via shell)
   - file_discovery: Find files by pattern (use instead of find/fd via shell)
   - code_context: Get function/class/import info (use instead of cat + manual parsing)
   These tools return structured JSON, are permission-scoped, and are more token-efficient \
than raw shell commands. ALWAYS prefer these over run_shell_command for code exploration.
"""

# ── Singleton-based State Management ──────────────────────────────────────────
# v13.5.2: Replaced bare global variables with thread-safe singleton classes.
# The singletons provide proper encapsulation, locking, and cleanup semantics.
# For backward compatibility, the original function signatures are preserved
# and delegate to the singleton instances.

from .singletons import (  # ruff: ignore[E402]
    AgentCallTracker,
    OrchestratorChannelManager,
    ProcessRegistry,
)


async def add_process(proc: asyncio.subprocess.Process) -> None:
    """Thread-safe addition to active processes (delegates to ProcessRegistry)."""
    await ProcessRegistry.instance().register(proc)


async def remove_process(proc: asyncio.subprocess.Process) -> None:
    """Thread-safe removal from active processes (delegates to ProcessRegistry)."""
    await ProcessRegistry.instance().unregister(proc)


async def increment_agent_call(parent_workflow_id: str) -> int:
    """Atomically increment agent call count (delegates to AgentCallTracker)."""
    return await AgentCallTracker.instance().increment(parent_workflow_id)


async def get_agent_call_count(parent_workflow_id: str) -> int:
    """Get current agent call count for workflow (delegates to AgentCallTracker)."""
    return await AgentCallTracker.instance().get_count(parent_workflow_id)


async def reset_agent_call_counter(parent_workflow_id: str) -> None:
    """Reset agent call counter for workflow (delegates to AgentCallTracker)."""
    await AgentCallTracker.instance().reset(parent_workflow_id)


async def cleanup_agent_call_counter(parent_workflow_id: str) -> None:
    """Remove agent call counter for workflow (delegates to AgentCallTracker)."""
    await AgentCallTracker.instance().cleanup(parent_workflow_id)


async def set_orchestrator_channel(channel: asyncio.Queue) -> None:
    """Set the global orchestrator channel (delegates to OrchestratorChannelManager)."""
    await OrchestratorChannelManager.instance().set_channel(channel)


async def ping_orchestrator(message: dict) -> bool:
    """Send a message to the orchestrator (delegates to OrchestratorChannelManager).

    Args:
        message: Dict containing agent results, status, research path, etc.

    Returns:
        True if message was sent, False if no channel available

    """
    return await OrchestratorChannelManager.instance().send_message(message)


def cleanup_processes() -> None:
    """Clean up any orphaned subprocesses on exit (delegates to ProcessRegistry)."""
    ProcessRegistry.instance().cleanup_all()


atexit.register(cleanup_processes)


class SignalHandler:
    """Signal handler with injectable exit callback for testability.

    Replaces the previous _test_mode environment variable hack with
    proper dependency injection. Tests can inject a custom exit callback
    instead of checking environment variables.

    Usage:
        # Production (default behavior):
        handler = SignalHandler()

        # Testing (injectable callback):
        handler = SignalHandler(exit_callback=lambda sig: None)
    """

    def __init__(self, exit_callback: Any | None = None) -> None:
        self._exit_callback = exit_callback
        self._installed = False

    def handle_signal(self, signum: int, _frame: Any) -> None:
        """Handle termination signals gracefully.

        Uses os._exit() instead of sys.exit() — sys.exit() triggers the full
        Python atexit/shutdown chain, which attempts to flush databases, close
        connections, and release resources. If any lock is held during that
        chain (e.g., TrackingDatabase._lock), the process deadlocks permanently
        and the DB is corrupted for all subsequent runs (v13.17.1 fix).
        os._exit() terminates immediately with no cleanup — locks held in the
        dying process are released by the kernel when the process exits.
        """
        logger.warning(f"Received signal {signum}, cleaning up...")
        cleanup_processes()

        if self._exit_callback is not None:
            # Dependency-injected callback for testing
            self._exit_callback(signum)
        else:
            # Production: os._exit avoids atexit/deadlock cascade
            os._exit(128 + signum)

    def install(self) -> None:
        """Install signal handlers on the main thread."""
        if threading.current_thread() is not threading.main_thread():
            logger.warning("Cannot install signal handlers from non-main thread")
            return
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        self._installed = True
        logger.info("Signal handlers installed (SIGTERM, SIGINT)")

    def uninstall(self) -> None:
        """Remove installed signal handlers."""
        if not self._installed:
            return
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self._installed = False
        logger.info("Signal handlers removed")


# Create default signal handler (installs in production, not in tests)
_signal_handler = SignalHandler()

if not os.environ.get("TESTING") and not os.environ.get("PYTEST_CURRENT_TEST"):
    _signal_handler.install()


# ── EVH Validation ─────────────────────────────────────────────────────────────


async def run_evh_validation(
    node_name: str,
    state: AgentState,
    env: dict,
    result_text: str,
) -> bool:
    """Evidence-based validation of node output.

    v13.0: Now implements AST-based code validation for Python code blocks
    found in agent output. Uses validate_python_code_ast() from security
    module instead of brittle regex patterns.

    Checks:
    1. Python code blocks within the output for dangerous AST constructs
    2. Shell command patterns in backtick blocks (existing security)
    3. File existence claims (ground-truth validation delegated to CVCP)

    Args:
        node_name: Name of the workflow node.
        state: Current agent state.
        env: Environment dict for the agent.
        result_text: The raw output text from the goose subprocess.

    Returns:
        True if validation passes, False if dangerous constructs detected.

    """
    import re as _re

    validation_details: list[str] = []

    # ── Check 1: AST validation of Python code blocks ──────────────────────
    # Extract Python code blocks from markdown-style ```python ... ``` fences
    python_blocks = _re.findall(r"```python\s*\n(.*?)```", result_text, re.DOTALL | re.IGNORECASE)
    # Also catch ```py blocks
    python_blocks += _re.findall(r"```py\s*\n(.*?)```", result_text, re.DOTALL | re.IGNORECASE)

    strict_mode = state.get("strict_validation", True)  # type: ignore[attr-defined]

    for i, code_block in enumerate(python_blocks):
        is_valid, error_msg = validate_python_code_ast(code_block, strict=strict_mode)
        if not is_valid:
            validation_details.append(f"Python code block {i + 1}: {error_msg}")
            logger.warning(
                f"[{node_name}] EVH: AST validation FAILED for code block {i + 1}: {error_msg}"
            )

    # ── Check 2: Shell command injection in backtick blocks ────────────────
    backtick_content = _re.findall(r"`([^`]+)`", result_text)
    dangerous_cmds = [
        "rm -rf",
        "curl | sh",
        "wget | sh",
        "> /dev/",
        "chmod",
        "chown",
        "dd if=",
        "mkfs",
        "nc ",
        "ncat ",
        "bash -i",
        "/dev/tcp/",
    ]
    for content in backtick_content:
        for cmd in dangerous_cmds:
            if cmd in content:
                validation_details.append(f"Dangerous command in backticks: {cmd}")

    # ── Determine verdict ──────────────────────────────────────────────────
    passed = len(validation_details) == 0
    detail_str = "; ".join(validation_details) if validation_details else "All checks passed"

    if not passed:
        logger.warning(f"[{node_name}] EVH validation FAILED: {detail_str}")
    else:
        logger.debug(
            f"[{node_name}] EVH validation PASSED ({len(python_blocks)} code blocks checked)"
        )

    get_event_bus().publish(
        EVHValidationResult(
            workflow_id=state.workflow_id,
            node_name=node_name,
            passed=passed,
            details=detail_str,
        )
    )
    return passed


# ── Final Answer Extraction ────────────────────────────────────────────────────


def extract_final_answer(result_text: str) -> str:
    """Extract <final_answer> content from goose subprocess output.

    Uses BeautifulSoup DOM parser to avoid ReDoS vulnerabilities and
    correctly handle malformed XML.

    Args:
        result_text: Raw stdout from goose subprocess.

    Returns:
        Extracted final answer text, or full result_text if no tags found.

    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(result_text, "html.parser")
        final_tags = soup.find_all("final_answer")
        if final_tags:
            text_matches = [
                t.get_text(strip=True) for t in final_tags if len(t.get_text(strip=True)) > 10
            ]
            return text_matches[-1] if text_matches else final_tags[-1].get_text(strip=True)  # type: ignore[no-any-return]
    except (IndexError, AttributeError) as exc:
        logger.warning(
            "Cannot extract a <final_answer> tag from the agent response (%s); "
            "returning the whole response text instead.",
            exc,
        )
    return result_text.strip()


# ── BeagleDAGNode ────────────────────────────────────────────────────────────────


class BeagleDAGNode(DAGNode):
    """DAGNode with Beagle execution capabilities."""

    @classmethod
    def from_node(cls, node: DAGNode) -> BeagleDAGNode:
        """From node."""

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
        # Uses safe_file_ops to auto-create missing recipes instead of failing
        from ..utils.env_manager import get_workspace_root
        from ..utils.safe_file_ops import ensure_recipe_exists

        recipes_dir = get_workspace_root() / "recipes"
        recipe_path = recipes_dir / f"{self.skill_name}.xml"
        recipe_path = ensure_recipe_exists(recipe_path)
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
        async def _execute_with_retry() -> None:
            logger.info(f"[{self.name}] Executing node...")
            start_time = time.monotonic()

            # Phase 8.3: Context compression integration
            # If context is growing large, compress before building the prompt
            # to avoid exceeding the model's context window.
            if self.context_compression and state.should_compress_context():
                pre_size = len(state.raw_execution_context)
                state.compress_context()
                post_size = len(state.raw_execution_context)
                if post_size < pre_size:
                    logger.info(
                        f"[{self.name}] Context compressed: "
                        f"{pre_size} → {post_size} chars "
                        f"({post_size / max(pre_size, 1):.0%} of original)"
                    )

            # Re-evaluate dynamic prompt parts per attempt
            prompt = self.prompt_builder(state) if self.prompt_builder else state.query  # type: ignore[truthy-function]

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

            # Inject compressed fold pointers into memory_pointers
            # If context was compressed, tell goose where to find the full data
            fold_pointers = ""
            if hasattr(state, "metadata") and "_compressed_fold_ids" in state.metadata:
                fold_ids = state.metadata["_compressed_fold_ids"]
                if fold_ids:
                    fold_pointers = (
                        "\n[CONTEXT NOTE: Previous context was compressed for efficiency. "
                        f"Fold IDs: {', '.join(fold_ids[-3:])}. "
                        "Use `decompress(query)` to retrieve specific sections.]\n"
                    )
            effective_memory = (memory_pointers or "") + fold_pointers

            # Build from cache (Phase 8.1)
            poml_prompt, prompt_meta = prompt_cache.build_prompt(
                node_name=self.name,
                intent=prompt,
                steering=steering_block,
                constraints=constraints_section,
                memory_pointers=effective_memory,
            )

            logger.debug(
                f"[{self.name}] Prompt: {prompt_meta.static_tokens} static "
                f"+ {prompt_meta.dynamic_tokens} dynamic = {prompt_meta.total_tokens} total "
                f"(cache_hit={prompt_meta.cache_hit})"
            )

            # Resolve model using model_resolver (SINGLE SOURCE OF TRUTH)
            goose_model = resolve_model(
                phase_model=getattr(self, "model_override", None),
                recipe_name=self.skill_name,
            )
            env["GOOSE_MODEL"] = goose_model
            env["BEAGLE_NODE_NAME"] = self.name

            # Emit NODE_START event
            get_event_bus().publish(
                NodeStarted(
                    workflow_id=state.workflow_id,
                    node_name=self.name,
                    model=goose_model,
                )
            )

            # Run Goose subprocess with modern CLI format
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
            goose_provider = os.environ.get("GOOSE_PROVIDER", "ollama_cloud")
            goose_model_env = os.environ.get("GOOSE_MODEL", "minimax-m3:cloud")

            cmd = [
                goose_bin,
                "run",
                "--provider",
                goose_provider,
                "--model",
                goose_model_env,
                "--with-builtin",
                "developer",
                "-i",
                "-",
                "--system",
                poml_prompt,
                "-q",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            await add_process(process)

            # Write prompt to stdin
            await process.stdin.write(prompt.encode() if isinstance(prompt, str) else prompt)  # type: ignore[misc,union-attr]
            await process.stdin.close()  # type: ignore[misc,union-attr]

            stdout_data: list[str] = []
            stderr_data: list[str] = []

            async def read_stream(stream, target_list, stream_type) -> None:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    target_list.append(decoded)
                    get_event_bus().publish(
                        NodeOutput(
                            workflow_id=state.workflow_id,
                            node_name=self.name,
                            content=decoded,
                            stream_type=stream_type,
                        )
                    )

            await asyncio.gather(
                read_stream(process.stdout, stdout_data, "stdout"),
                read_stream(process.stderr, stderr_data, "stderr"),
            )

            timeout = (
                self.timeout
                if isinstance(getattr(self, "timeout", None), int)
                else DEFAULT_SUBPROCESS_TIMEOUT
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                logger.error(
                    f"[{self.name}] Subprocess timeout ({timeout}s) — escalating: SIGTERM → SIGKILL"
                )
                _terminate_process_group(process, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    _terminate_process_group(process, signal.SIGKILL)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10.0)
                    except TimeoutError:
                        logger.critical(f"[{self.name}] Subprocess unkillable after SIGKILL")
                await remove_process(process)
                raise SubprocessTimeoutError(
                    f"[{self.name}] Timed out after {timeout}s and was forcefully terminated"
                ) from None

            await remove_process(process)

            if process.returncode != 0:
                error_msg = "\n".join(stderr_data) or "Unknown error"
                raise GooseExecutionError(
                    f"Goose failed with exit code {process.returncode}: {error_msg}"
                )

            result_text = "\n".join(stdout_data)
            final_answer = extract_final_answer(result_text)

            # Phase 8.2: EVH Validation
            validation_passed = await run_evh_validation(self.name, state, env, final_answer)
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

            return final_answer  # type: ignore[return-value]

        start_time = time.monotonic()
        goose_model: str | None = None

        try:
            result = await _execute_with_retry()  # type: ignore[func-returns-value]
            # Update state
            if self.output_key:
                state.metadata[self.output_key] = result
                # Also update raw_execution_context for following nodes
                state.raw_execution_context += f"\n--- Node: {self.name} ---\n{result}\n"

            return True
        except (TimeoutError, RuntimeError, OSError, ValueError) as e:
            # Classify the error for structured debugging
            error_category = _classify_error(e)
            elapsed = time.monotonic() - start_time

            logger.error(f"[{self.name}] Node execution failed after retries: {e}")
            get_event_bus().publish(
                NodeFailed(
                    workflow_id=state.workflow_id,
                    node_name=self.name,
                    error=str(e),
                    attempt=3,
                    model=goose_model,
                    error_category=error_category,
                    duration_seconds=elapsed,
                    node_phase=_infer_phase(self.name),
                )
            )
            state.errors.append(f"{self.name} failed: {e!s}")
            return False


__all__ = [
    "DEFAULT_MAX_NESTED_AGENTS",
    "DEFAULT_SUBPROCESS_TIMEOUT",
    "DEFAULT_VALIDATION_TIMEOUT",
    "SUBPROCESS_MEMORY_LIMIT",
    "SYSTEM_DIRECTIVE_TEMPLATE",
    "BeagleDAGNode",
    "add_process",
    "cleanup_agent_call_counter",
    "cleanup_processes",
    "extract_final_answer",
    "get_agent_call_count",
    "increment_agent_call",
    "ping_orchestrator",
    "remove_process",
    "reset_agent_call_counter",
    "run_evh_validation",
    "set_orchestrator_channel",
]
