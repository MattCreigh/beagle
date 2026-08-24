"""Security and communication protocols."""

from importlib import import_module

__all__ = [
    "cvcp",
]


def __getattr__(name: str):
    """Lazy import protocol components."""
    lazy_imports = {
        "cvcp": ".cvcp",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
