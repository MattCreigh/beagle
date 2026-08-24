"""Input validation functions for Goose Agentic Workflow.

Provides validate_query(), validate_agent_type(), validate_goose_binary(),
validate_prompt(), validate_file_path(), sanitize_container_name(),
and related validation helpers.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import threading
import unicodedata as _ud
from typing import Any

from beagle.config.config import get_config

# SP-7: validate_goose_binary lives in security/binary_validator.py (a leaf
# module with no intra-package imports) to break the security.validation <->
# security.firewall cycle. Re-exported here for backward compatibility.
from .constants import (
    _INJECTION_REGEX,
    MAX_PROMPT_LENGTH,
)
from .sanitization import RegexTimeoutError, regex_search_safe

# Module-level logger
logger = logging.getLogger("Beagle.beagle.security")


# ── Security context ───────────────────────────────────────────────────────────


class SecurityContext:
    """Security context for tracking validation state.

    v13.20.6 (R4.1): thread-safe. The prior implementation had three
    mutable attributes (`validation_errors`, `scrubbed_count`,
    `blocked_operations`) that were mutated by callers in the
    validation/feedback loop, in the firewall subprocess, and in
    VIGIL — all of which can run on separate threads (the firewall
    uses `asyncio.to_thread` per R3.3; feedback emits via the event
    bus which spins up publisher threads). The R2 audit flagged this
    as a process-global-without-thread-safety hazard (audit C7).
    Fix: a single `threading.Lock` guards all three mutations. The
    lock is created in __init__ so a fresh SecurityContext is fully
    self-contained; the global `_security_context` is reset via
    `reset_security_context()` (which holds no lock — it just
    rebinds the module-level name; callers reading `_security_context`
    either hold the lock already or accept best-effort visibility).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.validation_errors: list[str] = []
        self.scrubbed_count: int = 0
        self.blocked_operations: list[str] = []

    def log_error(self, error: str) -> None:
        """Log a validation error (thread-safe)."""
        with self._lock:
            self.validation_errors.append(error)

    def log_scrub(self, pattern: str) -> None:
        """Log a scrubbing operation (thread-safe)."""
        with self._lock:
            self.scrubbed_count += 1

    def log_blocked(self, operation: str) -> None:
        """Log a blocked operation (thread-safe)."""
        with self._lock:
            self.blocked_operations.append(operation)

    def get_summary(self) -> dict[str, Any]:
        """Get summary of security events (thread-safe snapshot)."""
        with self._lock:
            return {
                "validation_errors": len(self.validation_errors),
                "secrets_scrubbed": self.scrubbed_count,
                "operations_blocked": len(self.blocked_operations),
                "errors": self.validation_errors[:5],  # First 5
                "blocked": self.blocked_operations[:5],
            }


# Module-level security context
_security_context = SecurityContext()


def get_security_context() -> SecurityContext:
    """Get the global security context."""
    return _security_context


def reset_security_context() -> None:
    """Reset the global security context."""
    global _security_context
    _security_context = SecurityContext()


# ── Cached pattern check ───────────────────────────────────────────────────────


# Maximum length for cached inputs. Inputs longer than this are checked
# uncached to prevent cache-busting DoS (S04 remediation). Injection
# payloads are typically short; legitimate queries exceeding this limit
# are still validated — just not cached.
_CACHED_INPUT_MAX_LENGTH = 5000

# v13.20.2 (R2.3): Patterns specific to user-input validation. These detect
# input-side prompt-injection vectors (e.g. "forget everything", "you are
# now a different X") that don't apply to model outputs. Absorbed from
# beagle/security/_shared_patterns.py per the R2.3 doctrine (consolidate
# pattern lists into the consumer module that uses them; delete the
# intermediate). Currently NOT wired into _cached_pattern_check_impl —
# integration into the validation pipeline is deferred to a follow-up
# audit pass; this constant is kept as a documented SSOT site for the
# next integration.
INPUT_ONLY_PATTERNS: tuple[str, ...] = (
    r"forget\s+(everything|all)",
    r"new\s+instructions?\s*:",
    r"you\s+are\s+now\s+a\s+different",
    r"pretend\s+you\s+are",
    r"act\s+as\s+if\s+you\s+are",
)

# v13.21 (F2 remediation): Compile input-only patterns for use alongside
# the general _INJECTION_REGEX. Previously, INPUT_ONLY_PATTERNS was defined
# but not wired into the validation pipeline — queries like "new instructions:"
# and "act as if you are" could pass the cached pattern check undetected.
_INPUT_ONLY_REGEX = re.compile("|".join(INPUT_ONLY_PATTERNS), re.IGNORECASE)


@functools.lru_cache(maxsize=1024)
def _cached_pattern_check_impl(normalized_text: str) -> bool:
    """Internal cached check — only receives pre-normalized text."""
    return bool(
        _INJECTION_REGEX.search(normalized_text) or _INPUT_ONLY_REGEX.search(normalized_text)
    )


def validate_regex_pattern(pattern: str) -> tuple[bool, str]:
    """Validate a regex pattern for syntax errors.

    v1.2.0 (RG-5, BGL-006): this function is no longer used by the code_search
    MCP tool. The tool validates patterns with the engine that executes them
    (ripgrep's Rust regex crate) via the subprocess return code, because the
    Python `re` dialect and the Rust regex dialect disagree in both directions.
    This function remains exported for callers that need a Python-`re` syntax
    check only.

    Note: the ReDoS claim was removed. The prior implementation ran the pattern
    against the empty string, which cannot trigger catastrophic backtracking
    (that needs a long subject string), so the ReDoS detection was mostly
    inactive. A genuine ReDoS guard must test against a long subject string.

    Returns:
        (True, "") if the pattern compiles under Python `re`.
        (False, error_message) if the pattern is invalid or times out.

    """
    if not isinstance(pattern, str):
        return False, "Pattern must be a string"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return False, f"Invalid regex pattern: {exc}"
    try:
        regex_search_safe(compiled, "")
    except RegexTimeoutError as exc:
        return False, f"Regex pattern timed out (possible ReDoS): {exc}"
    except re.error as exc:
        return False, f"Invalid regex pattern: {exc}"
    return True, ""


# ── Cached pattern check ───────────────────────────────────────────────────────


def _cached_pattern_check(text: str) -> bool:
    """Cached pattern check for repeated queries.

    Normalizes input to prevent cache-busting via whitespace/case/zero-width
    variations (OWASP A10 — DoS mitigation). Delegates to an internal
    @lru_cache'd function that only receives pre-normalized text.

    S04 remediation: inputs exceeding _CACHED_INPUT_MAX_LENGTH are checked
    uncached to prevent unbounded LRU key expansion.
    """
    # Step 0: NFKC normalisation (fullwidth/homoglyph → ASCII) BEFORE case-fold
    normalized = _ud.normalize("NFKC", text)
    # Step 1: Case-fold
    normalized = normalized.lower()
    # Step 2: Remove zero-width and control characters used for evasion
    normalized = "".join(
        c for c in normalized if _ud.category(c) not in ("Cf", "Cc") and c != "\u200b"
    )
    # Step 3: Collapse whitespace
    normalized = " ".join(normalized.split())
    # Step 4 (S04): Bypass cache for oversized inputs to prevent DoS
    if len(normalized) > _CACHED_INPUT_MAX_LENGTH:
        return bool(_INJECTION_REGEX.search(normalized) or _INPUT_ONLY_REGEX.search(normalized))
    return _cached_pattern_check_impl(normalized_text=normalized)


# ── Regex timeout helpers (re-exported from sanitization for internal use) ──────

# ── Validation functions ────────────────────────────────────────────────────────


def get_agent_whitelist() -> set[str]:
    """Get the set of allowed agent types.

    Returns:
        Set of valid agent type names

    """
    from ..utils.env_manager import get_workspace_root

    workspace = get_workspace_root()
    recipes_dir = workspace / "recipes"

    if not recipes_dir.exists():
        return set()

    # Build whitelist from recipe files (agent names without .xml extension)
    return {p.stem.lower() for p in recipes_dir.glob("*.xml")}


def validate_agent_type(agent_type: str) -> tuple[bool, str]:
    """Validate that an agent type is in the whitelist.

    Args:
        agent_type: The agent type to validate

    Returns:
        Tuple of (is_valid, error_message)

    """
    if not agent_type:
        return False, "Agent type cannot be empty"

    if not isinstance(agent_type, str):
        return False, f"Agent type must be string, got {type(agent_type)}"

    # Enforce max length to prevent resource exhaustion
    if len(agent_type) > 100:
        return False, f"Agent type too long: {len(agent_type)} chars (max 100)"

    # Sanitize input - reject if contains control characters
    if any(ord(c) < 32 and c not in "\t\n" for c in agent_type):
        return False, "Agent type contains control characters"

    # Strip and lowercase
    agent_type = agent_type.strip().lower()

    # Check for path traversal and other dangerous patterns
    if ".." in agent_type or "/" in agent_type or "\\" in agent_type:
        return False, f"Invalid agent type (path characters): {agent_type}"

    # Check for SQL/NoSQL injection patterns in agent type
    injection_chars = ["'", '"', ";", "--", "/*", "*/", "xp_", "sp_"]
    for pattern in injection_chars:
        if pattern in agent_type:
            return False, f"Invalid agent type (suspicious pattern): {agent_type}"

    whitelist = get_agent_whitelist()

    if not whitelist:
        # If whitelist is empty (no recipes found), allow any alphanumeric
        if re.match(r"^[a-z0-9][a-z0-9_\-]*$", agent_type):
            return True, ""
        return False, f"Invalid agent type format: {agent_type}"

    if agent_type not in whitelist:
        return (
            False,
            f"Unknown agent type: {agent_type}. Valid types: {sorted(whitelist)[:10]}...",
        )

    return True, ""


async def validate_query_async(query: str, mock_firewall: bool = False) -> tuple[bool, str]:
    """Async-safe variant of validate_query for use in a running event loop.

    The sync `validate_query` explicitly raises RuntimeError when called from
    an async context to prevent `asyncio.run` from being invoked inside a
    running loop. This variant applies the same non-LLM checks and then awaits
    the LLM-based semantic firewall directly.

    Active callers (do not remove without migration):
      - core/autonomous_orchestrator.py
      - infrastructure/tools/_impl.py:844
    """
    is_valid, error = _validate_query_core(query)
    if not is_valid:
        return is_valid, error

    if not mock_firewall:
        from .firewall import semantic_firewall

        try:
            is_safe = await semantic_firewall(query)
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional — fail closed
            is_safe = False

        if not is_safe:
            return False, "Query blocked by semantic firewall (failed security check)"

    return True, ""


def _validate_query_core(query: str) -> tuple[bool, str]:
    """Shared non-LLM validation logic for sync and async variants.

    Performs length, type, injection-pattern, and backtick checks.
    """
    if not query:
        return False, "Query cannot be empty"

    if not isinstance(query, str):
        return False, f"Query must be string, got {type(query)}"

    # Length check
    max_query_length = get_config().security.max_query_length
    if len(query) > max_query_length:
        return False, (f"Query too long: {len(query)} chars (max {max_query_length} character cap)")

    # Check for injection patterns (with timeout-safe regex)
    # Use cached check for repeated queries (common in iterative workflows)
    try:
        # Try cache first for exact query matches
        has_injection = _cached_pattern_check(query)
        if has_injection:
            # Use normalized text for regex too — NFKC + Cf/Cc strip + collapsed
            _normalized_raw = _ud.normalize("NFKC", query)
            _normalized = " ".join(
                "".join(
                    c
                    for c in _normalized_raw.lower()
                    if _ud.category(c) not in ("Cf", "Cc") and c != "\u200b"
                ).split()
            )
            match = regex_search_safe(_INJECTION_REGEX, _normalized, timeout_secs=2)
            if match:
                return False, (
                    f"Potential prompt injection detected by semantic firewall: '{match.group()}'"
                )
    except RegexTimeoutError:
        return False, "Query regex evaluation timed out (possible ReDoS)"

    # Check for shell command injection in backticks
    # Use timeout to prevent ReDoS on backtick matching
    if query.count("`") > 0:
        backtick_content = re.findall(r"`([^`]+)`", query)
        for content in backtick_content:
            # Allow code examples, but flag dangerous commands
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
            for cmd in dangerous_cmds:
                if cmd in content:
                    return False, f"Dangerous command in backticks: {cmd}"

    # Additional check for nested backticks which could indicate evasion attempts
    if query.count("`") >= 6 and re.search(r"`.*[\\$|&;<>].*`", query):
        return False, "Potential command obfuscation detected in backticks"

    return True, ""


def validate_query(query: str, mock_firewall: bool = False) -> tuple[bool, str]:
    """Validate a user query for safety.

    Checks for:
    - Length limits
    - Prompt injection patterns
    - Dangerous commands
    - LLM-based semantic firewall

    Args:
        query: The query to validate

    Returns:
        Tuple of (is_valid, error_message)

    Raises:
        RuntimeError: If called from a running event loop. Use
            `validate_query_async` in async contexts.

    """
    is_valid, error = _validate_query_core(query)
    if not is_valid:
        return is_valid, error

    if not mock_firewall:
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # v13.19.4: Raise RuntimeError so the caller knows to use
                # the async variant. Previously we silently failed closed
                # (returned False) which was correct security-wise but
                # left the caller confused about why their query was
                # blocked.
                raise RuntimeError(
                    "validate_query() called from a running event loop — "
                    "use validate_query_async() instead."
                )
            else:
                from .firewall import semantic_firewall

                is_safe = asyncio.run(semantic_firewall(query))
        except RuntimeError:
            # Re-raise the explicit guidance so the caller can recover.
            raise
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
            is_safe = False  # Fail closed on any error

        if not is_safe:
            return False, "Query blocked by semantic firewall (failed security check)"

    return True, ""


def validate_prompt(prompt: str) -> tuple[bool, str]:
    """Validate a constructed prompt before sending to agent.

    Args:
        prompt: The prompt to validate

    Returns:
        Tuple of (is_valid, error_message)

    """
    if not prompt:
        return False, "Prompt cannot be empty"

    if len(prompt) > MAX_PROMPT_LENGTH:
        return False, f"Prompt too long: {len(prompt)} chars (max {MAX_PROMPT_LENGTH})"

    # Check for common injection attempts
    if "<system_directive>" in prompt.lower():
        # Count occurrences - one is expected from POML wrapper
        count = prompt.lower().count("<system_directive>")
        if count > 1:
            return False, "Multiple system_directive tags detected (injection attempt)"

    return True, ""


def validate_file_path(
    path: str, allow_absolute: bool = False, base_dir: str | None = None
) -> tuple[bool, str]:
    """Validate a file path for safety.

    Args:
        path: The path to validate
        allow_absolute: Whether to allow absolute paths
        base_dir: If provided, validate path resolves within this directory

    Returns:
        Tuple of (is_valid, error_message)

    """
    if not path:
        return False, "Path cannot be empty"

    # Check for null bytes (path injection)
    if "\x00" in path:
        return False, "Path contains null bytes"

    # Check for path traversal
    if ".." in path:
        return False, "Path traversal (..) not allowed"

    # Check for absolute path if not allowed
    if not allow_absolute and path.startswith("/"):
        return False, "Absolute paths not allowed"

    # Normalize and check for symlink escapes
    try:
        # Expand user home and resolve symlinks
        expanded = os.path.expanduser(path)
        normalized = os.path.normpath(expanded)

        if base_dir:
            base_dir = os.path.normpath(os.path.expanduser(base_dir))
            # Ensure the path is within base_dir (prevents symlink escapes).
            # B-8 (audit v13.22.0): use Path.relative_to() per the project
            # doctrine (io.py:29) rather than str.startswith. Path.relative_to
            # raises ValueError if the path is not under the base, which
            # is exactly the containment check we want.
            from pathlib import Path as _Path  # local import to avoid cycles

            resolved = _Path(os.path.realpath(normalized))
            base_resolved = _Path(os.path.realpath(base_dir))
            try:
                resolved.relative_to(base_resolved)
            except ValueError:
                # resolved is not under base_resolved; the only exception
                # is when they are equal (which we allow, meaning the
                # path IS the base).
                if resolved != base_resolved:
                    return False, f"Path escapes base directory: {path}"
    except (OSError, ValueError):
        return False, f"Invalid path: {path}"

    # Check for dangerous patterns (S08 remediation: check both original and
    # resolved paths to prevent bypass via path normalisation tricks like
    # `./etc/./passwd` which normalises to `/etc/passwd`).
    dangerous = [
        "/etc/passwd",
        "/etc/shadow",
        "/root/",
        "~/.ssh/",
        "~/.gnupg/",
        ".env",
        "sopsSecrets",
    ]
    # Build set of paths to check: original + normalised + resolved (if available).
    # str() every member so the dangerous-pattern membership test
    # ('d in check_path') never sees a PosixPath — `'etc' in PosixPath(...)`
    # raises TypeError, which propagated out of the validator and
    # crashed the security gate on inputs where base_dir was set AND
    # the path needed realpath() resolution (audit v13.22.4, S1).
    paths_to_check: set[str] = {str(path), str(normalized)}
    if base_dir:
        paths_to_check.add(str(resolved))
    for check_path in paths_to_check:
        for d in dangerous:
            if d in check_path:
                return False, f"Access to sensitive path not allowed: {d}"

    return True, ""


def sanitize_container_name(name: str) -> str | None:
    """Sanitize and validate a container name.

    Args:
        name: The container name to sanitize

    Returns:
        Sanitized name or None if invalid

    """
    if not name:
        return None

    # Docker container name regex
    pattern = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")

    name = name.strip()

    if pattern.match(name):
        return name

    return None


# ── URL scheme validation ─────────────────────────────────────────────────────

# <invariant>
# Every URL handed to urllib.request.urlopen passes through
# validate_http_url first. urlopen is not an HTTP client — it dispatches on
# the scheme, so a url that reaches it as "file:///etc/shadow" or
# "ftp://host/x" is a local-file read or an outbound FTP fetch wearing the
# shape of an HTTP request. The scheme must be checked before the call, not
# inferred from the fact that the surrounding code is about HTTP.
# </invariant>
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def validate_http_url(url: str) -> str:
    """Check that a URL is http or https before it reaches a URL opener.

    Args:
        url: The URL to check.

    Returns:
        The URL unchanged, so the call can wrap an existing argument in place.

    Raises:
        ValueError: The URL has no host, or its scheme is not http or https.

    """
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"Refusing to open URL with scheme {parsed.scheme!r}: "
            f"only {sorted(_ALLOWED_URL_SCHEMES)} are permitted."
        )
    if not parsed.hostname:
        raise ValueError(f"Refusing to open URL with no host: {url!r}")
    return url


# ── Cypher identifier validation ───────────────────────────────────────────
# The doctrine forbids interpolating unvalidated values into SQL/Cypher.
# ruff S608 only understands SQL, not Cypher, so these four sites were
# invisible to the linter. Every identifier interpolated into a Kùzu query
# must pass through this allowlist+pattern gate first.
#
# <invariant>
#   No value may be interpolated into a Cypher query unless it matches
#   ^[A-Za-z_][A-Za-z0-9_]*$ and is not a reserved Cypher keyword.
# </invariant>

_CYPHER_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CYPHER_FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "DELETE",
        "REMOVE",
        "DETACH",
        "CREATE",
        "MERGE",
        "MATCH",
        "LOAD",
        "EXPORT",
        "COPY",
        "SLEEP",
        "RETURN",
        "WITH",
        "UNWIND",
        "WHERE",
        "SET",
    }
)


def validate_cypher_identifier(identifier: str) -> str:
    """Validate a Cypher identifier before it is interpolated into a query.

    Args:
        identifier: The string to interpolate as an identifier (a table name,
            relationship type, or label).

    Returns:
        The validated identifier unchanged.

    Raises:
        ValueError: If the identifier is empty, contains characters outside
            ``[A-Za-z0-9_]``, fails to start with a letter or underscore, or
            uppercases to a reserved Cypher keyword.

    <verification-checklist>
      1. "CALLS" passes.
      2. "" raises ValueError.
      3. "DROP" raises ValueError.
      4. "; DROP MATCH ()" raises ValueError (semicolon rejected by pattern).
      5. "ASTNode_tenant_acme" passes.
      6. "INHERITS_FROM" passes.
    </verification-checklist>

    """
    if not identifier:
        msg = "Cypher identifier is empty"
        raise ValueError(msg)
    if not _CYPHER_ID_PATTERN.match(identifier):
        msg = (
            f"Cypher identifier {identifier!r} contains forbidden characters; "
            f"expected ^[A-Za-z_][A-Za-z0-9_]*$"
        )
        raise ValueError(msg)
    if identifier.upper() in _CYPHER_FORBIDDEN_KEYWORDS:
        msg = f"Cypher identifier {identifier!r} is a reserved Cypher keyword"
        raise ValueError(msg)
    return identifier
