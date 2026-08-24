"""System directive template and model fallback configuration.

Contains the SYSTEM_DIRECTIVE_TEMPLATE that instructs subagents on
protocol, constraints, and output format, plus model fallback chain
configuration loaded from config.toml.
"""

from __future__ import annotations

import importlib.util as _importlib_util
import logging

logger = logging.getLogger("Beagle.orchestrator")

# Agent spawning limits - prevent runaway subprocess proliferation
DEFAULT_MAX_NESTED_AGENTS = 3

SYSTEM_DIRECTIVE_TEMPLATE = """\
<system_directives>
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
</system_directives>

<work_protocol>
MANDATORY: All non-trivial work MUST follow this structured plan protocol.

1. PLAN FIRST: Before writing ANY code, create a plan file listing every section \
of work as a checklist. Each section must be completable in ≤150 lines of changes.
   - Write the plan to .beagle/current_phase.md (or a task-specific file)
   - Each section: one logical unit, one commit

2. SECTION-BY-SECTION EXECUTION:
   - Work on ONE section at a time
   - git commit after EACH section with a descriptive message
   - Update the plan file marking completed sections with [x]
   - Do NOT batch multiple sections into one commit

3. CONTEXT DISCIPLINE:
   - Maximum 150 lines per Edit/Write call
   - Read only the parts of files you need (use offset/limit)
   - Do not hold more than 3 files in working memory at once
   - When reading a large file, note what you need in .beagle/progress.md \
rather than keeping raw content in context

4. EXTERNAL MEMORY:
   - After every commit, update .beagle/progress.md with:
     DONE: [what you just finished]
     NEXT: [the next section]
     FILES_REMAINING: [what's left to modify]
   - If compacted, your FIRST action is: read .beagle/progress.md

5. VERIFICATION:
   - Run lint after every section (ruff check on modified files)
   - Run relevant tests after every 2-3 sections
   - Full test suite at the end
</work_protocol>

<output_protocol>
Before any action, output your reasoning inside <thinking> tags.
Then output your tool calls or code inside <action> tags.
</output_protocol>
"""


def _load_model_fallbacks() -> dict[str, list[str]]:
    """Load model fallback chains from config.toml.

    v13.20.1: SSOT is config.toml [models.fallback_chains]. Returns the
    full chain table (keyed by primary model). If the TOML is missing the
    key (e.g. an older deployment pre-v13.20.1), the loader returns the
    schema default from GooseConfig.fallback_chains, which is `{}` — in
    that case this function returns an empty dict and the caller
    (`MODEL_FALLBACKS`) treats it as "use per-model hardcoded default".
    We deliberately do NOT keep a Python-side hardcoded chain here: the
    whole point of R2.2 is to make the chain structurally single-source.
    """
    try:
        from beagle.config.config import get_config

        chains = get_config().goose.fallback_chains
        if chains:
            return {k: list(v) for k, v in chains.items()}
    except (FileNotFoundError, TypeError, KeyError, OSError, AttributeError) as exc:
        logger.warning(
            "Cannot read [goose].fallback_chains from configuration (%s); returning an "
            "empty chain map, so no provider fallback will be attempted.",
            exc,
        )
    return {}


# Model fallback chain for resilience
MODEL_FALLBACKS = _load_model_fallbacks()

# Detect skill library / code mode availability for enhanced routing/execution.
# This is a module-availability probe, not a use of the imported names, so
# check the modules resolve rather than importing symbols we do not reference.
try:
    _code_mode_ok = _importlib_util.find_spec("beagle.code_mode") is not None
    _skill_lib_ok = _importlib_util.find_spec("beagle.core.skill_library") is not None
    ENHANCED_MODES = _code_mode_ok and _skill_lib_ok
except (ImportError, ValueError) as _e:
    logger.debug(f"Skill library/code mode not available: {_e}")
    ENHANCED_MODES = False
