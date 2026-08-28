"""Compressed Embedding Store — persist and query TurboQuant-compressed fold data.

Stores TurboQuant fold sidecar files and provides retrieval-by-similarity
for the hydration pipeline to recover relevant context from compressed folds.

Storage layout:
    .beagle/context_folds/
        {fold_id}_manifest.json   — metadata (seed, shape, chunk_map, timestamps)
        {fold_id}_embeddings.bin   — TurboQuant-compressed embedding matrix

Query flow:
    1. Load manifest + compressed embeddings from sidecar
    2. Decompress embeddings using TurboQuant (seed from manifest)
    3. Embed query text via EmbeddingAdapter
    4. Compute cosine similarity → return top-k chunk texts

Auto-cleanup: keeps only the N most recent folds (default 3).
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

from .embedding_adapter import EmbeddingAdapter, get_embedding_adapter

# Availability probe without importing: satisfies vulture (no unused import)
# and is cheaper than the import-and-discard form this replaced.
NUMPY_AVAILABLE = importlib.util.find_spec("numpy") is not None

try:
    from beagle.core.turboquant import TurboQuantCompressor

    TURBOQUANT_AVAILABLE = True
except ImportError:
    # TurboQuant (and its numpy dependency) is OPTIONAL — compression degrades
    # gracefully when it is unavailable (see TURBOQUANT_AVAILABLE guards below).
    # Bind the name to None so importers can still do
    # `from .compressed_store import TurboQuantCompressor` without the whole
    # context package becoming unimportable in a minimal/numpy-less environment.
    TurboQuantCompressor = None  # type: ignore[assignment,misc]
    TURBOQUANT_AVAILABLE = False

logger = logging.getLogger("Beagle.context.compressed_store")

# Default number of folds to keep. v13.19.4: bumped from 3 to 200
# per the test_defaults test — 3 was a development-default; 200 is the
# production retention cap (≈ days of hourly context folds at full
# utilization).
DEFAULT_MAX_FOLDS = 200

# v13.19.4: Folds must be at least this old (in seconds) before they
# are eligible for cleanup. The default of 24h prevents very recent
# folds (still being referenced by an active run) from being deleted
# during a cleanup pass. Previously, cleanup_old_folds had no age gate.
DEFAULT_MIN_AGE_SECONDS = 86400  # 24 hours


class FoldNotFoundError(FileNotFoundError):
    """Raised when a fold ID does not exist in the store."""


class CompressedStore:
    """Persist and query TurboQuant-compressed context fold data.

    Provides:
      - store_fold: Save a TurboFoldResult to disk
      - retrieve_fold: Load a fold by ID
      - query_fold: Semantic search within a compressed fold
      - list_folds: List available folds
      - cleanup_old_folds: Remove folds beyond retention limit
    """

    _instance: CompressedStore | None = None
    _initialized: bool = False

    def __new__(cls, *_args: Any, **_kwargs: Any) -> CompressedStore:
        """Create or return the singleton CompressedStore instance.

        The variadic parameters exist only to satisfy the ``__new__``
        signature contract when callers pass constructor arguments; the
        underscore prefix marks them as intentionally unused.
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        store_dir: str | Path | None = None,
        max_folds: int = DEFAULT_MAX_FOLDS,
        min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    ) -> None:
        """Initialize the compressed embedding store.

        Args:
            store_dir: Directory for persisting fold data. Defaults to
                ``context_folds/`` under ``get_data_root()`` (``~/.beagle``
                by default).
            max_folds: Maximum number of folded context snapshots to retain.
            min_age_seconds: Minimum age (in seconds) a fold must reach
                before it becomes eligible for cleanup. Default 86400 (24h).

        """
        if self._initialized:
            # Allow re-init only if store_dir changed
            if store_dir and hasattr(self, "_store_dir"):
                new_path = str(Path(store_dir).resolve())
                if str(self._store_dir) != new_path:  # type: ignore[has-type]
                    self._store_dir = Path(new_path)
            return

        if store_dir:
            self._store_dir = Path(store_dir)
        else:
            # get_data_root(), not get_workspace_root(): workspace_root is the
            # package install tree under a wheel (site-packages), which is
            # exactly where fold state was observed leaking. State anchors to
            # the writable data root per the paths.py contract.
            from ..config.paths import get_data_root

            self._store_dir = get_data_root() / "context_folds"

        self._max_folds = max_folds
        self._min_age_seconds = min_age_seconds  # v13.19.4: track age gate
        self._adapter: EmbeddingAdapter | None = None
        self._compressor: TurboQuantCompressor | None = None
        self._initialized = True

        if TURBOQUANT_AVAILABLE:
            self._compressor = TurboQuantCompressor(bits=3)

    @property
    def store_dir(self) -> Path:
        """The directory where fold sidecars are stored."""
        self._store_dir.mkdir(parents=True, exist_ok=True)
        return self._store_dir

    def _get_adapter(self) -> EmbeddingAdapter:
        """Lazy-init the embedding adapter."""
        if self._adapter is None:
            self._adapter = get_embedding_adapter()
        return self._adapter

    def store_fold(self, manifest: dict[str, Any], embeddings: bytes) -> str:
        """Store a compressed fold to disk.

        Args:
            manifest: Dict with fold metadata (seed, shape, chunk_map, etc.)
            embeddings: Raw bytes of TurboQuant-compressed embedding matrix

        Returns:
            The fold_id string.

        """
        fold_id = manifest.get("fold_id", "")
        if not fold_id:
            raise ValueError("Manifest must contain a fold_id")

        manifest_path = self.store_dir / f"{fold_id}_manifest.json"
        embeddings_path = self.store_dir / f"{fold_id}_embeddings.bin"

        manifest_path.write_text(json.dumps(manifest, indent=2))
        embeddings_path.write_bytes(embeddings)

        # Auto-cleanup
        self.cleanup_old_folds()

        logger.info(
            f"[CompressedStore] Stored fold {fold_id}: "
            f"{len(embeddings)} bytes, manifest at {manifest_path}"
        )
        return fold_id

    def retrieve_fold(self, fold_id: str) -> tuple[dict[str, Any], bytes]:
        """Load a fold from disk.

        Args:
            fold_id: The fold identifier.

        Returns:
            Tuple of (manifest_dict, compressed_embeddings_bytes).

        Raises:
            FoldNotFoundError: If the fold does not exist.

        """
        manifest_path = self.store_dir / f"{fold_id}_manifest.json"
        embeddings_path = self.store_dir / f"{fold_id}_embeddings.bin"

        if not manifest_path.exists():
            raise FoldNotFoundError(f"Fold {fold_id} not found at {manifest_path}")

        manifest = json.loads(manifest_path.read_text())
        embeddings = embeddings_path.read_bytes() if embeddings_path.exists() else b""

        return manifest, embeddings

    def query_fold(
        self,
        query: str,
        fold_id: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Semantic search within a compressed fold.

        1. Decompress embeddings from fold
        2. Embed the query
        3. Compute cosine similarity
        4. Return top-k chunks with their text previews

        Args:
            query: Natural language query.
            fold_id: Fold to search within.
            top_k: Number of results to return.

        Returns:
            List of dicts with keys: index, similarity, text_preview, text_hash

        """
        manifest, compressed_bytes = self.retrieve_fold(fold_id)

        if not NUMPY_AVAILABLE:
            logger.warning("[CompressedStore] NumPy not available for query")
            return []

        if not compressed_bytes:
            logger.warning(f"[CompressedStore] No embeddings data for fold {fold_id}")
            return []

        # Decompress embeddings
        if not TURBOQUANT_AVAILABLE or not self._compressor:
            logger.warning("[CompressedStore] TurboQuant not available for decompression")
            return []

        seed = manifest["seed"]
        n_chunks = manifest["n_chunks"]
        embedding_dim = manifest["embedding_dim"]
        shape = (n_chunks, embedding_dim)

        try:
            decompressed = self._compressor.decompress(compressed_bytes, seed, shape)
        except (
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:  # catch: NARROWED  # RATIONALE=TurboQuantCompressor.decompress raises RuntimeError when NumPy is absent and ValueError/TypeError on corrupt fold buffers or shape mismatch — a bad fold yields no results, not a crash
            logger.error(f"[CompressedStore] Decompression failed: {exc}")
            return []

        # Embed query
        adapter = self._get_adapter()
        query_vec = adapter.embed_text(query)

        # Find top-k by cosine similarity
        results = adapter.top_k_by_similarity(query_vec, decompressed, k=top_k)

        # Enrich with chunk map info
        chunk_map = manifest.get("chunk_map", [])
        enriched = []
        for idx, similarity in results:
            entry: dict[str, Any] = {
                "index": idx,
                "similarity": similarity,
            }
            if idx < len(chunk_map):
                entry["text_preview"] = chunk_map[idx].get("text_preview", "")
                entry["text_hash"] = chunk_map[idx].get("text_hash", "")
                entry["char_start"] = chunk_map[idx].get("char_start", 0)
                entry["char_end"] = chunk_map[idx].get("char_end", 0)
            enriched.append(entry)

        return enriched

    def query_all_folds(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search across all available folds.

        Args:
            query: Natural language query.
            top_k: Number of results per fold.

        Returns:
            Combined results from all folds, sorted by similarity.

        """
        all_results: list[dict[str, Any]] = []

        for fold_id in self.list_folds():
            try:
                fold_results = self.query_fold(query, fold_id, top_k=top_k)
                for r in fold_results:
                    r["fold_id"] = fold_id
                all_results.extend(fold_results)
            except (
                FoldNotFoundError,
                OSError,
                ValueError,
                RuntimeError,
                TypeError,
            ) as exc:  # catch: NARROWED  # RATIONALE=missing fold sidecar, unreadable manifest, or decompress failure on one fold must not fail the whole multi-fold query
                logger.debug(f"[CompressedStore] Query fold {fold_id} failed: {exc}")

        # Sort by similarity descending
        all_results.sort(key=lambda r: r.get("similarity", 0.0), reverse=True)
        return all_results[:top_k]

    def list_folds(self) -> list[str]:
        """List available fold IDs, sorted by creation time (newest first)."""
        manifests = sorted(
            self.store_dir.glob("*_manifest.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [p.stem.replace("_manifest", "") for p in manifests]

    def cleanup_old_folds(self, keep: int | None = None) -> int:
        """Remove old folds beyond retention limit.

        v13.19.4: Now respects ``self._min_age_seconds``. A fold whose
        mtime is newer than ``now - min_age_seconds`` is protected from
        eviction regardless of the cap. This prevents an in-flight run
        from losing its context fold mid-execution.

        Args:
            keep: Number of folds to keep (default: self._max_folds)

        Returns:
            Number of folds removed.

        """
        import time as _time

        keep = keep or self._max_folds
        manifests = sorted(
            self.store_dir.glob("*_manifest.json"),
            key=lambda p: p.stat().st_mtime,
        )
        # v13.19.4: split the manifest list into "eligible" (older than
        # min_age) and "protected" (still recent). Eviction only
        # considers eligible manifests; protected ones are never deleted.
        now = _time.time()
        cutoff = now - self._min_age_seconds
        eligible: list[Path] = []
        protected_count = 0
        for m in manifests:
            if m.stat().st_mtime < cutoff:
                eligible.append(m)
            else:
                protected_count += 1

        removed = 0
        # Evict oldest eligible folds until the total manifest count is
        # at or below `keep`. Note: ``protected_count`` still counts
        # against `keep` — if you have 200 protected folds, you cannot
        # add a 201st, but you also cannot evict any of the 200.
        while len(eligible) + protected_count > keep and eligible:
            old_manifest = eligible.pop(0)
            old_stem = old_manifest.stem.replace("_manifest", "")
            old_emb = self.store_dir / f"{old_stem}_embeddings.bin"
            old_manifest.unlink(missing_ok=True)
            old_emb.unlink(missing_ok=True)
            removed += 1
            logger.info(f"Evicted fold {old_stem} (age guard)")

        return removed

    def fold_stats(self, fold_id: str) -> dict[str, Any] | None:
        """Get statistics about a stored fold without decompressing.

        Returns:
            Dict with manifest metadata, or None if fold not found.

        """
        try:
            manifest, embeddings = self.retrieve_fold(fold_id)
            return {
                "fold_id": fold_id,
                "seed": manifest.get("seed"),
                "n_chunks": manifest.get("n_chunks", 0),
                "embedding_dim": manifest.get("embedding_dim", 0),
                "original_tokens": manifest.get("original_tokens", 0),
                "compressed_tokens": manifest.get("compressed_tokens", 0),
                "compression_ratio": manifest.get("compression_ratio", 0.0),
                "created_at": manifest.get("created_at", 0.0),
                "embeddings_bytes": len(embeddings),
                "manifest_bytes": len(json.dumps(manifest)),
            }
        except FoldNotFoundError:
            return None


def get_compressed_store(
    store_dir: str | Path | None = None,
    max_folds: int = DEFAULT_MAX_FOLDS,
) -> CompressedStore:
    """Get or create the global CompressedStore singleton."""
    return CompressedStore(store_dir=store_dir, max_folds=max_folds)


def reset_compressed_store() -> None:
    """Reset the global store (for testing)."""
    CompressedStore._instance = None
    CompressedStore._initialized = False
