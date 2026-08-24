"""Pool configuration and fallback-chain resolution.

Decomposed from utils/subprocess_pool.py (2026-06-03).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TypeVar

from beagle.config.config import get_config

logger = logging.getLogger("Beagle.utils.subprocess.pool_config")

# Beagle doctrine: edge-inference tuning. Honour this clamp on EVERY code path
# (including the psutil-missing fallback below), not only the happy path. A
# 32-core edge node without psutil would otherwise return 8 workers and
# oversubscribe the process table.
_EDGE_MAX_WORKERS = 4


def _get_pool_config_workers() -> int:
    """Get max workers from config, falling back to memory/CPU-based default.

    On edge nodes (< 8 GB available) the ceiling is clamped to 4 workers
    regardless of CPU count to prevent process-table exhaustion.
    """
    try:
        import psutil

        mem_gb = psutil.virtual_memory().available / (1024**3)
        cpu_count = os.cpu_count() or 4

        if mem_gb < 2:
            return min(cpu_count, 2, _EDGE_MAX_WORKERS)
        elif mem_gb < 8:
            return min(cpu_count, 4, _EDGE_MAX_WORKERS)
        else:
            return min(cpu_count * 2, 8, _EDGE_MAX_WORKERS)
    except ImportError:
        # v13.20: also honour _EDGE_MAX_WORKERS on the fallback path. Audit V5/B2.
        return min(int((os.cpu_count() or 4) * 1.5), 8, _EDGE_MAX_WORKERS)


def _get_pool_config_timeout() -> int:
    """Get default timeout from config."""
    try:
        return get_config().pool.default_timeout_seconds
    except (ImportError, AttributeError, KeyError, ValueError):  # catch: NARROWED
        return 300


_T = TypeVar("_T")


# v13.22.3: Provider fallback chain. Was hardcoded to ["openai"] on the
# assumption that Ollama Cloud's OpenAI-compatible endpoint could be
# reached via the goose `--provider openai` CLI invocation. That
# assumption silently failed on this host: no OPENAI_API_KEY / no
# OPENAI_HOST export, so every subprocess call hit api.openai.com and
# got 401, tripping the per-model circuit breaker after five tries and
# keeping the `verify` workflow permanently rejected. The actual
# provider name that the goose CLI accepts and that resolves cleanly
# without an API key on this host is `ollama_cloud`, matching the
# `[goose].provider` and `[models].default_provider` SSOT in
# config.toml.
# v13.22.5: Provider now sources from the SAME SSOT as everything else —
# [goose].provider in config.toml (openrouter). Previously this was a
# hardcoded literal, so the pool kept spinning `ollama_cloud` subprocesses
# after the fleet moved to OpenRouter. No divergence: model comes from
# _get_fallback_chain(), provider from _get_provider_chain() — both from
# config.toml.
def _get_provider_chain() -> list[str]:
    """Load the subprocess-pool provider chain from config.toml [goose].provider.

    Single source of truth for the provider name (openrouter). Falls back to
    openrouter if config is unavailable.
    """
    try:
        config = get_config()
        prov = config.goose.provider
        if prov:
            return [prov]
    except (ImportError, AttributeError, KeyError, ValueError) as exc:
        logger.warning(
            "Cannot read [goose].provider from configuration (%s); falling back to "
            "the built-in provider list ['openrouter'].",
            exc,
        )
    return ["openrouter"]


# Back-compat alias — kept so callers referencing the old constant still work.
PROVIDER_FALLBACK_CHAIN: list[str] = _get_provider_chain()


def _get_fallback_chain() -> list[str]:
    """Load the subprocess-pool default chain from config.toml.

    v13.20.1: Reads [goose].default_pool_chain from config.toml (SSOT per
    doctrine "config.toml is SSOT for config"). This is the chain the pool
    tries in order when the caller does not supply a model_override. Note
    this is distinct from [models.fallback_chains] (per-model fallback
    tables used by the orchestrator for circuit-breaker failover); this
    function returns the pool's *initial* chain, not the failover chain.
    """
    try:
        config = get_config()
        chain = list(config.goose.default_pool_chain)
        if chain:
            return chain
    except (ImportError, AttributeError, KeyError, ValueError) as exc:
        logger.warning(
            "Cannot read [goose].default_pool_chain from configuration (%s); falling "
            "back to the schema default chain.",
            exc,
        )
    # Schema default — same as GooseConfig.default_pool_chain default_factory.
    # This is a *fallback* (config.toml missing the key entirely), not a SSOT;
    # the SSOT lives in config.toml.
    return [
        "minimax-m3:cloud",
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

        db = TrackingDatabase.get_instance()
        rankings = db.query_model_rankings(
            node_type=node_type,
            min_executions=lr_config.min_executions,
        )

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

    except (
        ImportError,
        AttributeError,
        OSError,
        RuntimeError,
    ):  # catch: NARROWED  # RATIONALE=four-tuple: lazy import of pool, attribute on config schema, runtime guard on env parsing, OS errors on tmp/pid file
        return base_chain


# ── Module-level counters for metrics ────────────────────────────────────────
_pool_stats = {"completed": 0, "failed": 0, "total": 0, "fallback_used": 0}
_pool_stats_lock = threading.Lock()

# v0.3.0: Rate limit attempts with TTL eviction (was function attribute, leaked forever)
_rate_limit_attempts: dict[str, int] = {}
_rate_limit_timestamps: dict[str, float] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_TTL = 300.0  # 5 minutes
