"""Centralized model allowlist enforcement.

SSOT for the model whitelist lives in ``config.toml`` under
``[models.allowed]`` (a TOML array of strings). All model strings that
flow into the LLM bridge, the subprocess pool, the orchestrator, or any
other code path that selects a model for an inference call MUST be
validated against this allowlist at the boundary.

Doctrine source: ``beagle_core_directives.toml [CRITICAL_ROUTING_PROTOCOL]``
and the Beagle Project Contract. The allowlist is the security perimeter
between "an LLM call is about to happen" and "the LLM call proceeds".

Why a runtime allowlist (and not just config.toml being correct):
    A typo in a Python literal, a copy-paste from a stale audit, or a
    refactor that drops the :cloud suffix can all produce a model string
    that is not in the user-specified whitelist. Without runtime
    validation, the bug surfaces only as a 404 from Ollama Cloud, after
    the agent has already paid the network round-trip cost and (worse)
    potentially hallucinated around the failure. Validating at the
    boundary turns "model not found" from a silent failure into a
    loud, early, named exception.
"""

from __future__ import annotations

import logging

try:
    import tomllib
except ImportError:  # pragma: no cover — Python 3.11+ has tomllib
    import tomli as tomllib
from ._config_path import find_config_toml as _find_config_toml

logger = logging.getLogger("Beagle.config.allowlist")


_ALLOWED_CACHE: frozenset[str] | None = None


def _load_allowed() -> frozenset[str]:
    """Load the model allowlist from config.toml [models.allowed].

    Returns a frozenset of exact model strings. The function reads the
    file directly (bypassing the cached Config object) so the allowlist
    is available even at module-import time before Config has been
    instantiated.
    """
    global _ALLOWED_CACHE
    if _ALLOWED_CACHE is not None:
        return _ALLOWED_CACHE

    config_path = _find_config_toml()
    if not config_path.is_file():
        raise RuntimeError(
            f"config.toml not found at {config_path}; model allowlist cannot be loaded."
        )
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    models = data.get("models", {})
    raw = models.get("allowed")
    # v13.21: Accept either TOML array of strings
    #     [models.allowed]
    #     allowed = ["minimax-m3:cloud", "glm-5.1:cloud"]
    # OR the table-of-booleans form
    #     [models.allowed]
    #     "minimax-m3:cloud" = true
    #     "glm-5.1:cloud"    = true
    # The table form is preferred (it sorts and greps better, and a
    # duplicate key is a hard TOML parse error). The array form is
    # supported for backwards-compatibility with hand-written configs.
    if isinstance(raw, list) and raw:
        out = [str(m).strip() for m in raw if str(m).strip()]
    elif isinstance(raw, dict) and raw:
        out = [str(k).strip() for k, v in raw.items() if v and str(k).strip()]
    else:
        raise RuntimeError(
            "config.toml [models.allowed] is required and must be either a "
            "non-empty array of model strings or a table of "
            "'<model> = true' entries. Hard-coded Python defaults are "
            "forbidden by Beagle doctrine ('config.toml is SSOT for config')."
        )
    _ALLOWED_CACHE = frozenset(out)
    return _ALLOWED_CACHE


def reload_allowlist() -> frozenset[str]:
    """Force a re-read of the allowlist from disk.

    Used by tests that mutate config.toml in-place and by any code path
    that has legitimately updated the allowlist at runtime.
    """
    global _ALLOWED_CACHE
    _ALLOWED_CACHE = None
    return _load_allowed()


def validate_against_allowlist(models: list[str], *, on_violation: str = "raise") -> list[str]:
    """Cross-validate a list of model strings against the runtime allowlist.

    Use this at any boundary where a model *chain* (e.g. the
    ``[goose].fallback_chain`` or ``[models.fallback_chains]`` entries) is
    loaded from config but has not yet been checked against the
    user-curated allowlist. This is the F6 fix — the allowlist and the
    fallback chain are two different config sections that the v13.21
    audit flagged as a "dual SSOT" risk. They are *intentionally* dual
    (the allowlist is the security perimeter; the chain is the routing
    intent), but every chain entry MUST be in the allowlist or the
    misconfiguration is silent until the LLM call returns a 404.

    Args:
        models: Candidate model strings. May be empty (returns ``[]``).
        on_violation:
            - ``"raise"`` (default) — raise :class:`ModelNotAllowedError`
              on the first violating model. The exception's
              ``.model`` and ``.allowed`` attributes identify the
              misconfiguration.
            - ``"warn"`` — log a warning naming the violators and return
              the input list unchanged. Use this for hot-reload paths
              where a misconfiguration should be visible but should not
              crash the process.
            - ``"filter"`` — return only the models that are in the
              allowlist. Use this for best-effort startup where a
              degraded chain is acceptable but a crash is not.

    Returns:
        The validated list. For ``"raise"`` and ``"warn"`` the input is
        returned unchanged (or the function raises). For ``"filter"``
        the result is a subset of the input.

    Raises:
        ModelNotAllowedError: if ``on_violation="raise"`` and any model
            is not in the allowlist.

    """
    if not models:
        return list(models)
    allowed = _load_allowed()
    violators = [m for m in models if m not in allowed]
    if not violators:
        return list(models)
    if on_violation == "raise":
        # Raise on the first violator; ModelNotAllowedError already
        # includes the full allowlist so the operator can fix the config
        # without having to dig through logs.
        raise ModelNotAllowedError(model=violators[0], allowed=sorted(allowed))
    if on_violation == "warn":
        logger.warning(
            "validate_against_allowlist: %d model(s) not in allowlist: %r. "
            "Allowed: %r. Returning input unchanged (on_violation='warn').",
            len(violators),
            violators,
            sorted(allowed),
        )
        return list(models)
    if on_violation == "filter":
        kept = [m for m in models if m in allowed]
        logger.warning(
            "validate_against_allowlist: filtered %d model(s) not in allowlist: %r. Kept: %r.",
            len(violators),
            violators,
            kept,
        )
        return kept
    raise ValueError(f"on_violation must be one of 'raise', 'warn', 'filter'; got {on_violation!r}")


def allowed_models() -> frozenset[str]:
    """Return the current allowlist (read-only frozenset)."""
    return _load_allowed()


def is_allowed(model: str) -> bool:
    """Return True if *model* is in the allowlist, False otherwise.

    Empty strings, None, and non-string inputs always return False.
    """
    if not isinstance(model, str) or not model:
        return False
    return model in _load_allowed()


def validate_model(model: str) -> str:
    """Validate *model* against the allowlist, returning the model on success.

    Raises:
        ValueError: if *model* is not a non-empty string.
        ModelNotAllowedError: if *model* is not in the allowlist.

    Returns:
        The input *model* string, unchanged, on success. The return value
        is provided for fluent use: ``client.call(model=validate_model(m))``.

    """
    if not isinstance(model, str) or not model:
        raise ValueError(f"model must be a non-empty string, got {type(model).__name__}: {model!r}")
    if not is_allowed(model):
        raise ModelNotAllowedError(model=model, allowed=sorted(allowed_models()))
    return model


class ModelNotAllowedError(RuntimeError):
    """Raised when a model string is not in the runtime allowlist.

    Distinct from ValueError so callers can catch this specifically and
    emit a structured error to the user (or fall back to a different
    model from the chain) rather than treating it as a generic argument
    error.
    """

    def __init__(self, model: str, allowed: list[str]) -> None:
        self.model = model
        self.allowed = allowed
        super().__init__(
            f"Model {model!r} is not in the Beagle model allowlist. "
            f"Allowed models: {allowed}. "
            f"To add {model!r}, edit config.toml [models.allowed] and call "
            f"allowlist.reload_allowlist()."
        )


class DeploymentNotAllowedError(RuntimeError):
    """Raised when a (provider, model) deployment is not allowlisted.

    Additive form (B9) — the string form :data:`ModelNotAllowedError` is
    preserved; this tuple form is the newer deployment-level check.
    """

    def __init__(
        self,
        deployment: object,
        reason: str,
        *,
        allowed: list[str] | None = None,
    ) -> None:
        self.deployment = deployment
        self.reason = reason
        self.allowed = allowed or []
        fqid = getattr(deployment, "fqid", str(deployment))
        super().__init__(f"Deployment {fqid!r} not allowed: {reason}")


def validate_deployment(
    deployment: object,
    providers: dict | None = None,
    *,
    strict: bool = True,
) -> bool:
    """Validate a (provider, model) deployment against the allowlist (additive, B9).

    Checks, in order:
      1. The provider is registered (in *providers* or the registry).
      2. The bare model is in the global ``[models.allowed]`` allowlist.
      3. The model is in the provider's own ``allowed_models`` (when declared).

    The **string** allowlist API is untouched — ``validate_model`` and friends
    still operate on bare model strings (B9 back-compat).

    Args:
        deployment: A ``ModelDeployment``-like object with ``.provider`` and
            ``.model`` attributes.
        providers: Optional provider map (``{name: Provider}``). When None,
            resolved lazily from the registry.
        strict:
            - ``True`` (primary) — raise ``DeploymentNotAllowedError`` on the
              first violation (fail-fast, 7.4).
            - ``False`` (fallback) — log a warning and return ``False`` on any
              violation (skip-with-warning, 7.4).

    Returns:
        ``True`` when the deployment is permitted; ``False`` when not and
        *strict* is ``False``.

    """
    model = getattr(deployment, "model", None)
    provider_name = getattr(deployment, "provider", None)
    if not isinstance(provider_name, str) or not provider_name:
        if strict:
            raise DeploymentNotAllowedError(deployment, "missing provider")
        logger.warning("validate_deployment: deployment %r has no provider; skipping", deployment)
        return False
    if not isinstance(model, str) or not model:
        if strict:
            raise DeploymentNotAllowedError(deployment, "missing model")
        logger.warning("validate_deployment: deployment %r has no model; skipping", deployment)
        return False

    # 1. provider registered
    if providers is None:
        try:
            from . import registry as _registry  # lazy, avoid import cycle

            provider_obj = _registry.get_provider(provider_name)
        except (ImportError, RuntimeError):  # pragma: no cover — registry unavailable
            provider_obj = None
    else:
        provider_obj = providers.get(provider_name)
    if provider_obj is None:
        reason = f"provider {provider_name!r} is not registered"
        if strict:
            raise DeploymentNotAllowedError(deployment, reason)
        logger.warning("validate_deployment: %s (fallback skipped)", reason)
        return False

    # 2. bare model in global allowlist
    if not is_allowed(model):
        reason = f"model {model!r} not in [models.allowed]"
        if strict:
            raise DeploymentNotAllowedError(deployment, reason, allowed=sorted(allowed_models()))
        logger.warning("validate_deployment: %s (fallback skipped)", reason)
        return False

    # 3. provider-level allowlist (when declared)
    provider_allowed = getattr(provider_obj, "allowed_models", None)
    if provider_allowed is not None and provider_allowed and model not in provider_allowed:
        reason = f"model {model!r} not in providers.{provider_name}.allowed_models"
        if strict:
            raise DeploymentNotAllowedError(deployment, reason, allowed=sorted(provider_allowed))
        logger.warning("validate_deployment: %s (fallback skipped)", reason)
        return False

    return True


__all__ = [
    "DeploymentNotAllowedError",
    "ModelNotAllowedError",
    "allowed_models",
    "is_allowed",
    "reload_allowlist",
    "validate_against_allowlist",
    "validate_deployment",
    "validate_model",
]
