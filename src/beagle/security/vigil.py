"""VIGIL — Verify-before-commit tool output validation.

Inspired by VIGIL [arxiv 2601.05755] and AEGIS [arxiv 2603.12621].
Validates tool outputs BEFORE they enter workflow state, preventing
tool stream injection attacks where malicious content in tool
results could hijack agent behavior.

v13.7.0: Adds output-side validation to complement the existing
input-side semantic firewall.

Usage:
    from beagle.security.vigil import validate_tool_output

    is_safe, sanitized = validate_tool_output("sql_query", raw_output)
    if not is_safe:
        logger.warning("Tool output blocked by VIGIL")
"""

from __future__ import annotations

import logging
import re

from .constants import INJECTION_PATTERNS
from .sanitization import regex_search_safe

logger = logging.getLogger("Beagle.security.vigil")

# ── Output-specific injection patterns ────────────────────────────────────────
# These patterns detect attempts to inject instructions via tool outputs.
# Distinct from input INJECTION_PATTERNS: these target output-side vectors.
#
# v13.20.2 (R2.3): Renamed from `_OUTPUT_INJECTION_PATTERN_STRS` to
# `OUTPUT_ONLY_PATTERNS` and absorbed from beagle/security/_shared_patterns.py
# per the R2.3 doctrine. The absorbed tuple is a strict subset of this
# list (we already had `<system>`, `</system>`, `[SYSTEM]`, `[INST]`,
# `<<SYS>>`, and the `disregard (all)? (previous|prior)` patterns as a
# superset); we keep the superset and document the source.
#
# Absorbed patterns (now in this list):
#   - r"(?i)new\s+instructions?:"          — line 43 below
#   - r"(?i)override\s+instructions?:"     — line 44 below
#   - r"(?i)you\s+must\s+now\s+follow"     — line 45 below
#   - r"(?i)assistant:\s*I will now"       — line 49 below
#   - r"(?i)human:\s*Please ignore"        — line 50 below
#   - r"&#x3C;system&#x3E;"                — line 53 below
#   - r"%3Csystem%3E;"                     — line 54 below
OUTPUT_ONLY_PATTERNS: list[str] = [
    # System prompt overrides embedded in tool results
    r"<system>",
    r"</system>",
    r"\[SYSTEM\]",
    r"\[INST\]",
    r"<<SYS>>",
    # Instruction override attempts
    r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
    r"(?i)new\s+instructions?:",
    r"(?i)override\s+instructions?:",
    r"(?i)you\s+must\s+now\s+follow",
    r"(?i)disregard\s+(all\s+)?(previous|prior)",
    # Hidden prompt injection in tool output
    r"(?i)assistant:\s*I will now",
    r"(?i)human:\s*Please ignore",
    # Encoded injection attempts
    r"&#x3C;system&#x3E;",  # HTML-encoded <system>
    r"%3Csystem%3E",  # URL-encoded <system>
]

# Pre-compile for performance and regex_search_safe compatibility
# v13.20.2 (R2.3): was `_OUTPUT_INJECTION_COMPILED` from
# `_OUTPUT_INJECTION_PATTERN_STRS`; both renamed to OUTPUT_ONLY_PATTERNS
# / _OUTPUT_INJECTION_COMPILED.
_OUTPUT_INJECTION_COMPILED = [re.compile(p) for p in OUTPUT_ONLY_PATTERNS]
_INPUT_INJECTION_COMPILED = [re.compile(p) for p in INJECTION_PATTERNS]

# ── Anomaly thresholds ────────────────────────────────────────────────────────

# Maximum output length before flagging (500KB — tool outputs should be bounded)
MAX_OUTPUT_LENGTH = 512_000

# Minimum entropy ratio for structured outputs (detect random/encrypted payloads)
MIN_PRINTABLE_RATIO = 0.85

# Maximum ratio of non-ASCII characters (detect obfuscation)
MAX_NON_ASCII_RATIO = 0.30

# v0.3.0: Per-tool threshold overrides (tools with legitimately different profiles)
TOOL_THRESHOLDS: dict[str, dict[str, float]] = {
    "web_scrape": {"min_printable_ratio": 0.60, "max_non_ascii_ratio": 0.60},
    "web_search": {"min_printable_ratio": 0.70, "max_non_ascii_ratio": 0.50},
    "sql_query": {"min_printable_ratio": 0.90, "max_non_ascii_ratio": 0.10},
}


def validate_tool_output(
    tool_name: str,
    output: str,
    max_length: int = MAX_OUTPUT_LENGTH,
) -> tuple[bool, str]:
    """Validate a tool output before committing it to workflow state.

    Three-pass validation:
    1. Size & structure checks (fast, no regex)
    2. Injection pattern scanning (reuses firewall patterns)
    3. Anomaly detection (entropy, encoding)

    Args:
        tool_name: Name of the tool that produced this output.
        output: Raw tool output string.
        max_length: Maximum allowed output length.

    Returns:
        Tuple of (is_safe: bool, sanitized_output: str).
        If unsafe, sanitized_output contains the reason.

    """
    if not isinstance(output, str):
        output = str(output)

    # ── Pass 1: Size & structure ──────────────────────────────────────────
    if len(output) > max_length:
        reason = f"VIGIL: Output from {tool_name} exceeds max length ({len(output)} > {max_length})"
        logger.warning(reason)
        # Truncate rather than block — oversized output is suspicious but not
        # necessarily malicious
        output = output[:max_length] + "\n... [VIGIL: truncated]"

    if not output.strip():
        return True, output  # Empty output is safe

    # ── Pass 2: Injection pattern scanning ────────────────────────────────
    for compiled in _OUTPUT_INJECTION_COMPILED:
        match = regex_search_safe(compiled, output, timeout_secs=1)
        if match:
            reason = (
                f"VIGIL: Injection pattern detected in {tool_name} output: "
                f"matched {compiled.pattern!r}"
            )
            logger.warning(reason)
            sanitized = compiled.sub("[VIGIL:REDACTED]", output)
            return False, sanitized

    # Also check the shared input-side injection patterns
    for compiled in _INPUT_INJECTION_COMPILED:
        match = regex_search_safe(compiled, output, timeout_secs=1)
        if match:
            reason = (
                f"VIGIL: Shared injection pattern in {tool_name} output: "
                f"matched {compiled.pattern!r}"
            )
            logger.warning(reason)
            sanitized = compiled.sub("[VIGIL:REDACTED]", output)
            return False, sanitized

    # ── Pass 3: Anomaly detection ─────────────────────────────────────────
    # v0.3.0: Look up per-tool thresholds, fallback to module defaults
    tool_cfg = TOOL_THRESHOLDS.get(tool_name, {})
    min_printable = tool_cfg.get("min_printable_ratio", MIN_PRINTABLE_RATIO)
    max_non_ascii = tool_cfg.get("max_non_ascii_ratio", MAX_NON_ASCII_RATIO)

    if len(output) > 100:  # Only check non-trivial outputs
        printable_count = sum(1 for c in output if c.isprintable() or c in "\n\r\t")
        printable_ratio = printable_count / len(output)

        if printable_ratio < min_printable:
            reason = (
                f"VIGIL: Low printable ratio in {tool_name} output: "
                f"{printable_ratio:.2%} (min {min_printable:.0%})"
            )
            logger.warning(reason)
            return False, f"[VIGIL: binary/encoded content blocked from {tool_name}]"

        non_ascii_count = sum(1 for c in output if ord(c) > 127)
        non_ascii_ratio = non_ascii_count / len(output)

        if non_ascii_ratio > max_non_ascii:
            reason = (
                f"VIGIL: High non-ASCII ratio in {tool_name} output: "
                f"{non_ascii_ratio:.2%} (max {max_non_ascii:.0%})"
            )
            logger.warning(reason)
            return False, f"[VIGIL: obfuscated content blocked from {tool_name}]"

    return True, output


__all__ = ["validate_tool_output"]
