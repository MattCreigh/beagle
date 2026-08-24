"""Command-line interface entry points."""

from importlib import import_module

__all__ = [
    "cli",
    "cli_graceful_shutdown",
]


def __getattr__(name: str):
    """Lazy import CLI components."""
    lazy_imports = {
        "cli": ".cli",
        "cli_graceful_shutdown": ".cli_graceful_shutdown",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
