"""Configuration and path management."""

from importlib import import_module
from types import ModuleType

__all__ = [
    "agent_config",
    "config",
    "defaults",
    "get_config",
    "load_config",
    "env_overrides",
    "loader",
    "model_resolver",
    "model_routing",
    "models",
    "paths",
    "registry",
    "schema",
]


def __getattr__(name: str) -> ModuleType:
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
    # Re-exported callables, not modules: the lazy table above maps to module
    # paths, so these need their own branch. `from beagle.config import
    # get_config` was an ImportError before this, and prometheus_exporter.py
    # calls it on two paths.
    reexported = {
        "get_config": ("loader", "get_config"),
        "load_config": ("loader", "load_config"),
    }
    if name in reexported:
        module_name, attr = reexported[name]
        return getattr(import_module("." + module_name, __package__), attr)
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
