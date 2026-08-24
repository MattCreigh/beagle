"""Configuration and path management."""

from importlib import import_module

__all__ = [
    "agent_config",
    "config",
    "defaults",
    "env_overrides",
    "loader",
    "model_resolver",
    "model_routing",
    "models",
    "paths",
    "registry",
    "schema",
]


def __getattr__(name: str):
    """Lazy import config components."""
    lazy_imports = {
        "config": ".config",
        "paths": ".paths",
        "model_resolver": ".model_resolver",
        "models": ".models",
        "agent_config": ".agent_config",
        "schema": ".schema",
        "loader": ".loader",
        "env_overrides": ".env_overrides",
        "model_routing": ".model_routing",
        "registry": ".registry",
        "defaults": ".defaults",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
