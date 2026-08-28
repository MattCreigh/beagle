"""Centralized model resolution for Beagle v13.22.4.

This is the ONE place where model selection happens.
Precedence (highest to lowest):
1. GOOSE_MODEL env var (global override)
2. phase_model (from YAML workflow phase `model:` field)
3. config.toml [models] per-recipe mapping
4. Complexity routing (trivial→cheap, complex→large)
5. config.toml [goose] default_model fallback

MODEL ASSIGNMENT (Ollama Cloud — SSOT is config.toml [model_presets]):
  preset.default          = minimax-m3:cloud      (primary workhorse)
  preset.cheap            = gemma4:31b-cloud       (fast/cheap)
  preset.orchestration    = glm-5.2:cloud          (multi-agent workflows)
  preset.coding           = kimi-k2.7-code:cloud   (code-specialised)
  preset.writing          = kimi-k2.6:cloud        (prose/synthesis)
  preset.reasoning        = nemotron-3-ultra:cloud (deep reasoning)
  preset.fact_checking    = qwen3.5:397b-cloud     (deterministic verification)
  preset.deep_analysis    = deepseek-v4-pro:cloud  (profiling/debugging)
  preset.devops           = deepseek-v4-flash:cloud (infrastructure)
  preset.meta             = gemma4:31b-cloud        (self-improvement)

agents.toml uses Jinja {{ preset.xxx }} variables that pull from
config.toml [model_presets] — change config.toml and every agent
updates automatically. No hardcoded model strings in agents.toml.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("Beagle.config.model_resolver")

# Use tomllib for Python 3.11+, fall back to tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


# Config file path — resolved via canonical get_workspace_root()
def _get_config_path() -> Path:
    from ._config_path import find_config_toml

    return find_config_toml()


def _resolve_config_path() -> Path:
    """Resolve config path — delegates to canonical resolver."""
    return _get_config_path()


CONFIG_PATH = _resolve_config_path()


# Default complexity → model mapping. v13.21: this dict is the literal-
# last-resort fallback if config.toml has no [complexity_routing] table.
# In normal operation, the table is read at first call and cached in
# ``_COMPLEXITY_FROM_CONFIG``. The fallback values are all in the
# [models.allowed] allowlist and pass validate_model at the boundary.
COMPLEXITY_MODELS: dict[str, str] = {
    "trivial": "gemma4:31b-cloud",
    "simple": "gemma4:31b-cloud",
    "normal": "minimax-m3:cloud",
    "complex": "deepseek-v4-pro:cloud",
}

_COMPLEXITY_FROM_CONFIG: dict[str, str] | None = None


def _get_complexity_models() -> dict[str, str]:
    """Read [complexity_routing] from config.toml (SSOT); fall back to the
    hardcoded dict if the table is missing or config is unreadable.

    Result is cached at module scope after first call. The cache is
    invalidated implicitly on interpreter restart; runtime config changes
    require a process restart (or an explicit ``reload_complexity()`` call
    which is currently not exposed — config.toml is read at startup only).
    """
    global _COMPLEXITY_FROM_CONFIG
    if _COMPLEXITY_FROM_CONFIG is not None:
        return _COMPLEXITY_FROM_CONFIG
    config = _load_config()
    table = config.get("complexity_routing")
    if isinstance(table, dict) and table:
        _COMPLEXITY_FROM_CONFIG = {str(k): str(v) for k, v in table.items()}
        return _COMPLEXITY_FROM_CONFIG
    return COMPLEXITY_MODELS


# Cache for loaded config
_config_cache: dict | None = None


def _load_config() -> dict:
    """Load config from TOML file."""
    global _config_cache
    if _config_cache is None:
        if CONFIG_PATH.exists() and tomllib:
            with open(CONFIG_PATH, "rb") as f:
                _config_cache = tomllib.load(f)
        else:
            _config_cache = {}
    return _config_cache


# Last-resort model name, used only when config.toml is unreadable or has no
# [model_presets] table. Defined ONCE here.
# <invariant>
#   This is the only hardcoded model-name literal permitted in the config
#   package. Before v1.0.0 the same literal was duplicated in
#   recipe_agent_bridge.py, agent_config.py and here, and the three drifted:
#   agent_config said minimax-m3 while schema.py said glm-5.1, so which model
#   a default agent got depended on which module answered first. Callers MUST
#   route through get_preset() rather than re-introducing a literal.
# </invariant>
# Provider-neutral: beagle ships NO model presets. An unset preset
# resolves to "" and callers must surface configuration guidance.
_PRESET_LAST_RESORT = ""


def get_preset(name: str = "default") -> str:
    """Resolve a model name for a preset category (plan v3: registry-backed).

    The SSOT is the ``presets/`` card directory loaded via :mod:`.registry`.
    The legacy ``config.toml [model_presets]`` table has been retired (v1.0.7);
    this accessor now resolves exclusively through the registry, with
    :data:`_PRESET_LAST_RESORT` as the only fallback.

    Args:
        name: Preset category to resolve (``default``, ``cheap``, ``coding``…).

    Returns:
        The configured BARE model name for the category.

    """
    try:
        from . import registry as _registry

        return _registry.resolve_model(name)
    except (ImportError, RuntimeError, KeyError) as exc:
        # A missing providers.toml is the NORMAL configless state — debug, not warn.
        logger.debug("registry resolve failed (%s); using last-resort default", exc)
    return _PRESET_LAST_RESORT


def resolve_model(
    phase_model: str | None = None,
    recipe_name: str | None = None,
    complexity: str = "normal",
) -> str:
    """Resolve the best model for a given context.

    Args:
        phase_model: Explicit model from workflow YAML phase definition
        recipe_name: Name of the agent recipe being used
        complexity: Task complexity assessment ("trivial" | "simple" | "normal" | "complex")

    Returns:
        Model name string suitable for Goose --model flag

    Raises:
        ModelNotAllowedError: if the resolved model is not in config.toml
            [models.allowed]. This is the security perimeter — every
            resolved model string is validated before being returned, so
            a typo or stale literal in any caller surface fails loud and
            early rather than producing a 404 from Ollama Cloud.

    """
    from .allowlist import validate_model

    config = _load_config()

    # 1. Environment override (highest priority)
    env_model = os.environ.get("GOOSE_MODEL", os.environ.get("GOOSE_DEFAULT_MODEL"))
    if env_model:
        return validate_model(env_model)

    # 2. Phase model from workflow YAML
    if phase_model:
        return validate_model(phase_model)

    # 3. Config per-recipe mapping
    if config and recipe_name:
        models = config.get("models", {})
        recipe_model = models.get(recipe_name)
        if recipe_model:
            return validate_model(recipe_model)
    # 4. Complexity-based routing
    complexity = complexity.lower()
    cm = _get_complexity_models()
    if complexity in cm:
        return validate_model(cm[complexity])

    # 5. Default from config or hardcoded fallback
    if config:
        default = config.get("goose", {}).get("default_model")
        if default:
            return validate_model(default)
    # v13.22.4: read from config.toml [model_presets].default or [goose].default_model
    # instead of hardcoding a model name that drifts on refresh.
    return validate_model(get_preset("default"))


def resolve_provider(
    phase_provider: str | None = None,
) -> str:
    """Resolve the provider for Goose.

    Args:
        phase_provider: Explicit provider from workflow YAML

    Returns:
        Provider name (e.g., "openrouter", "ollama_cloud", "google")

    """
    config = _load_config()

    # 1. Environment override
    env_provider = os.environ.get("GOOSE_PROVIDER")
    if env_provider:
        return env_provider

    # 2. Phase provider
    if phase_provider:
        return phase_provider

    # 3. Config default
    if config:
        default = config.get("goose", {}).get("provider")
        if default:
            return default
    # 4. Fleet-card SSOT (v1.0.8): the provider is owned by the active
    # preset card, never hardcoded here. OpenRouter is the default fleet.
    from .registry import resolve_provider as _registry_provider

    try:
        return _registry_provider("default")
    except (KeyError, RuntimeError, ValueError, TypeError) as _exc:
        # Registry unavailable or no default preset — fall back to the
        # default fleet provider rather than hardcoding a provider name.
        return "openrouter"


def clear_config_cache() -> None:
    """Clear the cached config (useful for testing)."""
    global _config_cache
    _config_cache = None


# ---------------------------------------------------------------------------
# Agent profile integration
# ---------------------------------------------------------------------------


def get_agent_profile(name: str = "default") -> dict:
    """Convenience bridge: resolve an agent profile via agent_config.

    This allows callers that already use model_resolver to access
    agent_config functionality without a direct dependency.

    Returns:
        Dict with 'provider', 'model', 'temperature' keys.

    """
    try:
        from beagle.config.agent_config import get_agent

        agent = get_agent(name)
        return {
            "provider": agent.provider,
            "model": agent.model,
            "temperature": agent.temperature,
            "name": agent.name,
        }
    except ImportError:
        # agent_config not available — fall back to model_resolver defaults
        return {
            "provider": resolve_provider(),
            "model": resolve_model(),
            "temperature": 0.4,
            "name": name,
        }
