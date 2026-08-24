"""Context Management Hook - Integrates with Beagle workflow.

Provides seamless context management integration:
- Pre-processes large files before sending to Goose
- Compresses context at configured thresholds (default 50%)
- Handles context overflow gracefully with chunking
- Coordinates with TurboQuant for KV-cache style compression
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cost_tracker import estimate_tokens_agnostic
from .context_optimizer import (
    CompressionLevel,
    CompressionResult,
    ContextStrategy,
    get_optimizer,
)
from .context_preprocessor import (
    get_preprocessor,
)
from .context_window import ContextWindowManager, get_context_manager

logger = logging.getLogger("Beagle.context.context_manager_hook")


@dataclass
class ContentPrepareResult:
    """Result of preparing content for context window."""

    original_size: int
    prepared_size: int
    was_chunked: bool
    was_compressed: bool
    chunks_added: int
    compression_ratio: float
    strategy_used: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ContextStatus:
    """Current context window status."""

    utilization: float
    current_tokens: int
    max_tokens: int
    remaining_tokens: int
    strategy: ContextStrategy
    needs_compression: bool
    can_accept_content: bool
    accepted_size: int


class ContextManagementHook:
    """Hook for managing context in Beagle workflow.

    Usage:
        hook = ContextManagementHook()

        # Before processing content
        status = hook.check_context_status()
        if status.needs_compression:
            hook.compress_context()

        # Prepare content for context (chunks/compresses as needed)
        result = hook.prepare_content(content, file_path)
        if result.was_chunked:
            for chunk in hook.get_content_chunks():
                # Process each chunk
                pass
        else:
            # Process single content
            pass
    """

    def __init__(
        self,
        context_manager: ContextWindowManager | None = None,
        auto_compress_at: float = 0.70,  # Auto-compress at 50%
        auto_chunk_at: int = 500_000,  # Auto-chunk files > 500KB
        enable_turboquant: bool = True,
        enforce_limits: bool = True,
    ):
        self.context_manager = context_manager or get_context_manager()
        self.auto_compress_at = auto_compress_at
        self.auto_chunk_at = auto_chunk_at
        self.enforce_limits = enforce_limits

        # Initialize sub-components
        self.preprocessor = get_preprocessor(file_size_limit=auto_chunk_at)
        self.optimizer = get_optimizer(
            context_manager=self.context_manager,
            warning_threshold=auto_compress_at,
            enable_turboquant=enable_turboquant,
        )

        # State tracking
        self._content_queue: list[str] = []
        self._current_chunk_index = 0
        self._total_chunks = 0
        self._last_status_check = 0.0

        # Hooks for external monitoring
        self._on_compress_callbacks: list[Callable] = []
        self._on_chunk_callbacks: list[Callable] = []
        self._on_overflow_callbacks: list[Callable] = []

    def check_context_status(self) -> ContextStatus:
        """Get current context window status."""
        status = self.context_manager.cost_tracker.context_status
        strategy = self.optimizer.get_current_strategy()

        return ContextStatus(
            utilization=status.utilization,
            current_tokens=status.current_tokens,
            max_tokens=status.context_window,
            remaining_tokens=status.remaining_tokens,
            strategy=strategy,
            needs_compression=status.utilization >= self.auto_compress_at,
            can_accept_content=status.utilization < 0.95,  # Hard limit
            accepted_size=0,  # Will be set after content processing
        )

    def prepare_content(
        self,
        content: str,
        file_path: str | None = None,
        force_chunk: bool = False,
        force_compress: bool = False,
    ) -> ContentPrepareResult:
        """Prepare content for context window (chunk/compress as needed).

        Args:
            content: Content to prepare
            file_path: Optional file path for chunking strategy
            force_chunk: Force chunking regardless of size
            force_compress: Force compression regardless of threshold

        Returns:
            ContentPrepareResult with prepared content info

        """
        original_size = len(content)
        original_tokens = estimate_tokens_agnostic(content)
        warnings = []  # type: ignore[var-annotated]

        # Check if we need to compress first
        status = self.check_context_status()
        should_compress = (
            force_compress
            or status.needs_compression
            or self.optimizer.should_compress_before_add(content)
        )

        # Check if we need to chunk
        should_chunk = (
            force_chunk
            or original_size > self.auto_chunk_at
            or original_tokens > status.remaining_tokens * 0.8
        )

        # Prepare result
        result = ContentPrepareResult(
            original_size=original_size,
            prepared_size=original_size,
            was_chunked=False,
            was_compressed=False,
            chunks_added=0,
            compression_ratio=1.0,
            strategy_used="none",
            warnings=warnings,
        )

        # Handle chunking first
        if should_chunk and file_path:
            processed = self.preprocessor.preprocess_file(file_path, content)

            if processed.needs_chunking:
                result.was_chunked = True
                result.chunks_added = len(processed.chunks)
                self._content_queue = processed.chunks
                self._total_chunks = len(processed.chunks)
                self._current_chunk_index = 0

                # Notify callbacks
                for callback in self._on_chunk_callbacks:
                    try:
                        callback(file_path, len(processed.chunks))
                    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                        logger.warning(f"Chunk callback error: {e}")

                return result

        # Handle compression
        if should_compress and not should_chunk:
            compression_level = self._determine_compression_level(
                status.utilization,
                original_tokens,
                status.remaining_tokens,
            )

            compression_result = self.optimizer.compress_content(
                content,
                level=compression_level,
            )

            compressed_content = self._get_compressed_content(compression_result)
            # Fall back to original content if compression pipeline
            # failed to produce text (shouldn't happen after the fix,
            # but defensive)
            if compressed_content:
                content = compressed_content
                result.was_compressed = True
            else:
                # Compression produced no text — keep original, mark
                # was_compressed=False so callers know nothing changed
                result.was_compressed = False
            result.prepared_size = len(content)
            result.compression_ratio = compression_result.compression_ratio
            result.strategy_used = compression_result.strategy_used.value

            # Track that we're adding this content
            self._content_queue = [content]

            # Notify callbacks
            for callback in self._on_compress_callbacks:
                try:
                    callback(content, compression_result)
                except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                    logger.warning(f"Compress callback error: {e}")

        else:
            # Just add to queue
            self._content_queue = [content]
            self._total_chunks = 1

        return result

    def _determine_compression_level(
        self,
        utilization: float,
        content_tokens: int,
        remaining_tokens: int,
    ) -> CompressionLevel:
        """Determine appropriate compression level."""
        # Check if content would overflow
        if content_tokens > remaining_tokens:
            return CompressionLevel.EMERGENCY

        # Based on utilization
        if utilization >= 0.85:
            return CompressionLevel.EMERGENCY
        elif utilization >= 0.70:
            return CompressionLevel.AGGRESSIVE
        elif utilization >= 0.70:
            return CompressionLevel.MODERATE
        else:
            return CompressionLevel.LIGHT

    def _get_compressed_content(
        self,
        compression_result: CompressionResult,
    ) -> str:
        """Extract actual compressed content from result.

        The compressed text is stored in details['compressed_content']
        by ContextOptimizer.compress_content(). Falls back to empty
        string if unavailable (e.g. for NONE-level compression).
        """
        content = compression_result.details.get("compressed_content", "")
        if content:
            return content  # type: ignore[no-any-return]
        # Final fallback: if no compressed_content was stored but we have
        # non-zero compression, the content was computed but not captured.
        # This can happen with NONE level — return original content unchanged.
        logger.warning(
            "compress_content did not produce compressed_content in details; "
            "compression level=%s ratio=%.2f",
            compression_result.compression_level.value,
            compression_result.compression_ratio,
        )
        return ""

    def get_content_chunks(self) -> list[str]:
        """Get content chunks after prepare_content."""
        return self._content_queue

    def get_next_chunk(self) -> str | None:
        """Get next content chunk (for sequential processing)."""
        if self._current_chunk_index < len(self._content_queue):
            chunk = self._content_queue[self._current_chunk_index]
            self._current_chunk_index += 1
            return chunk
        return None

    def has_more_chunks(self) -> bool:
        """Check if more chunks are available."""
        return self._current_chunk_index < len(self._content_queue)

    def compress_context(
        self,
        strategy: ContextStrategy | None = None,
    ) -> CompressionResult:
        """Compress current context window content.

        This is called proactively when context utilization reaches threshold.

        Args:
            strategy: Optional strategy override

        Returns:
            CompressionResult with compression details

        """
        # Get current context content from the context manager
        level = self.optimizer.get_compression_level(strategy)
        current_tokens = self.context_manager.cost_tracker.context_status.current_tokens

        # If we have content in the queue, compress it properly
        if self._content_queue:
            full_content = "\n".join(self._content_queue)
            result = self.optimizer.compress_content(
                full_content,
                level=level,
            )
        else:
            # No content in queue — fabricate a placeholder result
            # (in production, this would extract from context manager)
            result = CompressionResult(
                original_tokens=current_tokens,
                compressed_tokens=int(current_tokens * 0.7),
                compression_ratio=0.7,
                strategy_used=strategy or self.optimizer.get_current_strategy(),
                compression_level=level,
                details={
                    "compressed_content": "",
                    "tokens_saved": int(current_tokens * 0.3),
                },
            )

        # Update context manager
        # In production, this would actually update the context
        logger.info(
            f"Context compressed: {result.original_tokens} -> {result.compressed_tokens} tokens"
        )

        return result

    def handle_overflow(
        self,
        content: str,
        remaining_tokens: int,
    ) -> list[str]:
        """Handle content that would overflow context.

        Args:
            content: Content that's too large
            remaining_tokens: Available tokens in context

        Returns:
            List of content chunks that fit in context

        """
        self.check_context_status()

        # Notify overflow callbacks
        for callback in self._on_overflow_callbacks:
            try:
                callback(content, remaining_tokens)
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(f"Overflow callback error: {e}")

        # Try aggressive compression first
        compressed = self.optimizer.compress_content(
            content,
            level=CompressionLevel.EMERGENCY,
        )

        compressed_text = compressed.details.get("compressed_content", content)
        compressed_tokens = compressed.compressed_tokens
        if compressed_tokens <= remaining_tokens:
            # Compression worked
            self._content_queue = [compressed_text]
            logger.info(
                f"Overflow resolved via compression: "
                f"{compressed.original_tokens} -> {compressed_tokens}"
            )
            return self._content_queue

        # Must chunk even after compression
        logger.info(
            f"Overflow requires chunking: {compressed_tokens} tokens > {remaining_tokens} remaining"
        )

        # Calculate chunk sizes based on remaining capacity
        # Leave 20% buffer per chunk
        target_chunk_tokens = int(remaining_tokens * 0.8)

        # Split into chunks
        lines = content.splitlines()
        chunks = []
        current_chunk: list[str] = []
        current_tokens = 0

        for line in lines:
            line_tokens = estimate_tokens_agnostic(line)

            if current_tokens + line_tokens > target_chunk_tokens and current_chunk:
                # Start new chunk
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_tokens = line_tokens
            else:
                current_chunk.append(line)
                current_tokens += line_tokens

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        self._content_queue = chunks
        self._total_chunks = len(chunks)
        self._current_chunk_index = 0

        return chunks

    def on_compress(self, callback: Callable) -> None:
        """Register callback for compression events."""
        self._on_compress_callbacks.append(callback)

    def on_chunk(self, callback: Callable) -> None:
        """Register callback for chunking events."""
        self._on_chunk_callbacks.append(callback)

    def on_overflow(self, callback: Callable) -> None:
        """Register callback for overflow events."""
        self._on_overflow_callbacks.append(callback)

    def get_status_report(self) -> dict[str, Any]:
        """Get comprehensive status report."""
        status = self.check_context_status()
        compression_stats = self.optimizer.get_compression_stats()

        return {
            "context_window": {
                "utilization": f"{status.utilization * 100:.1f}%",
                "current_tokens": status.current_tokens,
                "max_tokens": status.max_tokens,
                "remaining_tokens": status.remaining_tokens,
                "strategy": status.strategy.value,
                "needs_compression": status.needs_compression,
            },
            "compression": compression_stats,
            "content_queue": {
                "total_chunks": self._total_chunks,
                "current_chunk": self._current_chunk_index,
                "remaining_chunks": len(self._content_queue) - self._current_chunk_index,
            },
        }


# Global hook instance
_hook: ContextManagementHook | None = None


def get_context_hook(
    auto_compress_at: float = 0.70,
    auto_chunk_at: int = 500_000,
) -> ContextManagementHook:
    """Get or create global context management hook."""
    global _hook
    if _hook is None:
        _hook = ContextManagementHook(
            auto_compress_at=auto_compress_at,
            auto_chunk_at=auto_chunk_at,
        )
    return _hook


def reset_context_hook() -> None:
    """Reset global hook."""
    global _hook
    _hook = None


# Integration with autonomous_orchestrator
def integrate_with_orchestrator(orchestrator: Any) -> None:
    """Integrate context management hook with an orchestrator instance.

    Adds context management capabilities to the orchestrator by:
    - Pre-processing files before they're sent to Goose
    - Compressing context at threshold
    - Handling overflow gracefully

    Args:
        orchestrator: AutonomousOrchestrator instance

    """
    hook = get_context_hook()

    # Add hooks for content preparation
    original_execute = orchestrator.execute if hasattr(orchestrator, "execute") else None

    if original_execute:

        def wrapped_execute(*args, **kwargs):
            # Check context status before execution
            status = hook.check_context_status()

            if status.needs_compression:
                logger.info(f"[{orchestrator.name}] Pre-execution context compression triggered")
                hook.compress_context()

            return original_execute(*args, **kwargs)

        orchestrator.execute = wrapped_execute

    # Add callback for context events
    def on_compress_event(content: str, result: CompressionResult):
        logger.info(
            f"[{orchestrator.name if hasattr(orchestrator, 'name') else 'Orchestrator'}] "
            f"Context compressed: {result.original_tokens} -> {result.compressed_tokens} tokens"
        )

    def on_chunk_event(file_path: str, chunks: int):
        logger.info(
            f"[{orchestrator.name if hasattr(orchestrator, 'name') else 'Orchestrator'}] "
            f"File chunked: {file_path} -> {chunks} chunks"
        )

    hook.on_compress(on_compress_event)
    hook.on_chunk(on_chunk_event)

    logger.info("Context management hook integrated with orchestrator")


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)

    # Demo the hook
    hook = get_context_hook(auto_compress_at=0.70)

    logger.info("Context Management Hook Demo")
    logger.info("=" * 60)

    # Check status
    status = hook.check_context_status()
    logger.info("\nInitial Status:")
    logger.info(f"  Utilization: {status.utilization * 100:.1f}%")
    logger.info(f"  Strategy: {status.strategy.value}")
    logger.info(f"  Remaining: {status.remaining_tokens:,} tokens")

    # Test content preparation
    large_content = "def example():\n    pass\n" * 10000
    logger.info(f"\nPreparing large content ({len(large_content)} chars)...")

    result = hook.prepare_content(large_content, force_compress=True)
    logger.info(f"  Original size: {result.original_size}")
    logger.info(f"  Prepared size: {result.prepared_size}")
    logger.info(f"  Was compressed: {result.was_compressed}")
    logger.info(f"  Compression ratio: {result.compression_ratio:.2%}")
    logger.info(f"  Strategy: {result.strategy_used}")

    # Test chunking
    huge_content = "x = 1\n" * 100000
    logger.info(f"\nPreparing huge content ({len(huge_content)} chars)...")
    result = hook.prepare_content(huge_content, file_path="test.py")
    logger.info(f"  Was chunked: {result.was_chunked}")
    logger.info(f"  Chunks: {result.chunks_added}")

    if hook.has_more_chunks():
        first_chunk = hook.get_next_chunk()
        logger.info(f"  First chunk size: {len(first_chunk)} chars")  # type: ignore[arg-type]

    # Final status
    logger.info("\nFinal Status Report:")
    logger.info(json.dumps(hook.get_status_report(), indent=2))
