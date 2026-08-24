"""Context Window Optimizer - Progressive context management.

Integrates with Goose Beagle workflow to:
1. Monitor context utilization proactively
2. Apply TurboQuant compression at 70% threshold
3. Implement progressive compression strategies
4. Handle context overflow gracefully
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..cost_tracker import estimate_tokens_agnostic
from ..utils.text_utils import build_skeleton as _shared_build_skeleton
from .context_window import ContextWindowManager, get_context_manager

logger = logging.getLogger("Beagle.context.context_optimizer")


class CompressionLevel(Enum):
    """Compression level for context."""

    NONE = 0
    LIGHT = 1  # Trim whitespace, dedupe
    MODERATE = 2  # Summarize older content
    AGGRESSIVE = 3  # TurboQuant + summarize
    EMERGENCY = 4  # Drop non-essential


class ContextStrategy(Enum):
    """Strategy for managing context."""

    NORMAL = "normal"  # Under 70%
    COMPRESS = "compress"  # 50-70%
    AGGRESSIVE = "aggressive"  # 70-85%
    EMERGENCY = "emergency"  # 85%+


@dataclass
class CompressionResult:
    """Result of compression operation."""

    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    strategy_used: ContextStrategy
    compression_level: CompressionLevel
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextPlan:
    """Plan for managing context window."""

    current_utilization: float
    strategy: ContextStrategy
    recommended_actions: list[str]
    estimated_room: int
    compression_targets: list[dict[str, Any]]


# Precompiled regex patterns for performance - compiled once, used many times
_STRUCTURE_PATTERN = re.compile(r"^(class|async\s+def|def|async\s+class)\s+\w+")
_BLANK_LINE_PATTERN = re.compile(r"\n{3,}")


class ContextOptimizer:
    """Optimizes context window usage with progressive strategies."""

    def __init__(
        self,
        context_manager: ContextWindowManager | None = None,
        warning_threshold: float = 0.70,  # Start compression at 70%
        aggressive_threshold: float = 0.70,
        emergency_threshold: float = 0.85,
        enable_turboquant: bool = True,
    ):
        from ..core.turboquant import turboquant_available

        self.context_manager = context_manager or get_context_manager()
        self.warning_threshold = warning_threshold
        self.aggressive_threshold = aggressive_threshold
        self.emergency_threshold = emergency_threshold
        self.enable_turboquant = enable_turboquant and turboquant_available()

        # TurboQuant compressor (lazy-initialized)
        self._compressor = None
        if self.enable_turboquant:
            try:
                from ..core.turboquant import TurboQuantCompressor

                self._compressor = TurboQuantCompressor(bits=3)
            except ImportError:
                self._compressor = None

        # Compression history
        self._compression_history: list[CompressionResult] = []
        self._last_compression_time = 0.0
        self._total_tokens_saved = 0

    def get_current_strategy(self) -> ContextStrategy:
        """Determine current context strategy based on utilization."""
        utilization = self.context_manager.cost_tracker.context_status.utilization

        if utilization >= self.emergency_threshold:
            return ContextStrategy.EMERGENCY
        elif utilization >= self.aggressive_threshold:
            return ContextStrategy.AGGRESSIVE
        elif utilization >= self.warning_threshold:
            return ContextStrategy.COMPRESS
        else:
            return ContextStrategy.NORMAL

    def get_compression_level(self, strategy: ContextStrategy | None = None) -> CompressionLevel:
        """Get recommended compression level for strategy."""
        strategy = strategy or self.get_current_strategy()

        if strategy == ContextStrategy.EMERGENCY:
            return CompressionLevel.EMERGENCY
        elif strategy == ContextStrategy.AGGRESSIVE:
            return CompressionLevel.AGGRESSIVE
        elif strategy == ContextStrategy.COMPRESS:
            return CompressionLevel.MODERATE
        else:
            return CompressionLevel.LIGHT

    def analyze_context_needs(
        self,
        pending_content: str | None = None,
    ) -> ContextPlan:
        """Analyze current context state and plan actions.

        Args:
            pending_content: Content about to be added

        Returns:
            ContextPlan with recommendations

        """
        status = self.context_manager.cost_tracker.context_status
        current_tokens = status.current_tokens
        context_window = status.context_window
        utilization = status.utilization

        strategy = self.get_current_strategy()
        remaining = context_window - current_tokens

        # Calculate room for new content
        if pending_content:
            pending_tokens = estimate_tokens_agnostic(pending_content)
            room_needed = pending_tokens
        else:
            room_needed = 0
            pending_tokens = 0

        # Calculate estimated room
        estimated_room = remaining - room_needed
        estimated_utilization = (current_tokens + room_needed) / context_window

        # Determine recommended actions
        actions = []

        if estimated_utilization >= self.emergency_threshold:
            actions.append("EMERGENCY: Immediate context compression required")
            actions.append("Consider: Dropping non-essential history")
            actions.append("Consider: TurboQuant compression of embeddings")
        elif estimated_utilization >= self.aggressive_threshold:
            actions.append("WARNING: Approaching context limit")
            actions.append("Consider: Aggressive compression of older content")
        elif estimated_utilization >= self.warning_threshold:
            actions.append("NOTICE: Context approaching compression threshold")
            actions.append("Consider: Light compression to free space")

        # Identify compression targets
        compression_targets = self._identify_compression_targets(
            pending_tokens,
            estimated_utilization,
        )

        return ContextPlan(
            current_utilization=utilization,
            strategy=strategy,
            recommended_actions=actions,
            estimated_room=estimated_room,
            compression_targets=compression_targets,
        )

    def _identify_compression_targets(
        self,
        pending_tokens: int,
        estimated_utilization: float,
    ) -> list[dict[str, Any]]:
        """Identify content that can be compressed."""
        targets = []

        # Get node metrics from context manager
        node_metrics = self.context_manager.node_metrics

        # Sort nodes by token usage
        sorted_nodes = sorted(
            node_metrics.items(),
            key=lambda x: x[1].total_tokens,
            reverse=True,
        )

        # Identify large nodes
        for node_name, metrics in sorted_nodes:
            if metrics.total_tokens > pending_tokens * 0.5:
                targets.append(
                    {
                        "node": node_name,
                        "tokens": metrics.total_tokens,
                        "action": "Compress node output",
                        "priority": "high" if metrics.total_tokens > pending_tokens else "medium",
                    }
                )

        return targets

    def compress_content(
        self,
        content: str,
        level: CompressionLevel | None = None,
        preserve_structure: bool = True,
    ) -> CompressionResult:
        """Compress content using appropriate strategy.

        Args:
            content: Content to compress
            level: Compression level (auto-detect if None)
            preserve_structure: Try to preserve structural elements

        Returns:
            CompressionResult with compressed content and metadata

        """
        level = level or self.get_compression_level()

        if level == CompressionLevel.NONE:
            return CompressionResult(
                original_tokens=len(content) // 4,
                compressed_tokens=len(content) // 4,
                compression_ratio=1.0,
                strategy_used=ContextStrategy.NORMAL,
                compression_level=level,
                details={
                    "compressed_content": content,
                    "tokens_saved": 0,
                },
            )

        original_tokens = estimate_tokens_agnostic(content)
        start_time = time.monotonic()

        # Progressive compression strategies
        compressed = content

        # Level 1: Light - trim and dedupe
        if level.value >= CompressionLevel.LIGHT.value:
            compressed = self._light_compression(compressed)

        # Level 2: Moderate - summarize older sections
        if level.value >= CompressionLevel.MODERATE.value:
            compressed = self._moderate_compression(compressed, preserve_structure)

        # Level 3: Aggressive - TurboQuant for numeric data
        if level.value >= CompressionLevel.AGGRESSIVE.value:
            compressed = self._aggressive_compression(compressed)

        # Level 4: Emergency - drop non-essential
        if level.value >= CompressionLevel.EMERGENCY.value:
            compressed = self._emergency_compression(compressed)

        compressed_tokens = estimate_tokens_agnostic(compressed)
        compression_ratio = compressed_tokens / original_tokens if original_tokens > 0 else 1.0

        elapsed = time.monotonic() - start_time

        result = CompressionResult(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            strategy_used=self.get_current_strategy(),
            compression_level=level,
            details={
                "elapsed_seconds": elapsed,
                "tokens_saved": original_tokens - compressed_tokens,
                "compressed_content": compressed,
            },
        )

        self._compression_history.append(result)
        self._total_tokens_saved += result.details.get("tokens_saved", 0)

        return result

    def _light_compression(self, content: str) -> str:
        """Apply light compression - trim whitespace, remove obvious duplicates."""
        # Use precompiled pattern for performance
        content = _BLANK_LINE_PATTERN.sub("\n\n", content)

        # Remove trailing whitespace per line
        lines = [line.rstrip() for line in content.splitlines()]

        # Remove consecutive duplicate lines
        deduped_lines = []
        prev_line = None
        for line in lines:
            if line != prev_line or not line.strip():
                deduped_lines.append(line)
            prev_line = line

        return "\n".join(deduped_lines)

    def _moderate_compression(
        self,
        content: str,
        preserve_structure: bool = True,
    ) -> str:
        """Apply moderate compression - summarize older content."""
        lines = content.splitlines()

        # Preserving structure means keeping:
        # - First 50 lines (setup/context)
        # - Last 100 lines (current context)
        # - Structural markers (class def, function def, etc.)

        if len(lines) <= 150:
            # Too short to summarize meaningfully
            return content

        if preserve_structure:
            # Find structural boundaries
            structure_lines = set()
            for i, line in enumerate(lines):
                # Python structure
                if _STRUCTURE_PATTERN.match(line):
                    structure_lines.add(i)
                    # Include 5 lines after structure
                    for j in range(i + 1, min(i + 6, len(lines))):
                        structure_lines.add(j)

            # Keep important sections
            keep_lines = set()  # type: ignore[var-annotated]
            keep_lines.update(range(min(50, len(lines))))  # First 50
            keep_lines.update(range(max(len(lines) - 100, 0), len(lines)))  # Last 100
            keep_lines.update(structure_lines)

            # Build compressed version with summary markers
            result = []
            in_omitted = False
            omitted_count = 0

            for i, line in enumerate(lines):
                if i in keep_lines:
                    if in_omitted:
                        result.append(f"... [{omitted_count} lines omitted] ...")
                        in_omitted = False
                        omitted_count = 0
                    result.append(line)
                else:
                    in_omitted = True
                    omitted_count += 1

            return "\n".join(result)
        else:
            # Simple summary: first 30%, last 40%, summarize middle
            first_end = int(len(lines) * 0.3)
            last_start = int(len(lines) * 0.6)

            result = lines[:first_end]
            result.append(
                f"... [{len(lines) - first_end - (len(lines) - last_start)} lines summarized] ..."
            )
            result.extend(lines[last_start:])

            return "\n".join(result)

    def _aggressive_compression(self, content: str) -> str:
        """Apply aggressive compression using TurboQuant embedding + skeleton.

        Instead of the broken regex-for-numeric-arrays approach, this:
        1. Embeds the text content in chunks (768-dim vectors)
        2. Compresses the embedding matrix with TurboQuant (3-bit)
        3. Replaces the content with a structural skeleton
        4. Sidecar stores compressed embeddings for later retrieval

        Falls back to structural line selection if embeddings unavailable.
        """
        if not content or len(content) < 1000:
            return content

        # Attempt TurboQuant embedding compression
        if self.enable_turboquant:
            try:
                from .embedding_adapter import get_embedding_adapter

                adapter = get_embedding_adapter()
                if adapter.available:
                    # Chunk text for embedding
                    chunks = self._chunk_for_embedding(content)
                    if len(chunks) >= 3:
                        embeddings = adapter.embed_batch(chunks)
                        # Compress embedding matrix
                        compressed_bytes, seed = self._compressor.compress(embeddings)  # type: ignore[union-attr]

                        # Build structural skeleton
                        skeleton = self._build_skeleton(content)

                        # Persist sidecar via CompressedStore
                        from .compressed_store import get_compressed_store

                        store = get_compressed_store()
                        fold_id = store.store_fold(
                            manifest={
                                "fold_id": f"agg-{uuid.uuid4().hex}",
                                "seed": seed,
                                "n_chunks": len(chunks),
                                "embedding_dim": embeddings.shape[1],
                                "chunk_map": [
                                    {
                                        "index": i,
                                        "char_start": i * 512,
                                        "char_end": min((i + 1) * 512, len(content)),
                                        "text_preview": c[:80],
                                    }
                                    for i, c in enumerate(chunks)
                                ],
                                "original_tokens": len(content) // 4,
                                "compressed_tokens": len(skeleton) // 4,
                                "compression_ratio": len(skeleton) / max(len(content), 1),
                                "created_at": time.time(),
                                "source": "aggressive_compression",
                            },
                            embeddings=compressed_bytes,
                        )

                        logger.info(
                            f"[ContextOptimizer] Aggressive TurboQuant: "
                            f"{len(content)} → {len(skeleton)} chars, "
                            f"{len(chunks)} chunks, fold #{fold_id}"
                        )

                        return skeleton

            except ImportError:
                logger.debug("[ContextOptimizer] TurboQuant/embedding not available")
            except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(
                    f"[ContextOptimizer] TurboQuant aggressive failed: {exc}, falling back"
                )

        # Fallback: structural line selection (emergency compression)
        return self._emergency_compression(content)

    @staticmethod
    def _chunk_for_embedding(text: str, chunk_size: int = 512) -> list[str]:
        """Split text into chunks respecting line boundaries."""
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            if current_len + len(line) + 1 > chunk_size and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1

        if current:
            chunks.append("\n".join(current))

        return chunks

    @staticmethod
    def _build_skeleton(text: str) -> str:
        """Extract structural skeleton with deduplication.

        Delegates to :func:`beagle.utils.text_utils.build_skeleton`
        for the implementation. Kept as staticmethod for backward compatibility
        with callers that reference ``ContextOptimizer._build_skeleton``.
        """
        return _shared_build_skeleton(text)

    def _emergency_compression(self, content: str) -> str:
        """Emergency compression - keep only essential content."""
        lines = content.splitlines()

        # Keep only:
        # - First 30 lines
        # - Last 50 lines
        # - Lines containing critical keywords

        critical_keywords = [
            "error",
            "exception",
            "traceback",
            "fail",
            "class",
            "def",
            "function",
            "result",
            "import",
            "from",
            "return",
        ]

        if len(lines) <= 80:
            return content

        keep_lines = set()  # type: ignore[var-annotated]
        keep_lines.update(range(min(30, len(lines))))
        keep_lines.update(range(max(len(lines) - 50, 0), len(lines)))

        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in critical_keywords):
                keep_lines.add(i)

        result = []
        last_kept = -1

        for i, line in enumerate(lines):
            if i in keep_lines:
                if last_kept >= 0 and i - last_kept > 5:
                    result.append(f"... [{i - last_kept - 1} lines] ...")
                result.append(line)
                last_kept = i

        return "\n".join(result)

    def should_compress_before_add(
        self,
        new_content: str,
        threshold: float | None = None,
    ) -> bool:
        """Check if compression is needed before adding new content.

        Args:
            new_content: Content to be added
            threshold: Compression threshold (default: warning_threshold)

        Returns:
            True if compression should happen first

        """
        threshold = threshold or self.warning_threshold

        status = self.context_manager.cost_tracker.context_status

        new_tokens = estimate_tokens_agnostic(new_content)
        projected_utilization = (status.current_tokens + new_tokens) / status.context_window

        return projected_utilization >= threshold

    def get_compression_stats(self) -> dict[str, Any]:
        """Get statistics about compression operations."""
        return {
            "total_operations": len(self._compression_history),
            "total_tokens_saved": self._total_tokens_saved,
            "average_compression_ratio": (
                sum(r.compression_ratio for r in self._compression_history)
                / len(self._compression_history)
                if self._compression_history
                else 1.0
            ),
            "last_compression_time": self._last_compression_time,
            "turboquant_enabled": self.enable_turboquant,
        }


# Global optimizer instance
_optimizer: ContextOptimizer | None = None


def get_optimizer(
    context_manager: ContextWindowManager | None = None,
    warning_threshold: float = 0.70,
    enable_turboquant: bool = True,
) -> ContextOptimizer:
    """Get or create global context optimizer."""
    global _optimizer
    if _optimizer is None:
        _optimizer = ContextOptimizer(
            context_manager=context_manager,
            warning_threshold=warning_threshold,
            enable_turboquant=enable_turboquant,
        )
    return _optimizer


def reset_optimizer() -> None:
    """Reset global optimizer."""
    global _optimizer
    _optimizer = None


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)

    # Test compression
    test_content = (
        """
# Large file content

class ExampleClass:
    def __init__(self):
        pass

    def method1(self):
        pass

    def method2(self):
        pass

# ... many more lines ...
"""
        * 100
    )

    optimizer = get_optimizer()

    logger.info("Testing compression levels:\n")

    for level in CompressionLevel:
        result = optimizer.compress_content(test_content, level=level)
        logger.info(f"{level.name}:")
        logger.info(f"  Original: {result.original_tokens} tokens")
        logger.info(f"  Compressed: {result.compressed_tokens} tokens")
        logger.info(f"  Ratio: {result.compression_ratio:.2%}")
        logger.info(f"  Tokens saved: {result.details.get('tokens_saved', 0)}")
        logger.info("")

    logger.info("\nCompression stats:")
    logger.info(json.dumps(optimizer.get_compression_stats(), indent=2))
