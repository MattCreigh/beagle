"""LangGraph node functions for Beagle v13.

Each node wraps Goose subprocess execution and returns
state update dicts following LangGraph conventions.

H-MEM Integration:
- Nodes receive pre-hydrated context from hydration_node
- Manifest-based documentation is auto-loaded
- Constraints are injected into system directives
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ..config.config import (
    assess_task_complexity,
    get_config,
    resolve_model_for_task,
)
from ..cost_tracker import estimate_tokens_agnostic
from ..events import NodeCompleted, NodeFailed, NodeStarted, get_event_bus
from ..utils.env_manager import get_recipes_dir
from .agent_spawner import _classify_error
from .autonomous_orchestrator import DEFAULT_SUBPROCESS_TIMEOUT

logger = logging.getLogger("Beagle.nodes")


def _infer_phase_from_skill(skill_name: str) -> str:
    """Infer workflow phase from a skill/node name.

    Args:
        skill_name: Skill or node name (e.g., 'research-planner', 'fact-checker').

    Returns:
        One of: planning, execution, verification, synthesis, unknown

    """
    name_lower = skill_name.lower()
    if any(kw in name_lower for kw in ("plan", "research", "deep-plan", "architect")):
        return "planning"
    if any(kw in name_lower for kw in ("execut", "search", "cod", "implement", "fix")):
        return "execution"
    if any(kw in name_lower for kw in ("verif", "check", "audit", "valid", "cvcp")):
        return "verification"
    if any(kw in name_lower for kw in ("synth", "report", "summar", "writ")):
        return "synthesis"
    return "unknown"


# v1.0.2 (P-fix4): Structured error envelope. The previous
# ``err_msg = f"{skill_name}: Timeout after {timeout}s"`` strings
# forced downstream consumers (orchestrator status mapper, MCP layer,
# CI tooling) to substring-match prose to distinguish a timeout from a
# permission error from a value error. The envelope keeps the prose
# ``err_msg`` for human logs but additionally returns a parseable
# ``kind`` + ``details`` dict so callers can branch on structured
# fields instead of string parsing.
#
# Schema:
#   {
#     "skill":   "fact-checker",
#     "kind":    "timeout" | "not_found" | "permission" | "system"
#              | "validation" | "runtime",
#     "message": "human-readable summary (same as the prose err_msg)",
#     "retryable": bool,
#     "details": { ... kind-specific structured payload ... },
#   }
#
# ``retryable`` is True for transient errors (timeout, system) and False
# for permanent ones (validation, permission).
_RETRYABLE_KINDS: frozenset[str] = frozenset({"timeout", "system"})


def _structured_error(
    skill_name: str,
    kind: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    """Build a parseable error envelope.

    Returns a dict that downstream consumers can branch on without
    substring-matching the prose ``message``. The envelope is also
    JSON-serializable so it can flow through the MCP layer or into
    audit logs cleanly.
    """
    return {
        "skill": skill_name,
        "kind": kind,
        "message": message,
        "retryable": kind in _RETRYABLE_KINDS,
        "details": details,
    }


# H-MEM: Import context manifest for skill-specific loading
try:
    from .context_manifest import build_structural_template, get_manifest

    CONTEXT_MANIFEST_AVAILABLE = True
except ImportError:
    CONTEXT_MANIFEST_AVAILABLE = False
    logger.debug("Context manifest not available")

# FIX: Import hierarchical memory for context management across nodes.
# Only the factory functions are used in this module; HierarchicalMemory
# itself is imported lazily by callers (memory/hierarchical_memory.py
# exposes it as the return type of get_hierarchical_memory). Keeping the
# try/except guards the optional dependency.
try:
    from ..memory.hierarchical_memory import (
        MemoryLevel,
        get_hierarchical_memory,
    )

    HIERARCHICAL_MEMORY_AVAILABLE = True
except ImportError:
    HIERARCHICAL_MEMORY_AVAILABLE = False
    logger.debug("Hierarchical memory not available — context tracking limited")


async def execute_headless_goose(
    prompt: str,
    system_directive: str,
    node_name: str,
    timeout: int = DEFAULT_SUBPROCESS_TIMEOUT,
    model: str | None = None,
    readonly: bool = False,
) -> tuple[str, str]:
    """Run a headless goose subprocess via the bounded GoosePool.

    Delegates to ..utils.subprocess_pool.run_goose(), which enforces a concurrency
    cap (default: min(cpu_count, 4)) to prevent CPU oversubscription on low-core
    hardware like the i7-6700T.

    Args:
        prompt: User prompt text.
        system_directive: System directive / recipe content.
        node_name: Name of the node (for logging and model resolution).
        timeout: Subprocess timeout in seconds.
        model: Explicit model override (reserved for future per-call routing).
        readonly: If True, set BEAGLE_READONLY_MODE=1 in the subprocess env.

    Returns:
        Tuple of (final_answer_content, raw_stdout_text)

    """
    # pylint: disable=import-outside-toplevel  # lazy import avoids circular deps
    from ..utils.subprocess_pool import run_goose

    return await run_goose(
        prompt=prompt,
        system_directive=system_directive,
        node_name=node_name,
        timeout=timeout,
        readonly=readonly,
        model_override=model,
    )


_READ_ONLY_DIRECTIVE = """\
## STRICT READ-ONLY MODE — ENFORCED

You are operating in READ-ONLY mode. This is a non-negotiable constraint.

**ALLOWED actions:**
- Read files using `cat`, `head`, `tail`, `less`
- Search files using `find`, `grep`, `rg`, `ag`
- List files using `ls`, `tree`
- Inspect code using any read-only tool
- Use Goose's built-in Glob, Grep, and Read tools

**FORBIDDEN actions (will cause workflow failure):**
- Writing, creating, modifying, or deleting ANY file
- Using `echo >`, `cat >`, `sed -i`, `awk`, `tee`, `mv`, `cp`, `rm`, `mkdir`
- Using the `write` or `patch` tools
- Running `git commit`, `git add`, `git checkout`, `git reset`
- Using any editor (`vim`, `nano`, `ed`)
- Running `pip install`, `npm install`, or any package manager
- Creating or modifying directories

If you feel the urge to "fix" or "improve" something you find, RESIST IT.
Document the finding in your report instead.
Your job is to OBSERVE and REPORT, not to change anything.
"""

_READ_WRITE_DIRECTIVE = """\
## READ-WRITE MODE — DEVELOPMENT ACTIVE

You are operating in development mode. You MAY read and write files.

**Guidelines:**
- Make targeted, minimal changes — do not refactor code you weren't asked to change
- Always verify changes compile/pass tests before reporting success
- Document every file you modify in your report
- Do NOT modify files outside the target project directory
- Do NOT delete files unless explicitly instructed
"""


def _get_mode_directive(mode: str) -> str:
    """Return the system directive prefix for the given workflow mode."""
    if mode in ("audit", "research"):
        return _READ_ONLY_DIRECTIVE
    elif mode == "develop":
        return _READ_WRITE_DIRECTIVE
    return _READ_ONLY_DIRECTIVE  # Default to safe mode


async def execute_langchain_tool_node(
    state: dict[str, Any],
    phase_spec: dict[str, Any],
    output_key: str,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Execute a LangChain BaseTool as an Beagle workflow node.

    Called when workflow_loader detects executor="langchain_tool"
    in the phase YAML. Falls through to goose executor for any
    phase that doesn't specify an executor (backward compatible).
    """
    from ..bridges.tool_node import execute_langchain_tool_node as _exec

    return await _exec(state, phase_spec, output_key, timeout)


async def execute_langchain_llm_node(
    state: dict[str, Any],
    phase_spec: dict[str, Any],
    output_key: str,
    model_override: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Execute an LLM call in-process via OllamaCloudChatModel.

    Called when workflow_loader detects executor="langchain_llm"
    in the phase YAML. Uses BaseChatModel.ainvoke() instead of
    Goose subprocess, enabling LangSmith tracing and structured output.
    """
    from ..bridges.llm_node import execute_langchain_llm_node as _exec

    return await _exec(state, phase_spec, output_key, model_override, timeout)


async def execute_goose_node(
    state: dict[str, Any],
    skill_name: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    output_key: str,
    model_override: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Generic LangGraph node wrapper around Goose execution.

    FIX: Enhanced context tracking with hierarchical memory integration.
    - Retrieves relevant memories before execution
    - Stores results in memory after successful completion
    - Maintains context across workflow nodes

    Args:
        state: Current workflow state dict
        skill_name: Agent recipe name (e.g., "research-planner")
        prompt_builder: Function that takes state and returns prompt string
        output_key: Key to store result in state

    Returns:
        State update dict (LangGraph convention)

    """
    # FIX: Retrieve relevant memories from hierarchical memory
    memory_context = ""
    if HIERARCHICAL_MEMORY_AVAILABLE:
        try:
            mem = await get_hierarchical_memory()
            state.get("workflow_id", "unknown")
            # Query for relevant memories based on the current query
            memories = await mem.traverse(
                state.get("query", ""),
                budget_tokens=2000,  # Limit to ~2000 tokens of context
            )
            if memories:
                memory_entries = "\n".join(f"## Memory: {m.content[:500]}..." for m in memories[:3])
                memory_context = (
                    f"\n\n<context_from_memory>\n{memory_entries}\n</context_from_memory>\n"
                )
                logger.debug(f"[{skill_name}] Loaded {len(memories)} relevant memories")
        except (RuntimeError, ValueError, OSError) as mem_err:
            logger.warning(f"[{skill_name}] Memory retrieval failed: {mem_err}")

    prompt = prompt_builder(state)

    # Load recipe as system directive (BASE content)
    # Uses safe_file_ops to auto-create missing recipes instead of skipping
    from ..utils.safe_file_ops import ensure_recipe_exists

    recipe_path = get_recipes_dir() / f"{skill_name}.xml"
    recipe_path = ensure_recipe_exists(recipe_path)
    recipe_content = recipe_path.read_text(encoding="utf-8")
    system_directive = recipe_content

    # H-MEM: Inject hierarchical memory context into system directive
    if memory_context:
        system_directive += memory_context

    # H-MEM: Inject hydrated context from hydration_node (v13)
    # This includes constraints, RAG-derived code, documentation, and session episodes
    hydrated_context = state.get("hydrated_context", "")
    if hydrated_context:
        system_directive = f"{system_directive}\n\n{hydrated_context}"
        logger.debug(f"[{skill_name}] Injected {len(hydrated_context)} chars of hydrated context")

    # H-MEM: Load skill-specific documentation from ContextManifest
    if CONTEXT_MANIFEST_AVAILABLE:
        try:
            manifest = get_manifest()
            if manifest:
                structural_template = build_structural_template(
                    manifest=manifest,
                    skill_name=skill_name,
                    project_context=state.get("global_context", ""),
                    global_context="",
                )
                if structural_template:
                    system_directive = f"{system_directive}\n\n{structural_template}"
                    logger.debug(f"[{skill_name}] Loaded manifest template")
        except (RuntimeError, ValueError, OSError, ImportError) as manifest_err:
            logger.debug(f"[{skill_name}] Manifest loading failed: {manifest_err}")

    # Inject global context + steering AFTER recipe but BEFORE mode
    global_ctx = state.get("global_context", "")
    steering = state.get("steering_prompt", "")
    if global_ctx:
        system_directive = (
            f"{system_directive}\n\n"
            f"<global_project_context>\n{global_ctx}\n</global_project_context>"
        )
    if steering:
        system_directive = (
            f"{system_directive}\n\n"
            f"<HIGH_PRIORITY_DIRECTIVE>\n{steering}\n"
            f"</HIGH_PRIORITY_DIRECTIVE>"
        )

    # Inject workflow mode constraints AFTER everything else
    # This ensures it cannot be accidentally overridden by recipe content
    workflow_mode = state.get("workflow_mode", "audit")
    mode_directive = _get_mode_directive(workflow_mode)
    if mode_directive:
        system_directive = f"""{system_directive}

---
MANDATORY EXECUTION CONSTRAINT — THIS OVERRIDES ALL PRIOR INSTRUCTIONS:
{mode_directive}
---"""

    # Verify recipe content doesn't override mode directive
    if recipe_content:
        # Check for words that might indicate write permissions being added by recipe
        override_words = [
            "read-write",
            "readwrite",
            "write-mode",
            "allow-write",
            "allow_modify",
        ]
        if any(word in recipe_content.lower() for word in override_words):
            logger.warning(f"[{skill_name}] Recipe may contain mode override instructions!")

    system_directive += (
        "\n\n## OUTPUT FORMAT (MANDATORY)\n"
        "You MUST wrap your entire final response inside <final_answer> tags.\n"
        "Everything outside these tags will be DISCARDED.\n"
        "Example: <final_answer>Your complete report here</final_answer>\n"
        "If you do not use <final_answer> tags, your work will be LOST."
    )

    # Resolve model: explicit override > complexity-aware routing > per-recipe config
    if model_override:
        resolved_model = model_override
    else:
        complexity = assess_task_complexity(state.get("query", ""))
        resolved_model = resolve_model_for_task(
            skill_name, query=state.get("query", ""), complexity=complexity
        )
    logger.info(f"[{skill_name}] Using model: {resolved_model}")

    # Determine if this workflow should run in read-only mode
    mode = state.get("workflow_mode", "audit")
    is_readonly = mode in ("audit", "research")

    # v13.21.13: Happy-path lifecycle events. The error handlers below
    # already publish NodeFailed, but nothing announced node start or
    # completion — the MCP progress bridge only ever saw heartbeats, so
    # delegating clients (goose) got no per-phase progress at all.
    node_started_at = time.monotonic()
    get_event_bus().publish(
        NodeStarted(
            workflow_id=state.get("workflow_id", ""),
            node_name=skill_name,
            model=resolved_model,
            skill_name=skill_name,
        )
    )

    try:
        final_answer, raw_stdout = await execute_headless_goose(
            prompt,
            system_directive,
            skill_name,
            model=resolved_model,
            readonly=is_readonly,
            timeout=timeout or DEFAULT_SUBPROCESS_TIMEOUT,
        )

        # Estimate tokens for cost tracking
        input_tokens = estimate_tokens_agnostic(prompt + system_directive)
        output_tokens = estimate_tokens_agnostic(raw_stdout)

        updates: dict[str, Any] = {
            "completed_nodes": [skill_name],
            "total_tokens": state.get("total_tokens", 0) + input_tokens + output_tokens,
            "metadata": {**state.get("metadata", {}), output_key: final_answer},
        }

        # Map output_key to known state fields — centralized mapping
        from beagle.utils.field_mapping import map_output_to_state

        target_key = map_output_to_state(output_key, skill_name=skill_name)
        if target_key:
            updates[target_key] = final_answer
            logger.debug(
                f"[{skill_name}] Mapped {output_key} -> {target_key} ({len(final_answer)} chars)"
            )

        # FIX: Always run synthesis to produce a final report, even if previous phases had issues
        # The workflow should never end without a synthesis phase for complex queries
        logger.info(f"[{skill_name}] Node completed: {output_key}")

        # v13.21.13: Happy-path completion event (cost is tracked elsewhere;
        # tokens + duration are what the progress bridge surfaces per phase).
        get_event_bus().publish(
            NodeCompleted(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                result=final_answer[:200],
                tokens=input_tokens + output_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=time.monotonic() - node_started_at,
            )
        )

        # FIX: Store result in hierarchical memory for cross-node context sharing
        if HIERARCHICAL_MEMORY_AVAILABLE:
            try:
                mem = await get_hierarchical_memory()
                # Store as episodic memory (persists across nodes)
                await mem.store(
                    content=final_answer[:5000],  # Limit stored content
                    level=MemoryLevel.EPISODIC,
                    metadata={
                        "skill": skill_name,
                        "output_key": output_key,
                        "workflow_id": state.get("workflow_id", ""),
                        "query": state.get("query", "")[:200],
                    },
                )
                logger.debug(
                    f"[{skill_name}] Stored {len(final_answer)} chars in hierarchical memory"
                )
            except (RuntimeError, ValueError, OSError) as mem_err:
                logger.warning(f"[{skill_name}] Memory storage failed: {mem_err}")

        return updates

    except TimeoutError as e:
        err_msg = f"{skill_name}: Timeout after {timeout}s"
        # v1.0.2 (P-fix4): attach a structured envelope alongside the
        # prose err_msg so downstream consumers can branch on
        # ``kind="timeout"`` + ``details["seconds"]`` rather than parsing
        # the prose string.
        envelope = _structured_error(skill_name, "timeout", err_msg, seconds=timeout, cause=str(e))
        logger.error(f"[{skill_name}] Node execution timed out after {timeout}s: {e}")
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="timeout",
                duration_seconds=timeout,
                node_phase=_infer_phase_from_skill(skill_name),
            )
        )
        return {
            "errors": [err_msg],
            "errors_structured": [envelope],
            "completed_nodes": [f"{skill_name}(timeout)"],
        }
    except FileNotFoundError as e:
        # Auto-creation should prevent this, but handle gracefully regardless
        from ..utils.safe_file_ops import safe_write

        err_msg = f"{skill_name}: File not found - {e}"
        envelope = _structured_error(skill_name, "not_found", err_msg, path=str(e).strip("'\""))
        logger.warning(f"[{skill_name}] File not found (auto-creation may have failed): {e}")
        # Attempt to create the missing file and retry once
        try:
            from pathlib import Path

            missing_path = Path(str(e).strip("'\""))
            safe_write(missing_path, "# Auto-created placeholder\n", create_if_missing=True)
            logger.info(f"[{skill_name}] Auto-created missing file: {missing_path}")
        except (OSError, RuntimeError) as create_exc:
            logger.warning(
                "[%s] Cannot auto-create the missing file (%s); the node failure below "
                "is reported without a retry.",
                skill_name,
                create_exc,
            )
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="system",
                node_phase=_infer_phase_from_skill(skill_name),
            )
        )
        return {
            "errors": [err_msg],
            "errors_structured": [envelope],
            "completed_nodes": [f"{skill_name}(failed)"],
        }
    except PermissionError as e:
        err_msg = f"{skill_name}: Permission denied - {e}"
        envelope = _structured_error(skill_name, "permission", err_msg, cause=str(e))
        logger.error(f"[{skill_name}] Permission denied: {e}")
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="system",
                node_phase=_infer_phase_from_skill(skill_name),
            )
        )
        return {
            "errors": [err_msg],
            "errors_structured": [envelope],
            "completed_nodes": [f"{skill_name}(failed)"],
        }
    except OSError as e:
        err_msg = f"{skill_name}: System error - {e}"
        envelope = _structured_error(skill_name, "system", err_msg, cause=str(e))
        logger.error(f"[{skill_name}] System error during execution: {e}")
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="system",
                node_phase=_infer_phase_from_skill(skill_name),
            )
        )
        return {
            "errors": [err_msg],
            "errors_structured": [envelope],
            "completed_nodes": [f"{skill_name}(failed)"],
        }
    except (ValueError, TypeError) as e:
        err_msg = f"{skill_name}: Configuration error - {e}"
        envelope = _structured_error(skill_name, "validation", err_msg, cause=str(e))
        logger.error(f"[{skill_name}] Invalid state or configuration: {e}")
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category="validation",
                node_phase=_infer_phase_from_skill(skill_name),
            )
        )
        return {
            "errors": [err_msg],
            "errors_structured": [envelope],
            "completed_nodes": [f"{skill_name}(failed)"],
        }
    except RuntimeError as e:
        err_msg = f"{skill_name}: {type(e).__name__} - {e}"
        envelope = _structured_error(
            skill_name, "runtime", err_msg, exc_type=type(e).__name__, cause=str(e)
        )
        logger.error(f"[{skill_name}] Unexpected error: {type(e).__name__}: {e}", exc_info=True)
        get_event_bus().publish(
            NodeFailed(
                workflow_id=state.get("workflow_id", ""),
                node_name=skill_name,
                error=err_msg,
                attempt=1,
                error_category=_classify_error(e),
                node_phase=_infer_phase_from_skill(skill_name),
            )
        )
        return {
            "errors": [err_msg],
            "errors_structured": [envelope],
            "completed_nodes": [f"{skill_name}(failed)"],
        }
        # Checkpointer is managed by the graph via checkpointer_cm
        # Nodes don't need to trigger saves separately


# ── Concrete node functions for standard workflows ──────────────────────────


def _make_prompt_builder(
    template: str,
    node_name: str = "",
) -> Callable[[dict[str, Any]], str]:
    """Create a prompt builder from a template with {variable} substitution.

    SP-7: implementation moved to ``beagle.utils.prompt_builder.make_prompt_builder``
    (a leaf module) to break the core.nodes <-> bridges.llm_node cycle. This
    wrapper keeps the existing call sites working.
    """
    from ..utils.prompt_builder import make_prompt_builder as _make

    return _make(template, node_name)


async def planning_node(state: dict[str, Any]) -> dict[str, Any]:
    """Planning phase — generates a structured investigation plan.

    Injects hydrated project documentation so the planner understands
    the architecture BEFORE creating a search plan.
    """

    def _build_planning_prompt(s: dict[str, Any]) -> str:
        query = s.get("query", "")
        # Include hydrated documentation so planner knows the architecture
        hydrated = s.get("hydrated_context", "")
        doc = s.get("hydration_documentation", "")
        context_block = ""
        if hydrated:
            context_block += f"\n\n<project_context>\n{hydrated}\n</project_context>\n"
        elif doc:
            context_block += f"\n\n<project_documentation>\n{doc}\n</project_documentation>\n"
        return (
            f"Generate a structured investigation plan for: '{query}'"
            f"{context_block}\n\n"
            f"IMPORTANT: Use the project documentation above to understand the "
            f"architecture BEFORE designing searches. Do NOT plan searches for "
            f"things the documentation already explains."
        )

    return await execute_goose_node(
        state,
        skill_name="research-planner",
        prompt_builder=_build_planning_prompt,
        output_key="research_plan",
        timeout=get_config().node_timeout.planning_seconds,
    )


async def execution_node(state: dict[str, Any]) -> dict[str, Any]:
    """Execution phase — executes the research plan with project context."""

    def _build_execution_prompt(s: dict[str, Any]) -> str:
        plan = s.get("research_plan", "")
        query = s.get("query", "")
        # Include hydrated documentation so executor knows the architecture
        hydrated = s.get("hydrated_context", "")
        doc = s.get("hydration_documentation", "")
        context_block = ""
        if hydrated:
            context_block += f"\n\n<project_context>\n{hydrated}\n</project_context>\n"
        elif doc:
            context_block += f"\n\n<project_documentation>\n{doc}\n</project_documentation>\n"
        return (
            f"Execute this research plan:\n{plan}\n\n"
            f"Original query: {query}"
            f"{context_block}\n\n"
            f"IMPORTANT: Read the project documentation above FIRST. "
            f"Use it to understand the architecture before running any searches. "
            f"Do NOT search for things the docs already explain."
        )

    return await execute_goose_node(
        state,
        skill_name="search-executor",
        prompt_builder=_build_execution_prompt,
        output_key="execution",
        timeout=get_config().node_timeout.execution_seconds,
    )


async def verification_node(state: dict[str, Any]) -> dict[str, Any]:
    """Verification phase — fact-checks execution results."""
    return await execute_goose_node(
        state,
        skill_name="fact-checker",
        prompt_builder=lambda s: (
            f"Verify these findings for accuracy:\n{s.get('raw_execution_context', '')}\n\n"
            f"Original query: {s.get('query', '')}"
        ),
        output_key="verification",
        timeout=get_config().node_timeout.verification_seconds,
    )


async def synthesis_node(state: dict[str, Any]) -> dict[str, Any]:
    """Synthesis phase — consolidates into final report."""
    return await execute_goose_node(
        state,
        skill_name="synthesis-writer",
        prompt_builder=lambda s: (
            f"Synthesize a comprehensive report from:\n"
            f"Plan: {s.get('research_plan', '')}\n"
            f"Findings: {s.get('raw_execution_context', '')}\n"
            f"Verified facts: {s.get('verified_facts', '')}\n\n"
            f"Original query: {s.get('query', '')}\n\n"
            "CRITICAL: In your <final_answer>, include BOTH:\n"
            "1. A prose executive summary (2-3 paragraphs).\n"
            "2. A JSON code block with structured findings:\n"
            "```json\n"
            "{\n"
            '  "summary": "...",\n'
            '  "findings": [\n'
            "    {\n"
            '      "severity": "critical|high|medium|low|info",\n'
            '      "category": "bug|security|performance|style|architecture",\n'
            '      "title": "Short one-line description",\n'
            '      "description": "Detailed explanation",\n'
            '      "file_path": "path/to/file.py",\n'
            '      "line_start": 42,\n'
            '      "suggested_fix": "Description of how to fix"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```"
        ),
        output_key="synthesis",
        timeout=_synthesis_timeout(state),
    )


def _synthesis_timeout(state: dict[str, Any]) -> int:
    """Return the wall-clock budget for synthesis.

    Static default (``node_timeout.synthesis_seconds``) is the floor; when
    upstream DAG phases have accumulated a large input (e.g. self-improvement
    fans out to 5+ phases producing 13-17K tokens of context), scale the
    budget proportionally. The model is large and the recipe requires
    per-claim file:line citations, so output is at least as big as input
    plus a ~2K-token overhead for citations. Use a pessimistic 30 tok/s
    floor for the wall-clock estimate.
    """
    base = get_config().node_timeout.synthesis_seconds

    research_plan = state.get("research_plan", "") or ""
    raw_execution_context = state.get("raw_execution_context", "") or ""
    verified_facts = state.get("verified_facts", "") or ""
    accumulated = f"{research_plan}\n{raw_execution_context}\n{verified_facts}"

    input_tokens = estimate_tokens_agnostic(accumulated)
    expected_output_tokens = max(input_tokens + 2000, 4000)
    min_seconds_for_work = expected_output_tokens // 30

    timeout = max(base, min_seconds_for_work)
    if timeout > base:
        logger.info(
            "[synthesis_node] Scaling timeout %ds -> %ds "
            "(input=%d tokens, expected_output~%d tokens)",
            base,
            timeout,
            input_tokens,
            expected_output_tokens,
        )
    return timeout
