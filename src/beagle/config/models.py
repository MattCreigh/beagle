"""Centralized model configuration for Beagle v12.3.

This is the single source of truth for model metadata including context windows,
pricing, fallback chains, and task-to-model mapping. Other modules (cost_tracker,
model_resolver, etc.) should import from here rather than maintaining their own
model lists.

Generated from: MASTER_IMPROVEMENT_PLAN P2.1, plan_beagle_enterprise_upgrade.md EU1
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """Metadata for a single model."""

    name: str
    context_window: int
    provider: str
    default_temperature: float = 0.7
    max_output_tokens: int = 8000
    input_cost_per_1m: float = 1.0
    output_cost_per_1m: float = 4.0
    best_for: str = ""
    notes: str = ""


# ── Model Registry ──────────────────────────────────────────────────────────────
# v13.22.3-2026-07-27: Jul-2026 model refresh. Bare names (no :cloud suffix)
# are what Ollama Cloud accepts — verified against /api/tags on 2026-07-27.
# kimi-k3 promoted to top-tier per user direction "use kimi-k3 where high
# performance is needed, as in top top". All models below are in
# config.toml [models.allowed] (SSOT) — config/allowlist.py validates at
# startup; cost_tracker.py reads context_window / pricing from here.
MODEL_CONFIGS: dict[str, ModelInfo] = {
    # ── Top-tier (Kimi k3, Jul 2026) ──────────────────────────────────────────
    "kimi-k3:cloud": ModelInfo(
        name="kimi-k3:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=2.50,
        output_cost_per_1m=8.00,
        best_for="ORCHESTRATION",
        notes="Top-tier flagship (Jul 2026). Use where high performance is needed.",
    ),
    # ── Code-specialised Kimi (NEW Jul 2026) ──────────────────────────────────
    "kimi-k2.7-code:cloud": ModelInfo(
        name="kimi-k2.7-code:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.5,
        max_output_tokens=65536,
        input_cost_per_1m=1.50,
        output_cost_per_1m=5.00,
        best_for="CODING",
        notes="Code-specialised Kimi (NEW Jul 2026). Beats deepseek-v4-pro on Python refactor + multi-file edits per internal tests.",
    ),
    # ── Flagship GLM (NEW Jun 2026, MoE judge candidate) ───────────────────────
    "glm-5.2:cloud": ModelInfo(
        name="glm-5.2:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.80,
        output_cost_per_1m=3.00,
        best_for="ORCHESTRATION",
        notes="Flagship GLM (NEW Jun 2026). MoE architecture, strong orchestration quality.",
    ),
    # ── Deepseek-v4 family ────────────────────────────────────────────────────
    "deepseek-v4-pro:cloud": ModelInfo(
        name="deepseek-v4-pro:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=1.20,
        output_cost_per_1m=4.80,
        best_for="DEEP_ANALYSIS",
        notes="Deep analysis / coding (replaces deepseek-v3.2).",
    ),
    "deepseek-v4-flash:cloud": ModelInfo(
        name="deepseek-v4-flash:cloud",
        context_window=80_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.50,
        output_cost_per_1m=2.00,
        best_for="DEVOPS",
        notes="Fast deepseek (new tier) — DevOps / small surface.",
    ),
    "deepseek-v3.2:cloud": ModelInfo(
        name="deepseek-v3.2:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.50,
        output_cost_per_1m=1.60,
        best_for="CODING",
        notes="Legacy deepseek (kept for fallback).",
    ),
    # ── minimax family ────────────────────────────────────────────────────────
    "minimax-m3:cloud": ModelInfo(
        name="minimax-m3:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.80,
        output_cost_per_1m=2.00,
        best_for="GENERAL",
        notes="Workhorse (general agents, default profile). NEW Jun 2026.",
    ),
    "minimax-m2.7:cloud": ModelInfo(
        name="minimax-m2.7:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.80,
        output_cost_per_1m=2.00,
        best_for="CODING",
        notes="Legacy minimax (kept for fallback).",
    ),
    "minimax-m2.5:cloud": ModelInfo(
        name="minimax-m2.5:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.80,
        output_cost_per_1m=2.00,
        best_for="CODING",
    ),
    "minimax-m2:cloud": ModelInfo(
        name="minimax-m2:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.60,
        output_cost_per_1m=1.50,
        best_for="CODING",
    ),
    # ── Qwen family ───────────────────────────────────────────────────────────
    "qwen3.5:397b-cloud": ModelInfo(
        name="qwen3.5:397b-cloud",
        context_window=397_000,
        provider="ollama_cloud",
        default_temperature=0.6,
        max_output_tokens=65536,
        input_cost_per_1m=1.20,
        output_cost_per_1m=4.80,
        best_for="DEEP_ANALYSIS",
        notes="Largest context, deterministic verification.",
    ),
    "qwen3-coder:480b-cloud": ModelInfo(
        name="qwen3-coder:480b-cloud",
        context_window=480_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=1.50,
        output_cost_per_1m=5.00,
        best_for="CODING",
    ),
    "qwen3-next:80b-cloud": ModelInfo(
        name="qwen3-next:80b-cloud",
        context_window=80_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=1.00,
        output_cost_per_1m=4.00,
        best_for="REASONING",
    ),
    "qwen3:235b-cloud": ModelInfo(
        name="qwen3:235b-cloud",
        context_window=235_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=1.20,
        output_cost_per_1m=5.00,
        best_for="GENERAL",
    ),
    # ── Gemma family (fast / routing) ─────────────────────────────────────────
    "gemma4:31b-cloud": ModelInfo(
        name="gemma4:31b-cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.20,
        output_cost_per_1m=0.80,
        best_for="FAST_ROUTING",
        notes="Cheapest — use for routing, classification, compression (replaces gemma3:27b).",
    ),
    "gemma3:27b-cloud": ModelInfo(
        name="gemma3:27b-cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.20,
        output_cost_per_1m=0.80,
        best_for="FAST_ROUTING",
        notes="Legacy gemma (kept for fallback).",
    ),
    # ── Kimi legacy ───────────────────────────────────────────────────────────
    "kimi-k2.6:cloud": ModelInfo(
        name="kimi-k2.6:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=1.00,
        output_cost_per_1m=4.00,
        best_for="WRITING",
        notes="Legacy kimi (writing / synthesis, large context).",
    ),
    "kimi-k2.5:cloud": ModelInfo(
        name="kimi-k2.5:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=1.00,
        output_cost_per_1m=4.00,
        best_for="WRITING",
        notes="Legacy kimi (kept for fallback).",
    ),
    "kimi-k2-thinking:cloud": ModelInfo(
        name="kimi-k2-thinking:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.5,
        max_output_tokens=65536,
        input_cost_per_1m=1.20,
        output_cost_per_1m=5.00,
        best_for="REASONING",
        notes="Legacy chain-of-thought kimi (kept for fallback).",
    ),
    # ── GLM legacy ────────────────────────────────────────────────────────────
    "glm-5.1:cloud": ModelInfo(
        name="glm-5.1:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.50,
        output_cost_per_1m=2.00,
        best_for="ORCHESTRATION",
        notes="Legacy GLM 5.1 (kept for fallback).",
    ),
    "glm-5:cloud": ModelInfo(
        name="glm-5:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.80,
        output_cost_per_1m=3.00,
        best_for="ORCHESTRATION",
    ),
    "glm-4.7:cloud": ModelInfo(
        name="glm-4.7:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=0.60,
        output_cost_per_1m=2.00,
        best_for="GENERAL",
    ),
    # ── DevStral family (DevOps) ──────────────────────────────────────────────
    "devstral-2:123b-cloud": ModelInfo(
        name="devstral-2:123b-cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=1.00,
        output_cost_per_1m=3.00,
        best_for="DEVOPS",
    ),
    "devstral-small-2:24b-cloud": ModelInfo(
        name="devstral-small-2:24b-cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.30,
        output_cost_per_1m=1.00,
        best_for="DEVOPS",
    ),
    # ── Nemotron family ───────────────────────────────────────────────────────
    "nemotron-3-ultra:cloud": ModelInfo(
        name="nemotron-3-ultra:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,  # invariant: must stay < context_window (128_000)
        input_cost_per_1m=1.50,
        output_cost_per_1m=5.00,
        best_for="REASONING",
        notes="Deep reasoning / alt-cloud.",
    ),
    "nemotron-3-super:cloud": ModelInfo(
        name="nemotron-3-super:cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=65536,
        input_cost_per_1m=1.20,
        output_cost_per_1m=4.00,
        best_for="REASONING",
    ),
    "nemotron-3-nano:30b-cloud": ModelInfo(
        name="nemotron-3-nano:30b-cloud",
        context_window=128_000,
        provider="ollama_cloud",
        default_temperature=0.7,
        max_output_tokens=32768,
        input_cost_per_1m=0.30,
        output_cost_per_1m=1.00,
        best_for="FAST_ROUTING",
    ),
    # ── Google ────────────────────────────────────────────────────────────────
    "gemini-2.5-pro": ModelInfo(
        name="gemini-2.5-pro",
        context_window=1_000_000,
        provider="google",
        default_temperature=0.7,
        input_cost_per_1m=1.25,
        output_cost_per_1m=10.00,
        best_for="DEEP_ANALYSIS",
    ),
    "gemini-2.0-flash": ModelInfo(
        name="gemini-2.0-flash",
        context_window=1_000_000,
        provider="google",
        default_temperature=0.7,
        input_cost_per_1m=0.10,
        output_cost_per_1m=0.40,
        best_for="FAST_ROUTING",
    ),
    # ── OpenAI ────────────────────────────────────────────────────────────────
    "gpt-4o": ModelInfo(
        name="gpt-4o",
        context_window=128_000,
        provider="openai",
        default_temperature=0.7,
        input_cost_per_1m=2.50,
        output_cost_per_1m=10.00,
        best_for="GENERAL",
    ),
    "gpt-4o-mini": ModelInfo(
        name="gpt-4o-mini",
        context_window=128_000,
        provider="openai",
        default_temperature=0.7,
        input_cost_per_1m=0.15,
        output_cost_per_1m=0.60,
        best_for="FAST_ROUTING",
    ),
}

# ── Task-to-Model Mapping ──────────────────────────────────────────────────────
# v13.22.3-2026-07-27-final: kimi-k3 RESERVED for special top-tier use only.
# Default workhorse is minimax-m3; glm-5.2 for orchestration-quality
# (orchestration, planning, writing, synthesis, fact-checker,
# deep analysis, API design); kimi-k2.7-code for code-specialised
# (sota-dev, python-backend, rust-cpp-systems, etc.); nemotron-3-ultra
# for reasoning-heavy (security-auditor, ground-truth-validator);
# gemma4:31b for cheap meta tasks. kimi-k3 is in [models.allowed]
# but NOT in this default routing — opt-in only.
TASK_MODEL_MAP: dict[str, str] = {
    # Orchestration & Planning (top-tier)
    "agent-orchestrator": "glm-5.2:cloud",
    "research-planner": "glm-5.2:cloud",
    "deep-planner": "glm-5.2:cloud",
    # Coding (code-specialised Kimi primary; top-tier for sota-dev)
    "sota-dev": "kimi-k2.7-code:cloud",
    "python-backend": "kimi-k2.7-code:cloud",
    "api-designer": "glm-5.2:cloud",
    "react-frontend-dev": "kimi-k2.7-code:cloud",
    "frontend-architect": "kimi-k2.7-code:cloud",
    "db-migration-specialist": "kimi-k2.7-code:cloud",
    "rust-cpp-systems": "kimi-k2.7-code:cloud",
    "new-ai-dev": "kimi-k2.7-code:cloud",
    # Writing & Synthesis (top-tier)
    "synthesis-writer": "glm-5.2:cloud",
    "documentation-writer": "glm-5.2:cloud",
    "consulting-strategist": "glm-5.2:cloud",
    "developer-advocate": "glm-5.2:cloud",
    # Reasoning & Verification (top-tier)
    "fact-checker": "glm-5.2:cloud",
    "security-auditor": "nemotron-3-ultra:cloud",
    "ground-truth-validator": "nemotron-3-ultra:cloud",
    "architecture-auditor": "glm-5.2:cloud",
    "root-cause-analyst": "glm-5.2:cloud",
    "e2e-tester": "glm-5.2:cloud",
    # Deep Analysis (top-tier)
    "protocol-debugger": "glm-5.2:cloud",
    "performance-profiler": "glm-5.2:cloud",
    "latency-hunter": "glm-5.2:cloud",
    "memory-forensics": "glm-5.2:cloud",
    "code-profiler": "kimi-k2.7-code:cloud",
    "patent-analyst": "glm-5.2:cloud",
    # Fast/routing (cheap — gemma4:31b)
    "context-compressor": "gemma4:31b-cloud",
    "curator": "gemma4:31b-cloud",
    "self-improver": "gemma4:31b-cloud",
    "prompt-engineer": "gemma4:31b-cloud",
    "resource-optimizer": "gemma4:31b-cloud",
    "claude-integration": "gemma4:31b-cloud",
    "cli-ux-designer": "gemma4:31b-cloud",
    "ui-ux-designer": "gemma4:31b-cloud",
    "compress-context": "gemma4:31b-cloud",
    # DevOps (fast)
    "devops-pipeline-architect": "deepseek-v4-flash:cloud",
    "infrastructure": "deepseek-v4-flash:cloud",
}

# v13.20.1: Per-model fallback chains are SSOT in config.toml under
# v13.20.1: Per-model fallback chains are SSOT in config.toml under
# [models.fallback_chains]. Readers MUST go through
# `get_config().goose.fallback_chains` — do not reintroduce a Python dict here.
# (The previous MODEL_FALLBACK_CHAINS dict drifted from utils/subprocess/pool_config.py
# and core/orchestrator/system_directive.py — that drift is now structurally impossible.)

# ── Ensemble Panel ─────────────────────────────────────────────────────────────

# <invariant>
#   The ensemble panel and judge are declared ONCE, in config.toml [ensemble],
#   and reach code through config/schema.py::EnsembleConfig (populated by
#   loader.py) — read them via `get_config().ensemble`.
#
#   This module previously carried ENSEMBLE_PANEL_MODELS and
#   ENSEMBLE_JUDGE_MODEL as Python literals. Nothing imported either one, so
#   they were free to rot: the panel still named deepseek-v3.2 and
#   kimi-k2-thinking and the judge named kimi-k2.5 — all three retired
#   upstream and absent from [models.allowed] — while the live [ensemble]
#   table stayed current. Re-introducing a Python copy here would recreate
#   exactly that silent drift.
#
#   Verified by tests/test_ensemble_fallback_matches_config.py.
# </invariant>


# ── Helper Functions ───────────────────────────────────────────────────────────


def get_model_for_task(task_name: str) -> str:
    """Get the best model for a task by its recipe/agent name.

    Args:
        task_name: Agent recipe name (e.g., 'sota-dev', 'fact-checker')

    Returns:
        Model name string

    """
    return TASK_MODEL_MAP.get(task_name, "glm-5.1:cloud")


def get_context_window(model: str) -> int:
    """Get the context window size for a model.

    Args:
        model: Model name string

    Returns:
        Context window in tokens (default: 128000)

    """
    if model in MODEL_CONFIGS:
        return MODEL_CONFIGS[model].context_window
    return 128_000


def _normalize_model_name(model: str) -> str:
    """Strip date-tag suffixes from production model names.

    Ollama Cloud model names sometimes carry a date tag, e.g.
    ``deepseek-v4-flash:0731-cloud`` or ``deepseek-v4-flash:0731:cloud``.
    Map those to the canonical name used in MODEL_CONFIGS while keeping
    size tags such as ``gemma4:31b-cloud`` intact.
    """
    import re

    if ":" not in model:
        return model
    parts = model.split(":")
    if len(parts) == 3 and parts[2] in {"cloud", "local"}:
        # family:size-tag:cloud → family:cloud (drop only the date/size middle)
        return f"{parts[0]}:{parts[2]}"
    if len(parts) == 2 and re.fullmatch(r"\d+[\-:]?(cloud|local)", parts[1]):
        # family:0731-cloud → family:cloud
        return f"{parts[0]}:cloud"
    return model


def get_max_output_tokens(model: str) -> int:
    """Get the declared max output token budget for a model.

    The returned value is clamped to the model's context window so a
    misconfigured config can never emit an invalid budget that exceeds
    the context window.

    Args:
        model: Model name string.

    Returns:
        Max output tokens (default: 8000, matching the historical bridge
        default). Known models resolve to the value declared in
        MODEL_CONFIGS; unknown models resolve to 8000 so callers never
        see a zero/negative budget.

    """
    normalized = _normalize_model_name(model)
    if normalized in MODEL_CONFIGS:
        info = MODEL_CONFIGS[normalized]
        return min(info.max_output_tokens, info.context_window)
    return 8000


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate the cost of a model call.

    Args:
        model: Model name string
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens

    Returns:
        Estimated cost in USD

    """
    if model in MODEL_CONFIGS:
        info = MODEL_CONFIGS[model]
        input_cost = (input_tokens / 1_000_000) * info.input_cost_per_1m
        output_cost = (output_tokens / 1_000_000) * info.output_cost_per_1m
        return input_cost + output_cost

    # Default pricing
    return (input_tokens / 1_000_000) * 1.0 + (output_tokens / 1_000_000) * 4.0


def get_fallback_chain(model: str) -> list[str]:
    """Get the fallback chain for a model.

    Args:
        model: Primary model name

    Returns:
        Ordered list of fallback model names (excluding the primary).
        Reads from config.toml [models.fallback_chains] via the cached
        config singleton (SSOT). Returns the single-element default
        ["gemma4:31b-cloud"] when the model is not in the chain table —
        this preserves the v13.19.5 contract for callers that still expect
        a non-empty list. (v13.20.1: was reading from a Python dict; now
        reads the TOML SSOT.)

    <invariant>
    The last-resort default MUST be a model that is both in
    config.toml [models.allowed] AND live on the Ollama Cloud
    /api/tags catalogue. Until 2026-07-28 this was "gemma3:27b-cloud",
    which Ollama Cloud no longer serves (superseded by gemma4:31b) —
    so the terminal fallback was guaranteed to 404 and surface as a
    circuit-breaker trip rather than a config error. Re-verify this
    name whenever the allowlist is refreshed.
    </invariant>

    """
    from beagle.config.config import get_config

    chains = get_config().goose.fallback_chains
    if chains:
        return list(chains.get(model, ["gemma4:31b-cloud"]))
    return ["gemma4:31b-cloud"]


def get_default_pool_chain() -> list[str]:
    """Return the default pool fallback chain from TOML SSOT.

    v13.20.1: was derived from the now-deleted MODEL_FALLBACK_CHAINS dict;
    now reads [goose].default_pool_chain from config.toml via the cached
    config singleton. Falls back to the schema default if the TOML key
    is absent.
    """
    from beagle.config.config import get_config

    return list(get_config().goose.default_pool_chain)


def get_temperature(model: str) -> float:
    """Get the default temperature for a model.

    Args:
        model: Model name string

    Returns:
        Default temperature (0.0-2.0)

    """
    if model in MODEL_CONFIGS:
        return MODEL_CONFIGS[model].default_temperature
    return 0.7


def list_models_by_capability(capability: str) -> list[ModelInfo]:
    """List models suitable for a given capability.

    Args:
        capability: Capability string (e.g., 'CODING', 'WRITING', 'REASONING')

    Returns:
        List of ModelInfo objects matching the capability

    """
    capability = capability.upper()
    return [info for info in MODEL_CONFIGS.values() if capability in info.best_for.upper()]
