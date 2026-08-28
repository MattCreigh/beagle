"""Bounded subprocess pool for Goose headless execution.

Pools a limited number of concurrent Goose subprocesses to prevent CPU
oversubscription on low-core machines (e.g. i7-6700T = 4 cores).

All goose headless executions in nodes.py and autonomous_orchestrator.py
MUST route through GoosePool to enforce the concurrency cap.

Model Fallback: If the primary model fails, automatically tries fallback models
in order: glm-5.1:cloud -> deepseek-v3.2 -> qwen3.5:397b -> gemma3:27b

H-MEM v13: KV Cache Efficiency Optimization
- Large outputs (>10,000 tokens) are truncated with header/footer pattern
- Full outputs stored in VFS archive for retrieval if needed
- Reduces context window pressure while preserving key information

v12.3 Semantic Error Translation:
- SecurityAccessViolation and SandboxViolation exceptions are caught before
  they crash the subprocess loop.  They are translated into polite, structured
  guidance prompts wrapped in <final_answer> tags so that fallback LLM models
  (e.g. gemma3:27b) can understand and self-correct, instead of choking on a
  raw Python traceback and omitting the mandatory <final_answer> XML tags.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import signal
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import TypeVar

from beagle.config.config import get_config
from beagle.core.orchestrator_types import GooseExecutionError
from beagle.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    get_circuit_breaker,
)
from beagle.utils.env_manager import _build_safe_env
from beagle.utils.subprocess.output_handlers import (
    _terminate_process_group,
)
from beagle.utils.subprocess.pool_config import _get_provider_chain

logger = logging.getLogger("Beagle.utils.subprocess_pool")

# ── Argv Size Ceiling (Phase 4.3) ──────────────────────────────────────────────
# ARG_MAX on Linux is typically 2 MB, but per-arg MAX_ARG_STRLEN is 128 KiB.
# We use a conservative 128 KiB ceiling for the total argv + envp byte count
# to stay safely under both limits.  When exceeded, large payloads are written
# to temp files and passed via the `@file` idiom understood by the goose binary.

_ARGV_CEILING_BYTES: int = 128 * 1024  # 128 KiB


def _compute_argv_size(cmd: list[str], env: dict[str, str] | None = None) -> int:
    """Compute total byte size of argv + envp (mimics kernel accounting).

    Each arg accounts for (len(arg) + 1) to include the null terminator.
    Each env entry accounts for (len(k) + len(v) + 2) for 'k=v\\0'.

    Args:
        cmd: Command list (e.g. ['/bin/echo', 'hello']).
        env: Environment dict or None.

    Returns:
        Total byte count of argv + envp.

    """
    total = sum(len(arg.encode("utf-8")) + 1 for arg in cmd)
    if env:
        total += sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) + 2 for k, v in env.items())
    return total


def _argv_overflow_safe(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    ceiling: int = _ARGV_CEILING_BYTES,
) -> tuple[list[str], str | None]:
    """Inspect cmd for argv overflow and offload large args to a temp file.

    When the total argv + envp byte count exceeds *ceiling*, the function
    identifies the single largest positional argument (ignoring flags and the
    binary path at index 0), writes it to a temp file, and replaces it with a
    ``@<filepath>`` reference understood by the goose binary.  This mirrors the
    technique used in firewall.py (v13.16).

    Args:
        cmd: Command list (binary + flags + positional args).
        env: Environment dict (optional, used for size accounting).
        ceiling: Maximum allowed argv + envp bytes (default 128 KiB).

    Returns:
        Tuple of (potentially_replaced_cmd, temp_file_path_or_None).
        The caller MUST unlink the temp file path when done.

    """
    total = _compute_argv_size(cmd, env)
    if total <= ceiling:
        return cmd, None

    # Find the largest positional argument (skip index 0 = binary, skip flags
    # starting with '-' or '--', also skip `@file` references already in place)
    largest_idx = 0
    largest_size = 0
    for i in range(1, len(cmd)):
        if cmd[i].startswith("-") or cmd[i].startswith("@"):
            continue
        sz = len(cmd[i].encode("utf-8"))
        if sz > largest_size:
            largest_size = sz
            largest_idx = i

    if largest_idx == 0:
        # All args are flags — nothing to offload; return as-is
        logger.warning(
            "argv overflow (%d bytes) but no offloadable positional arg found",
            total,
        )
        return cmd, None

    original_arg = cmd[largest_idx]
    now_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    task_id = str(uuid.uuid4())
    fd, tmp_path = tempfile.mkstemp(
        suffix=".txt",
        prefix=f"beagle_argv_{now_ts}_{task_id[:8]}_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(original_arg)
    except (OSError, UnicodeError) as exc:
        os.unlink(tmp_path)
        raise RuntimeError(f"Failed to write argv temp file: {exc}") from exc

    new_cmd = list(cmd)
    new_cmd[largest_idx] = f"@{tmp_path}"

    new_total = _compute_argv_size(new_cmd, env)
    logger.info(
        "Argv overflow detected (%d bytes > %d ceiling) — "
        "offloaded %d bytes to %s (new total: %d bytes)",
        total,
        ceiling,
        largest_size,
        tmp_path,
        new_total,
    )
    return new_cmd, tmp_path


# ── Semantic Error Translation Layer ────────────────────────────────────────────
#
# When security or sandbox violations occur inside a Goose subprocess, the
# raw Python traceback is confusing to fallback LLM models (especially
# smaller ones like gemma3:27b).  They tend to echo the traceback verbatim
# and omit the mandatory <final_answer> XML tags, which causes a secondary
# RuntimeError that rapidly exhausts the CircuitBreaker's 5-failure
# threshold, paralysing the entire orchestration pipeline.
#
# This layer catches those violations *before* they propagate, and
# translates them into a structured semantic guidance prompt that:
#   1. Preserves the <final_answer> format the pipeline expects
#   2. Politely explains what went wrong and suggests alternatives
#   3. Prevents the CircuitBreaker from counting the violation as a
#      failure (because the model *did* produce valid output)
# ────────────────────────────────────────────────────────────────────────────────


def translate_security_violation(
    violation: Exception,
    node_name: str = "",
) -> tuple[str, str]:
    """Translate a security/sandbox violation into a structured guidance prompt.

    Produces a polite, LLM-friendly <final_answer> response that explains
    the restriction and offers a safe alternative.  This keeps the
    orchestrator's output contract intact and prevents cascade failures
    through the CircuitBreaker.

    Args:
        violation: The original SecurityAccessViolation, SandboxViolation,
                   or any Exception.
        node_name: Name of the node where the violation occurred.

    Returns:
        Tuple of (final_answer_content, raw_stdout_text) — identical to
        the normal return contract of _execute_single_model.

    """
    from beagle.errors import (
        SandboxViolation,
        SecurityAccessViolation,
    )

    if isinstance(violation, SecurityAccessViolation | SandboxViolation):
        restriction = violation.restriction
        suggestion = violation.suggestion
        severity = violation.severity
    else:
        # Generic exception — extract what we can
        restriction = str(violation)
        suggestion = "Review the query and retry with a simpler approach."
        severity = "medium"

    # Build a deterministic, LLM-friendly guidance prompt
    answer = (
        f"<final_answer>\n"
        f"## Security Policy Restriction — {severity.upper()}\n\n"
        f"**What happened:** {restriction}\n\n"
        f"**What you can do instead:** {suggestion}\n\n"
        f"The workflow safely halted this operation. No system damage occurred. "
        f"Please rephrase your request to work within the allowed boundaries.\n"
        f"</final_answer>"
    )

    node_prefix = f"[{node_name}] " if node_name else ""
    logger.info(
        f"{node_prefix}Translated {type(violation).__name__} into semantic "
        f"guidance (severity={severity})"
    )

    return answer, answer


def is_security_violation(exc: Exception) -> bool:
    """Return True if the exception is a security/sandbox violation."""
    from beagle.errors import (
        SandboxViolation,
        SecurityAccessViolation,
    )

    return isinstance(exc, SecurityAccessViolation | SandboxViolation)


# ── KV Cache Efficiency: Output Truncation ──────────────────────────────────────

# Token threshold for truncation (~4 chars per token)
TRUNCATION_HEADER_LINES = 10  # First N lines to show
TRUNCATION_FOOTER_LINES = 10  # Last N lines to show


def truncate_large_output(
    output: str,
    header_lines: int = TRUNCATION_HEADER_LINES,
    footer_lines: int = TRUNCATION_FOOTER_LINES,
) -> str:
    """Truncate large outputs with header/footer pattern.

    H-MEM v13: Implements KV Cache Efficiency Optimization.
    For outputs >10,000 tokens, show first 10 lines and last 10 lines
    with a truncation marker in the middle.

    Args:
        output: Output string to potentially truncate
        header_lines: Number of lines to show at start
        footer_lines: Number of lines to show at end

    Returns:
        Truncated output with header/footer pattern if large,
        otherwise original output

    """
    truncation_threshold = get_config().output.truncation_threshold
    if len(output) <= truncation_threshold:
        return output

    lines = output.splitlines()
    total_lines = len(lines)

    # Don't truncate if already short enough
    if total_lines <= header_lines + footer_lines:
        return output

    # Extract header and footer
    header = "\n".join(lines[:header_lines])
    footer = "\n".join(lines[-footer_lines:])

    # Calculate hidden lines
    hidden_lines = total_lines - header_lines - footer_lines

    # Build truncated output
    truncated = f"""{header}

... [TRUNCATED: {hidden_lines} lines hidden - {len(output) // 4} tokens]
... Use VFS archive to retrieve full output if needed ...

{footer}"""

    logger.info(
        f"Truncated large output: {total_lines} lines -> {header_lines + footer_lines} lines "
        f"({len(output) // 4} tokens -> {len(truncated) // 4} tokens)"
    )

    return truncated


def _get_pool_config_workers() -> int:
    """Get max workers from config, falling back to memory/CPU-based default."""
    try:
        import psutil

        mem_gb = psutil.virtual_memory().available / (1024**3)
        cpu_count = os.cpu_count() or 4

        if mem_gb < 2:
            return min(cpu_count, 2)
        elif mem_gb < 8:
            return min(cpu_count, 4)
        else:
            return min(cpu_count * 2, 8)
    except ImportError:
        return min(int((os.cpu_count() or 4) * 1.5), 8)


def _get_pool_config_timeout() -> int:
    """Get default timeout from config."""
    try:
        return get_config().pool.default_timeout_seconds
    except (AttributeError, KeyError, ImportError):
        return 300


_T = TypeVar("_T")


# v13.22.3: Provider fallback chain. Was hardcoded to ["openai"] on the
# assumption that Ollama Cloud's OpenAI-compatible endpoint could be
# reached via the openai provider with OPENAI_HOST set. In practice,
# when OPENAI_HOST is unset (the default), goose's openai provider
# hits https://api.openai.com/v1/chat/completions and returns 401, and
# the failure cascades through the 5-failure threshold on
# get_circuit_breaker("goose-subprocess") and trips the breaker
# permanently — every workflow aborts with
# "Circuit breaker 'goose-subprocess' is OPEN".
#
# The config.toml [goose].provider = "ollama_cloud" (matches
# agents.toml default_provider = "ollama_cloud") is the actually-working
# provider on this host; ollama_cloud uses the model name's native
# auth (OLLAMA_CLOUD_API_KEY or local daemon) rather than the
# OpenAI-compatible shim, and works without OPENAI_HOST.
def _get_fallback_chain() -> list[str]:
    """Load the model fallback chain from config.toml.

    Returns the list of models to try in order when the primary model fails.
    Falls back to a hardcoded default if config is unavailable.
    """
    try:
        config = get_config()
        return list(config.goose.fallback_chain)  # type: ignore[attr-defined]
    except (AttributeError, KeyError, ImportError):
        # Schema default (matches config.toml [goose].fallback_chain). Hardcoded
        # fallbacks are forbidden by Beagle doctrine ("config.toml is SSOT for
        # config"); this list mirrors the SSOT and is the literal-last-resort
        # fallback if both the config and the SSOT file are unreadable.
        return [
            "minimax-m3:cloud",
            "glm-5.1:cloud",
            "deepseek-v4-pro:cloud",
            "gemma4:31b-cloud",
        ]


def _get_learned_fallback_chain(
    base_chain: list[str],
    node_type: str = "",
) -> list[str]:
    """Reorder fallback chain based on historical model performance.

    Queries the tracking database for per-model success rates and
    latency. Models with enough history are sorted by
    (success_rate DESC, latency ASC). Models without history retain
    their original position.

    Respects LearnedRoutingConfig for enabled flag and min_executions.

    Args:
        base_chain: Static fallback chain from config.
        node_type: Node type for type-specific rankings.

    Returns:
        Reordered chain with best-performing models first,
        or base_chain if learned routing is disabled.

    """
    try:
        from beagle.config.config import get_config
        from beagle.tracking.database import (
            TrackingDatabase,
        )

        lr_config = get_config().learned_routing
        if not lr_config.enabled:
            return base_chain

        # v13.17.1: DB access can deadlock the asyncio event loop if the
        # TrackingDatabase._lock is held (e.g., by a prior signal-handler
        # sys.exit that didn't release it). Fall back to static chain on
        # any DB error — learned routing is a performance optimization,
        # not a correctness requirement.
        db = TrackingDatabase.get_instance()
        try:
            rankings = db.query_model_rankings(
                node_type=node_type,
                min_executions=lr_config.min_executions,
            )
        except (RuntimeError, OSError, TimeoutError, ValueError) as _db_err:
            logger.debug(
                f"TrackingDatabase query failed ({_db_err}), falling back to static fallback chain"
            )
            return base_chain

        if not rankings:
            return base_chain

        # Build ranked order from DB
        ranked_models = [r["model"] for r in rankings]

        # Merge: ranked first (DB order), then unranked (original order)
        reordered: list[str] = []
        for model in ranked_models:
            if model in base_chain and model not in reordered:
                reordered.append(model)
        for model in base_chain:
            if model not in reordered:
                reordered.append(model)

        if reordered != base_chain:
            logger.info(
                f"[LearnedRouting] Reordered fallback chain "
                f"for {node_type or 'all'}: "
                f"{' → '.join(reordered)}"
            )
        return reordered

    # v13.x: narrow catch to the specific exceptions that the DB layer and
    # config loaders actually raise (RuntimeError from connection failures,
    # OSError from filesystem, ValueError from invalid query params,
    # KeyError from missing DB rows). A blind ``except Exception`` masked
    # real bugs in this path; if a new exception type appears, surface it
    # loudly here instead of silently swallowing it.
    except (RuntimeError, OSError, ValueError, KeyError, AttributeError) as _lr_err:
        logger.debug(f"Learned-routing query failed ({_lr_err!r}), falling back to static chain")
        return base_chain


# ── Module-level counters for metrics ────────────────────────────────────────
_pool_stats = {"completed": 0, "failed": 0, "total": 0, "fallback_used": 0}
_pool_stats_lock = threading.Lock()

# v0.3.0: Rate limit attempts with TTL eviction (was function attribute, leaked forever)
_rate_limit_attempts: dict[str, int] = {}
_rate_limit_timestamps: dict[str, float] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_TTL = 300.0  # 5 minutes


def _get_rate_limit_attempt(key: str) -> int:
    """Get rate limit attempt count, evicting stale entries first."""
    with _rate_limit_lock:
        now = time.monotonic()
        # Evict entries older than TTL
        stale = [k for k, ts in _rate_limit_timestamps.items() if now - ts > _RATE_LIMIT_TTL]
        for k in stale:
            _rate_limit_attempts.pop(k, None)
            _rate_limit_timestamps.pop(k, None)
        return _rate_limit_attempts.get(key, 0)


def _set_rate_limit_attempt(key: str, value: int) -> None:
    """Set rate limit attempt count with timestamp."""
    with _rate_limit_lock:
        # Evict stale entries to prevent unbounded growth
        now = time.monotonic()
        stale = [k for k, ts in _rate_limit_timestamps.items() if now - ts > _RATE_LIMIT_TTL]
        for k in stale:
            _rate_limit_attempts.pop(k, None)
            _rate_limit_timestamps.pop(k, None)
        # Hard cap as safety net (500 entries max)
        if len(_rate_limit_attempts) > 500:
            oldest = sorted(_rate_limit_timestamps, key=lambda k: _rate_limit_timestamps[k])[
                : len(_rate_limit_attempts) - 400
            ]
            for k in oldest:
                _rate_limit_attempts.pop(k, None)
                _rate_limit_timestamps.pop(k, None)
        _rate_limit_attempts[key] = value
        _rate_limit_timestamps[key] = now


def _increment_completed() -> None:
    with _pool_stats_lock:
        _pool_stats["completed"] += 1
        _pool_stats["total"] += 1


def _increment_failed() -> None:
    with _pool_stats_lock:
        _pool_stats["failed"] += 1
        _pool_stats["total"] += 1


def _increment_fallback() -> None:
    with _pool_stats_lock:
        _pool_stats["fallback_used"] += 1


def get_pool_stats() -> dict:
    """Return pool statistics for metrics/monitoring."""
    return {
        **_pool_stats,
        "active": _pool_stats.get("active", 0),
    }


def reset_pool_stats() -> None:
    """Reset counters (for testing)."""
    _pool_stats["completed"] = 0
    _pool_stats["failed"] = 0
    _pool_stats["total"] = 0
    _pool_stats["fallback_used"] = 0


class GoosePool:
    """Semaphore-bounded pool of concurrent Goose subprocess slots."""

    def __init__(
        self,
        max_workers: int | None = None,
        default_timeout: int | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_workers or _get_pool_config_workers())
        self._default_timeout = default_timeout or _get_pool_config_timeout()
        self._active: int = 0
        self._lock = asyncio.Lock()

    @property
    def max_workers(self) -> int:
        return self._semaphore._value

    @property
    def active(self) -> int:
        return self._active

    def stats(self) -> dict:
        """Return pool statistics for monitoring/metrics."""
        return {
            "max_workers": self.max_workers,
            "active": self._active,
            "available": self.max_workers - self._active,
            "type": "GoosePool",
        }

    async def run(
        self,
        prompt: str,
        system_directive: str,
        node_name: str,
        timeout: int | None = None,
        readonly: bool = False,
        model_override: str | None = None,
        provider_override: str | None = None,
    ) -> tuple[str, str]:
        """Execute a Goose headless call within the pool."""
        timeout = timeout or self._default_timeout

        async with self._semaphore:
            async with self._lock:
                self._active += 1
            logger.info(
                f"GoosePool: {self._active}/{self.max_workers} active, "
                f"{_pool_stats['completed']} completed, {_pool_stats['failed']} failed"
            )

            try:
                result = await _execute_goose_with_fallback(
                    prompt=prompt,
                    system_directive=system_directive,
                    node_name=node_name,
                    timeout=timeout,
                    readonly=readonly,
                    model_override=model_override,
                    provider_override=provider_override,
                )
                _increment_completed()
                return result
            except (TimeoutError, RuntimeError, OSError):
                _increment_failed()
                raise
            finally:
                async with self._lock:
                    self._active -= 1

    async def __aenter__(self) -> GoosePool:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        # v13.x: ``_exc_info`` is required by the context-manager protocol
        # (the interpreter passes the exception triple when ``__aexit__`` is
        # invoked with a pending exception), but the body does not need it
        # because the active-count draining loop is purely a timeout
        # concern. Underscore-prefixing silences vulture without changing
        # the public signature.
        max_wait = 60.0  # Prevent infinite hang
        waited = 0.0
        while waited < max_wait:
            async with self._lock:
                if self._active == 0:
                    break
            await asyncio.sleep(0.1)
            waited += 0.1
        else:
            logger.warning(
                f"GoosePool.__aexit__ timed out after {max_wait}s; "
                f"{self._active} tasks still active"
            )


# ── Low-level subprocess executor with model fallback ───────────────────────────

_active_processes: set[asyncio.subprocess.Process] = set()
_processes_lock: asyncio.Lock | None = None
_processes_lock_init = threading.Lock()


def _get_processes_lock() -> asyncio.Lock:
    global _processes_lock
    if _processes_lock is None:
        with _processes_lock_init:
            if _processes_lock is None:
                _processes_lock = asyncio.Lock()
    return _processes_lock


async def _execute_goose_with_fallback(
    prompt: str,
    system_directive: str,
    node_name: str,
    timeout: int | None = None,
    readonly: bool = False,
    model_override: str | None = None,
    provider_override: str | None = None,
) -> tuple[str, str]:
    """Execute Goose subprocess with automatic model fallback.

    If the primary model fails (timeout, error, no <final_answer>), automatically
    tries fallback models in order: glm-5.1:cloud -> deepseek-v3.2 -> qwen3.5:397b -> gemma3:27b

    Args:
        prompt: User prompt text.
        system_directive: System directive.
        node_name: Node name for logging.
        timeout: Seconds before SIGTERM.
        readonly: If True, set BEAGLE_READONLY_MODE=1 in the subprocess env.
        model_override: Use specific model instead of fallback chain.
        provider_override: Use specific provider instead of fallback chain.

    Returns:
        Tuple of (final_answer_content, raw_stdout_text).

    """
    config = get_config()
    timeout = timeout or config.pool.default_timeout_seconds
    goose_bin = os.environ.get("GOOSE_BIN", config.goose.binary_path)

    # Determine models to try (learned routing from execution history)
    static_chain = _get_fallback_chain()
    models_to_try = (
        [model_override]
        if model_override
        else _get_learned_fallback_chain(static_chain, node_type=node_name)
    )

    providers_to_try = [provider_override] if provider_override else _get_provider_chain()

    # Get circuit breaker for subprocess failures
    circuit = await get_circuit_breaker(
        "goose-subprocess",
        CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=30.0,
        ),
    )

    # Check if a call should be attempted — this also performs the
    # OPEN→HALF_OPEN state transition when the cooldown has elapsed.
    # Reading `is_open` alone is a bug: the breaker would stay OPEN
    # forever because no caller was triggering the state transition.
    if not await circuit._can_attempt():
        retry_after = circuit.get_retry_after()
        logger.warning(f"[{node_name}] Circuit breaker OPEN, retry after {retry_after:.1f}s")
        raise CircuitBreakerOpenError("goose-subprocess", retry_after)

    last_error: Exception | None = None

    # Try each model/provider combination until one succeeds
    for model_idx, goose_model in enumerate(models_to_try):
        for provider_idx, goose_provider in enumerate(providers_to_try):
            # Skip already-tried combinations
            if model_idx > 0 or provider_idx > 0:
                _increment_fallback()
                logger.info(f"[{node_name}] Trying fallback model: {goose_model}/{goose_provider}")

            # v13.21.13: Surface each model attempt on the EventBus — model
            # cold-starts and fallback hops are the slowest invisible part
            # of a workflow, exactly what delegating clients wait on.
            attempt_kind = "fallback" if (model_idx > 0 or provider_idx > 0) else "attempt"
            _publish_node_output(
                node_name,
                f"[{attempt_kind}] spawning goose subprocess "
                f"model={goose_model} provider={goose_provider} timeout={timeout}s",
                "stderr",
            )

            try:
                result = await _execute_single_model(
                    goose_bin=goose_bin,
                    goose_model=goose_model,
                    goose_provider=goose_provider,
                    prompt=prompt,
                    system_directive=system_directive,
                    node_name=node_name,
                    timeout=timeout,
                    readonly=readonly,
                    circuit=circuit,
                )
                # Success - record and return
                with contextlib.suppress(Exception):
                    await circuit._record_success()
                return result

            except (TimeoutError, GooseExecutionError, RuntimeError, OSError) as e:
                last_error = e
                logger.warning(f"[{node_name}] {goose_model}/{goose_provider} failed: {e}")
                _publish_node_output(
                    node_name,
                    f"[attempt-failed] {goose_model}/{goose_provider}: {str(e)[:200]}",
                    "stderr",
                )

                # ── Primary-timeout bail-out (v13.x) ────────────────────────
                # If the PRIMARY model timed out, the wall-clock budget is the
                # problem, not the model. Smaller fallback models will hit the
                # same ceiling (they cannot write the report faster than the
                # primary), and the cascading timeouts amplify total runtime
                # to N * budget — wasting minutes before the orchestrator's
                # outer timeout kills the whole workflow. Bail out on the
                # primary timeout so the caller can retry with a larger
                # budget instead of a smaller model.
                if isinstance(e, TimeoutError) and model_idx == 0 and provider_idx == 0:
                    logger.warning(
                        f"[{node_name}] Primary model timed out at {timeout}s "
                        f"— skipping fallback chain (budget issue, not model issue)"
                    )
                    raise

                # ── Semantic Error Translation (v12.3) ───────────────────────
                # SecurityAccessViolation / SandboxViolation must NOT be counted
                # as a circuit-breaker failure.  They are *intentional* policy
                # enforcements, not infrastructure failures.  If we let them
                # flow through, fallback models (especially gemma3:27b) choke on
                # the raw Python traceback, omit <final_answer> tags, and the
                # resulting RuntimeError cascades through the 5-failure threshold,
                # paralysing the pipeline.
                if is_security_violation(e):
                    logger.info(
                        f"[{node_name}] Security violation intercepted — "
                        f"translating to semantic guidance (circuit breaker NOT tripped)"
                    )
                    # Record a SUCCESS so the circuit breaker does NOT open
                    await circuit._record_success()
                    # Translate into a structured, LLM-friendly response
                    return translate_security_violation(e, node_name=node_name)

                # Genuine infrastructure failure — record for circuit breaker
                with contextlib.suppress(Exception):
                    await circuit._record_failure()
                # Continue to next fallback
                continue

    # All models failed — check if the last error was a security violation
    # (edge case: all models in chain were security-blocked before translation)
    if last_error is not None and is_security_violation(last_error):
        return translate_security_violation(last_error, node_name=node_name)

    raise last_error or RuntimeError(f"All models in fallback chain failed for {node_name}")


def _publish_node_output(node_name: str, content: str, stream_type: str = "stdout") -> None:
    """Best-effort EventBus liveness publish for subprocess activity.

    v13.21.13: The streaming reader sees every line the sub-goose emits,
    but until now none of it reached the EventBus — so the MCP progress
    bridge (and any delegating client) was blind between node start and
    node end. Telemetry only: the events module is never a hard dep, and
    EventBus.publish() never raises by contract.
    """
    if not content:
        return
    try:
        from ..events import NodeOutput, get_event_bus
    except ImportError:
        return
    with contextlib.suppress(TypeError, ValueError, RuntimeError):
        get_event_bus().publish(
            NodeOutput(
                workflow_id=os.environ.get("BEAGLE_WORKFLOW_ID", ""),
                node_name=node_name,
                stream_type=stream_type,
                content=content[:500],
            )
        )


# ── Streaming Early Termination ───────────────────────────────────────────────
#
# When enabled, reads subprocess stdout line-by-line instead of using
# process.communicate().  Once </final_answer> is detected, the process
# is terminated early — saving ~10-30% wall-clock time and tokens for
# long-running models that produce trailing debug output.

_STREAMING_FINAL_ANSWER_PATTERN = re.compile(r"</final_answer\s*>")

_MAX_EARLY_DRAIN_LINES = 50  # Max lines to read after early termination


async def _streaming_read(
    process: asyncio.subprocess.Process,
    prompt: str,
    timeout: int,
    node_name: str,
) -> tuple[bytes, bytes]:
    """Read subprocess output with early termination on </final_answer>.

    Returns (stdout_bytes, stderr_bytes) — same as process.communicate().
    If </final_answer> is detected in stdout, terminates early.

    Writes *prompt* to stdin and closes it before reading stdout/stderr.
    The sub-goose was launched with ``-i -`` (read stdin until EOF),
    so without this write+close the process blocks forever waiting for input.
    """
    # Write prompt to stdin and close it so the sub-goose can start executing.
    # This must happen BEFORE we start reading stdout, otherwise the sub-goose
    # blocks forever on stdin EOF that never arrives.
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    if stdin is not None:
        try:
            stdin.write(prompt.encode("utf-8"))
            await stdin.drain()
            stdin.close()
            with contextlib.suppress(Exception):
                await stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.warning(f"[{node_name}] stdin write failed: {exc}")

    final_answer_detected = asyncio.Event()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def _read_stdout() -> None:
        while True:
            if stdout is None:
                return
            line = await stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            stdout_lines.append(decoded)
            _publish_node_output(node_name, decoded.strip(), "stdout")
            if _STREAMING_FINAL_ANSWER_PATTERN.search(decoded):
                final_answer_detected.set()
                # Continue reading a few more lines for trailing context
                for _ in range(_MAX_EARLY_DRAIN_LINES):
                    try:
                        extra = await asyncio.wait_for(stdout.readline(), timeout=0.1)
                        if not extra:
                            break
                        stdout_lines.append(extra.decode("utf-8", errors="replace"))
                    except TimeoutError:
                        break
                return

    async def _read_stderr() -> None:
        while True:
            if stderr is None:
                return
            line = await stderr.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            stderr_lines.append(decoded)
            _publish_node_output(node_name, decoded.strip(), "stderr")

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())

    # Wait for either early termination or process exit
    process_done = asyncio.create_task(process.wait())
    _done, _pending = await asyncio.wait(
        [process_done, asyncio.create_task(final_answer_detected.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # If early-terminated, give process a moment then kill the whole
    # process group (v13.17: killpg instead of single-PID to reap
    # orphaned goose grandchildren).
    if final_answer_detected.is_set():
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            _terminate_process_group(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                _terminate_process_group(process, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await process.wait()
        logger.debug(f"[{node_name}] Early-terminated after </final_answer>")
    else:
        # Normal completion
        await asyncio.gather(stdout_task, stderr_task)

    # Clean up tasks
    for t in [stdout_task, stderr_task]:
        t.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    stdout_bytes = "".join(stdout_lines).encode("utf-8", errors="replace")
    stderr_bytes = "".join(stderr_lines).encode("utf-8", errors="replace")
    return stdout_bytes, stderr_bytes


async def _execute_single_model(
    goose_bin: str,
    goose_model: str,
    goose_provider: str,
    prompt: str,
    system_directive: str,
    node_name: str,
    timeout: int,
    readonly: bool,
    circuit: CircuitBreaker,
) -> tuple[str, str]:
    """Execute a single Goose subprocess with specific model/provider.

    Args:
        goose_bin: Path to goose binary.
        goose_model: Model name.
        goose_provider: Provider name.
        prompt: User prompt.
        system_directive: System directive.
        node_name: Node name for logging.
        timeout: Seconds before SIGTERM.
        readonly: If True, set BEAGLE_READONLY_MODE=1.
        circuit: Circuit breaker instance.

    Returns:
        Tuple of (final_answer_content, raw_stdout_text).

    Raises:
        RuntimeError: If execution fails or no <final_answer> found.

    """
    _start_time = time.monotonic()

    # Strip YAML frontmatter (---…---) from system directives — goose's arg parser
    # treats values starting with "---" as unknown flags (exit code 2)
    cleaned_directive = re.sub(
        r"\A---\n.*?\n---\n?", "", system_directive, count=1, flags=re.DOTALL
    )

    # Inject mandatory <final_answer> tag requirement so all models comply,
    # regardless of what the recipe says
    final_answer_reminder = (
        "\n\nCRITICAL OUTPUT REQUIREMENT: You MUST wrap your entire final response "
        "in <final_answer></final_answer> XML tags. Do NOT use ```xml code fences "
        "around these tags. Output ONLY plain <final_answer>your response here</final_answer>. "
        "If you do not include these tags, your output will be discarded."
    )
    cleaned_directive = cleaned_directive.rstrip() + final_answer_reminder

    # Build the command — pass args directly via create_subprocess_exec to avoid
    # shell expansion issues (backticks in system directives were interpreted as
    # command substitution when using sh -c + $(cat file))
    cmd = [
        goose_bin,
        "run",
        "--provider",
        goose_provider,
        "--model",
        goose_model,
        "-i",
        "-",
        "--system",
        cleaned_directive,
        "--with-builtin",
        "developer",
        "-q",
    ]

    # ── Phase 4.3: argv size ceiling ─────────────────────────────────────────
    # When the total argv + envp exceeds _ARGV_CEILING_BYTES (128 KiB), offload
    # the largest positional argument (typically the system directive) to a temp
    # file with the `@file` idiom.  The caller is responsible for unlinking the
    # temp file after the subprocess completes.
    env = _build_safe_env(readonly=readonly)
    argv_tmp_path: str | None = None
    try:
        cmd, argv_tmp_path = _argv_overflow_safe(cmd, env=env)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        # Clean up temp file on failure to spawn
        if argv_tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(argv_tmp_path)
        raise RuntimeError(f"Failed to spawn subprocess: {exc}") from exc

    async with _get_processes_lock():
        _active_processes.add(process)

    stdout_bytes = b""
    stderr_bytes = b""

    try:
        # Use streaming reader with early termination when enabled
        stream_cfg = get_config().streaming
        if stream_cfg.enabled and stream_cfg.early_termination:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                _streaming_read(process, prompt=prompt, timeout=timeout, node_name=node_name),
                timeout=timeout,
            )
        else:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=timeout,
            )
    except TimeoutError:
        logger.warning(f"[{node_name}] Subprocess timed out after {timeout}s")
        _terminate_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            _terminate_process_group(process, signal.SIGKILL)
            await process.wait()
        stdout_bytes = b""
        stderr_bytes = b""
        # ── Phase 4.3: cleanup argv temp file on timeout path ──────────────
        if argv_tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(argv_tmp_path)
            argv_tmp_path = None
        # Record failure for learned routing
        with contextlib.suppress(Exception):
            from beagle.tracking.database import (
                TrackingDatabase,
            )

            db = TrackingDatabase.get_instance()
            db.record_model_outcome(
                model=goose_model,
                provider=goose_provider,
                node_type=node_name,
                success=False,
                latency_seconds=time.monotonic() - _start_time,
                failure_reason="timeout",
            )
        raise RuntimeError(f"Timeout after {timeout}s") from None

    async with _get_processes_lock():
        _active_processes.discard(process)

    # ── Phase 4.3: cleanup argv temp file ──────────────────────────────────
    if argv_tmp_path is not None:
        with contextlib.suppress(OSError):
            os.unlink(argv_tmp_path)
        argv_tmp_path = None

    raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
    raw_stderr = stderr_bytes.decode("utf-8", errors="replace")

    # Even if return code is non-zero, check if we got valid <final_answer> output
    # Goose sometimes exits with code 1 due to "not connected" errors but still produces output
    # Also accept partial answers where model hit max tokens before closing tag
    has_final_answer = "<final_answer>" in raw_stdout

    if not has_final_answer:
        # No valid output - this is a real failure
        # Record failure for learned routing
        with contextlib.suppress(Exception):
            from beagle.tracking.database import (
                TrackingDatabase,
            )

            db = TrackingDatabase.get_instance()
            db.record_model_outcome(
                model=goose_model,
                provider=goose_provider,
                node_type=node_name,
                success=False,
                latency_seconds=time.monotonic() - _start_time,
                failure_reason="no_final_answer",
            )
        if process.returncode != 0:
            raise RuntimeError(
                f"Process exited with code {process.returncode}\nstderr: {raw_stderr[:500]}"
            )
        else:
            raise RuntimeError(f"No <final_answer> found in output\nstdout: {raw_stdout[:200]}")

    # Check for rate limiting - implement exponential backoff with jitter
    # This is SOTA pattern for handling 429 errors in LLM APIs
    if "429" in raw_stderr or "rate limit" in raw_stderr.lower():
        # Exponential backoff with jitter for 429 errors
        # Formula: min(base * 2^attempt + random_jitter, max_backoff)
        # This is the recommended pattern from AWS, Google Cloud, and OpenAI.
        # v13.x: use ``random.SystemRandom()`` (backed by ``os.urandom``) instead
        # of the default ``random`` module so this passes the S311
        # non-cryptographic-RNG doctrine rule while remaining correct for
        # jitter (no security claim is being made, but SystemRandom costs
        # ~the same as random.uniform and satisfies the doctrine floor).
        base_delay = 2.0  # seconds
        max_delay = 60.0  # seconds
        _rng = random.SystemRandom()
        jitter = _rng.uniform(0, 1)  # 0-1 second jitter

        # Track per-model/provider rate limit state to avoid redundant retries
        rate_limit_key = f"{goose_model}:{goose_provider}"
        attempt = _get_rate_limit_attempt(rate_limit_key)
        delay = min(base_delay * (2**attempt) + jitter, max_delay)
        _set_rate_limit_attempt(rate_limit_key, attempt + 1)

        logger.warning(
            f"[{node_name}] Rate limit detected for {goose_model}/{goose_provider}, "
            f"attempt {attempt + 1}, backing off for {delay:.2f}s"
        )
        await asyncio.sleep(delay)

        # After sleeping, raise to trigger retry loop - this model/provider combo
        # will be retried in the fallback chain with the updated attempt counter
        # Record failure for learned routing
        with contextlib.suppress(Exception):
            from beagle.tracking.database import (
                TrackingDatabase,
            )

            db = TrackingDatabase.get_instance()
            db.record_model_outcome(
                model=goose_model,
                provider=goose_provider,
                node_type=node_name,
                success=False,
                latency_seconds=time.monotonic() - _start_time,
                failure_reason="rate_limited",
            )
        raise RuntimeError(
            f"Rate limited ({goose_model}/{goose_provider}), backed off {delay:.1f}s"
        )

    # Log stderr (errors from Goose itself)
    for line in raw_stderr.splitlines():
        if line.strip():
            logger.error(f"[{node_name} - Goose stderr] {line.strip()}")

    # Extract <final_answer> block from stdout
    # Take the LAST match to avoid session header false positives
    # Filter out matches that are mostly tool code
    all_matches = re.findall(r"<final_answer>(.*?)</final_answer>", raw_stdout, re.DOTALL)
    if not all_matches and "<final_answer>" in raw_stdout:
        # Model hit max tokens — closing tag missing. Extract everything after last opening tag.
        partial = raw_stdout.rsplit("<final_answer>", 1)[1]
        # Strip trailing code fences if the model wrapped in ```xml
        partial = re.sub(r"\s*```\s*$", "", partial)
        all_matches = [partial]

    # FIX: Strip code fences from all matches (models often wrap in ```json or ```xml)
    cleaned_matches = []
    for match in all_matches:
        # Remove leading ```json or ```xml
        match = re.sub(r"^\s*```(?:json|xml|yaml)\s*\n?", "", match)
        # Remove trailing ```
        match = re.sub(r"\s*```\s*$", "", match)
        cleaned_matches.append(match)

    if cleaned_matches:
        # Filter out matches that are mostly tool code (contain backticks or tool_code)
        text_matches = [
            m
            for m in cleaned_matches
            if len(m.strip()) > 10 and not (m.count("```") > 2 or "tool_code" in m)
        ]
        # Fall back to last match if all are tool code
        final_answer = (text_matches[-1] if text_matches else cleaned_matches[-1]).rstrip()
    elif raw_stdout:
        lines = raw_stdout.rstrip().splitlines()
        final_answer = lines[-1].rstrip() if lines else raw_stdout.rstrip()
    else:
        final_answer = ""

    # H-MEM v13: Apply KV Cache Efficiency - truncate large outputs
    final_answer = truncate_large_output(final_answer)

    # Record success for learned routing
    with contextlib.suppress(Exception):
        from beagle.tracking.database import (
            TrackingDatabase,
        )

        db = TrackingDatabase.get_instance()
        db.record_model_outcome(
            model=goose_model,
            provider=goose_provider,
            node_type=node_name,
            success=True,
            latency_seconds=time.monotonic() - _start_time,
            input_tokens=0,  # Filled by cost tracker
            output_tokens=0,
            cost_usd=0.0,
        )

    return final_answer, raw_stdout


# ── Legacy single-model executor (kept for backward compatibility) ─────────────


async def _execute_goose_subprocess_legacy(
    prompt: str,
    system_directive: str,
    node_name: str,
    timeout: int | None = None,
    readonly: bool = False,
) -> tuple[str, str]:
    """Legacy single-model subprocess executor.

    Deprecated: Use _execute_goose_with_fallback instead.
    This function is kept for backward compatibility with any direct callers.
    """
    config = get_config()
    timeout = timeout or config.pool.default_timeout_seconds
    goose_bin = os.environ.get("GOOSE_BIN", config.goose.binary_path)
    goose_provider = os.environ.get("GOOSE_PROVIDER", config.goose.provider)
    goose_model = os.environ.get("GOOSE_MODEL", config.goose.default_model)

    cmd = [
        goose_bin,
        "run",
        "--provider",
        goose_provider,
        "--model",
        goose_model,
        "-i",
        "-",
        "--system",
        system_directive,
        "--with-builtin",
        "developer",
        "-q",
    ]

    env = _build_safe_env(readonly=readonly)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
    )

    async with _get_processes_lock():
        _active_processes.add(process)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=prompt.encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(f"[{node_name}] Subprocess timed out after {timeout}s")
        _terminate_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            _terminate_process_group(process, signal.SIGKILL)
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except TimeoutError:
                logger.error("Process stuck after SIGKILL — abandoning")
        stdout_bytes = b""
        stderr_bytes = b""
        raise RuntimeError(f"Timeout after {timeout}s") from None
    finally:
        async with _get_processes_lock():
            _active_processes.discard(process)

    raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
    raw_stderr = stderr_bytes.decode("utf-8", errors="replace")

    # Extract <final_answer> block - filter out tool code
    all_matches = re.findall(r"<final_answer>(.*?)</final_answer>", raw_stdout, re.DOTALL)
    if all_matches:
        text_matches = [
            m
            for m in all_matches
            if len(m.strip()) > 10 and not (m.count("```") > 2 or "tool_code" in m)
        ]
        final_answer = text_matches[-1].rstrip() if text_matches else all_matches[-1].rstrip()
    elif raw_stdout:
        lines = raw_stdout.rstrip().splitlines()
        final_answer = lines[-1].rstrip() if lines else raw_stdout.rstrip()
    else:
        final_answer = ""

    for line in raw_stderr.splitlines():
        if line.strip():
            logger.error(f"[{node_name} - Goose stderr] {line.strip()}")

    return final_answer, raw_stdout


# ── Standalone convenience helper ─────────────────────────────────────────────

_pool: GoosePool | None = None
_pool_lock: asyncio.Lock | None = None
_pool_lock_init = threading.Lock()


def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        with _pool_lock_init:
            if _pool_lock is None:
                _pool_lock = asyncio.Lock()
    return _pool_lock


async def _get_pool() -> GoosePool:
    global _pool
    async with _get_pool_lock():
        if _pool is None:
            _pool = GoosePool()
        return _pool


async def run_goose(
    prompt: str,
    system_directive: str,
    node_name: str,
    timeout: int | None = None,
    readonly: bool = False,
    model_override: str | None = None,
    provider_override: str | None = None,
) -> tuple[str, str]:
    """Run a Goose subprocess via the shared pool with automatic model fallback.

    All Beagle nodes should use this instead of calling
    asyncio.create_subprocess_exec directly.

    Args:
        prompt: User prompt text.
        system_directive: System directive.
        node_name: Node name for logging.
        timeout: Seconds before SIGTERM.
        readonly: If True, set BEAGLE_READONLY_MODE=1.
        model_override: Use specific model instead of fallback chain.
        provider_override: Use specific provider instead of fallback chain.

    Returns:
        Tuple of (final_answer_content, raw_stdout_text).

    """
    pool = await _get_pool()
    return await pool.run(
        prompt=prompt,
        system_directive=system_directive,
        node_name=node_name,
        timeout=timeout,
        readonly=readonly,
        model_override=model_override,
        provider_override=provider_override,
    )


logger.info("✅ GoosePool loaded with model fallback support")
