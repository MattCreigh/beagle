"""Registry — single accessor + cache owner for providers/presets/bundles.

Plan v3 (card system): this is the ONE place that parses and caches the
registry files. Every other module reads through it — there are no parallel
caches and no ad-hoc ``tomllib`` reads of the registry files.

Files (all at project root beside config.toml, resolved by ``_config_path``):
    - ``providers.toml`` — [providers.<name>] registry.
    - ``presets/`` directory — card files with [presets.<role>] role presets
      and/or [bundles.<name>] bundles (one or more ``*.toml``). A legacy
      single ``presets.toml`` is still honoured as a back-compat fallback
      when ``presets/`` is absent (see :func:`_config_path.find_preset_cards`).
    - ``config.toml`` overlay — [goose].model_profile / [goose].default_role /
      [overrides] (plain TOML; no Jinja in config.toml).

Non-owning readers may call :func:`reload_registry` to invalidate the merged
cache (mirrors ``allowlist.reload_allowlist``). :func:`validate_cards` runs
inside ``reload_registry`` and fails fast on unknown provider/role references.

Resolution surface (defers to model_resolver for precedence, but returns rich
preset objects):
    - :func:`get_preset(role) -> ModelPreset`
    - :func:`get_provider(name) -> Provider`
    - :func:`get_bundle(name) -> PresetBundle`
    - :func:`resolve_model(role) -> str`  (bare model string — the N1 contract)
    - :func:`resolve_provider(role) -> str`
    - :func:`resolve_deployment(role) -> ModelDeployment`
    - :func:`active_roles() -> list[str]`
    - :func:`validate_cards() -> list[str]`  (cross-card reference errors)
    - :func:`build_template_context() -> dict`  (scalar leaves for Jinja)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ._config_path import (
    find_config_toml,
    find_preset_cards,
    find_providers_toml,
)
from .model_types import ModelDeployment, ModelPreset, PresetBundle, Provider

logger = logging.getLogger("Beagle.config.registry")


def _load_toml(path: Path) -> dict:
    """Parse a TOML file (plain tomllib). Registry files are Jinja-free."""
    import tomllib

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:  # pragma: no cover — callers validate existence
        logger.debug("Registry file not found: %s", path)
        return {}
    return data


# ---------------------------------------------------------------------------
# Cache (single owner)
# ---------------------------------------------------------------------------


class _RegistryStore:
    """Holds the merged registry state; invalidated atomically via reload."""

    def __init__(self) -> None:
        self.providers: dict[str, Provider] = {}
        self.presets: dict[str, ModelPreset] = {}
        self.bundles: dict[str, PresetBundle] = {}
        self._ctx: dict[str, Any] | None = None


class _EmptyRoleView:
    """Neutral stand-in for an unconfigured preset role.

    Renders as the EMPTY STRING in Jinja ({{ preset.meta }} -> "") while
    still exposing scalar attributes for dotted access.
    """

    model = ""
    provider = ""
    temperature = 0.4
    fqid = ""
    strategy = ""

    def __str__(self) -> str:
        return ""


class _EmptyRoleDict(dict):
    """dict that yields a neutral preset view for unknown roles.

    Configless contract: ``{{ preset.anything }}`` renders empty scalars
    (model="", provider="") rather than raising StrictUndefined.
    """

    def __missing__(self, key: str) -> Any:
        return _EmptyRoleView()


class _EmptyFallbackDict(dict):
    """dict that yields an empty chain list for unknown roles."""

    def __missing__(self, key: str) -> list[dict[str, Any]]:
        return []


_store = _RegistryStore()


def _parse_providers(data: dict) -> dict[str, Provider]:
    raw = data.get("providers") or {}
    out: dict[str, Provider] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        out[name] = Provider(
            name=str(name),
            kind=str(spec.get("kind", "openai_compat")),
            base_url=spec.get("base_url"),
            env_key=spec.get("env_key"),
            requests_per_minute=spec.get("requests_per_minute"),
            tokens_per_minute=spec.get("tokens_per_minute"),
            allowed_models={str(k) for k, v in (spec.get("allowed_models") or {}).items() if v},
        )
    return out


def _parse_presets(data: dict) -> tuple[dict[str, ModelPreset], dict[str, PresetBundle]]:
    raw_presets = data.get("presets") or {}
    presets: dict[str, ModelPreset] = {}
    for name, spec in raw_presets.items():
        if isinstance(spec, dict):
            presets[str(name)] = ModelPreset.from_dict(str(name), spec)

    raw_bundles = data.get("bundles") or {}
    bundles: dict[str, PresetBundle] = {}
    for name, spec in raw_bundles.items():
        if isinstance(spec, dict):
            bundles[str(name)] = PresetBundle.from_dict(str(name), spec)
    return presets, bundles


def reload_registry() -> None:
    """Re-read providers.toml + preset cards and rebuild the merged cache.

    Providers come from the single ``providers.toml``. Presets and bundles
    come from the ``presets/`` card directory (one or more ``*.toml`` files),
    falling back to the legacy single ``presets.toml`` when the directory is
    absent (see :func:`_config_path.find_preset_cards`).

    When two cards define the same role or bundle, the later-loaded card
    replaces it wholesale (a WARNING is logged naming both the role and the
    card file, so the operator knows which card won).

    Builds into local variables first and assigns to ``_store`` only after
    validation passes, so a mid-load failure leaves the previous state intact
    (F4).
    """
    global _store

    providers_path = find_providers_toml()
    card_paths = find_preset_cards()

    # Configless is a NORMAL state in the open-source distribution: no
    # providers.toml / preset cards simply means zero presets and an empty
    # provider table — resolve_model() falls back to "" and callers surface
    # configuration guidance. Only MALFORMED files are errors.
    if not providers_path.is_file():
        logger.info(
            "providers.toml not found at %s — model registry starts empty "
            "(provider-neutral install); presets activate once configured",
            providers_path,
        )
        _store = _RegistryStore()
        return
    if not card_paths:
        logger.info(
            "No preset cards found (presets/ directory or presets.toml) — "
            "model registry starts empty; presets activate once configured"
        )
        _store = _RegistryStore()
        return

    # Build into locals — _store is untouched until validation passes (F4).
    providers_data = _load_toml(providers_path)
    new_providers = _parse_providers(providers_data)

    merged_presets: dict[str, ModelPreset] = {}
    merged_bundles: dict[str, PresetBundle] = {}
    for card_path in card_paths:
        data = _load_toml(card_path)
        presets, bundles = _parse_presets(data)
        for name, preset in presets.items():
            if name in merged_presets:
                logger.warning(
                    "preset role '%s' in %s overrides a previous definition",
                    name,
                    card_path.name,
                )
            merged_presets[name] = preset
        for name, bundle in bundles.items():
            if name in merged_bundles:
                logger.warning(
                    "bundle '%s' in %s overrides a previous definition",
                    name,
                    card_path.name,
                )
            merged_bundles[name] = bundle

    # Validate against the new locals before committing to _store (F4/F5).
    errors = validate_cards(providers=new_providers, presets=merged_presets, bundles=merged_bundles)
    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(f"Preset card validation failed: {detail}")

    # Atomic assignment — _store transitions from old to new in one step.
    _store.providers = new_providers
    _store.presets = merged_presets
    _store.bundles = merged_bundles
    _store._ctx = None  # invalidate rendered context

    logger.debug(
        "Registry loaded from %d card(s): %d providers, %d presets, %d bundles",
        len(card_paths),
        len(_store.providers),
        len(_store.presets),
        len(_store.bundles),
    )


def _ensure_loaded() -> None:
    if not _store.providers:
        reload_registry()


# ---------------------------------------------------------------------------
# Cross-card validation (plan v3) — every referenced provider exists in
# providers.toml and every bundle's `includes` reference a real role.
# Fail fast at load time so a broken card cannot silently default a role.
# ---------------------------------------------------------------------------


def validate_cards(
    *,
    providers: dict[str, Provider] | None = None,
    presets: dict[str, ModelPreset] | None = None,
    bundles: dict[str, PresetBundle] | None = None,
) -> list[str]:
    """Validate the loaded card store and return a list of cross-card errors.

    Checks:
      1. Every role preset's ``primary.provider`` and each ``fallback.provider``
         names a registered provider in ``providers.toml``.
      2. Every bundle's ``includes`` names a registered role preset.

    Does NOT validate the model allowlist — that is :mod:`.allowlist`'s job
    (data-vs-policy separation, B9).

    Args:
        providers: Override the provider map (defaults to ``_store.providers``).
            Used by :func:`reload_registry` to validate before committing.
        presets: Override the preset map (defaults to ``_store.presets``).
        bundles: Override the bundle map (defaults to ``_store.bundles``).

    Returns:
        A (possibly empty) list of human-readable error strings. Unknown
        references are reported, not raised; callers decide whether to fail
        fast (``reload_registry``) or merely surface them.

    """
    errors: list[str] = []
    provider_map = providers if providers is not None else _store.providers
    preset_map = presets if presets is not None else _store.presets
    bundle_map = bundles if bundles is not None else _store.bundles
    provider_names = set(provider_map)

    for role, preset in preset_map.items():
        for label, dep in (
            ("primary", preset.primary),
            *[(f"fallback[{i}]", d) for i, d in enumerate(preset.fallbacks)],
        ):
            if dep.provider not in provider_names:
                errors.append(
                    f"role '{role}' {label} references unknown provider "
                    f"'{dep.provider}' (have: {sorted(provider_names)})"
                )

    for bundle_name, bundle in bundle_map.items():
        for role in bundle.includes:
            if role not in preset_map:
                errors.append(
                    f"bundle '{bundle_name}' includes unknown role '{role}' "
                    f"(have: {sorted(preset_map)})"
                )

    return errors


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


def get_provider(name: str) -> Provider | None:
    _ensure_loaded()
    return _store.providers.get(name)


def get_preset(role: str) -> ModelPreset | None:
    _ensure_loaded()
    return _store.presets.get(role)


def get_bundle(name: str) -> PresetBundle | None:
    _ensure_loaded()
    return _store.bundles.get(name)


def preset_names() -> list[str]:
    _ensure_loaded()
    return list(_store.presets)


def bundle_names() -> list[str]:
    _ensure_loaded()
    return list(_store.bundles)


def resolve_deployment(role: str) -> ModelDeployment:
    """Return the primary deployment for a role (validate against allowlist)."""
    from .allowlist import validate_deployment

    preset = get_preset(role)
    if preset is None:
        # Fall back to the 'default' role, then to a hard last resort.
        preset = get_preset("default")
    if preset is None:
        raise KeyError(f"no role preset named '{role}' and no 'default' role present")
    validate_deployment(preset.primary, providers=_store.providers)
    return preset.primary


def resolve_model(role: str) -> str:
    """Resolve the BARE model string for a role (N1: model is never an fqid)."""
    return resolve_deployment(role).model


def resolve_provider(role: str) -> str:
    """Resolve the provider name for a role."""
    return resolve_deployment(role).provider


def fallback_models(role: str, include_primary: bool = True) -> list[str]:
    """Ordered bare-model fallback chain for a role (primary first).

    7.4: a fallback whose provider has no configured key is **skipped with a
    warning** (a fallback must never take the whole call down); the primary is
    always returned (its eligibility is enforced via :func:`resolve_deployment`).
    Each retained fallback is also checked against the allowlist (non-strict).
    """
    from .allowlist import validate_deployment

    preset = get_preset(role) or get_preset("default")
    if preset is None:
        return []
    chain = preset.deployment_chain()
    if not include_primary:
        chain = chain[1:]

    out: list[str] = []
    primary_provider = _store.presets.get(role, get_preset("default"))
    primary_provider_name = (
        primary_provider.primary.provider if primary_provider is not None else ""
    )
    for i, d in enumerate(chain):
        if i > 0:
            provider = _store.providers.get(d.provider)
            # 7.4: only suppress a fallback whose PROVIDER DIFFERS from the
            # primary and whose key is genuinely absent. Same-provider chains
            # (v1: all ollama_cloud) share the primary's key and must not be
            # dropped just because the key isn't exported in the current shell.
            cross_provider = provider is not None and d.provider != primary_provider_name
            if (
                cross_provider
                and provider is not None
                and provider.env_key
                and not os.environ.get(provider.env_key)
            ):
                logger.warning(
                    "fallback %d for role '%s': provider '%s' has env_key %s unset; skipping",
                    i,
                    role,
                    d.provider,
                    provider.env_key,
                )
                continue
            if not validate_deployment(d, providers=_store.providers, strict=False):
                continue
        out.append(d.model)
    return out


# ---------------------------------------------------------------------------
# Bundles / config overlay
# ---------------------------------------------------------------------------


def _config_overlay() -> dict:
    """Read the plain-TOML config.toml overlay fields used by the registry."""
    data = _load_toml(find_config_toml())
    goose = data.get("goose") or {}
    overrides = data.get("overrides") or {}
    return {
        "model_profile": goose.get("model_profile"),
        "default_role": goose.get("default_role", "default"),
        "overrides": (overrides if isinstance(overrides, dict) else {}),
    }


def active_bundle() -> str | None:
    """Name of the active bundle from config.toml [goose].model_profile."""
    return _config_overlay().get("model_profile")


def active_roles() -> list[str]:
    """Roles enabled by the active profile/bundle (or all presets when none)."""
    overlay = _config_overlay()
    profile = overlay.get("model_profile")
    if profile and profile in _store.bundles:
        bundle = _store.bundles[profile]
        return list(bundle.includes)
    # No bundle configured: every role preset is available.
    _ensure_loaded()
    return list(_store.presets)


def resolve_bundle(name: str) -> dict[str, ModelPreset]:
    """Expand a bundle into a concrete ``{role: ModelPreset}`` map."""
    from ._merge import expand_bundle

    _ensure_loaded()
    bundle = _store.bundles.get(name)
    if bundle is None:
        raise KeyError(f"unknown bundle '{name}' (have: {sorted(_store.bundles)})")
    return expand_bundle(
        bundle.name,
        bundle.includes,
        bundle.overrides,
        role_resolver=lambda role: get_preset(role),
    )


# ---------------------------------------------------------------------------
# Jinja context (scalar leaves ONLY — B1/N1; no env values — B6)
# ---------------------------------------------------------------------------


class _PresetRoleView:
    """Back-compat view of a role preset for Jinja consumers.

    - ``{{ preset.<role> }}`` renders the BARE model string (byte-identical to
      today's flat ``[model_presets]`` string) — B1-safe: ``__str__`` returns
      the model, never a ``repr``.
    - ``{{ preset.<role>.model | .provider | .temperature }}`` exposes the
      scalar leaves for the upgraded agents.toml (N1).
    """

    __slots__ = ("_fqid", "_model", "_provider", "_strategy", "_temperature")

    def __init__(
        self, model: str, provider: str, temperature: float, fqid: str, strategy: str
    ) -> None:
        self._model = model
        self._provider = provider
        self._temperature = temperature
        self._fqid = fqid
        self._strategy = strategy

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def fqid(self) -> str:
        return self._fqid

    @property
    def strategy(self) -> str:
        return self._strategy

    def __str__(self) -> str:
        # B1/N1: rendering the role yields the bare model string, NOT an object repr.
        return self._model

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"<PresetRole {self._provider}/{self._model}>"


def build_template_context() -> dict[str, Any]:
    """Build the render context for consumer templates (agents.toml, YAML).

    Exposes flat, scalar leaves — never whole preset objects (B1). The ``model``
    value is the BARE model string, never an fqid (N1). ``env_flags`` carries
    booleans only, never secret values (B6).
    """
    _ensure_loaded()
    if _store._ctx is not None:
        return _store._ctx

    presets_ctx: dict[str, _PresetRoleView] = {}
    fallback_ctx: dict[str, list[dict[str, Any]]] = {}
    for role, preset in _store.presets.items():
        prim = preset.primary
        presets_ctx[role] = _PresetRoleView(
            model=prim.model,  # BARE model (N1)
            provider=prim.provider,
            temperature=preset.effective_temperature() or 0.4,
            fqid=prim.fqid,  # derived — for logging/tests, NOT model field
            strategy=preset.strategy,
        )
        fallback_ctx[role] = [
            {"provider": d.provider, "model": d.model, "fqid": d.fqid}
            for d in preset.deployment_chain()
        ]

    if not presets_ctx:
        # Configless registry: templates referencing {{ preset.<role> }} must
        # render provider-neutral empties instead of raising StrictUndefined.
        presets_ctx = _EmptyRoleDict()
    if not fallback_ctx:
        fallback_ctx = _EmptyFallbackDict()
    providers_ctx: dict[str, dict[str, Any]] = {}
    for name, prov in _store.providers.items():
        providers_ctx[name] = {
            "kind": prov.kind,
            "base_url": prov.base_url or "",
            "has_key": bool(prov.env_key and os.environ.get(prov.env_key)),
            "requests_per_minute": prov.requests_per_minute,
            "tokens_per_minute": prov.tokens_per_minute,
        }

    bundles_ctx: dict[str, dict[str, Any]] = {}
    for name, bundle in _store.bundles.items():
        bundles_ctx[name] = {
            "includes": list(bundle.includes),
            "budget_usd": bundle.overrides.get("budget_usd"),
            "description": bundle.description,
        }

    ctx: dict[str, Any] = {
        "preset": presets_ctx,
        "provider": providers_ctx,
        "fallback": fallback_ctx,
        "bundle": bundles_ctx,
        # B6: booleans only — never expose secret values to templates.
        "env_flags": {
            "has_ollama_cloud_key": bool(os.environ.get("OLLAMA_CLOUD_API_KEY")),
            "has_goose_model": bool(os.environ.get("GOOSE_MODEL")),
        },
    }
    _store._ctx = ctx
    return ctx


__all__ = [
    "active_bundle",
    "active_roles",
    "build_template_context",
    "bundle_names",
    "fallback_models",
    "get_bundle",
    "get_preset",
    "get_provider",
    "preset_names",
    "reload_registry",
    "resolve_bundle",
    "resolve_deployment",
    "resolve_model",
    "resolve_provider",
    "validate_cards",
]
