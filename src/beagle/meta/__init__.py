"""Meta-process package: steerable, observable self-regulating loops (D1).

Each meta-process tunes and observes one of Beagle's meta-level subsystems
(context folding, memory consolidation, budget enforcement, routing,
verification). The tuning knobs live in the configuration TOML; the
observations are returned as a structured report so an MCP client can read
and steer the loop without a restart.
"""

from beagle.meta.process import (
    BaseMetaProcess,
    Knob,
    MetaObservation,
    MetaProcess,
    get_process,
    list_processes,
    register,
)

__all__ = [
    "BaseMetaProcess",
    "Knob",
    "MetaObservation",
    "MetaProcess",
    "get_process",
    "list_processes",
    "register",
]
