"""Model routing and task complexity assessment for Goose Agentic Workflow.

Resolves which model to use for a given recipe, with complexity-aware
upgrades for reasoning-critical agents.
"""

from __future__ import annotations

import os
import re

from .loader import get_config


def _load_raw_config() -> dict:
    """Load raw config.toml as a dict (for [model_presets] access)."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    from .allowlist import _find_config_toml

    path = _find_config_toml()
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def resolve_model(recipe_name: str) -> str:
    """Resolve the model for a given recipe.

    Precedence: GOOSE_MODEL env var > [models] per-recipe override > default_model.

    Args:
        recipe_name: Agent recipe name (e.g., "research-planner")

    Returns:
        Model name string

    """
    env_model = os.environ.get("GOOSE_MODEL")
    if env_model:
        return env_model
    # v13.22.5: source from the registry (presets.toml — the Jinja TOML SSOT),
    # not the hardcoded [models] block in config.toml. Every role resolves via
    # get_preset(); unknowns fall back to the fleet default.
    from beagle.config import registry

    return registry.resolve_model(recipe_name)


# ── Task complexity detection ───────────────────────────────────────────────────

TRIVIAL_KEYWORDS = frozenset(
    {
        "ping",
        "status",
        "hello",
        "hi ",
        "hey",
        "bye",
        "thanks",
        "thank you",
        "help",
        "what can you do",
        "list commands",
        "show help",
    }
)
COMPLEX_KEYWORDS = frozenset(
    {
        "architect",
        "redesign",
        "migrate",
        "refactor",
        "audit",
        "security",
        "performance",
        "benchmark",
        "parallel",
        "concurrent",
        "distributed",
        "multi-agent",
        "ensemble",
        "grpo",
        "cvcp",
        "protocol",
        "deep",
        "analyze",
        "comprehensive",
        "thorough",
        "fix",
        "bug",
        "error",
        "crash",
        "hang",
        "leak",
    }
)
COMPLEX_PATTERNS = (
    r"\b(migrate|refactor|architect|redesign|comprehensive|thorough)\b",
    r"\b(security|performance|benchmark|concurren)\w*",
    r"\b(multi-agent|ensemble|grpo|cvcp)\b",
    r"\b(ring buffer|lock-free|cache coher| NUMA)\b",
    r"(?s).{2000,}",  # Long queries tend to be complex
)


def assess_task_complexity(query: str) -> str:
    """Assess whether a task is trivial, normal, or complex.

    Uses keyword matching and pattern detection — no LLM needed.

    Args:
        query: The user query string

    Returns:
        "trivial", "normal", or "complex"

    """
    ql = query.lower().strip()

    # Trivial check
    if ql in TRIVIAL_KEYWORDS:
        return "trivial"
    for kw in TRIVIAL_KEYWORDS:
        if ql == kw or ql.startswith(kw + " "):
            return "trivial"

    # Complex check
    for kw in COMPLEX_KEYWORDS:
        if kw in ql:
            return "complex"

    for pat in COMPLEX_PATTERNS:
        if re.search(pat, query, re.IGNORECASE):
            return "complex"

    return "normal"


def resolve_model_for_task(
    recipe_name: str,
    query: str = "",
    complexity: str | None = None,
) -> str:
    """Resolve the best model for a recipe given task context.

    Uses complexity assessment when no explicit override is set.
    Falls back to per-recipe routing when complexity is trivial (use fast/cheap).

    Precedence:
      1. GOOSE_MODEL env var (global override)
      2. Per-recipe model override in [models] config
      3. Complexity-adjusted default (complex tasks get reasoning models)

    Args:
        recipe_name: Agent recipe name (e.g., "fact-checker")
        query: Optional user query for complexity assessment
        complexity: Pre-assessed complexity ("trivial"/"normal"/"complex"),
                   or None to auto-detect

    Returns:
        Model name string

    """
    env_model = os.environ.get("GOOSE_MODEL")
    if env_model:
        return env_model

    # v13.22.5: source all models from the registry (presets.toml — the Jinja
    # TOML SSOT). The hardcoded [models]/[model_presets] blocks in config.toml
    # are removed from the resolution path so the fleet cannot re-pick a
    # :cloud model. get_preset(role) falls back to the fleet default.
    from beagle.config import registry

    config = get_config()

    # Auto-detect complexity if not provided
    if complexity is None:
        complexity = assess_task_complexity(query)

    # For trivial tasks, always use the cheapest/fastest model preset.
    if complexity == "trivial":
        return registry.resolve_model("cheap")

    # For complex tasks, upgrade planning/reasoning agents to the reasoning preset.
    if complexity == "complex":
        from beagle.config.registry import get_preset

        upgrade_name = COMPLEX_TASK_UPGRADE.get(recipe_name, recipe_name)
        if recipe_name in COMPLEX_TASK_UPGRADE and get_preset(upgrade_name):
            return registry.resolve_model(upgrade_name)
        return registry.resolve_model(recipe_name)

    # Normal complexity — use standard routing (fleet default).
    return config.goose.default_model


# ── Per-recipe model overrides for complex tasks ───────────────────────────────

# When complexity == "complex", these agents get upgraded to the reasoning preset.
# v13.22.4: Read from config.toml [model_presets].reasoning instead of hardcoding
# a model name that drifts. The actual model is resolved at call time from config.
COMPLEX_TASK_UPGRADE: dict[str, str] = {
    "research-planner": "reasoning",
    "deep-planner": "reasoning",
    "fact-checker": "reasoning",
    "security-auditor": "reasoning",
    "architecture-auditor": "reasoning",
    "root-cause-analyst": "reasoning",
}


def resolve_model_for_complex_task(recipe_name: str) -> str:
    """Resolve model for complex tasks, upgrading reasoning agents.

    Args:
        recipe_name: Agent recipe name

    Returns:
        Upgraded model for complex tasks, or per-recipe default otherwise

    """
    env_model = os.environ.get("GOOSE_MODEL")
    if env_model:
        return env_model
    # v13.22.5: source from the registry (presets.toml — Jinja TOML SSOT).
    from beagle.config import registry

    if recipe_name in COMPLEX_TASK_UPGRADE:
        preset_name = COMPLEX_TASK_UPGRADE[recipe_name]
        return registry.resolve_model(preset_name)
    return registry.resolve_model(recipe_name)
