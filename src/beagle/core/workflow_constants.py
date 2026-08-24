"""Workflow mode directives — extracted from core/nodes.py (v13.16.2).

Centralised so these large multi-line directive strings don't inflate
the nodes module and can be reused across the codebase.
"""

from __future__ import annotations

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

# ── Token budget constants ─────────────────────────────────────────────────────

# v13.16.2: Hard per-node token budget. Exceeding this triggers early termination
# with a warning and the partial output is returned. Prevents runaway agents from
# burning thousands of tokens without any circuit breaker triggering.
DEFAULT_NODE_TOKEN_BUDGET = 16000  # ~$0.15 at Ollama Cloud pricing
MAX_NODE_TOKEN_BUDGET = 32000  # Absolute ceiling — node terminates regardless

# ── Context Sovereignty constants ──────────────────────────────────────────────
# v13.16.3: Beagle establishes absolute sovereignty over context folding.
# When the orchestrator's accumulated context reaches this threshold, Beagle
# MUST pause the node, extract active state, run TurboQuant compaction, and
# rehydrate the subprocess with the condensed vector summary BEFORE spawning
# a new Goose subprocess. Goose's internal auto-compaction is treated as a
# dumb pipe — Beagle is the sole arbiter of memory.
HARD_SOVEREIGN_THRESHOLD = 0.80  # Beagle folds at 80% — BEFORE goose fires at 70%


def get_mode_directive(mode: str) -> str:
    """Return the system directive prefix for the given workflow mode."""
    if mode in ("audit", "research"):
        return _READ_ONLY_DIRECTIVE
    if mode == "develop":
        return _READ_WRITE_DIRECTIVE
    return _READ_ONLY_DIRECTIVE  # Default to safe mode
