"""Utility modules for caching, rate limiting, tracing, etc."""

from importlib import import_module

__all__ = [
    "cache",
    "circuit_breaker",
    "ensemble",
    "env_manager",
    "field_mapping",
    "file_writer",
    "rate_limiter",
    "subprocess_pool",
    "tracing",
]


def __getattr__(name: str):
    """Lazy import utility components."""
    lazy_imports = {
        "cache": ".cache",
        "rate_limiter": ".rate_limiter",
        "circuit_breaker": ".circuit_breaker",
        "tracing": ".tracing",
        "ensemble": ".ensemble",
        "env_manager": ".env_manager",
        "subprocess_pool": ".subprocess_pool",
        "field_mapping": ".field_mapping",
        "file_writer": ".file_writer",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
