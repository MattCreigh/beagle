"""Context Integration - Orchestrator integration layer.

Integrates context management with the Beagle autonomous orchestrator:
1. Pre-processes files before Goose reads them
2. Applies TurboQuant compression at context_fold stage
3. Proactive compression at 50% threshold
4. Graceful overflow handling with chunking
5. RAG staleness tracking — marks RAG as stale after context fold,
   triggering hot-swap reingestion on the next hydration cycle
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.text_utils import build_skeleton
from .context_manager_hook import (
    get_context_hook,
)
from .context_optimizer import (
    CompressionLevel,
)
from .embedding_adapter import get_embedding_adapter
from .rag_staleness import get_staleness_tracker

logger = logging.getLogger("Beagle.context.context_integration")


@dataclass
class FileProcessResult:
    """Result of processing a file for context."""

    file_path: str
    original_size: int
    processed: bool
    needs_chunking: bool
    chunk_count: int
    current_chunk: int | None
    error: str | None = None


@dataclass
class TurboFoldResult:
    """Result of a TurboQuant-enhanced context fold.

    Contains the structural skeleton (kept in text for the LLM context),
    the compressed embeddings (stored as sidecar binary for retrieval),
    and a chunk map for reconstructing which text chunks map to which
    compressed embeddings.
    """

    skeleton_text: str
    compressed_embeddings: bytes
    seed: int
    chunk_map: list[dict[str, Any]]
    original_tokens: int
    compressed_tokens: int
    embedding_dim: int
    n_chunks: int
    compression_ratio: float
    fold_id: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.fold_id:
            self.fold_id = hashlib.sha256(
                self.compressed_embeddings[:64] + str(self.seed).encode()
            ).hexdigest()[:12]
        if not self.created_at:
            self.created_at = time.time()

    def to_manifest(self) -> dict[str, Any]:
        """Serialize to a JSON-safe manifest (embeddings stored separately)."""
        return {
            "fold_id": self.fold_id,
            "seed": self.seed,
            "chunk_map": self.chunk_map,
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "embedding_dim": self.embedding_dim,
            "n_chunks": self.n_chunks,
            "compression_ratio": self.compression_ratio,
            "created_at": self.created_at,
            "skeleton_tokens": len(self.skeleton_text) // 4,
        }


class ContextIntegration:
    """Integrates context management with Beagle orchestrator.

    This class provides:
    - Pre-processing of files before they're read by Goose
    - TurboQuant-enhanced context folding
    - Proactive compression monitoring
    - Graceful overflow handling

    Usage:
        integration = ContextIntegration()

        # Before reading a file
        result = integration.preprocess_file("/path/to/large_file.py")
        if result.needs_chunking:
            for chunk_index in range(result.chunk_count):
                content = integration.get_chunk(file_path, chunk_index)
                # Process chunk...

        # During context fold
        compressed = integration.enhanced_context_fold(
            data_to_fold,
            fold_mode="turboquant"
        )
    """

    def __init__(
        self,
        auto_compress_threshold: float = 0.70,
        file_size_limit: int = 500_000,
        enable_turboquant: bool = True,
    ):
        from ..core.turboquant import TurboQuantCompressor, turboquant_available

        self.auto_compress_threshold = auto_compress_threshold
        self.file_size_limit = file_size_limit
        self.enable_turboquant = enable_turboquant and turboquant_available()

        # Get the context management hook
        self.hook = get_context_hook(
            auto_compress_at=auto_compress_threshold,
            auto_chunk_at=file_size_limit,
        )

        # File processing state
        self._processed_files: dict[str, FileProcessResult] = {}
        self._file_chunks: dict[str, list[str]] = {}

        # TurboQuant compressor for context folding
        self._compressor = TurboQuantCompressor(bits=3) if self.enable_turboquant else None

        # Statistics
        self.stats = {
            "files_processed": 0,
            "files_chunked": 0,
            "total_chunks_created": 0,
            "context_folds": 0,
            "tokens_saved_compression": 0,
            "tokens_saved_chunking": 0,
        }

    def preprocess_file(
        self,
        file_path: str,
        content: str | None = None,
    ) -> FileProcessResult:
        """Pre-process a file before it enters context.

        This is called before files are read by Goose nodes.

        Args:
            file_path: Path to the file
            content: Optional content (if already loaded)

        Returns:
            FileProcessResult with processing details

        """
        # Check if already processed
        if file_path in self._processed_files:
            return self._processed_files[file_path]

        try:
            path = Path(file_path)

            # Load content if not provided
            if content is None:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError as e:  # catch: NARROWED  # RATIONALE=read_text with errors="replace" can only fail on filesystem errors
                    logger.warning(f"Could not read {file_path}: {e}")
                    return FileProcessResult(
                        file_path=file_path,
                        original_size=0,
                        processed=False,
                        needs_chunking=False,
                        chunk_count=0,
                        current_chunk=None,
                        error=str(e),
                    )

            original_size = len(content)
            self.stats["files_processed"] += 1

            # Use the hook to prepare content
            prepare_result = self.hook.prepare_content(
                content=content,
                file_path=file_path,
            )

            result = FileProcessResult(
                file_path=file_path,
                original_size=original_size,
                processed=True,
                needs_chunking=prepare_result.was_chunked,
                chunk_count=prepare_result.chunks_added,
                current_chunk=0 if prepare_result.was_chunked else None,
            )

            # Store chunks if chunked
            if prepare_result.was_chunked:
                self._file_chunks[file_path] = self.hook.get_content_chunks()
                self.stats["files_chunked"] += 1
                self.stats["total_chunks_created"] += prepare_result.chunks_added

                # Calculate tokens saved by chunking
                # Each chunk is processed separately, avoiding overflow
                self.stats["tokens_saved_chunking"] += original_size // 4  # rough estimate

            self._processed_files[file_path] = result
            return result

        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
        ) as e:  # catch: NARROWED  # RATIONALE=preprocessing is path I/O plus chunk/token arithmetic; a single bad file must not abort the batch
            logger.error(f"Error preprocessing {file_path}: {e}")
            return FileProcessResult(
                file_path=file_path,
                original_size=0,
                processed=False,
                needs_chunking=False,
                chunk_count=0,
                current_chunk=None,
                error=str(e),
            )

    def get_chunk(
        self,
        file_path: str,
        chunk_index: int | None = None,
    ) -> str | None:
        """Get a specific chunk of a processed file.

        Args:
            file_path: Path to the file
            chunk_index: Optional chunk index (defaults to next chunk)

        Returns:
            Chunk content or None if no more chunks

        """
        if file_path not in self._file_chunks:
            return None

        chunks = self._file_chunks[file_path]

        if chunk_index is None:
            # Get next chunk
            processed = self._processed_files[file_path]
            if processed.current_chunk is None:
                processed.current_chunk = 0

            if processed.current_chunk >= len(chunks):
                return None

            chunk = chunks[processed.current_chunk]
            processed.current_chunk += 1
            return chunk

        # Get specific chunk
        if chunk_index < 0 or chunk_index >= len(chunks):
            return None

        return chunks[chunk_index]

    def get_file_summary(self, file_path: str) -> str:
        """Get a summary header for a chunked file.

        Args:
            file_path: Path to the file

        Returns:
            Summary string for context

        """
        if file_path not in self._processed_files:
            return ""

        result = self._processed_files[file_path]

        if not result.needs_chunking:
            return f'<file path="{file_path}" full="true"/>\n'

        current = result.current_chunk or 0
        return (
            f'<file path="{file_path}" chunked="true" '
            f'chunks="{result.chunk_count}" current="{current + 1}">\n'
            f"  <!-- Use context_manager.get_chunk('{file_path}', "
            f"chunk_index) to access specific chunks -->\n"
        )

    def should_compress_context(self) -> bool:
        """Check if context should be compressed proactively."""
        status = self.hook.check_context_status()
        return status.utilization >= self.auto_compress_threshold

    def get_compression_recommendation(self) -> dict[str, Any]:
        """Get compression recommendation for current context state."""
        status = self.hook.check_context_status()
        plan = self.hook.optimizer.analyze_context_needs()

        return {
            "utilization": status.utilization,
            "remaining_tokens": status.remaining_tokens,
            "strategy": status.strategy.value,
            "needs_compression": status.needs_compression,
            "recommended_actions": plan.recommended_actions,
            "compression_targets": plan.compression_targets,
        }

    async def enhanced_context_fold(
        self,
        data_to_fold: str,
        fold_mode: str = "auto",
    ) -> str:
        """Enhanced context fold with TurboQuant compression.

        Args:
            data_to_fold: Data to compress
            fold_mode: "auto", "light", "moderate", "aggressive", "turboquant"

        Returns:
            Compressed/folded data string

        """
        self.stats["context_folds"] += 1
        logger.info("[ContextIntegration] Enhanced context fold invoked")

        # Mark RAG as stale when context folds at MODERATE or higher.
        # This ensures that the next hydration cycle triggers a hot-swap
        # reingestion, refreshing the RAG index with current codebase state
        # before serving stale results.
        staleness = get_staleness_tracker()
        if staleness and not staleness.is_stale:
            staleness.mark_stale(reason="context_fold")
            logger.info(
                "[ContextIntegration] RAG marked stale due to context fold — "
                "next hydration will trigger hot-swap reingestion"
            )

        # Determine compression level from fold_mode
        if fold_mode == "turboquant" or fold_mode == "aggressive":
            level = CompressionLevel.AGGRESSIVE
        elif fold_mode == "moderate":
            level = CompressionLevel.MODERATE
        elif fold_mode == "light":
            level = CompressionLevel.LIGHT
        else:
            # Auto: determine from context state
            status = self.hook.check_context_status()
            if status.utilization >= 0.85:
                level = CompressionLevel.EMERGENCY
            elif status.utilization >= 0.70:
                level = CompressionLevel.AGGRESSIVE
            elif status.utilization >= 0.50:
                level = CompressionLevel.MODERATE
            else:
                level = CompressionLevel.LIGHT

        # Apply compression
        result = self.hook.optimizer.compress_content(
            data_to_fold,
            level=level,
        )

        self.stats["tokens_saved_compression"] += result.original_tokens - result.compressed_tokens

        # Extract compressed text from the compression result
        compressed_text = result.details.get("compressed_content", "")

        # For TurboQuant mode, additionally apply vector compression
        if fold_mode == "turboquant" and self.enable_turboquant:
            tqr = self._apply_turboquant_fold(result, raw_text=data_to_fold)
            # _apply_turboquant_fold returns TurboFoldResult or str fallback
            compressed = tqr.skeleton_text if isinstance(tqr, TurboFoldResult) else tqr
        else:
            # Use the compressed text from the pipeline, falling back
            # to the content queue (set by prepare_content) or original
            if compressed_text:
                compressed = compressed_text
            else:
                chunks = self.hook.get_content_chunks()
                compressed = chunks[0] if chunks else data_to_fold

        logger.info(
            f"[ContextIntegration] Context folded: {result.original_tokens} -> "
            f"{result.compressed_tokens} tokens ({result.compression_ratio:.1%})"
        )

        return compressed

    def _apply_turboquant_fold(
        self,
        compression_result: Any,
        chunk_size: int = 512,
        raw_text: str = "",
    ) -> TurboFoldResult | str:
        """Apply TurboQuant compression to folded context.

        Pipeline:
        1. Chunk the text into chunk_size-char segments
        2. Embed each chunk via EmbeddingAdapter (768-dim)
        3. Stack embeddings into (n_chunks, 768) array
        4. TurboQuant compress the embedding matrix (3-bit)
        5. Build structural skeleton from key lines (class, def, imports)
        6. Store compressed embeddings + manifest as sidecar
        7. Return skeleton text for LLM context

        Args:
            compression_result: CompressionResult from ContextOptimizer.
            chunk_size: Character size for text chunking before embedding.
            raw_text: The original text to fold (falls back to hook chunks).

        Returns:
            TurboFoldResult with skeleton + compressed embeddings,
            or str fallback if TurboQuant/embeddings unavailable.

        """
        # Fallback if TurboQuant or embeddings not available
        if not self.enable_turboquant or not self._compressor:
            # Prefer compressed content from the pipeline, then hook chunks, then raw
            compressed_text = (
                compression_result.details.get("compressed_content", "")
                if compression_result
                else ""
            )
            if compressed_text:
                return compressed_text
            chunks = self.hook.get_content_chunks()
            return chunks[0] if chunks else raw_text

        # Step 1: Get the text content to fold
        # Prefer raw_text, then compressed content from pipeline, then hook chunks
        if raw_text:
            full_text = raw_text
        elif compression_result and compression_result.details.get("compressed_content"):
            full_text = compression_result.details["compressed_content"]
        else:
            hook_chunks = self.hook.get_content_chunks()
            if not hook_chunks:
                logger.warning("[TurboFold] No content chunks or raw text to fold")
                return raw_text
            full_text = hook_chunks[0] if len(hook_chunks) == 1 else "\n".join(hook_chunks)

        # Step 2: Chunk into embedding-sized segments
        text_chunks = self._chunk_text(full_text, chunk_size)
        if not text_chunks:
            logger.warning("[TurboFold] Text chunking produced no results")
            return full_text  # Return uncompressed as last resort

        # Step 3: Embed chunks
        try:
            adapter = get_embedding_adapter()
            if not adapter.available:
                logger.warning(
                    "[TurboFold] Embedding adapter unavailable — "
                    "using pseudo-embeddings (no semantic search)"
                )
            embeddings = adapter.embed_batch(text_chunks)  # (n, 768)
        except (
            ImportError,
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
        ) as exc:  # catch: NARROWED  # RATIONALE=adapter import/init, remote embedding call, or batch-shape error — fall back to the uncompressed text rather than losing the fold
            logger.warning(f"[TurboFold] Embedding failed: {exc}")
            return full_text

        # Step 4: TurboQuant compress
        try:
            compressed_bytes, seed = self._compressor.compress(embeddings)
        except (
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:  # catch: NARROWED  # RATIONALE=compress raises RuntimeError when NumPy is absent and ValueError/TypeError on array-shape problems — same fallback as above
            logger.warning(f"[TurboFold] TurboQuant compress failed: {exc}")
            return full_text

        # Step 5: Build structural skeleton
        skeleton = self._build_skeleton(full_text)

        # Step 6: Build chunk map
        chunk_map = []
        for i, chunk_text in enumerate(text_chunks):
            chunk_map.append(
                {
                    "index": i,
                    "char_start": i * chunk_size,
                    "char_end": min((i + 1) * chunk_size, len(full_text)),
                    "text_preview": chunk_text[:80],
                    "text_hash": hashlib.sha256(chunk_text.encode()).hexdigest()[:16],
                }
            )

        # Step 7: Compute stats
        orig_tok = compression_result.original_tokens if compression_result else len(full_text) // 4
        skeleton_tokens = len(skeleton) // 4
        embedding_bytes = embeddings.nbytes
        compressed_embedding_bytes = len(compressed_bytes)
        embedding_ratio = (
            compressed_embedding_bytes / embedding_bytes if embedding_bytes > 0 else 1.0
        )

        # Total "effective" compressed tokens = skeleton + compressed embeddings / 4
        total_compressed = skeleton_tokens + (compressed_embedding_bytes // 4)
        overall_ratio = total_compressed / orig_tok if orig_tok > 0 else 1.0

        result = TurboFoldResult(
            skeleton_text=skeleton,
            compressed_embeddings=compressed_bytes,
            seed=seed,
            chunk_map=chunk_map,
            original_tokens=orig_tok,
            compressed_tokens=total_compressed,
            embedding_dim=embeddings.shape[1] if len(embeddings.shape) > 1 else adapter.dimension,
            n_chunks=len(text_chunks),
            compression_ratio=overall_ratio,
        )

        # Step 8: Persist sidecar
        self._save_fold_sidecar(result)

        logger.info(
            f"[TurboFold] Fold #{result.fold_id}: "
            f"{orig_tok} → {total_compressed} tokens "
            f"({overall_ratio:.1%}), "
            f"{len(text_chunks)} chunks, "
            f"embeddings: {embedding_bytes} → "
            f"{compressed_embedding_bytes} bytes "
            f"({embedding_ratio:.1%})"
        )

        return result

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512) -> list[str]:
        """Split text into chunks of approximately chunk_size characters.

        Respects line boundaries — won't break a line in the middle.
        """
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
            current_len += len(line) + 1  # +1 for newline

        if current:
            chunks.append("\n".join(current))

        return chunks

    @staticmethod
    def _build_skeleton(text: str) -> str:
        """Extract structural skeleton from text.

        Delegates to :func:`beagle.utils.text_utils.build_skeleton`.
        Kept as staticmethod for backward compatibility with callers
        that reference ``ContextIntegration._build_skeleton``.
        """
        return build_skeleton(text)

    def _save_fold_sidecar(self, result: TurboFoldResult) -> None:
        """Persist compressed fold data to sidecar files.

        Files:
          {data_root}/context_folds/{fold_id}_manifest.json  — metadata
          {data_root}/context_folds/{fold_id}_embeddings.bin  — compressed vectors

        Keeps the 3 most recent folds; older ones are auto-cleaned.
        """
        # get_data_root(), not get_workspace_root(): under a wheel install
        # workspace_root is site-packages itself, and fold state was observed
        # written there while readers looked in ~/.beagle — writes one path
        # could not see from the other.
        from ..config.paths import get_data_root

        fold_dir = get_data_root() / "context_folds"
        fold_dir.mkdir(parents=True, exist_ok=True)

        # Write manifest
        manifest_path = fold_dir / f"{result.fold_id}_manifest.json"
        manifest_path.write_text(json.dumps(result.to_manifest(), indent=2))

        # Write compressed embeddings
        emb_path = fold_dir / f"{result.fold_id}_embeddings.bin"
        emb_path.write_bytes(result.compressed_embeddings)

        # Auto-cleanup: keep only last 3 folds
        existing = sorted(fold_dir.glob("*_manifest.json"), key=lambda p: p.stat().st_mtime)
        while len(existing) > 3:
            old_manifest = existing.pop(0)
            old_stem = old_manifest.stem.replace("_manifest", "")
            old_emb = fold_dir / f"{old_stem}_embeddings.bin"
            old_manifest.unlink(missing_ok=True)
            old_emb.unlink(missing_ok=True)
            logger.debug(f"[TurboFold] Cleaned old fold: {old_stem}")

        # Track in stats
        self.stats["turbo_fold_id"] = result.fold_id  # type: ignore[assignment]
        self.stats["turbo_fold_ratio"] = result.compression_ratio  # type: ignore[assignment]

    def get_stats(self) -> dict[str, Any]:
        """Get context integration statistics."""
        hook_stats = self.hook.get_status_report()

        return {
            "integration": self.stats,
            "context_window": hook_stats["context_window"],
            "compression": hook_stats["compression"],
            "processed_files": len(self._processed_files),
            "chunked_files": len(self._file_chunks),
        }

    def reset(self) -> None:
        """Reset integration state."""
        self._processed_files.clear()
        self._file_chunks.clear()
        self.hook = get_context_hook(
            auto_compress_at=self.auto_compress_threshold,
            auto_chunk_at=self.file_size_limit,
        )


# Global integration instance
_integration: ContextIntegration | None = None


def get_context_integration(
    auto_compress_threshold: float = 0.70,
    file_size_limit: int = 500_000,
) -> ContextIntegration:
    """Get or create global context integration."""
    global _integration
    if _integration is None:
        _integration = ContextIntegration(
            auto_compress_threshold=auto_compress_threshold,
            file_size_limit=file_size_limit,
        )
    return _integration


def reset_context_integration() -> None:
    """Reset global integration."""
    global _integration
    _integration = None


def create_orchestrator_context_hook() -> Callable[..., Awaitable[str]]:
    """Create a hook function for the autonomous orchestrator.

    This returns a function that can be assigned to replace
    the _context_fold method in AutonomousOrchestrator.

    Usage:
        from context_integration import create_orchestrator_context_hook  # noqa: E402

        # In AutonomousOrchestrator.__init__
        original_fold = self._context_fold
        enhanced_fold = create_orchestrator_context_hook()

        # Use enhanced fold
        async def _context_fold_with_enhancement(state, data, mode):
            integration = get_context_integration()
            result = await integration.enhanced_context_fold(data, mode)
            return result

        self._context_fold = _context_fold_with_enhancement
    """

    async def enhanced_context_fold(_state: Any, data_to_fold: str, fold_mode: str = "auto") -> str:
        # _state kept for signature compatibility with the patched-in call
        # sites (orchestrator passes it positionally); the integration method
        # does not take it. No caller passes it by keyword (verified v1.0.0).
        integration = get_context_integration()
        return await integration.enhanced_context_fold(data_to_fold, fold_mode)

    return enhanced_context_fold


# Patch for autonomous_orchestrator.py
def patch_orchestrator(orchestrator_class: type) -> type:
    """Patch an orchestrator class with enhanced context management.

    This modifies the class to use the enhanced context integration.

    Args:
        orchestrator_class: The AutonomousOrchestrator class to patch

    Returns:
        Patched class

    """
    original_init = orchestrator_class.__init__  # type: ignore[misc]

    def enhanced_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        # Initialize context integration
        self._context_integration = get_context_integration()

        # Add hook integration
        from .context_manager_hook import integrate_with_orchestrator

        integrate_with_orchestrator(self)

        logger.info(f"[{self.name}] Context integration initialized with 50% threshold")

    async def enhanced_context_fold(self, _state, data_to_fold, fold_mode="auto"):
        # _state: see the module-level wrapper above — signature-compat only.
        integration = get_context_integration()
        return await integration.enhanced_context_fold(data_to_fold, fold_mode)

    # Apply patches
    orchestrator_class.__init__ = enhanced_init  # type: ignore[misc]
    orchestrator_class._context_fold = enhanced_context_fold  # type: ignore[attr-defined]

    logger.info(f"Patched {orchestrator_class.__name__} with enhanced context management")

    return orchestrator_class


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)

    # Test integration
    integration = get_context_integration(auto_compress_threshold=0.50)

    logger.info("Context Integration Demo")
    logger.info("=" * 60)

    # Test file processing
    logger.info("\nTesting file processing...")
    # A fixed /tmp path in a demo is still a predictable, world-writable target;
    # mkdtemp gives this run its own private directory.
    _demo_dir = tempfile.mkdtemp(prefix="beagle-context-demo-")
    test_file = str(Path(_demo_dir) / "test_large_file.py")
    Path(test_file).write_text("def test():\n    pass\n" * 10000, encoding="utf-8")

    result = integration.preprocess_file(test_file)
    logger.info(f"  File: {result.file_path}")
    logger.info(f"  Original size: {result.original_size}")
    logger.info(f"  Needs chunking: {result.needs_chunking}")
    logger.info(f"  Chunks: {result.chunk_count}")

    if result.needs_chunking:
        chunk1 = integration.get_chunk(test_file, 0)
        logger.info(f"  First chunk size: {len(chunk1) if chunk1 else 0}")

    # Test compression recommendation
    logger.info("\nCompression Recommendation:")
    rec = integration.get_compression_recommendation()
    logger.info(json.dumps(rec, indent=2))

    # Test context fold
    logger.info("\nTesting context fold...")
    large_data = "x" * 10000

    async def test_fold():
        result = await integration.enhanced_context_fold(large_data, "auto")
        logger.info(f"  Original size: {len(large_data)}")
        logger.info(f"  Folded size: {len(result)}")

    asyncio.run(test_fold())

    # Final stats
    logger.info("\nFinal Statistics:")
    logger.info(json.dumps(integration.get_stats(), indent=2))

    # Cleanup
    Path(test_file).unlink(missing_ok=True)
