"""Shared injection pattern definitions.

Both constants.py (input validation) and vigil.py (output validation)
overlap ~60% on injection patterns. This module defines the common base
with each consumer adding domain-specific extras on top.

This is an internal module (underscore prefix) — not exported from __init__.
"""

from __future__ import annotations

# ── Base patterns shared by input & output validation ─────────────────────────
# These detect prompt override attempts and system prompt injection markers
# that are dangerous regardless of whether they appear in user input or model output.

BASE_INJECTION_PATTERNS: tuple[str, ...] = (
    # Prompt override attempts
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
    r"disregard\s+(all\s+)?(previous|prior|above)",
    r"override\s+(all\s+)?(previous|prior|system)\s+(instructions?|prompts?)",
    # System prompt injection markers
    r"<system>",
    r"</system>",
    r"\[SYSTEM\]",
    r"\[INST\]",
    r"<<SYS>>",
    # Command substitution
    r"`[^`]*\$\(`",
    # Chained shell commands
    r";\s*(rm|wget|curl|chmod|chown|dd|mkfs|shutdown|reboot)\b",
    r"\|\s*(bash|sh|zsh|python|perl|ruby|nc|ncat)\b",
)

# v13.20.2 (R2.3): INPUT_ONLY_PATTERNS and OUTPUT_ONLY_PATTERNS were
# absorbed into the consumer modules per the R2.3 doctrine ("consolidate
# pattern lists into the consumer module that uses them; delete the
# intermediate"). INPUT_ONLY_PATTERNS now lives in validation.py;
# OUTPUT_ONLY_PATTERNS now lives in vigil.py (renamed from the prior
# `_OUTPUT_INJECTION_PATTERN_STRS` to the canonical name). This file
# remains the single source for BASE_INJECTION_PATTERNS (consumed by
# constants.py:INJECTION_PATTERNS).
