"""Model/Provider dataclass types — a leaf module with no intra-package imports.

SP-7 (beagle-spotless-phase2): these four types were defined in
``config/schema.py``, which created the cycle
``config.registry -> config.schema -> config.model_resolver -> config.registry``
(registry imports the types from schema; schema lazily imports model_resolver
for the preset default; model_resolver lazily imports registry for resolution).

Moving them here — a leaf that imports nothing from the ``beagle`` package —
lets ``schema``, ``registry``, and ``model_resolver`` all depend on this module
without forming a cycle. The layer order becomes:

    constants  <-  schema  <-  loader  <-  config  <-  services  <-  cli
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Provider:
    """A registered LLM provider (from providers.toml).

    Auth is via ``env_key`` (the NAME of an env var) ONLY — never an inline
    secret (B6). ``allowed_models`` is the provider's half of the
    ``(provider, model)`` allowlist; policy is still enforced by
    ``allowlist.py``, this table only supplies data (B9).
    """

    name: str
    kind: str = "openai_compat"
    base_url: str | None = None
    env_key: str | None = None
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    allowed_models: set[str] = field(default_factory=set)


@dataclass
class ModelDeployment:
    """A concrete provider/model binding (primary/fallback of a role preset).

    N1: :attr:`model` is the BARE model string and is what flows into the
    ``model`` field. :attr:`fqid` is a DERIVED property for logging and for the
    additive ``validate_deployment`` tuple form — it is never a config value.
    """

    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None

    @property
    def fqid(self) -> str:
        # Mirrors AgentProfile.litellm_model shape (NB-6) — reuse, don't fork.
        return f"{self.provider}/{self.model}"

    @classmethod
    def from_dict(cls, raw: dict) -> ModelDeployment:
        return cls(
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
            temperature=raw.get("temperature"),
            max_tokens=raw.get("max_tokens"),
        )


@dataclass
class ModelPreset:
    """A ROLE preset (from presets.toml [presets.<role>]).

    v1 strategy scope = ``primary`` | ``fallback_chain`` ONLY (NB-7). The
    load-balance strategies are reserved, not implemented. ``toolset`` is
    declared but NOT enforced as an agent gate in v1 (7.6).
    """

    name: str
    primary: ModelDeployment
    fallbacks: list[ModelDeployment] = field(default_factory=list)
    strategy: str = "fallback_chain"
    temperature: float | None = None
    budget_usd: float | None = None
    toolset: set[str] = field(default_factory=set)
    description: str = ""

    def deployment_chain(self) -> list[ModelDeployment]:
        """Primary + fallbacks, in resolution order."""
        return [self.primary, *self.fallbacks]

    def effective_temperature(self) -> float | None:
        """Role default temperature, falling back to the primary deployment's."""
        if self.temperature is not None:
            return self.temperature
        return self.primary.temperature

    @classmethod
    def from_dict(cls, name: str, raw: dict) -> ModelPreset:
        primary_raw = raw.get("primary") or raw.get("model") or {}
        if isinstance(primary_raw, str):
            # Accept shorthand: primary = "provider/model" or bare "model".
            provider, _, model = primary_raw.partition("/")
            if not model:
                model = provider
                provider = "ollama_cloud"
            primary = ModelDeployment(provider=provider, model=model)
        else:
            primary = ModelDeployment.from_dict(primary_raw)

        fallbacks_raw = raw.get("fallbacks") or []
        fallbacks = [
            ModelDeployment.from_dict(f)
            if isinstance(f, dict)
            else ModelDeployment(provider="ollama_cloud", model=str(f))
            for f in fallbacks_raw
        ]
        toolset_raw = raw.get("toolset") or []
        return cls(
            name=name,
            primary=primary,
            fallbacks=fallbacks,
            strategy=str(raw.get("strategy", "fallback_chain")),
            temperature=raw.get("temperature"),
            budget_usd=raw.get("budget_usd"),
            toolset={str(t) for t in toolset_raw},
            description=str(raw.get("description", "")),
        )


@dataclass
class PresetBundle:
    """A named BUNDLE (from presets.toml [bundles.<name>]).

    ROUTING-ONLY (7.6): composes role presets by name and carries overrides.
    It must NOT gate agent topology. ``overrides`` is a plain dict of key-path
    overrides applied last during expansion (v1 keeps it structural).
    """

    name: str
    includes: list[str] = field(default_factory=list)
    overrides: dict = field(default_factory=dict)
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, raw: dict) -> PresetBundle:
        return cls(
            name=name,
            includes=list(raw.get("includes") or []),
            overrides=dict(raw.get("overrides") or {}),
            description=str(raw.get("description", "")),
        )
