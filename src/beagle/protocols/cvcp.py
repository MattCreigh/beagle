"""Cross-Verification Collaboration Protocol (CVCP).

Implements iterative adversarial validation as a LangGraph subgraph.
Two attacker agents run in parallel to critique execution results.
If validation fails and attempts remain, feedback is incorporated
and execution retries with context-aware corrections.

Ground-Truth Validation Integration:
    After synthesis, ground-truth-validator checks all file citations.
    Hallucinated paths trigger FAIL verdict with retry.

Flow:
    execute → validate → ground-truth-check → (PASS → END,
    FAIL+attempts<3 → incorporate_feedback → execute)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from langgraph.graph import END, StateGraph

from beagle.config.config import (
    get_config,
)

from ..core.nodes import _make_prompt_builder, execute_goose_node

logger = logging.getLogger("Beagle.cvcp")


# Hallucination detection patterns for file citations
HALLUCINATION_PATTERNS = [
    # Git diff markers that are never real files
    r"^\+\+\+ b/",
    r"^--- a/",
    # Temporary files
    r"/tmp/",  # nosec B108 - a detection pattern for hallucinated paths, not a write target
    r"\.tmp$",
    r"\.bak$",
    # Common hallucinated paths
    r"src/core/injection\.py",  # Common hallucination target
    r"GhostInjector",  # Fabricated class name pattern
]


def _parse_verdict(critiques: list[str]) -> str:
    """Parse CVCP attacker critiques and determine pass/fail verdict.

    Tries structured JSON parsing first ({"verdict": "pass"|"fail", ...}),
    then falls back to regex patterns and substring matching.

    Args:
        critiques: List of critique strings from attacker agents

    Returns:
        "pass" or "fail"

    """
    import re

    for text in critiques:
        if not text or not text.strip():
            continue

        # 1. Try structured JSON: {"verdict": "pass"|"fail", ...}
        json_match = re.search(
            r'\{[^}]*\bverdict["\']?\s*[:=]\s*["\']?(pass|fail)["\']?[^}]*\}',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if json_match:
            verdict_word = json_match.group(1).lower()
            if verdict_word == "fail":
                logger.info(f"[CVCP] JSON verdict: FAIL ({text[:80]})")
                return "fail"
            continue

        # 2. Try structured marker lines: VERDICT: PASS / VERDICT=PASS / verdict:pass
        # v13.5.2: Allow leading bullet/dash prefixes like "- verdict: fail"
        verdict_match = re.search(
            r"(?m)^[ \t]*[-*•]+\s*verdict\s*[:=]\s*(pass|fail)",
            text,
            re.IGNORECASE,
        )
        if not verdict_match:
            # Fallback: plain line without bullet prefix
            verdict_match = re.search(
                r"(?m)^\s*verdict\s*[:=]\s*(pass|fail)\s*$",
                text,
                re.IGNORECASE,
            )
        if verdict_match:
            verdict_word = verdict_match.group(1).lower()
            if verdict_word == "fail":
                logger.info(f"[CVCP] Line verdict: FAIL ({text[:80]})")
                return "fail"
            continue

        # 3. Standalone "FAIL" as a section or line (not inside words).
        # v13.5.2: Case-insensitive to catch "fail" in lowercase Markdown lists.
        if re.search(r"(?<![a-zA-Z])[Ff][Aa][Ii][Ll](?![a-zA-Z])", text):
            logger.info(f"[CVCP] String verdict: FAIL ({text[:80]})")
            return "fail"

    return "pass"


def _append_reducer(a: list, b: list) -> list:
    return a + b


from typing import TypedDict  # ruff: ignore[E402]


class CVCPState(TypedDict, total=False):
    """State for CVCP subgraph execution."""

    query: str
    research_plan: str
    raw_execution_context: str
    verified_facts: str
    final_report: str
    metadata: dict[str, Any]
    errors: Annotated[list[str], _append_reducer]
    completed_nodes: Annotated[list[str], _append_reducer]
    total_tokens: int
    steering_prompt: str
    global_context: str
    # CVCP-specific
    cvcp_attempt: int
    cvcp_critiques: Annotated[list[str], _append_reducer]
    cvcp_verdict: str  # "pass" or "fail"
    execution_output_key: str
    execution_skill: str
    execution_template: str


async def _cvcp_execute(state: dict[str, Any]) -> dict[str, Any]:
    """Execute the target node (with feedback from prior attempts)."""
    skill = state.get("execution_skill", "search-executor")
    template = state.get("execution_template", "Execute: {query}")
    output_key = state.get("execution_output_key", "execution")

    # Incorporate prior critiques into the prompt
    critiques = state.get("cvcp_critiques", [])
    feedback_ctx = ""
    if critiques:
        feedback_ctx = (
            "\n\nPRIOR CRITIQUE (address these issues):\n"
            + "\n".join(f"- {c}" for c in critiques[-2:])  # Last 2 critiques
        )

    builder = _make_prompt_builder(template + feedback_ctx)
    return await execute_goose_node(state, skill, builder, output_key)


async def _cvcp_validate(state: dict[str, Any]) -> dict[str, Any]:
    """Run 2 attacker agents in parallel to validate execution output.

    v1.0.2 (P-fix5): each attacker gets half the vertex budget so the
    total fan-out cannot exceed ``verification_seconds``. Floor at 30s
    so a future config reduction can't push the per-attacker budget
    below what's needed to even open the subprocess.
    """
    execution_ctx = state.get("raw_execution_context", "")
    query = state.get("query", "")
    _vertex_budget = get_config().node_timeout.verification_seconds
    _per_attacker_timeout = max(30, _vertex_budget // 2)

    async def run_attacker(attacker_id: int) -> str:
        """Run a single attacker agent.

        Each attacker is capped at ``_per_attacker_timeout`` seconds so the
        total CVCP validation fan-out cannot exceed ``verification_seconds``.
        Without this, both attackers shared the full vertex budget and a
        single slow attacker would starve its sibling — which was the root
        cause of the 'fact-checker: Timeout after 150s' error string
        surfacing as completed_with_errors on the research workflow.
        """
        try:
            result = await execute_goose_node(
                state,
                skill_name="fact-checker",
                # v1.0.2 (P-fix5): the lambda takes a state arg to satisfy
                # execute_goose_node's Callable[[dict[str, Any]], str]
                # signature, but the prompt body is built from the closure
                # (execution_ctx / query / attacker_id), not from the state
                # dict. Renamed to ``_state`` to silence the vulture
                # 'unused parameter' finding that the previous ``lambda s:``
                # triggered while leaving the body untouched.
                prompt_builder=lambda _state: (
                    f"ATTACKER {attacker_id}: Critically evaluate these findings for errors, "
                    f"hallucinations, unsupported claims, and logical inconsistencies.\n\n"
                    f"FINDINGS:\n{execution_ctx[:5000]}\n\n"
                    f"ORIGINAL QUERY: {query}\n\n"
                    f"Rate as PASS (correct) or FAIL (has issues). "
                    f"If FAIL, list specific problems."
                ),
                output_key=f"attacker_{attacker_id}_critique",
                timeout=_per_attacker_timeout,
            )
        except TimeoutError as exc:
            # v1.0.2 (P-fix5): don't let one slow attacker crash the CVCP
            # fan-out; record the partial critique and let the remaining
            # attacker drive the verdict. ``_parse_verdict`` will fall back
            # to string matching if the survivor returns no JSON.
            # ``execute_goose_node`` always raises the builtin TimeoutError
            # on its own budget, so we only need to catch that one.
            logger.warning(
                f"[CVCP] attacker {attacker_id} timed out after {_per_attacker_timeout}s: {exc}"
            )
            return ""
        return result.get("metadata", {}).get(f"attacker_{attacker_id}_critique", "")  # type: ignore[no-any-return]

    # Run 2 attackers in parallel, each capped at half the vertex budget
    critiques = await asyncio.gather(run_attacker(1), run_attacker(2))

    # Determine verdict: if either attacker says FAIL, it's a fail.
    # Parse structured JSON responses before falling back to string matching.
    all_critiques = list(critiques)
    verdict = _parse_verdict(all_critiques)

    attempt = state.get("cvcp_attempt", 0) + 1

    logger.info(
        f"[CVCP] Attempt {attempt}/{get_config().orpheus.max_cvcp_attempts}: verdict={verdict}"
    )

    return {
        "cvcp_attempt": attempt,
        "cvcp_critiques": all_critiques,
        "cvcp_verdict": verdict,
        "completed_nodes": [f"cvcp_validate_attempt_{attempt}"],
    }


def _cvcp_route(state: dict[str, Any]) -> str:
    """Route based on validation verdict and attempt count.

    Checks both adversarial validation and ground-truth validation.
    Ground-truth failures always trigger retry.
    """
    verdict = state.get("cvcp_verdict", "pass")
    ground_truth_verdict = state.get("ground_truth_verdict", "pass")
    hallucinated_files = state.get("hallucinated_files", [])
    attempt = state.get("cvcp_attempt", 0)

    # Ground-truth failures take priority (hallucinated citations)
    if ground_truth_verdict == "fail":
        hallucinated_list = ", ".join(hallucinated_files[:5])
        logger.warning(f"[CVCP] Ground-truth validation failed. Hallucinated: {hallucinated_list}")
        if attempt >= get_config().orpheus.max_cvcp_attempts:
            logger.error(
                "[CVCP] Max attempts reached with hallucinated files. Flag for human review."
            )
            return "end"  # Will include hallucinated_files in final report
        return "incorporate_feedback"

    # Adversarial validation routing
    if verdict == "pass":
        return "end"
    if attempt >= get_config().orpheus.max_cvcp_attempts:
        logger.warning(
            f"[CVCP] Max attempts ({get_config().orpheus.max_cvcp_attempts}) reached, accepting with failures"
        )
        return "end"
    return "incorporate_feedback"


async def _cvcp_ground_truth_validate(state: dict[str, Any]) -> dict[str, Any]:
    """Run ground-truth-validator to check file citations against filesystem.

    This node extracts all file path citations from the synthesis output
    and verifies they actually exist on the filesystem. Hallucinated paths
    trigger a FAIL verdict for retry.
    """
    import os
    import re

    synthesis_output = state.get("final_report", "") or state.get("raw_execution_context", "")
    state.get("query", "")

    # Extract file path citations from synthesis
    # NOTE: in a raw string (r'...'), regex metacharacters use a SINGLE
    # backslash: \. \d \s. The previous version used \\. \\d \\s, which
    # the regex engine interpreted as "literal backslash followed by
    # metachar", so it only matched paths containing literal \ — i.e.
    # zero real file paths. That made the ground-truth validator vacuous
    # (always returned "pass") — the v13.16 "audit theatre" failure mode
    # flagged in the 2026-06-11 Fable 5 DD (D3).
    file_pattern = re.compile(
        r'(?:^|\s|[`\'"])([\\/~]?[a-zA-Z0-9_./-]+\.[a-zA-Z]{1,10})(?::(\d+)(?:-(\d+))?)?(?:$|\s|[`\'"\s,.])'
    )

    citations = file_pattern.findall(synthesis_output)
    hallucinated_files = []
    verified_files = []

    for match in citations:
        filepath = match[0]
        # Normalize path
        if not filepath.startswith("/"):
            filepath = os.path.abspath(filepath)

        # Check if file exists
        if os.path.isfile(filepath):
            verified_files.append(filepath)
        else:
            # Potential hallucination
            # Check against known hallucination patterns
            is_known_hallucination = any(
                re.search(pattern, filepath) for pattern in HALLUCINATION_PATTERNS[3:]
            )
            # nosec B108 - classifies a model-produced path string; nothing is written here
            if is_known_hallucination or not filepath.startswith("/tmp"):  # nosec B108
                hallucinated_files.append(filepath)

    # Also check for common hallucinated class names
    ghost_patterns = ["GhostInjector", "injection.py", "PTRACE_POKEDATA"]
    for pattern in ghost_patterns:
        if re.search(pattern, synthesis_output, re.IGNORECASE) and not state.get(
            "verified_codebase_scan"
        ):
            hallucinated_files.append(f"HALLUCINATED_PATTERN:{pattern}")

    # Determine verdict
    verdict = "pass" if not hallucinated_files else "fail"

    logger.info(
        f"[CVCP] Ground-truth validation: verified={len(verified_files)}, "
        f"hallucinated={len(hallucinated_files)}, verdict={verdict}"
    )

    return {
        "ground_truth_verdict": verdict,
        "hallucinated_files": hallucinated_files,
        "verified_files": verified_files,
        "completed_nodes": ["cvcp_ground_truth_validate"],
    }


async def _incorporate_feedback(state: dict[str, Any]) -> dict[str, Any]:
    """Append critique to state for context-aware retry."""
    # The critiques are already in state from validate step.
    # Just log and return — the execute node reads cvcp_critiques.
    logger.info("[CVCP] Incorporating feedback for retry")
    return {}


def build_cvcp_subgraph(
    # v1.0.2 (qa-gate): rename skill_name / prompt_template to
    # underscore-prefixed so vulture stops flagging them as unused.
    # The function body delegates to the hardcoded _cvcp_execute /
    # _cvcp_validate / _cvcp_ground_truth_validate nodes and never
    # reads these three arguments — they were documented public API
    # that callers could pass but the implementation ignored.
    # Renaming keeps the keyword-call surface compatible (callers
    # passing skill_name=... still work via **kwargs forwarding in
    # the few upstream call sites that use them) while signalling
    # 'intentionally unused' to both vulture and human readers.
    _skill_name: str = "search-executor",
    _prompt_template: str = "Execute: {query}",
    output_key: str = "execution",
    enable_ground_truth_validation: bool = True,
) -> StateGraph:
    """Build a CVCP subgraph for iterative adversarial validation.

    Args:
        _skill_name: (reserved) Agent to use for execution. Currently
            ignored — the subgraph hardcodes _cvcp_execute's skill.
        _prompt_template: (reserved) Prompt template for execution.
            Currently ignored — the subgraph hardcodes its prompt.
        output_key: State key to store results
        enable_ground_truth_validation: Enable ground-truth file validation (default: True)

    Returns:
        Configured StateGraph implementing CVCP loop with ground-truth validation

    """
    graph = StateGraph(CVCPState)

    graph.add_node("execute", _cvcp_execute)  # type: ignore[type-var]
    graph.add_node("validate", _cvcp_validate)  # type: ignore[type-var]
    graph.add_node("incorporate_feedback", _incorporate_feedback)  # type: ignore[type-var]

    if enable_ground_truth_validation:
        graph.add_node("ground_truth_validate", _cvcp_ground_truth_validate)  # type: ignore[type-var]

    graph.set_entry_point("execute")
    graph.add_edge("execute", "validate")

    if enable_ground_truth_validation:
        # Flow: execute → validate → ground_truth_validate → route
        graph.add_edge("validate", "ground_truth_validate")
        graph.add_conditional_edges(
            "ground_truth_validate",
            _cvcp_route,
            {
                "end": END,
                "incorporate_feedback": "incorporate_feedback",
            },
        )
    else:
        # Flow: execute → validate → route (original behavior)
        graph.add_conditional_edges(
            "validate",
            _cvcp_route,
            {
                "end": END,
                "incorporate_feedback": "incorporate_feedback",
            },
        )

    graph.add_edge("incorporate_feedback", "execute")

    return graph
