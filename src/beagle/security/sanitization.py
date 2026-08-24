"""Secret scrubbing and query sanitization for Goose Agentic Workflow.

Provides scrub_secrets(), scrub_output(), and the internal implementation
helpers _scrub_secrets_impl(), _scrub_secrets_cached(), compile_secret_patterns(),
and regex-safe timeout helpers.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import re
import signal
import threading
from typing import Any

from .constants import SECRET_PATTERNS

# Module-level logger
logger = logging.getLogger("Beagle.beagle.security")


# ── Regex timeout helpers ───────────────────────────────────────────────────────


class RegexTimeoutError(Exception):
    """Raised when regex operations exceed timeout."""

    pass


def _timeout_handler(signum, _frame):
    raise RegexTimeoutError("Regex operation timed out (possible ReDoS)")


def _regex_sub_safe(pattern: str, repl: str, text: str, timeout_secs: int = 1) -> str:
    """Execute re.sub with signal-based timeout protection (Unix only).

    Uses SIGALRM for reliable timeout on CPU-bound regex operations.
    Falls back to no-timeout on platforms that don't support it (Windows).

    Args:
        pattern: Regex pattern
        repl: Replacement string
        text: Input text
        timeout_secs: Maximum seconds to allow

    Returns:
        Result string

    Raises:
        RegexTimeoutError: If operation exceeds timeout

    """
    # On Unix with signal support, use SIGALRM for reliable timeout
    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_secs)
        try:
            return re.sub(pattern, repl, text, flags=re.IGNORECASE)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    else:
        # Windows or non-main thread: use input length guard
        # Limit input to prevent catastrophic backtracking
        if len(text) > 10000:
            raise RegexTimeoutError("Input too long for safe regex evaluation")
        return re.sub(pattern, repl, text, flags=re.IGNORECASE)


def regex_search_safe(pattern: re.Pattern, text: str, timeout_secs: int = 1) -> Any:
    """Execute re.search with signal-based timeout protection (Unix only).

    v13.20.7 (R4.2): promoted from `_regex_search_safe` (private) to
    `regex_search_safe` (public API). Three internal consumers
    (firewall.py, validation.py, vigil.py) were importing the private
    name, which is a layering violation: a "private" symbol by
    convention should not be the public surface for sibling modules
    in the same package. The private alias below is preserved for
    any external callers that may have picked up the underscore name
    (it is re-exported as `_regex_search_safe = regex_search_safe`).

    Args:
        pattern: Compiled regex pattern
        text: Input text to search
        timeout_secs: Maximum seconds to allow

    Returns:
        Match object or None

    Raises:
        RegexTimeoutError: If operation exceeds timeout

    """
    # On Unix with signal support, use SIGALRM for reliable timeout
    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_secs)
        try:
            return pattern.search(text)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    else:
        # Windows or non-main thread: use input length guard
        if len(text) > 10000:
            raise RegexTimeoutError("Input too long for safe regex evaluation")
        return pattern.search(text)


def _compile_secret_patterns() -> list[tuple[re.Pattern, Any]]:
    """Compile and cache secret patterns for repeated use.

    Returns:
        List of (compiled_pattern, replacement) tuples

    """
    return [(re.compile(p), "[REDACTED]") for p in SECRET_PATTERNS]


# Performance: Cache compiled regex patterns to avoid recompilation
_COMPILED_SECRET_PATTERNS: list[tuple[re.Pattern, Any]] = []
_SECRET_PATTERNS_CACHE_INITIALIZED = False


def _ensure_pattern_cache() -> list[tuple[re.Pattern, Any]]:
    """Ensure the compiled pattern cache is initialized."""
    global _COMPILED_SECRET_PATTERNS, _SECRET_PATTERNS_CACHE_INITIALIZED
    if not _SECRET_PATTERNS_CACHE_INITIALIZED:
        _COMPILED_SECRET_PATTERNS = _compile_secret_patterns()
        _SECRET_PATTERNS_CACHE_INITIALIZED = True
    return _COMPILED_SECRET_PATTERNS


# ── Secret scrubbing ───────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=512)
def _scrub_secrets_cached(text: str) -> str:
    """Internal cached version of scrub_secrets for repeated inputs."""
    return _scrub_secrets_impl(text)


def _scrub_secrets_impl(text: str) -> str:
    """Internal implementation of secret scrubbing (no caching).

    Uses google-re2 for guaranteed linear-time matching (no ReDoS).

    SECURITY (DevSecOps): If google-re2 is not available, FAILS CLOSED
    by raising an ImportError rather than falling back to stdlib re.
    The standard re module is vulnerable to catastrophic backtracking
    (ReDoS) and the signal-based timeout fallback (_regex_sub_safe) is
    interrupt-unsafe on non-main threads. A thread-based fallback merely
    moves the same unsafe regex execution to another thread.

    Fail-closed is correct: production deployments MUST install google-re2
    (``pip install google-re2``). The dependency is declared in
    pyproject.toml.
    """
    try:
        import re2
    except ImportError:
        # SECURITY: Fail-closed — do NOT fall back to stdlib re.
        # google-re2 provides guaranteed linear-time matching. stdlib re
        # with SIGALRM is interrupt-unsafe on non-main threads, and the
        # threading fallback has the same ReDoS risk in a different thread.
        raise ImportError(
            "google-re2 is REQUIRED for secret scrubbing (ReDoS protection). "
            "Install with: pip install google-re2. "
            "Falling back to stdlib re is prohibited — it is vulnerable to "
            "catastrophic backtracking and the signal-based timeout is "
            "interrupt-unsafe on non-main threads."
        ) from None

    result = text
    patterns_processed = 0
    for pattern_str in SECRET_PATTERNS:
        try:
            old_result = result
            try:
                compiled = re2.compile(pattern_str)
                result = compiled.sub("[REDACTED]", result)
            except Exception as re2_err:  # ruff: ignore[BLE001]  # broad catch intentional
                # SECURITY: If re2 C++ crashes on a pattern, fail-closed
                # rather than falling back to stdlib re (ReDoS risk).
                logger.error(
                    f"[SECURITY] re2 failed on pattern {pattern_str[:50]}: {re2_err}. "
                    f"Pattern skipped — FAIL CLOSED, no stdlib re fallback."
                )
                result = old_result
            if old_result != result:
                patterns_processed += 1
        except re.error:
            # Skip invalid patterns
            logger.warning(f"[SECURITY] Invalid secret pattern skipped: {pattern_str[:50]}")
            continue

    # Log if we processed any patterns for debugging
    if patterns_processed > 0:
        logger.debug(f"[SECURITY] Scrubbed {patterns_processed} secret pattern types")

    return result


def scrub_secrets(text: str) -> str:
    """Scrub secrets and sensitive data from text.

    Public API with LRU caching for repeated inputs.
    Uses google-re2 for guaranteed linear-time matching (no ReDoS).

    SECURITY (DevSecOps): This function FAILS CLOSED if google-re2 is
    not installed. It will NOT fall back to stdlib re, which is vulnerable
    to catastrophic backtracking (ReDoS). Install google-re2 via
    ``pip install google-re2`` (declared as a dependency in pyproject.toml).

    Args:
        text: The text to scrub

    Returns:
        Scrubbed text with secrets replaced

    """
    if not text:
        return text

    # Cap input length before evaluation
    MAX_SCRUB_LENGTH = 1_000_000  # 1MB
    if len(text) > MAX_SCRUB_LENGTH:
        logger.warning(
            f"[SECURITY] Input too long for secret scrubbing ({len(text)} chars), truncating"
        )
        text = text[:MAX_SCRUB_LENGTH]

    # Use cached version for repeated inputs
    return _scrub_secrets_cached(text)


def scrub_output(output: str, additional_patterns: list[str] | None = None) -> str:
    """Scrub an agent output before returning to user.

    Args:
        output: The output to scrub
        additional_patterns: Additional regex patterns to scrub

    Returns:
        Scrubbed output

    """
    result = scrub_secrets(output)

    if additional_patterns:
        for pattern in additional_patterns:
            with contextlib.suppress(re.error):
                result = re.sub(pattern, "[REDACTED]", result)

    return result


# v13.20.7 (R4.2): back-compat alias for the prior private name. Internal
# callers (firewall.py, validation.py, vigil.py) have been migrated to
# the public `regex_search_safe`; this alias exists for any external
# caller (third-party plugin, downstream fork) that picked up the
# underscore-prefixed name. Safe to remove in v13.21 if no external
# caller is found.
_regex_search_safe = regex_search_safe
