"""Context management for Beagle workflow orchestration."""

# ── Auto-Hydration ──
from .auto_hydration import (
    AutoHydrationConfig,
    HydrationResult,
    auto_hydrate,
    auto_hydrate_sync,
    should_hydrate,
)

# ── CLAUDE.md Updater ──
from .claude_md_updater import update_claude_md

# ── Compressed Store ──
from .compressed_store import (
    CompressedStore,
    FoldNotFoundError,
    TurboQuantCompressor,
    get_compressed_store,
    reset_compressed_store,
)

# ── Compaction Hook ──
from .context_compaction_hook import (
    CompactionCheckpoint,
    ContextMonitor,
    ContextStatus,
    discover_context_files,
    get_monitor,
    pre_compact_check,
)

# ── Context Integration ──
from .context_integration import (
    ContextIntegration,
    FileProcessResult,
    TurboFoldResult,
    get_context_hook,
    get_context_integration,
    patch_orchestrator,
)

# ── Context Manager Hook ──
from .context_manager_hook import (
    ContentPrepareResult,
    ContextManagementHook,
    integrate_with_orchestrator,
)

# ── Context Optimizer ──
from .context_optimizer import (
    CompressionLevel,
    CompressionResult,
    ContextOptimizer,
    ContextPlan,
    ContextStrategy,
    get_optimizer,
    reset_optimizer,
)

# ── Context Preprocessor ──
from .context_preprocessor import (
    ChunkMetadata,
    ContextPreprocessor,
    FileChunkPlan,
    ProcessedFile,
    get_preprocessor,
    reset_preprocessor,
)

# ── Context Tracker ──
from .context_tracker_ext import (
    ContextSnapshot,
    ContextTrackerState,
    estimate_tokens,
    get_context_status,
    get_context_summary,
    get_tracker,
    load_session_state,
    update_tracker_from_llm_response,
)

# ── Context Window ──
from .context_window import (
    ContextAwareCostTracker,
    ContextMetrics,
    ContextWindowManager,
    get_context_manager,
    reset_context_manager,
)

# ── Embedding Adapter ──
from .embedding_adapter import (
    EmbeddingAdapter,
    get_embedding_adapter,
    reset_embedding_adapter,
)

# ── Fork Context ──
from .fork_context import ForkContext

# ── Hydration Hook ──
from .hydration_hook import (
    on_session_end,
    on_session_start,
    quick_hydration_check,
)

# ── Prompt Cache ──
from .prompt_cache import PromptCache, PromptMetadata

# ── RAG Staleness ──
from .rag_staleness import (
    RAGStalenessTracker,
    StalenessRecord,
    get_staleness_tracker,
    reset_staleness_tracker,
)

# ── Session Model ──
from .session_model import BeagleEvent, RoutedMatch, RuntimeSession

__all__ = [
    # Auto-Hydration
    "AutoHydrationConfig",
    # Session Model
    "BeagleEvent",
    # Context Preprocessor
    "ChunkMetadata",
    # Compaction Hook
    "CompactionCheckpoint",
    # Compressed Store
    "CompressedStore",
    # Context Optimizer
    "CompressionLevel",
    "CompressionResult",
    # Context Manager Hook
    "ContentPrepareResult",
    # Context Window
    "ContextAwareCostTracker",
    # Context Integration
    "ContextIntegration",
    "ContextManagementHook",
    "ContextMetrics",
    "ContextMonitor",
    "ContextOptimizer",
    "ContextPlan",
    "ContextPreprocessor",
    # Context Tracker
    "ContextSnapshot",
    "ContextStatus",
    "ContextStrategy",
    "ContextTrackerState",
    "ContextWindowManager",
    # Embedding Adapter
    "EmbeddingAdapter",
    "FileChunkPlan",
    "FileProcessResult",
    "FoldNotFoundError",
    # Fork Context
    "ForkContext",
    "HydrationResult",
    "ProcessedFile",
    # Prompt Cache
    "PromptCache",
    "PromptMetadata",
    # RAG Staleness
    "RAGStalenessTracker",
    "RoutedMatch",
    "RuntimeSession",
    "StalenessRecord",
    "TurboFoldResult",
    "TurboQuantCompressor",
    "auto_hydrate",
    "auto_hydrate_sync",
    "discover_context_files",
    "estimate_tokens",
    "get_compressed_store",
    "get_context_hook",
    "get_context_integration",
    "get_context_manager",
    "get_context_status",
    "get_context_summary",
    "get_embedding_adapter",
    "get_monitor",
    "get_optimizer",
    "get_preprocessor",
    "get_staleness_tracker",
    "get_tracker",
    "integrate_with_orchestrator",
    "load_session_state",
    # Hydration Hook
    "on_session_end",
    "on_session_start",
    "patch_orchestrator",
    "pre_compact_check",
    "quick_hydration_check",
    "reset_compressed_store",
    "reset_context_manager",
    "reset_embedding_adapter",
    "reset_optimizer",
    "reset_preprocessor",
    "reset_staleness_tracker",
    "should_hydrate",
    # CLAUDE.md Updater
    "update_claude_md",
    "update_tracker_from_llm_response",
]
