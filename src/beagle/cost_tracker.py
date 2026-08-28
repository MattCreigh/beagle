"""Enhanced Cost Tracker with proper Context Window Tracking.

Fixes context window tracking by:
1. Properly tracking input/output tokens per LLM call
2. Monitoring context window utilization
3. Warning at 80% threshold
4. Graceful degradation when limit approached

v13.5.2: Consolidated model data — MODEL_CONTEXT_WINDOWS and MODEL_PRICING
are now derived from config/models.py (single source of truth). The local dicts
are kept only as lazy caches populated on first access, so existing callers that
import these symbols continue to work without changes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.beagle.cost_tracker")

# ── Thresholds ──────────────────────────────────────────────────────────────────

CONTEXT_WARNING_THRESHOLD = 0.80  # 80%
CONTEXT_CRITICAL_THRESHOLD = 0.95  # 95%
MAX_PROMPT_RATIO = 0.70  # Max 70% of context for input prompt


# ── Model Data (consolidated from config/models.py) ────────────────────────────
# These are lazy-populated from MODEL_CONFIGS on first access.
# Keeping them as module-level dicts for backward compatibility with callers
# that do `from cost_tracker import MODEL_PRICING`.

_DEFAULT_CONTEXT_WINDOW = 128000
_DEFAULT_PRICING: dict[str, float] = {"input": 1.00, "output": 4.00}

_MODEL_CONTEXT_WINDOWS_CACHE: dict[str, int] | None = None
_MODEL_PRICING_CACHE: dict[str, dict[str, float]] | None = None


def _build_model_caches() -> None:
    """Populate MODEL_CONTEXT_WINDOWS and MODEL_PRICING from config/models.py.

    Called lazily on first access so import-time failures in config/models.py
    don't break the entire cost_tracker module.
    """
    global _MODEL_CONTEXT_WINDOWS_CACHE, _MODEL_PRICING_CACHE
    if _MODEL_CONTEXT_WINDOWS_CACHE is not None:
        return

    try:
        from .config.models import MODEL_CONFIGS

        ctx: dict[str, int] = {}
        pricing: dict[str, dict[str, float]] = {}

        for name, info in MODEL_CONFIGS.items():
            ctx[name] = info.context_window
            pricing[name] = {
                "input": info.input_cost_per_1m,
                "output": info.output_cost_per_1m,
            }

        # Defaults for unknown models
        ctx["default"] = _DEFAULT_CONTEXT_WINDOW
        pricing["default"] = dict(_DEFAULT_PRICING)

        _MODEL_CONTEXT_WINDOWS_CACHE = ctx
        _MODEL_PRICING_CACHE = pricing

    except ImportError:
        logger.warning("config.models unavailable — cost_tracker using built-in defaults")
        _MODEL_CONTEXT_WINDOWS_CACHE = {"default": _DEFAULT_CONTEXT_WINDOW}
        _MODEL_PRICING_CACHE = {"default": dict(_DEFAULT_PRICING)}


def _resolve_default_model(model: str) -> str:
    """Return ``model`` if provided, else the canonical config default preset.

    v1.1.1 (S9): the previous ``"gemma3:27b"`` literal was a hardcoded
    default that drifted from the allowlisted fleet. The SSOT is config.toml
    ``[model_presets]``; an empty string resolves to that at runtime.
    """
    if model:
        return model
    try:
        from beagle.config.model_resolver import get_preset

        return get_preset("default")
    except (ImportError, KeyError, ValueError, RuntimeError):  # pragma: no cover
        return "gemma4:31b"


def _get_context_window(model: str) -> int:
    """Get context window size for a model (delegates to config/models.py)."""
    _build_model_caches()
    if _MODEL_CONTEXT_WINDOWS_CACHE is None:
        raise RuntimeError("Model context window cache is not initialized")
    return _MODEL_CONTEXT_WINDOWS_CACHE.get(model, _MODEL_CONTEXT_WINDOWS_CACHE["default"])


def _get_pricing(model: str) -> dict[str, float]:
    """Get pricing for a model (delegates to config/models.py)."""
    _build_model_caches()
    if _MODEL_PRICING_CACHE is None:
        raise RuntimeError("Model pricing cache is not initialized")
    if model in _MODEL_PRICING_CACHE:
        return _MODEL_PRICING_CACHE[model]
    logger.warning(f"No pricing data for model '{model}', using default fallback")
    return _MODEL_PRICING_CACHE["default"]


class _LazyModelDict(dict):
    """Dict subclass that auto-populates from config/models.py on first access.

    Solves the chicken-and-egg problem: callers do
        ``from cost_tracker import MODEL_CONTEXT_WINDOWS``
    at import time and then access keys like ``MODEL_CONTEXT_WINDOWS["default"]``
    at runtime. The plain ``{}`` was always empty because ``_build_model_caches()``
    hadn't been called yet, causing ``KeyError`` in downstream modules like
    ``context_window.py``.

    This subclass intercepts ``__contains__``, ``__getitem__``, ``get``,
    ``__len__``, ``keys()``, ``values()``, ``items()``, ``__iter__``,
    and ``__repr__`` to trigger the lazy build before delegating to the real
    dict stored in the cache.
    """

    _cache_attr: str  # subclass must set: "_MODEL_CONTEXT_WINDOWS_CACHE" or "_MODEL_PRICING_CACHE"

    def _ensure_built(self) -> None:
        _build_model_caches()
        # Sync this dict's contents with the freshly-built cache.
        self.clear()
        cache = globals()[self._cache_attr]
        if cache is None:
            raise RuntimeError(f"{self._cache_attr} was not initialized")
        self.update(cache)

    # ── dict method overrides ──────────────────────────────────────────────

    def __getitem__(self, key: str) -> Any:
        self._ensure_built()
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._ensure_built()
        return super().__contains__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self._ensure_built()
        return super().get(key, default)

    def __len__(self) -> int:
        self._ensure_built()
        return super().__len__()

    def keys(self) -> Any:
        self._ensure_built()
        return super().keys()

    def values(self) -> Any:
        self._ensure_built()
        return super().values()

    def items(self) -> Any:
        self._ensure_built()
        return super().items()

    def __iter__(self) -> Any:
        self._ensure_built()
        return super().__iter__()

    def __repr__(self) -> str:
        self._ensure_built()
        return super().__repr__()


class _LazyContextWindowsDict(_LazyModelDict):
    _cache_attr = "_MODEL_CONTEXT_WINDOWS_CACHE"


class _LazyPricingDict(_LazyModelDict):
    _cache_attr = "_MODEL_PRICING_CACHE"


# Public dicts for backward compatibility — populated lazily on first access.
# These _LazyModelDict instances auto-populate from config/models.py the
# moment any key is accessed, so ``MODEL_CONTEXT_WINDOWS["default"]`` works
# even at module-import time in downstream code.

MODEL_CONTEXT_WINDOWS: dict[str, int] = _LazyContextWindowsDict()
MODEL_PRICING: dict[str, dict[str, float]] = _LazyPricingDict()


def _sync_public_dicts() -> None:
    """Synchronise the public backward-compat dicts with the cached data.

    Called automatically by _LazyModelDict on first access, but also safe
    to call explicitly if you need to force a refresh after config changes.
    """
    _build_model_caches()
    if _MODEL_CONTEXT_WINDOWS_CACHE is None:
        raise RuntimeError("Model context window cache is not initialized")
    if _MODEL_PRICING_CACHE is None:
        raise RuntimeError("Model pricing cache is not initialized")
    MODEL_CONTEXT_WINDOWS._ensure_built()  # type: ignore[attr-defined]
    MODEL_PRICING._ensure_built()  # type: ignore[attr-defined]


@dataclass
class TokenUsage:
    """Token usage for a single operation."""

    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "default"
    timestamp: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        """Estimated cost in USD."""
        pricing = _get_pricing(self.model)
        input_cost = (self.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost


# Tokenizer cache (B-2 fix, audit v13.22.0).
# The previous implementation re-attempted ``import tiktoken`` on every call
# when the first import failed, causing a 50-200ms import-failure storm under
# load. The new implementation uses a three-state sentinel:
#   _TOKENIZER_STATE == None   → not yet tried
#   _TOKENIZER_STATE == True   → tiktoken available, _tokenizer_cache holds encoding
#   _TOKENIZER_STATE == False  → tiktoken missing, use heuristic, do not re-import
# The ImportError is therefore attempted at most ONCE per process.
# <invariant>
# These two globals are ONE cache in two parts and must always be reset
# together. Nulling _tokenizer_cache while leaving _TOKENIZER_STATE == True
# produces a state this module cannot reach on its own: _get_tokenizer_cached
# skips the import (the sentinel is not None) and returns None forever, so
# tiktoken is silently disabled for the life of the process and every estimate
# falls back to the heuristic that underestimates code tokens by 30-40%.
# Two test fixtures did exactly that; see tests/test_cost_tracker.py.
# </invariant>
_TOKENIZER_STATE: bool | None = None
_tokenizer_cache = None


def _get_tokenizer_cached() -> Any | None:
    """Return tiktoken cl100k_base encoding, or None if tiktoken is unavailable.

    Caches the import result so a missing tiktoken module is attempted at most
    once per process. See ``estimate_tokens_agnostic`` for the fallback path.
    """
    global _TOKENIZER_STATE, _tokenizer_cache
    if _TOKENIZER_STATE is None:
        try:
            import tiktoken

            _tokenizer_cache = tiktoken.get_encoding("cl100k_base")
            _TOKENIZER_STATE = True
        except ImportError as exc:
            logger.debug(f"tiktoken not available, using heuristic fallback: {exc}")
            _TOKENIZER_STATE = False
            _tokenizer_cache = None
    return _tokenizer_cache if _TOKENIZER_STATE else None


def _heuristic_estimate(text: str, model: str) -> int:
    """Hybrid word/char heuristic for when tiktoken is not available.

    This is the same heuristic the prior implementation used; it's documented
    to underestimate code tokens by 30-40% (see ARCH_REPORT §3 and the
    audit at audits/golden_master_v13.22.0.md B-2). The fix is to cache the
    import failure so we don't pay the import-failure penalty on every call.
    """
    # Model-specific token density calibration
    # higher token density means more tokens per character -> smaller divisor
    density = {
        "gemma3:27b": 3.8,  # Slightly higher density than default
    }
    divisor = density.get(model, 4.0)  # Default: char/4
    num_words = len(text.split())
    num_chars = len(text)
    estimate = int(num_words * 1.3 + num_chars / divisor)
    return max(1, estimate)


def estimate_tokens_agnostic(text: str, model: str = "default") -> int:
    """Universally estimates tokens using cl100k_base as a highly accurate proxy.

    B-2 (audit v13.22.0): the tiktoken import result is cached via a
    three-state sentinel in ``_TOKENIZER_STATE`` so a missing tiktoken is
    attempted exactly once. The prior implementation re-attempted the import
    on every call, causing measurable per-call latency and import-failure
    storms under load.
    """
    if not text:
        return 0

    tokenizer = _get_tokenizer_cached()
    if tokenizer is None:
        return _heuristic_estimate(text, model)

    try:
        base_estimate = len(tokenizer.encode(text))
    except (ValueError, TypeError, UnicodeError) as _enc_exc:
        # The encoding call itself can fail on pathological input
        # (e.g. surrogate pairs). Fall back to the heuristic rather than
        # blowing up the budget check.
        return _heuristic_estimate(text, model)

    return base_estimate


@dataclass
class ContextWindowStatus:
    """Track context window utilization."""

    model: str
    context_window: int
    current_tokens: int = 0
    peak_tokens: int = 0
    warning_issued: bool = False
    critical_issued: bool = False

    @property
    def utilization(self) -> float:
        """Current utilization as fraction."""
        if self.context_window <= 0:
            return 0.0
        return self.current_tokens / self.context_window

    @property
    def utilization_percent(self) -> float:
        """Current utilization as percentage."""
        return self.utilization * 100

    @property
    def remaining_tokens(self) -> int:
        """Remaining tokens in context."""
        return max(0, self.context_window - self.current_tokens)

    def should_warn(self) -> bool:
        """Check if warning threshold reached."""
        return self.utilization >= CONTEXT_WARNING_THRESHOLD and not self.warning_issued

    def should_critical(self) -> bool:
        """Check if critical threshold reached."""
        return self.utilization >= CONTEXT_CRITICAL_THRESHOLD and not self.critical_issued

    def acknowledge_warning(self) -> None:
        """Mark warning as issued."""
        self.warning_issued = True

    def acknowledge_critical(self) -> None:
        """Mark critical as issued."""
        self.critical_issued = True


class ContextAwareCostTracker:
    """Cost tracker with proper context window tracking.

    Key fixes:
    1. Tracks cumulative context across DAG nodes
    2. Monitors context window utilization
    3. Warns at 80% threshold
    4. Supports graceful degradation
    """

    def __init__(
        self,
        budget_usd: float = 10.0,
        model: str = "",
        context_window: int | None = None,
        workflow_id: str = "default",
    ):
        self.budget_usd = budget_usd
        self.model = _resolve_default_model(model)
        self.context_window = context_window or _get_context_window(self.model)
        self.workflow_id = workflow_id

        self.usage_history: list[TokenUsage] = []
        self.node_costs: dict[str, float] = {}
        self.start_time = time.monotonic()  # audit: monotonic for elapsed intervals

        # Context window tracking
        self.context_status = ContextWindowStatus(
            model=model,
            context_window=self.context_window,
        )

        # Running totals
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost = 0.0
        self._lock = asyncio.Lock()
        # v13.21.3: Tracks the last utilization percentage published to the
        # event bus so the TUI context bar updates continuously (not only on
        # threshold crossings). Debounced to 0.5% granularity — at ~5k tokens
        # per typical LLM turn that yields 1-2 events per turn, smoothing the
        # bar without flooding the bus. See _maybe_publish_context_update().
        self._last_published_pct: float = -1.0

    async def update_context(self, input_tokens: int, output_tokens: int) -> ContextWindowStatus:
        """Update context window with new token usage.

        v13.21.3: Publish a ContextWarning event on every call (debounced to
        0.5% utilization change) so the TUI context-progress bar reflects
        current usage instead of staying stuck at 0% until the warning
        threshold is crossed. The TUI already handles ContextWarning — no
        new event type needed.
        """
        if input_tokens < 0 or output_tokens < 0:
            logger.error(f"Negative token count rejected: in={input_tokens}, out={output_tokens}")
            return self.context_status
        async with self._lock:
            self._total_input_tokens += input_tokens
            self._total_output_tokens += output_tokens

            # Running estimate of context (approximation)
            self.context_status.current_tokens = (
                self._total_input_tokens + self._total_output_tokens
            )

            if self.context_status.current_tokens > self.context_status.peak_tokens:
                self.context_status.peak_tokens = self.context_status.current_tokens

            # v13.21.3: Publish a continuous context update so the TUI bar
            # animates smoothly. Debounced to 0.1% granularity — at <1.3M
            # tokens per model call this is still orders of magnitude below
            # the per-turn rate, so we get one event per visible bar tick
            # rather than per token.
            self._maybe_publish_context_update()

            # Check thresholds
            if self.context_status.should_warn():
                logger.warning(
                    f"[CONTEXT] {self.context_status.utilization_percent:.1f}% of "
                    f"context window used ({self.context_status.current_tokens:,} / "
                    f"{self.context_status.context_window:,} tokens)"
                )
                self.context_status.acknowledge_warning()
                try:
                    from .events import ContextWarning, get_event_bus

                    get_event_bus().publish(
                        ContextWarning(
                            workflow_id=self.workflow_id,
                            node_name="",
                            utilization=self.context_status.utilization_percent,
                            current_tokens=self.context_status.current_tokens,
                            max_tokens=self.context_status.context_window,
                        )
                    )
                except ImportError as e:
                    logger.debug(f"Failed to publish ContextWarning: {e}")

            if self.context_status.should_critical():
                logger.critical(
                    f"[CONTEXT] {self.context_status.utilization_percent:.1f}% CRITICAL - "
                    f"context window nearly exhausted!"
                )
                self.context_status.acknowledge_critical()
                try:
                    from .events import ContextWarning, get_event_bus

                    get_event_bus().publish(
                        ContextWarning(
                            workflow_id=self.workflow_id,
                            node_name="",
                            utilization=self.context_status.utilization_percent,
                            current_tokens=self.context_status.current_tokens,
                            max_tokens=self.context_status.context_window,
                        )
                    )
                except ImportError as e:
                    logger.debug(f"Failed to publish ContextWarning: {e}")

            return self.context_status

    def _maybe_publish_context_update(self) -> None:
        """Publish a ContextWarning event for the TUI progress bar (debounced).

        v13.21.3: The TUI's context-progress bar previously stayed at 0%
        because ContextWarning was only fired on threshold crossings. We now
        fire on every 0.5 percentage-point change so the bar animates
        smoothly with each LLM call. The first call (initial state) always
        publishes so the bar shows the starting position rather than 0%.
        """
        util_pct = self.context_status.utilization_percent
        if self._last_published_pct >= 0.0 and abs(util_pct - self._last_published_pct) < 0.5:
            return
        self._last_published_pct = util_pct
        try:
            from .events import ContextWarning, get_event_bus

            get_event_bus().publish(
                ContextWarning(
                    workflow_id=self.workflow_id,
                    node_name="",
                    utilization=util_pct,
                    current_tokens=self.context_status.current_tokens,
                    max_tokens=self.context_status.context_window,
                )
            )
        except ImportError as e:
            logger.debug(f"Failed to publish context update: {e}")

    def check_context_available(self, additional_tokens: int) -> tuple[bool, str]:
        """Check if there's room for more tokens in context."""
        available = self.context_window - self.context_status.current_tokens

        if additional_tokens > available:
            return False, (
                f"Context limit would be exceeded: need {additional_tokens:,} "
                f"but only {available:,} available"
            )

        # Check prompt ratio
        if (
            self.context_status.current_tokens + additional_tokens
            > self.context_window * MAX_PROMPT_RATIO
        ):
            return False, (f"Prompt would exceed {MAX_PROMPT_RATIO * 100:.0f}% of context window")

        return True, "OK"

    def compress_context_if_needed(self, current_tokens: int) -> bool:
        """Check if context should be compressed."""
        if self.context_status.utilization >= CONTEXT_CRITICAL_THRESHOLD:
            logger.warning("[CONTEXT] Critical threshold reached, context compression recommended")
            return True
        return False

    async def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "default",
        node_name: str | None = None,
        tenant_id: str | None = None,
    ) -> TokenUsage:
        """Record token usage and update context tracking.

        Args:
            input_tokens: Input token count.
            output_tokens: Output token count.
            model: Model name for pricing lookup.
            node_name: Optional DAG node name for per-node cost tracking.
            tenant_id: Optional tenant ID for per-tenant budget tracking.
                       If provided, cost is also recorded via TenantBudgetTracker.

        Returns:
            TokenUsage dataclass with cost breakdown.

        """
        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )

        async with self._lock:
            self.usage_history.append(usage)
            self._total_cost += usage.cost_usd

            if node_name:
                self.node_costs[node_name] = self.node_costs.get(node_name, 0) + usage.cost_usd

        # Update context tracking
        await self.update_context(input_tokens, output_tokens)

        # Per-tenant budget tracking
        if tenant_id:
            try:
                tenant_tracker = get_tenant_budget_tracker()
                await tenant_tracker.record_spend(tenant_id, usage.cost_usd)
            except Exception:  # broad catch intentional
                logger.warning(
                    f"Tenant budget tracking unavailable for '{tenant_id}'",
                    exc_info=True,
                )

        logger.debug(
            f"Recorded usage: {input_tokens} in, {output_tokens} out (${usage.cost_usd:.6f})"
        )

        return usage

    async def estimate_from_text(
        self,
        input_text: str,
        output_text: str,
        model: str = "default",
        node_name: str | None = None,
        tenant_id: str | None = None,
    ) -> TokenUsage:
        """Estimate and record token usage from text."""
        input_tokens = estimate_tokens_agnostic(input_text, model)
        output_tokens = estimate_tokens_agnostic(output_text, model)

        return await self.record_usage(
            input_tokens, output_tokens, model, node_name, tenant_id=tenant_id
        )

    def check_budget(self, warn_threshold: float = 0.8) -> bool:
        """Check if within budget.

        .. note::
            For full lock safety in async contexts, prefer ``await check_budget_async()``.
            This sync method reads ``_total_cost`` without the lock — safe under CPython's
            GIL for a single float read, but may observe a stale value under concurrent
            ``record_usage`` calls.
        """
        total_cost = self._total_cost

        if self.budget_usd <= 0:
            return True

        if total_cost >= self.budget_usd:
            logger.error(f"Budget exceeded: ${total_cost:.4f} / ${self.budget_usd:.2f}")
            return False

        usage_fraction = total_cost / self.budget_usd if self.budget_usd > 0 else 0

        if usage_fraction >= warn_threshold:
            logger.warning(
                f"Budget warning: ${total_cost:.4f} / ${self.budget_usd:.2f} "
                f"({usage_fraction * 100:.1f}% used)"
            )
            try:
                from .events import BudgetWarning, get_event_bus

                get_event_bus().publish(
                    BudgetWarning(  # type: ignore[call-arg]
                        workflow_id=self.workflow_id,
                        current_cost=total_cost,
                        limit=self.budget_usd,
                    )
                )
            except ImportError as e:
                logger.debug(f"Failed to publish BudgetWarning: {e}")

        return True

    async def check_budget_async(self, warn_threshold: float = 0.8) -> bool:
        """Check if within budget (async, lock-safe version).

        Preferred over the sync ``check_budget`` in async contexts where
        concurrent ``record_usage`` calls may be in flight.
        """
        async with self._lock:
            total_cost = self._total_cost

        if self.budget_usd <= 0:
            return True

        if total_cost >= self.budget_usd:
            logger.error(f"Budget exceeded: ${total_cost:.4f} / ${self.budget_usd:.2f}")
            return False

        usage_fraction = total_cost / self.budget_usd if self.budget_usd > 0 else 0

        if usage_fraction >= warn_threshold:
            logger.warning(
                f"Budget warning: ${total_cost:.4f} / ${self.budget_usd:.2f} "
                f"({usage_fraction * 100:.1f}% used)"
            )
            try:
                from .events import BudgetWarning, get_event_bus

                get_event_bus().publish(
                    BudgetWarning(  # type: ignore[call-arg]
                        workflow_id=self.workflow_id,
                        current_cost=total_cost,
                        limit=self.budget_usd,
                    )
                )
            except ImportError as e:
                logger.debug(f"Failed to publish BudgetWarning: {e}")

        return True

    async def _get_total_cost(self) -> float:
        """Get total cost safely."""
        async with self._lock:
            return sum(u.cost_usd for u in self.usage_history)

    @property
    def total_tokens(self) -> int:
        """Total tokens across all operations."""
        return self._total_input_tokens + self._total_output_tokens

    @property
    def budget_remaining(self) -> float:
        """Remaining budget in USD."""
        return max(0, self.budget_usd - self._total_cost)

    @property
    def budget_exceeded(self) -> bool:
        """Whether the budget has been exceeded."""
        return self.budget_usd > 0 and self._total_cost >= self.budget_usd

    @property
    def total_cost_usd(self) -> float:
        """Total cost in USD — uses internal accumulator for sync access."""
        return self._total_cost

    def get_summary(self) -> dict[str, Any]:
        """Get a summary including context window status."""
        elapsed = time.monotonic() - self.start_time
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "total_cost_usd": round(self._total_cost, 6),
            "budget_usd": self.budget_usd,
            "budget_remaining_usd": round(self.budget_remaining, 6),
            "budget_exceeded": self.budget_exceeded,
            "operations": len(self.usage_history),
            "context_window": self.context_window,
            "context_used": self.context_status.current_tokens,
            "context_utilization": f"{self.context_status.utilization_percent:.1f}%",
            "context_peak": self.context_status.peak_tokens,
            "node_costs": {k: round(v, 6) for k, v in self.node_costs.items()},
            "elapsed_seconds": round(elapsed, 2),
        }

    def format_report(self) -> str:
        """Format a human-readable cost report."""
        summary = self.get_summary()
        lines = [
            "Context-Aware Cost Tracking Report",
            "=" * 50,
            f"Total Tokens:    {summary['total_tokens']:,}",
            f"  Input:         {summary['input_tokens']:,}",
            f"  Output:        {summary['output_tokens']:,}",
            f"Total Cost:      ${summary['total_cost_usd']:.6f}",
            f"Budget:          ${summary['budget_usd']:.2f}",
            f"Remaining:       ${summary['budget_remaining_usd']:.6f}",
            "",
            f"Context Window:  {summary['context_window']:,} tokens",
            f"Context Used:    {summary['context_used']:,} tokens",
            f"Utilization:     {summary['context_utilization']}",
            f"Peak:            {summary['context_peak']:,} tokens",
        ]

        if summary["node_costs"]:
            lines.append("")
            lines.append("Per-Node Costs:")
            for node, cost in sorted(summary["node_costs"].items()):
                lines.append(f"  {node}: ${cost:.6f}")

        return "\n".join(lines)


# ── Per-Tenant Budget Tracking ────────────────────────────────────────────────


class TenantBudgetExceeded(Exception):
    """Raised when a tenant has exceeded its budget cap."""

    def __init__(self, tenant_id: str, budget_usd: float, spent_usd: float) -> None:
        super().__init__(
            f"Budget exceeded for tenant '{tenant_id}': ${spent_usd:.4f} / ${budget_usd:.2f}"
        )


class TenantBudgetTracker:
    """Per-tenant budget tracking for multi-tenant deployments.

    Tracks spending per tenant_id against per-tenant budget caps.
    When auth is disabled, all usage goes to tenant_id="default".

    Usage:
        tracker = TenantBudgetTracker(tenants=["org-a", "org-b"])
        tracker.set_budget("org-a", 0.50)
        await tracker.record_spend("org-a", 0.01)
        summary = tracker.get_summary()
    """

    def __init__(self, tenants: list | None = None) -> None:
        """Initialise with optional tenant definitions.

        Args:
            tenants: List of Tenant dataclass instances, plain dicts with
                     "tenant_id" and "default_budget_usd" keys, or plain
                     strings (which get $0 unlimited budget).
                     If None, creates a single "default" tenant with $0 budget.

        """

        self._budgets: dict[str, float] = {}
        self._spent: dict[str, float] = {}
        self._lock = asyncio.Lock()

        if tenants:
            for t in tenants:
                tid = None
                budget = 0.0
                # Try Tenant dataclass first.
                if hasattr(t, "tenant_id") and hasattr(t, "default_budget_usd"):
                    tid = t.tenant_id
                    budget = t.default_budget_usd
                elif isinstance(t, dict):
                    tid = t.get("tenant_id", "default")
                    budget = t.get("default_budget_usd", 0.0)
                else:
                    tid = str(t)
                    budget = 0.0
                if tid:
                    self._budgets[tid] = budget
                    self._spent[tid] = 0.0

        # Always have a default tenant.
        if "default" not in self._budgets:
            self._budgets["default"] = 0.0  # 0 = unlimited
            self._spent["default"] = 0.0

    def set_budget(self, tenant_id: str, budget_usd: float) -> None:
        """Set or update a tenant's budget cap (sync, pre-loop init only).

        For runtime updates from async callers, use ``set_budget_async``.
        This method is safe only during single-threaded initialisation
        before the event loop starts.

        Args:
            tenant_id: The tenant to configure.
            budget_usd: Maximum budget in USD. 0 = unlimited.

        """
        if tenant_id not in self._spent:
            self._budgets[tenant_id] = budget_usd
            self._spent[tenant_id] = 0.0
        else:
            self._budgets[tenant_id] = budget_usd

    async def set_budget_async(self, tenant_id: str, budget_usd: float) -> None:
        """Set or update a tenant's budget cap (async, lock-safe).

        Should be used for any budget mutation after the event loop starts.
        """
        async with self._lock:
            self.set_budget(tenant_id, budget_usd)

    async def record_spend(self, tenant_id: str, cost_usd: float) -> None:
        """Record cost against a tenant.

        Auto-creates unknown tenants with budget=0 (unlimited) so attribution
        is preserved even without pre-configuration.

        Args:
            tenant_id: The tenant incurring the cost.
            cost_usd: Cost in USD to record.

        """
        async with self._lock:
            if tenant_id not in self._spent:
                logger.warning(
                    f"Auto-creating unknown tenant_id '{tenant_id}' with unlimited budget"
                )
                self._budgets[tenant_id] = 0.0
                self._spent[tenant_id] = 0.0
            self._spent[tenant_id] += cost_usd

    def check_budget(self, tenant_id: str, warn_threshold: float = 0.8) -> bool:
        """Check if tenant is within budget (sync, lock-free read).

        Args:
            tenant_id: The tenant to check.
            warn_threshold: Fraction at which to emit a warning.

        Returns:
            True if within budget, False if exceeded.

        """
        if tenant_id not in self._budgets:
            logger.warning(
                f"Unknown tenant_id '{tenant_id}' in budget check — treating as unlimited"
            )
            return True  # Auto-created tenants have unlimited budget

        budget = self._budgets.get(tenant_id, 0.0)
        spent = self._spent.get(tenant_id, 0.0)

        if budget <= 0:
            return True  # Unlimited

        if spent >= budget:
            logger.error(
                f"[TENANT] Budget exceeded for '{tenant_id}': ${spent:.4f} / ${budget:.2f}"
            )
            return False

        usage_fraction = spent / budget
        if usage_fraction >= warn_threshold:
            logger.warning(
                f"[TENANT] Budget warning for '{tenant_id}': "
                f"${spent:.4f} / ${budget:.2f} ({usage_fraction * 100:.1f}%)"
            )

        return True

    async def check_budget_async(self, tenant_id: str, warn_threshold: float = 0.8) -> bool:
        """Check if tenant is within budget (async, lock-safe)."""
        async with self._lock:
            if tenant_id not in self._budgets:
                logger.warning(
                    f"Unknown tenant_id '{tenant_id}' in async budget check — treating as unlimited"
                )
                return True  # Auto-created tenants have unlimited budget
            budget = self._budgets.get(tenant_id, 0.0)
            spent = self._spent.get(tenant_id, 0.0)

        if budget <= 0:
            return True

        if spent >= budget:
            logger.error(
                f"[TENANT] Budget exceeded for '{tenant_id}': ${spent:.4f} / ${budget:.2f}"
            )
            return False

        usage_fraction = spent / budget
        if usage_fraction >= warn_threshold:
            logger.warning(
                f"[TENANT] Budget warning for '{tenant_id}': "
                f"${spent:.4f} / ${budget:.2f} ({usage_fraction * 100:.1f}%)"
            )

        return True

    def get_remaining(self, tenant_id: str) -> float:
        """Remaining budget for a tenant. Returns -1 if unlimited."""
        if tenant_id not in self._budgets:
            logger.debug(
                f"Unknown tenant_id '{tenant_id}' in remaining query — treating as unlimited"
            )
            return -1.0  # Auto-created tenants default to unlimited
        budget = self._budgets.get(tenant_id, 0.0)
        if budget <= 0:
            return -1.0  # Unlimited
        spent = self._spent.get(tenant_id, 0.0)
        return max(0.0, budget - spent)

    def get_summary(self, tenant_id: str | None = None) -> dict[str, Any]:
        """Get budget summary for one or all tenants.

        Args:
            tenant_id: Specific tenant, or None for all.

        Returns:
            Dict with per-tenant budget, spent, remaining.

        """
        if tenant_id:
            if tenant_id not in self._budgets:
                logger.warning(f"Unknown tenant_id '{tenant_id}' in summary — returning unlimited")
                return {
                    "tenant_id": tenant_id,
                    "budget_usd": 0.0,
                    "spent_usd": 0.0,
                    "remaining_usd": "unlimited",
                    "exceeded": False,
                }
            budget = self._budgets[tenant_id]
            spent = self._spent.get(tenant_id, 0.0)
            remaining = max(0.0, budget - spent) if budget > 0 else -1.0
            return {
                "tenant_id": tenant_id,
                "budget_usd": budget,
                "spent_usd": round(spent, 6),
                "remaining_usd": round(remaining, 6) if remaining >= 0 else "unlimited",
                "exceeded": budget > 0 and spent >= budget,
            }

        all_tenants = {}
        for tid in self._budgets:
            all_tenants[tid] = self.get_summary(tid)
        return all_tenants

    def format_report(self, tenant_id: str | None = None) -> str:
        """Format a human-readable tenant budget report."""
        summary = self.get_summary(tenant_id)
        lines = [
            "Per-Tenant Budget Report",
            "=" * 50,
        ]
        if tenant_id and isinstance(summary, dict) and "tenant_id" in summary:
            s = summary
            lines += [
                f"Tenant:    {s['tenant_id']}",
                f"Budget:    ${s['budget_usd']:.2f}",
                f"Spent:     ${s['spent_usd']:.6f}",
                f"Remaining: {s['remaining_usd']}",
                f"Exceeded:  {s['exceeded']}",
            ]
        else:
            for tid, s in summary.items():
                lines.append(f"\n  {tid}:")
                lines.append(f"    Budget:    ${s['budget_usd']:.2f}")
                lines.append(f"    Spent:     ${s['spent_usd']:.6f}")
                lines.append(f"    Remaining: {s['remaining_usd']}")
        return "\n".join(lines)


# Global tenant budget tracker instance
_global_tenant_tracker: TenantBudgetTracker | None = None
_tenant_tracker_lock = threading.Lock()


def get_tenant_budget_tracker(tenants: list | None = None) -> TenantBudgetTracker:
    """Get or create the global tenant budget tracker.

    Args:
        tenants: Optional list of Tenant dataclass instances for initialisation.
                 Only used on first call; subsequent calls return the existing tracker.

    Returns:
        The global TenantBudgetTracker instance.

    """
    global _global_tenant_tracker
    if _global_tenant_tracker is None:
        with _tenant_tracker_lock:
            if _global_tenant_tracker is None:
                _global_tenant_tracker = TenantBudgetTracker(tenants=tenants)
    return _global_tenant_tracker


def reset_tenant_budget_tracker(tenants: list | None = None) -> TenantBudgetTracker:
    """Reset the global tenant budget tracker atomically."""
    global _global_tenant_tracker
    with _tenant_tracker_lock:
        _global_tenant_tracker = TenantBudgetTracker(tenants=tenants)
    return _global_tenant_tracker


# ── Global tracker instance ───────────────────────────────────────────────────

_global_tracker: ContextAwareCostTracker | None = None
_tracker_lock = threading.Lock()


def get_cost_tracker(budget_usd: float = 10.0, model: str = "") -> ContextAwareCostTracker:
    """Get or create the global cost tracker (thread-safe singleton with double-checked locking).

    Args:
        budget_usd: Budget in USD for the tracker (only used on first creation).
        model: Model name for pricing (only used on first creation).

    Returns:
        The global ContextAwareCostTracker singleton instance.

    """
    global _global_tracker
    if _global_tracker is None:
        with _tracker_lock:
            if _global_tracker is None:
                _global_tracker = ContextAwareCostTracker(budget_usd=budget_usd, model=model)
    return _global_tracker


def reset_cost_tracker(budget_usd: float = 10.0, model: str = "") -> ContextAwareCostTracker:
    """Reset the global cost tracker atomically.

    Args:
        budget_usd: Budget in USD for the new tracker.
        model: Model name for pricing in the new tracker.

    Returns:
        The new ContextAwareCostTracker instance.

    """
    global _global_tracker
    with _tracker_lock:
        _global_tracker = ContextAwareCostTracker(budget_usd=budget_usd, model=model)
    return _global_tracker


# Backward compatibility alias
CostTracker = ContextAwareCostTracker


if __name__ == "__main__":
    # Fixed missing f-strings in test block
    tracker = ContextAwareCostTracker(budget_usd=5.0, model="qwen3.5:397b")
    logger.info(f"Testing tracker for model: {tracker.model}")
    logger.info(tracker.format_report())
    logger.info(f"Context status: {tracker.context_status.utilization_percent:.1f}%")
