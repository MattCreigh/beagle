"""Disk-backed embedding cache with SHA-256 keying and LRU eviction.

Used by TurboQuant fold path to avoid recomputing embeddings for identical
text chunks across compaction cycles. Reduces per-fold wall-clock from
~28s to <2s for cached content.

Cache format: .beagle/embedding_cache/{prefix_2}/{remainder}.npy
Eviction: LRU-by-mtime, capped at 10,000 entries (batch evict at 12,000).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(".beagle/embedding_cache")
DEFAULT_MAX_ENTRIES = 10_000
EVICT_AT = 12_000
EVICT_BATCH = 2_000  # Evict this many at once to avoid per-write cost

# Module-level stats
_hits: int = 0
_misses: int = 0
_evictions: int = 0
_stats_lock = threading.Lock()


def get_cache_stats() -> dict[str, int]:
    """Return current cache hit/miss/eviction counters."""
    with _stats_lock:
        return {"hits": _hits, "misses": _misses, "evictions": _evictions}


class EmbeddingCache:
    """Disk-backed cache for embedding vectors keyed by SHA-256 of chunk text."""

    def __init__(
        self,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # In-memory hot index: {sha256_hex: file_path}
        self._index: dict[str, Path] = {}
        self._load_index()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, chunk_text: str) -> np.ndarray | None:
        """Return cached embedding for *chunk_text*, or None on miss.

        Thread-safe: uses lock for index lookup, file read is atomic.
        """
        key = _hash_key(chunk_text)
        file_path = self._get_path(key)

        with self._lock:
            if key not in self._index:
                _bump_miss()
                return None

        try:
            arr: np.ndarray = np.load(file_path)
        except (OSError, ValueError) as exc:
            logger.debug(f"[EmbedCache] Corrupt cache entry {key}: {exc}")
            self._remove_entry(key, file_path)
            _bump_miss()
            return None

        _bump_hit()
        # Touch mtime for LRU ordering
        with contextlib.suppress(OSError):
            os.utime(file_path, None)
        return arr

    def put(self, chunk_text: str, embedding: np.ndarray) -> None:
        """Cache *embedding* for *chunk_text*.

        Thread-safe: file write + index update under lock.
        """
        key = _hash_key(chunk_text)
        file_path = self._get_path(key)

        file_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # Write atomically via temp file + rename.
            # Use .tmp.npy so np.save doesn't append another .npy suffix.
            tmp_path = file_path.with_suffix(".tmp.npy")
            np.save(tmp_path, embedding)
            os.replace(tmp_path, file_path)
            self._index[key] = file_path

        # Check eviction threshold outside lock to avoid contention
        if len(self._index) >= EVICT_AT:
            self._evict()

    def get_or_compute(
        self,
        chunks: list[str],
        compute_fn: Callable[[list[str]], np.ndarray],
    ) -> np.ndarray:
        """Bulk: return cached embeddings where possible, compute the rest.

        Args:
            chunks: List of chunk strings in desired order.
            compute_fn: Callable(list[str]) -> np.ndarray of shape (len, dim).

        Returns:
            np.ndarray of shape (len(chunks), dim) in the same order as *chunks*.

        """
        n = len(chunks)
        if n == 0:
            return np.zeros((0, 0), dtype=np.float32)

        # First pass: collect cached and uncached indices
        results: dict[int, np.ndarray] = {}
        uncached_idx: list[int] = []
        uncached_texts: list[str] = []

        for i, chunk in enumerate(chunks):
            cached = self.get(chunk)
            if cached is not None:
                results[i] = cached
            else:
                uncached_idx.append(i)
                uncached_texts.append(chunk)

        # Compute uncached ones in one batch
        if uncached_texts:
            new_embeddings = compute_fn(uncached_texts)
            for j, idx in enumerate(uncached_idx):
                emb = new_embeddings[j]
                results[idx] = emb
                self.put(chunks[idx], emb)

        # Reassemble in original order
        dim = next(iter(results.values())).shape[0] if results else 0
        out = np.zeros((n, dim), dtype=np.float32)
        for i in range(n):
            out[i] = results[i]
        return out

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_path(self, key: str) -> Path:
        """Two-level fanout: {prefix_2}/{remainder}.npy."""
        return self._cache_dir / key[:2] / f"{key[2:]}.npy"

    def _load_index(self) -> None:
        """Scan cache directory and rebuild in-memory index."""
        if not self._cache_dir.exists():
            return
        count = 0
        for npy in sorted(self._cache_dir.rglob("*.npy")):
            key = npy.stem
            prefix = npy.parent.name
            full_key = prefix + key
            self._index[full_key] = npy
            count += 1
        if count:
            logger.debug(f"[EmbedCache] Loaded {count} entries from {self._cache_dir}")

    def _remove_entry(self, key: str, file_path: Path) -> None:
        """Remove a single entry from index and disk."""
        with self._lock:
            self._index.pop(key, None)
        with contextlib.suppress(OSError):
            file_path.unlink(missing_ok=True)

    def _evict(self) -> None:
        """Batch-evict oldest entries by mtime."""
        with self._lock:
            if len(self._index) < EVICT_AT:
                return

            # Collect (mtime, key, path) for all entries
            entries: list[tuple[float, str, Path]] = []
            for key, path in self._index.items():
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                entries.append((mtime, key, path))

            # Sort oldest-first
            entries.sort(key=lambda x: x[0])

            # Evict the oldest EVICT_BATCH entries
            to_evict = entries[:EVICT_BATCH]
            for _, key, path in to_evict:
                self._index.pop(key, None)
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                _bump_eviction()

            logger.info(
                f"[EmbedCache] Evicted {len(to_evict)} entries; {len(self._index)} remaining"
            )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _hash_key(text: str) -> str:
    """SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bump_hit() -> None:
    global _hits
    with _stats_lock:
        _hits += 1


def _bump_miss() -> None:
    global _misses
    with _stats_lock:
        _misses += 1


def _bump_eviction() -> None:
    global _evictions
    with _stats_lock:
        _evictions += 1


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_cache: EmbeddingCache | None = None


def get_embedding_cache() -> EmbeddingCache:
    """Return the module-level EmbeddingCache singleton."""
    global _cache
    if _cache is None:
        _cache = EmbeddingCache()
    return _cache
