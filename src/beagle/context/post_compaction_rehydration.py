"""Post-Compaction Rehydration — Ensures Beagle sessions never lose critical context.

After goose performs context compaction, the session must be rehydrated with:
1. Beagle system identity (role, capabilities, non-stop directive)
2. Current task context (workflow_id, query, completed_nodes, errors)
3. Essential project context (CLAUDE.md, key constraints)
4. Resume instruction (don't stop, continue from where you left off)

This module provides the rehydration prompt that MUST be injected
after every compaction event. It is called by the context monitor
and by the DAGOrchestrator's compression path.

DESIGN PRINCIPLE:
  "A compacted Beagle session is not a new session — it is the same
   session with a refreshed context window. The agent MUST continue
   executing without stopping until the task is complete."
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..style_guides.render import render_canonical
from .context_compaction_hook import (
    CompactionCheckpoint,
    discover_context_files,
    load_context_file,
)

logger = logging.getLogger("Beagle.post_compaction_rehydration")

# v1.2.0 (RG-7, BGL-012): strong references to background fold tasks so the
# event loop cannot collect them before they complete. Mirrors events/bus.py.
_pending_fold_tasks: set[Any] = set()


def _memory_upload_on_final_enabled() -> bool:
    """Opt-in gate (default OFF) for the tide.comet.amber Memory Upload Procedure.

    When ``BEAGLE_MEMORY_UPLOAD_ON_FINAL`` is truthy, the post-final-answer hook
    distils session knowledge into inline RAG / hierarchical memory. Kept
    env-gated rather than hardcoded-on so the every-turn hot path is unchanged by
    default — untested distillation must be an explicit choice, not a surprise.
    """
    import os

    return os.environ.get("BEAGLE_MEMORY_UPLOAD_ON_FINAL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ── Beagle System Identity ──────────────────────────────────────────────────────
# This is the minimal identity block that MUST survive compaction.
# It is intentionally kept short (<800 tokens) to fit in any context window.


BEAGLE_SYSTEM_IDENTITY = """\
<system_identity>
You are the Beagle (Beagle) orchestration engine,
running inside goose. You coordinate multi-agent workflows to accomplish tasks.

CRITICAL RULES (never forget, even after compaction):
1. NEVER stop execution until the task is FULLY complete and verified.
2. If you encounter an error, diagnose it, fix it, and continue.
3. If context was compacted, RE-HYDRATE: read .beagle/progress.md FIRST.
4. If you are unsure what to do next, check .beagle/progress.md and continue.
5. Use delegation (delegate tool) for parallel sub-tasks.
6. Use Beagle workflows (research, develop, audit, etc.) for structured tasks.
7. Report progress frequently — brief status updates before each action.
8. When complete, output <final_answer> with the deliverable.

WORK PROTOCOL (always active):
- Plan in sections: each section ≤150 lines of changes, one commit per section
- Write plan to .beagle/current_phase.md before starting
- Update .beagle/progress.md after every commit (DONE/NEXT/FILES_REMAINING)
- Read only what you need from files (use offset/limit, not full reads)
- If compacted: read .beagle/progress.md first, then continue from NEXT
</system_identity>"""

# ── Adaptive Chunking ────────────────────────────────────────────────────────


def _build_chunking_directive(compaction_count: int) -> str:
    """Generate progressively stricter work decomposition instructions.

    This affects HOW THE AGENT THINKS AND PLANS, not just file I/O size.
    Each compaction level forces smaller planning units, shorter tool chains,
    and more frequent checkpoints.

    Args:
        compaction_count: Number of times context has been compacted.

    Returns:
        XML-tagged chunking directive string, or empty string if count is 0.

    """
    if compaction_count <= 0:
        return ""

    if compaction_count == 1:
        return (
            '<adaptive_chunking level="moderate">\n'
            "Context was compacted once. Your plan was too large for the "
            "context window.\nAdjust your approach:\n\n"
            "PLANNING:\n"
            "- Break remaining work into phases of 3-5 steps max\n"
            "- Only plan ONE phase at a time — don't load the full "
            "roadmap into context\n"
            "- After completing each phase, reassess what's next "
            "from scratch\n"
            "- Write your current phase plan to "
            ".beagle/current_phase.md (overwrite each time)\n\n"
            "EXECUTION:\n"
            "- Maximum 200 lines per file write/edit\n"
            "- git commit after each logical unit of work\n"
            "- Read only the parts of files you need (use "
            "offset/limit), not whole files\n"
            "- Don't hold more than 3 files in working memory — "
            "finish one, commit, move on\n\n"
            "CONTEXT MANAGEMENT:\n"
            "- Before starting work, write a 5-line summary of "
            "what's done and what's next\n"
            "  to .beagle/progress.md — this survives compaction\n"
            "- If you need to reference a large file later, note "
            "the file path and line range\n"
            "  in .beagle/progress.md instead of keeping it in "
            "context\n"
            "</adaptive_chunking>"
        )

    if compaction_count == 2:
        return (
            '<adaptive_chunking level="aggressive">\n'
            "WARNING: Context compacted TWICE. Your work units are "
            "still too large.\n\n"
            "PLANNING — MANDATORY:\n"
            "- Maximum 3 steps per phase. Plan 3 things, do them, "
            "commit, plan next 3.\n"
            "- Do NOT think about the full remaining task — only the "
            "immediate next 3 actions\n"
            "- Write each 3-step plan to .beagle/current_phase.md "
            "before starting\n"
            "- After each phase: commit, update .beagle/progress.md "
            "with what's done\n\n"
            "EXECUTION — MANDATORY:\n"
            "- Maximum 100 lines per Edit/Write call\n"
            "- ONE file per phase. Finish it completely before "
            "touching another file.\n"
            "- git commit after EVERY phase (every 3 steps)\n"
            "- Do NOT read files you're not about to edit in this "
            "phase\n\n"
            "CONTEXT MANAGEMENT — MANDATORY:\n"
            "- After every commit, write to .beagle/progress.md:\n"
            "  DONE: [what you just finished]\n"
            "  NEXT: [the next 3 steps only]\n"
            "  FILES_REMAINING: [list of files still to modify]\n"
            "- If you need information from a previous phase, read "
            ".beagle/progress.md,\n"
            "  don't try to remember it\n"
            "</adaptive_chunking>"
        )

    # compaction_count >= 3
    return (
        f'<adaptive_chunking level="minimal">\n'
        f"CRITICAL: Context compacted {compaction_count} TIMES. You keep "
        f"exceeding the window.\n\n"
        "PLANNING — ONE STEP AT A TIME:\n"
        "- Plan exactly ONE action. Do it. Commit. Then plan the "
        "next ONE action.\n"
        "- Do NOT look ahead. Do NOT think about what comes after "
        "the current step.\n"
        "- Each action must be completable in under 50 lines of "
        "changes.\n"
        "- If a step needs more than 50 lines: split it into "
        "sub-steps FIRST,\n"
        "  write the sub-steps to .beagle/current_phase.md, then "
        "do them one by one.\n\n"
        "EXECUTION — ABSOLUTE MINIMUMS:\n"
        "- Maximum 50 lines per Edit/Write\n"
        "- git commit after EVERY SINGLE file modification\n"
        "- If creating a new file > 50 lines: create it empty "
        "with just the docstring\n"
        "  and imports, commit, then add one function at a time "
        "with commits between each\n"
        "- Read ONLY the exact lines you need (offset + limit of "
        "~30 lines)\n"
        "- NEVER read more than one file between commits\n\n"
        "CONTEXT MANAGEMENT — SURVIVAL MODE:\n"
        "- .beagle/progress.md is your external memory. Update it "
        "after EVERY commit:\n"
        "  STEP_JUST_DONE: [one line]\n"
        "  NEXT_STEP: [one line]\n"
        "  REMAINING_STEPS: [numbered list]\n"
        "  CRITICAL_CONTEXT: [any values/paths you'll need and "
        "would otherwise forget]\n"
        "- Before ANY action, read .beagle/progress.md first to "
        "reorient\n"
        "- If compacted again, your FIRST action must be: read "
        ".beagle/progress.md\n"
        "</adaptive_chunking>"
    )


# ── Rehydration Builder ──────────────────────────────────────────────────────


def build_rehydration_prompt(
    checkpoint: CompactionCheckpoint | None = None,
    workflow_id: str = "",
    query: str = "",
    completed_nodes: list[str] | None = None,
    errors: list[str] | None = None,
    project_dir: Path | None = None,
) -> str:
    """Build the full rehydration prompt after context compaction.

    This prompt is designed to be the FIRST thing injected into goose's
    context after compaction. It provides:
    - System identity (who you are, non-stop directive)
    - Task context (what you were doing)
    - Progress state (where you are in the workflow)
    - Essential project context (CLAUDE.md, constraints)
    - Explicit resume instruction (continue, don't stop)

    Args:
        checkpoint: Saved state from before compaction (preferred).
        workflow_id: Current workflow ID (fallback if no checkpoint).
        query: Current task query (fallback if no checkpoint).
        completed_nodes: Nodes completed so far.
        errors: Any errors encountered.
        project_dir: Project root for context file discovery.

    Returns:
        Formatted rehydration prompt string.

    """
    parts: list[str] = []

    # 1. System identity (always first, always included)
    parts.append(BEAGLE_SYSTEM_IDENTITY)

    # 2. Compaction event notification
    parts.append("\n<compaction_event>")
    parts.append("Context compaction occurred. Your context window was cleared and rehydrated.")
    parts.append("This is NORMAL — do NOT stop. Continue executing the current task.")
    parts.append("</compaction_event>")

    # 3. Task context
    if checkpoint:
        wf_id = checkpoint.session_id or workflow_id or "unknown"
        task = checkpoint.current_task
        iteration = checkpoint.iteration
        total = checkpoint.total_iterations
        next_steps = checkpoint.next_steps
        files_modified = checkpoint.files_modified
    else:
        wf_id = workflow_id or "unknown"
        task = query or "unknown task"
        iteration = 0
        total = 0
        next_steps = []
        files_modified = []

    nodes_done = completed_nodes or []
    task_errors = errors or []

    parts.append("\n<task_context>")
    parts.append(f"Workflow ID: {wf_id}")
    parts.append(f"Task: {task}")
    if total > 0:
        parts.append(f"Progress: iteration {iteration}/{total}")
    if nodes_done:
        parts.append(f"Completed nodes: {', '.join(nodes_done)}")
    if task_errors:
        parts.append(f"Errors encountered: {len(task_errors)}")
        for err in task_errors[:3]:
            parts.append(f"  - {err}")
    parts.append("</task_context>")

    # 3b. Tool routing state (v13.7.0 — survives compaction)
    if checkpoint:
        _has_tool_state = (
            checkpoint.tool_preferences
            or checkpoint.model_overrides
            or checkpoint.tool_failure_history
        )
        if _has_tool_state:
            parts.append("\n<tool_routing_state>")
            if checkpoint.tool_preferences:
                parts.append("Active executor preferences:")
                for node, executor in checkpoint.tool_preferences.items():
                    parts.append(f"  - {node}: executor={executor}")
            if checkpoint.model_overrides:
                parts.append("Model overrides:")
                for node, model in checkpoint.model_overrides.items():
                    parts.append(f"  - {node}: model={model}")
            if checkpoint.fallback_directives:
                parts.append(f"Fallback chain: {', '.join(checkpoint.fallback_directives[:5])}")
            if checkpoint.tool_failure_history:
                parts.append(f"Recent tool failures ({len(checkpoint.tool_failure_history)}):")
                for failure in checkpoint.tool_failure_history[-3:]:
                    parts.append(
                        f"  - {failure.get('tool_name', '?')}: {failure.get('error', '?')[:100]}"
                    )
            parts.append("</tool_routing_state>")

    # Fold pointer (TurboQuant) — emitted when checkpoint has fold_id
    if checkpoint is not None and checkpoint.fold_id:
        parts.append("\n<fold_pointer>")
        parts.append(f"TurboQuant fold ID: {checkpoint.fold_id}")
        parts.append(
            "This fold contains your compressed context. Use query_fold tool to search it."
        )
        parts.append("</fold_pointer>")

    # 4. Resume instructions (THE KEY FIX — never stop)
    parts.append("\n<resume_directive>")
    parts.append(
        "CRITICAL: Do NOT stop executing. Context was compacted, but your task is NOT complete."
    )
    parts.append("You MUST continue from where you left off. Do NOT restart from the beginning.")
    if next_steps:
        parts.append("")
        parts.append("Next steps from checkpoint:")
        for step in next_steps[:5]:
            parts.append(f"  - {step}")
    parts.append("")
    parts.append("Resume protocol:")
    parts.append("  1. Review the task context above")
    parts.append("  2. Check what was already completed")
    parts.append("  3. Continue with the next unfinished step")
    parts.append("  4. If all steps are done, produce the final deliverable")
    parts.append("  5. NEVER halt and ask for instructions — keep working autonomously")

    # Adaptive chunking: tell agent to read progress file first
    compaction_count = 0
    if checkpoint:
        compaction_count = getattr(checkpoint, "compaction_count", 0)
    if compaction_count > 0:
        parts.append("")
        parts.append(
            "FIRST ACTION AFTER READING THIS: Read .beagle/progress.md for "
            "your external memory checkpoint. Do NOT try to reconstruct "
            "the task from scratch — your notes are there."
        )
    parts.append("</resume_directive>")

    # 5b. Adaptive chunking — learns from compaction frequency
    chunking = _build_chunking_directive(compaction_count)
    if chunking:
        parts.append(f"\n{chunking}")

    # 5. Project context (lightweight — just key files)
    parts.append("\n<project_context>")
    context_files = discover_context_files(project_dir)
    loaded_any = False
    for filename, filepath in context_files.items():
        # Load only the most critical files — keep under 2000 chars total
        content = load_context_file(filepath, max_size=2000)
        if content and content.strip():
            # Truncate aggressively for rehydration
            if len(content) > 1500:
                content = content[:1500] + "\n... [truncated for rehydration]"
            parts.append(f"\n### {filename}")
            parts.append(content)
            loaded_any = True

    if not loaded_any:
        parts.append("(No project context files found — proceed with task context above)")
    parts.append("</project_context>")

    # 6. Files modified (for verification)
    if files_modified:
        parts.append("\n<files_modified>")
        for f in files_modified[-10:]:
            parts.append(f"  - {f}")
        parts.append("</files_modified>")

    return "\n".join(parts)


def build_lightweight_rehydration(
    query: str = "",
    workflow_id: str = "",
    completed_nodes: list[str] | None = None,
) -> str:
    """Build a minimal rehydration prompt for frequent in-loop compaction.

    Used when compaction happens mid-node execution and we need
    a quick rehydration that fits in a small context space.

    Args:
        query: Current task query.
        workflow_id: Current workflow ID.
        completed_nodes: Nodes completed so far.

    Returns:
        Minimal rehydration prompt (~300 tokens).

    """
    nodes_str = ", ".join(completed_nodes) if completed_nodes else "none yet"

    return f"""\
{BEAGLE_SYSTEM_IDENTITY}

<quick_resume>
Context compacted mid-execution. Continue immediately.
Task: {query or "unknown"}
Workflow: {workflow_id or "unknown"}
Completed: [{nodes_str}]
DO NOT STOP. Continue the current task from where you left off.
</quick_resume>"""


def on_post_compaction(
    checkpoint: CompactionCheckpoint | None = None,
    workflow_id: str = "",
    query: str = "",
    completed_nodes: list[str] | None = None,
    errors: list[str] | None = None,
    project_dir: Path | None = None,
) -> str:
    """Main entry point: call this immediately after context compaction.

    This triggers the full rehydration pipeline:
    1. Build rehydration prompt with task context
    2. Inject Beagle system identity
    3. Load essential project files
    4. Generate resume directive
    5. RE-RENDER TOP-OF-MIND XML for immediate injection via tom extension

    The returned string should be prepended to the next goose prompt
    or injected as a system message.

    Args:
        checkpoint: Saved state from before compaction.
        workflow_id: Current workflow ID (fallback).
        query: Current task query (fallback).
        completed_nodes: Nodes completed so far.
        errors: Any errors encountered.
        project_dir: Project root for context files.

    Returns:
        Full rehydration prompt string to inject into context.

    """
    prompt = build_rehydration_prompt(
        checkpoint=checkpoint,
        workflow_id=workflow_id,
        query=query,
        completed_nodes=completed_nodes,
        errors=errors,
        project_dir=project_dir,
    )

    # RE-RENDER TOP-OF-MIND: This ensures the TOML rules are fresh in context
    # after compaction. The tom extension reads GOOSE_MOIM_MESSAGE_FILE on
    # EVERY turn, so writing it here guarantees immediate availability.
    try:
        if project_dir is not None:
            render_canonical(domain=None)
        else:
            # Try to infer project dir from checkpoint or cwd
            render_canonical(domain=None)
        logger.info(
            "[PostCompactionRehydration] Re-rendered Top-of-Mind XML to "
            "~/.config/goose/beagle_top_of_mind.xml"
        )
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — Top-of-Mind re-render is best-effort; failure must not block rehydration
        logger.warning(f"[PostCompactionRehydration] Top-of-Mind re-render failed: {e}")

    logger.info(
        f"[PostCompactionRehydration] Built rehydration prompt "
        f"({len(prompt)} chars) for workflow {workflow_id or 'unknown'}"
    )

    return prompt


def save_compaction_checkpoint_for_orchestrator(
    orchestrator: Any,
) -> CompactionCheckpoint:
    """Create a compaction checkpoint from a DAGOrchestrator's current state.

    Call this BEFORE compressing context in the orchestrator loop,
    so the post-compaction rehydration has the right context.

    Args:
        orchestrator: The DAGOrchestrator instance.

    Returns:
        CompactionCheckpoint ready for rehydration.

    """
    state = orchestrator.state
    checkpoint = CompactionCheckpoint(
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        current_task=state.query or "unknown",
        iteration=len(state.completed_nodes),
        total_iterations=len(orchestrator.nodes),
        files_modified=[],
        pending_commits=[],
        next_steps=[
            f"Continue from node: {state.completed_nodes[-1]}"
            if state.completed_nodes
            else "Start from first node",
            f"Workflow ID: {orchestrator.workflow_id}",
            "DO NOT STOP until all nodes are executed",
        ],
        session_id=orchestrator.workflow_id or "",
        # v13.7.0: Capture tool routing state for rehydration continuity
        tool_preferences={
            spec.get("name", ""): spec["executor"]
            for spec in getattr(orchestrator, "_node_specs", [])
            if spec.get("executor") and spec.get("executor") != "goose"
        },
        model_overrides={
            spec.get("name", ""): spec["model"]
            for spec in getattr(orchestrator, "_node_specs", [])
            if spec.get("model")
        },
        fallback_directives=list(
            getattr(
                getattr(orchestrator, "config", None),
                "fallback_chain",
                [],
            )
        ),
        tool_failure_history=(
            getattr(state, "tool_failure_history", [])[-10:]
            if hasattr(state, "tool_failure_history")
            else state.get("tool_failure_history", [])[-10:]
            if isinstance(state, dict)
            else []
        ),
    )

    # Write external progress file that survives compaction and process restart.
    # This is the agent's "external memory" — the rehydration prompt instructs
    # the agent to read this file first after compaction.
    try:
        progress_path = Path.home() / ".beagle" / "progress.md"
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        remaining = [n for n in orchestrator.nodes if n not in state.completed_nodes]
        compaction_count = 0
        # Try to get from monitor
        try:
            from beagle.context.context_compaction_hook import (
                get_monitor,
            )

            compaction_count = get_monitor()._compaction_count
        except (ImportError, AttributeError, RuntimeError, OSError) as exc:
            logger.warning(
                "Cannot read the compaction count from the context monitor (%s); the "
                "progress note will report 0 compactions.",
                exc,
            )

        lines = [
            "# Beagle Progress Checkpoint (auto-generated on compaction)",
            "",
            f"WORKFLOW: {orchestrator.workflow_id}",
            f"TASK: {state.query}",
            f"COMPLETED: {', '.join(state.completed_nodes) or 'none'}",
            f"REMAINING: {', '.join(remaining) or 'none'}",
            f"COMPACTION_COUNT: {compaction_count}",
            f"ERRORS: {len(state.errors)}",
        ]
        if state.errors:
            lines.append(f"LAST_ERROR: {str(state.errors[-1])[:200]}")
        lines.extend(
            [
                "",
                "## Resume Instructions",
                "- Read this file to reorient after compaction",
                "- Check REMAINING nodes above",
                "- Continue from the next remaining node",
                (
                    f"- Work in "
                    f"{'small' if compaction_count >= 2 else 'moderate'} "
                    f"chunks (compacted {compaction_count} times)"
                ),
            ]
        )
        progress_path.write_text("\n".join(lines))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(
            "Cannot write the post-compaction progress note (%s); the checkpoint is "
            "returned without it, so the next turn loses the compaction guidance.",
            exc,
        )

    return checkpoint


# ── Post-Final-Answer Fold (v13.21.13) — Runtime-side enforcement ────────────
#
# The TOML declares <post_final_answer_fold required="true"/>, but enforcement
# has historically been delegated to the model itself (it must remember to
# call check_and_fold_context after every </final_answer>). That pattern is
# unreliable across providers — the deepseek session in
# Projects/Skylon_Ecosystem/skylon recorded <next_step>Post-final-answer fold
# </next_step> in its progress.xml and then never executed it.
#
# This module provides a deterministic, runtime-side hook that fires
# UNCONDITIONALLY at workflow / session / sub-agent completion boundaries.
# It bypasses the 0.58 pre_compact threshold (a session ending at 30% context
# still folds) and writes the rehydration sidecar so the NEXT session's
# bootstrap can rehydrate without depending on the previous session's
# model-cooperative behaviour.


def enforce_post_final_answer_fold(
    workflow_id: str = "cli_session",
    query: str = "",
    completed_nodes: list[str] | None = None,
    project_dir: Path | None = None,
    percentage: float = 0.0,
    session_text: str = "",
) -> dict[str, Any]:
    """Runtime-side enforcement of the post_final_answer_fold TOML contract.

    Unlike :func:`check_and_fold_context`, this function ALWAYS fires the fold
    and ALWAYS writes the rehydration sidecar, regardless of the supplied
    ``percentage``. The threshold gate is the entire point of the gap: a
    session ending at 30% context is still required to leave a rehydration
    sidecar behind so the next session can resume cleanly.

    Args:
        workflow_id: Current workflow / session identifier.
        query: Current task description (used in the sidecar).
        completed_nodes: Nodes completed so far (used in the sidecar).
        project_dir: Optional project root for context-file discovery.
        percentage: Reported context-usage fraction (stored as metadata only;
            does NOT gate the fold).
        session_text: Optional richer session content to distil for the Memory
            Upload Procedure. When empty, a thin seed (query + completed_nodes)
            is used. Only consulted when ``BEAGLE_MEMORY_UPLOAD_ON_FINAL`` is set.

    Returns:
        Dict with keys: ``status``, ``workflow_id``, ``sidecar_path``,
        ``sidecar_chars``, ``fold_id``, ``action``, ``memory_uploaded``.
        ``action`` is always ``"compact_now"`` so the caller can confirm the
        fold executed; ``memory_uploaded`` is the number of knowledge points
        pushed into RAG (0 unless the opt-in memory-upload gate is enabled).

    """
    from .context_integration import get_context_integration

    sidecar_path = Path.home() / ".beagle" / "post_compaction_rehydration.txt"
    result: dict[str, Any] = {
        "status": "ok",
        "workflow_id": workflow_id,
        "sidecar_path": str(sidecar_path),
        "sidecar_chars": 0,
        "fold_id": "",
        "action": "compact_now",
        "trigger": "post_final_answer_fold",
        "percentage_reported": round(float(percentage), 4),
        "memory_uploaded": 0,
    }

    # 1. Build the full rehydration prompt via the existing pipeline.
    rehydration_full = on_post_compaction(
        workflow_id=workflow_id,
        query=query,
        completed_nodes=completed_nodes or [],
        project_dir=project_dir,
    )

    # 2. Write the rehydration sidecar UNCONDITIONALLY.
    #    A previous regression (v13.21.12 bridge fix 306eb08) addressed the
    #    orchestrator bridge; this addresses the runtime trigger that the
    #    bridge fix did not cover. Both paths must converge on the same
    #    sidecar so bootstrap can rehydrate from either entry point.
    try:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(rehydration_full)
        result["sidecar_chars"] = len(rehydration_full)
        logger.info(
            f"[PostFinalAnswerFold] Sidecar written ({len(rehydration_full)} chars) "
            f"for workflow {workflow_id} → {sidecar_path}"
        )
    except OSError as exc:
        result["status"] = "sidecar_write_failed"
        result["error"] = f"sidecar write failed: {exc}"
        logger.error(f"[PostFinalAnswerFold] Sidecar write failed for {workflow_id}: {exc}")

    # 2a. v13.22.0: Mark compaction_state.json with last_fold_type=
    #     "post_final_answer" so the WatchdogActor knows a fold just ran
    #     and respects the 1-hour timer.  Best-effort: a failed state
    #     write must NOT block the sidecar write above (which is the
    #     authoritative rehydration artefact).
    try:
        _state_path = Path.home() / ".beagle" / "compaction_state.json"
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        _state: dict[str, Any] = {}
        with suppress(OSError, ValueError, json.JSONDecodeError):
            _state = json.loads(_state_path.read_text()) if _state_path.is_file() else {}
        _state["last_compaction"] = time.time()
        _state["last_fold_type"] = "post_final_answer"
        _state["compaction_count"] = int(_state.get("compaction_count", 0)) + 1
        _state.setdefault("history", []).append(
            {
                "fold_type": "post_final_answer",
                "timestamp": time.time(),
                "workflow_id": workflow_id,
            }
        )
        _state["history"] = _state["history"][-10:]
        _tmp = _state_path.with_suffix(_state_path.suffix + ".tmp")
        _tmp.write_text(json.dumps(_state, indent=2))
        _tmp.replace(_state_path)
        logger.debug(
            f"[PostFinalAnswerFold] Marked compaction_state: count={_state['compaction_count']}"
        )
    except (OSError, ValueError, RuntimeError) as exc:
        # Non-fatal: the sidecar is the rehydration source of truth.
        logger.debug(f"[PostFinalAnswerFold] Failed to mark compaction_state: {exc}")

    # 2b. Memory Upload Procedure (tide.comet.amber) — opt-in, default OFF.
    #     Distils session knowledge into inline RAG / hierarchical memory at the
    #     post-final boundary. NEVER blocks finalize, NEVER runs unless
    #     BEAGLE_MEMORY_UPLOAD_ON_FINAL is set. The runtime re-applies the secret
    #     scrub + dedup + significance gates, so a thin seed safely uploads
    #     little; pass `session_text` for a richer distillation.
    if _memory_upload_on_final_enabled():
        try:
            from beagle.memory.memory_upload import remember

            seed = session_text.strip() or (
                f"Workflow {workflow_id}: {query}\n"
                f"Completed: {', '.join(completed_nodes or []) or '(none)'}"
            )
            upload = remember(seed, source=f"post_final:{workflow_id}")
            result["memory_uploaded"] = len(upload.uploaded)
            if upload.uploaded:
                logger.info(
                    "[PostFinalAnswerFold] memory_upload stored %d point(s) for %s",
                    len(upload.uploaded),
                    workflow_id,
                )
        except (ImportError, RuntimeError, OSError, ValueError) as exc:
            logger.warning(
                "[PostFinalAnswerFold] memory_upload skipped for %s: %s",
                workflow_id,
                exc,
            )

    # 3. Build a TurboQuant fold via ContextIntegration (best-effort).
    #    Failures here do NOT downgrade the status — the sidecar is the
    #    durable artefact; the fold is an optimization.
    try:
        import datetime as _dt

        integration = get_context_integration()
        data_payload = (
            f"# Post-Final-Answer Fold — {_dt.datetime.now(_dt.UTC).isoformat()}\n"
            f"Workflow: {workflow_id}\n"
            f"Query: {query or '(none)'}\n"
            f"Reported context usage: {percentage:.1%}\n"
            f"Completed nodes: {', '.join(completed_nodes or []) or '(none)'}\n"
            f"Rehydration: {rehydration_full[:200]}...\n"
        )
        # enhanced_context_fold is async; in the runtime-enforcement path
        # we are inside an async context (called from MCP tool or orchestrator
        # finalize). If for any reason we are NOT in an async context the
        # fold is skipped — the sidecar is still durable.
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # Schedule the fold without awaiting — orchestrator finalize
            # must not block on it. The sidecar is the source of truth.
            # v1.2.0 (RG-7, BGL-012): hold a strong reference to the task so
            # the loop cannot collect it before it completes. The set pattern
            # mirrors events/bus.py:72 — add the task, discard on done.
            _fold_task = loop.create_task(
                integration.enhanced_context_fold(data_payload, "turboquant")
            )
            _pending_fold_tasks.add(_fold_task)
            _fold_task.add_done_callback(_pending_fold_tasks.discard)
        else:
            # Sync fallback — common in tests. We use asyncio.run with a
            # short timeout so tests can deterministically assert the fold
            # was attempted.
            try:
                asyncio.run(
                    asyncio.wait_for(
                        integration.enhanced_context_fold(data_payload, "turboquant"),
                        timeout=10.0,
                    )
                )
            except (TimeoutError, RuntimeError) as exc:
                logger.debug(f"[PostFinalAnswerFold] Sync fold attempt skipped: {exc}")
        # Record the fold id if available
        try:
            fold_stats = integration.get_stats()
            if fold_stats and "turbo_fold_id" in fold_stats.get("integration", {}):
                result["fold_id"] = fold_stats["integration"]["turbo_fold_id"] or ""
        except (AttributeError, RuntimeError, OSError, ValueError) as exc:
            logger.warning(
                "[PostFinalAnswerFold] Cannot read the fold id from integration stats "
                "(%s); the result omits fold_id, so the fold cannot be correlated later.",
                exc,
            )
    except (ImportError, RuntimeError, OSError, ValueError, AttributeError) as exc:
        logger.warning(f"[PostFinalAnswerFold] ContextIntegration fold skipped: {exc}")
        result["fold_error"] = str(exc)

    return result
