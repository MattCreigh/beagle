"""Context Compaction Hook and Monitor — v13.16 decomposition shim.

All behaviour lives in context/{trigger,checkpoint,rehydration}.py.
This module re-exports the public API surface.
"""

from __future__ import annotations

from .checkpoint import (
    CompactionCheckpoint,
)
from .rehydration import (
    _build_constraints_section,
    create_resume_prompt,
    discover_context_files,
    get_post_compaction_context_instructions,
    load_context_file,
)
from .trigger import (
    ContextMonitor,
    ContextStatus,
    _get_constraint_extractor,
    get_monitor,
    pre_compact_check,
)

__all__ = [
    "CompactionCheckpoint",
    "ContextMonitor",
    "ContextStatus",
    "_build_constraints_section",
    "_get_constraint_extractor",
    "create_resume_prompt",
    "discover_context_files",
    "get_monitor",
    "get_post_compaction_context_instructions",
    "load_context_file",
    "pre_compact_check",
]
