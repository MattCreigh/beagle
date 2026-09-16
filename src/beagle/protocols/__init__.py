"""Security and communication protocols."""

from importlib import import_module
from types import ModuleType

__all__ = [
    "cvcp",
]


def __getattr__(name: str) -> ModuleType:
    """Lazy import protocol components."""
    lazy_imports = {
        "cvcp": ".cvcp",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
