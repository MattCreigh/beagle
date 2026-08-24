"""Centralized, provider-agnostic agent configuration loader.

Resolves agent profiles through a deterministic fallback chain:
  1. agents.toml[<name>]  (per-agent profile, Jinja-rendered from config.toml presets)
  2. agents.toml[default] (catch-all profile)
  3. config.toml[llm]     (global LLM defaults)
  4. config.toml[goose]   (legacy section — backward compat)
  5. Hardcoded safe defaults (fail-closed)

Environment variables GOOSE_MODEL and GOOSE_PROVIDER override any resolved value.

v13.22.4: agents.toml uses Jinja {{ preset.xxx }} variables that pull from
config.toml [model_presets]. The SSOT for model names is config.toml —
change a preset there and every agent in that category updates automatically.
No model names are hardcoded in agents.toml.

This module integrates with the existing model_resolver.py — get_agent() provides
per-agent profile resolution while resolve_model() handles complexity-based routing.
For most use cases, prefer get_agent() which returns both provider and model together.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.agent_config")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentProfile:
    """Immutable agent configuration resolved from the fallback chain.

    Attributes:
        name: Agent profile name (e.g. 'planner', 'security_firewall').
        provider: LLM provider identifier (e.g. 'ollama_cloud').
        model: Model identifier for the provider (e.g. 'glm-5').
        temperature: Sampling temperature for generation.
        description: Human-readable description of the agent's role.

    """

    name: str
    provider: str
    model: str
    temperature: float = 0.4
    description: str = ""

    @property
    def litellm_model(self) -> str:
        """Return '<provider>/<model>' for LiteLLM-style routing."""
        return f"{self.provider}/{self.model}"


# ---------------------------------------------------------------------------
# Safe hardcoded defaults (last-resort — never raises)
# ---------------------------------------------------------------------------

_HARDCODED_DEFAULTS: dict[str, str] = {
    "provider": "ollama_cloud",
    "temperature": "0.4",
}


def _default_model() -> str:
    """Last-resort model name, resolved from config.toml ``[model_presets]``.

    v1.0.0: this used to be a ``"model"`` literal inside
    :data:`_HARDCODED_DEFAULTS` whose comment claimed it was read from
    config — it never was. It drifted from ``config/schema.py`` (which still
    said ``glm-5.1:cloud``), so the default model depended on which module
    answered. The read is delegated to
    :func:`beagle.config.model_resolver.get_preset`, the single accessor.

    Imported lazily: ``model_resolver`` imports this module lazily in
    ``resolve_agent_profile``, so a module-level import here would close a
    cycle.

    Returns:
        The configured default model name.

    """
    from beagle.config.model_resolver import get_preset

    return get_preset("default")


# ---------------------------------------------------------------------------
# Internal cache (populated on first call to get_agent)
# ---------------------------------------------------------------------------

_agents_cache: dict[str, dict[str, Any]] | None = None
_llm_defaults_cache: dict[str, str] | None = None


def _get_agents_toml_path() -> Path:
    """Locate agents.toml next to this module."""
    return Path(__file__).resolve().parent / "agents.toml"


def _load_agents_toml() -> dict[str, dict[str, Any]]:
    """Parse agents.toml if it exists; return empty dict otherwise."""
    global _agents_cache
    if _agents_cache is not None:
        return _agents_cache

    path = _get_agents_toml_path()
    if not path.exists():
        logger.warning("agents.toml not found at %s — using global defaults", path)
        _agents_cache = {}
        return _agents_cache

    try:
        # v13.22.4: Render Jinja templates in agents.toml before parsing.
        # agents.toml uses {{ preset.orchestration }} etc. that pull from
        # config.toml [model_presets]. This is the SSOT enforcement mechanism —
        # change config.toml and every agent model updates automatically.
        from .toml_template import load_toml_with_templates

        data = load_toml_with_templates(path)
        _agents_cache = {k: v for k, v in data.items() if isinstance(v, dict)}
        logger.debug(
            "Loaded %d agent profiles from agents.toml (Jinja-rendered)", len(_agents_cache)
        )
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        logger.error("Failed to parse agents.toml: %s — using global defaults", exc)
        _agents_cache = {}

    return _agents_cache


def _load_llm_defaults() -> dict[str, str]:
    """Load [llm] section from config.toml, falling back to [goose]."""
    global _llm_defaults_cache
    if _llm_defaults_cache is not None:
        return _llm_defaults_cache

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    # v13.22.4: use the canonical config path resolver — one SSOT.
    from ._config_path import find_config_toml

    config_path = find_config_toml()
    defaults: dict[str, str] = {}

    if config_path.exists():
        try:
            with open(config_path, "rb") as fh:
                data = tomllib.load(fh)

            # Prefer [llm] section
            if "llm" in data:
                llm = data["llm"]
                defaults["provider"] = str(llm.get("default_provider", ""))
                defaults["model"] = str(llm.get("default_model", ""))
                defaults["cheap_model"] = str(llm.get("cheap_model", ""))
                defaults["cheap_provider"] = str(llm.get("cheap_provider", ""))

            # Fall back to [goose] section for missing values
            if "goose" in data:
                g = data["goose"]
                if not defaults.get("provider"):
                    defaults["provider"] = str(g.get("provider", ""))
                if not defaults.get("model"):
                    defaults["model"] = str(g.get("default_model", ""))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.error("Failed to read config.toml for LLM defaults: %s", exc)

    _llm_defaults_cache = defaults
    return _llm_defaults_cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_agent(name: str = "default") -> AgentProfile:
    """Resolve an agent profile by *name* through the fallback chain.

    Fallback order:
      1. agents.toml[<name>]
      2. agents.toml["default"]
      3. config.toml [llm] / [goose]
      4. Hardcoded safe defaults

    Environment variables ``GOOSE_MODEL`` and ``GOOSE_PROVIDER`` override
    the resolved provider/model **after** profile resolution.

    Parameters
    ----------
    name:
        Agent profile name (e.g. ``"planner"``, ``"security_firewall"``).
        Defaults to ``"default"``.

    Returns
    -------
    AgentProfile
        Fully resolved, immutable profile.

    """
    agents = _load_agents_toml()
    llm_defaults = _load_llm_defaults()

    # --- Resolve base profile ---
    profile_data: dict[str, Any] = {}

    if name in agents:
        profile_data = dict(agents[name])
    elif "default" in agents and name != "default":
        logger.debug("Agent '%s' not in agents.toml — falling back to [default]", name)
        profile_data = dict(agents["default"])

    # --- Fill missing keys from config.toml [llm]/[goose] ---
    provider = (
        str(profile_data.get("provider", ""))
        or llm_defaults.get("provider", "")
        or _HARDCODED_DEFAULTS["provider"]
    )
    model = str(profile_data.get("model", "")) or llm_defaults.get("model", "") or _default_model()
    temperature = float(profile_data.get("temperature", _HARDCODED_DEFAULTS["temperature"]))
    description = str(profile_data.get("description", ""))

    # --- Environment variable overrides (highest priority) ---
    env_model = os.environ.get("GOOSE_MODEL")
    env_provider = os.environ.get("GOOSE_PROVIDER")
    if env_model:
        model = env_model
    if env_provider:
        provider = env_provider

    return AgentProfile(
        name=name,
        provider=provider,
        model=model,
        temperature=temperature,
        description=description,
    )


def get_cheap_agent() -> AgentProfile:
    """Return the cheapest available agent — used for security firewall, compression, etc.

    Tries ``agents.toml["security_firewall"]`` first, then ``config.toml[llm].cheap_model``.
    """
    agents = _load_agents_toml()
    if "security_firewall" in agents:
        return get_agent("security_firewall")

    llm_defaults = _load_llm_defaults()
    cheap_model = llm_defaults.get("cheap_model", "") or _default_model()
    cheap_provider = llm_defaults.get("cheap_provider", "") or _HARDCODED_DEFAULTS["provider"]

    return AgentProfile(
        name="security_firewall",
        provider=cheap_provider,
        model=cheap_model,
        temperature=0.0,
        description="Auto-resolved cheap agent for security/utility tasks",
    )


def list_agents() -> dict[str, AgentProfile]:
    """Return all agent profiles defined in agents.toml plus the default.

    Useful for introspection, CLI display, and debugging.
    """
    agents = _load_agents_toml()
    result: dict[str, AgentProfile] = {}
    for name in agents:
        if name != "default":
            result[name] = get_agent(name)
    if "default" not in result:
        result["default"] = get_agent("default")
    return result


def invalidate_cache() -> None:
    """Clear all cached config data.  Useful in tests and after config changes."""
    global _agents_cache, _llm_defaults_cache
    _agents_cache = None
    _llm_defaults_cache = None
