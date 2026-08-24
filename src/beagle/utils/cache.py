"""Result caching layer for Goose workflow execution.

Provides content-addressable caching with TTL expiration,
LRU eviction, and optional Redis backend for distributed caching.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

# TurboQuant for optional KV compression
try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None  # type: ignore[assignment]
    logger.warning(
        "NumPy not installed. TurboQuant compression and advanced caching "
        "will be disabled. Install with: pip install numpy"
    )

try:
    from ..core.turboquant import (
        COMPRESSION_RATIO,
        simple_turboquant_compress,
        simple_turboquant_decompress,
        turboquant_available,
    )

    TURBOQUANT_AVAILABLE = True
except ImportError:
    TURBOQUANT_AVAILABLE = False
    logger.warning(
        "TurboQuant not available. KV cache compression will be disabled. "
        "Install with: pip install beagle[turboquant]"
    )

    def turboquant_available():  # type: ignore[misc]
        return False

    COMPRESSION_RATIO = 1.0

    def simple_turboquant_compress(*_args, **_kwargs):  # type: ignore[misc]
        return (b"", 0)

    def simple_turboquant_decompress(*_args, **_kwargs):  # type: ignore[misc]
        if NUMPY_AVAILABLE and np is not None:
            return np.array([0])
        return [0]


T = TypeVar("T")

# Public re-export surface (consumed by external code and tests).
__all__ = [
    "COMPRESSION_RATIO",
    "simple_turboquant_compress",
    "simple_turboquant_decompress",
    "turboquant_available",
]

# FHS-compliant path management
from ..config.paths import get_file_cache_dir  # ruff: ignore[E402]


def get_cache_dir() -> Path:
    """Get the cache directory path.

    Uses FHS-compliant cache directory.
    """
    return get_file_cache_dir()


def compute_cache_key(prompt: str, recipe: str, model: str = "") -> str:
    """Compute content-addressable cache key.

    Args:
        prompt: The input prompt
        recipe: The recipe content
        model: Optional model identifier

    Returns:
        SHA256 hash of the combined content

    """
    content = f"{prompt}|||{recipe}|||{model}"
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass
class CacheEntry:
    """Represents a cached result."""

    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    ttl_seconds: int = 86400  # 24 hours default
    hits: int = 0

    @property
    def is_expired(self) -> bool:
        """Check if entry has expired."""
        return time.time() > (self.created_at + self.ttl_seconds)

    def touch(self) -> None:
        """Update access time and hit count."""
        self.accessed_at = time.time()
        self.hits += 1


class MemoryCache:
    """In-memory LRU cache implementation with thread safety."""

    def __init__(
        self,
        max_size: int = 100,
        default_ttl: int = 86400,
    ):
        """Initialize memory cache.

        Args:
            max_size: Maximum number of entries
            default_ttl: Default TTL in seconds (24 hours)

        """
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._last_sweep: float = time.time()  # v0.3.0: proactive expiration

    def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired

        """
        with self._lock:
            # v0.3.0: Periodic sweep of expired entries (every 60s amortized)
            now = time.time()
            if now - self._last_sweep > 60.0:
                expired = [k for k, e in self._cache.items() if e.is_expired]
                for k in expired:
                    del self._cache[k]
                self._last_sweep = now

            entry = self._cache.get(key)

            if entry is None:
                return None

            if entry.is_expired:
                del self._cache[key]
                return None

            # Promote to MRU position
            self._cache.move_to_end(key)
            entry.touch()
            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Optional custom TTL

        """
        with self._lock:
            # Evict if at capacity (only if this is a new key)
            if key not in self._cache and len(self._cache) >= self.max_size:
                self._evict_lru()

            self._cache[key] = CacheEntry(
                key=key,
                value=value,
                ttl_seconds=ttl_seconds or self.default_ttl,
            )

    def delete(self, key: str) -> bool:
        """Delete entry from cache.

        Args:
            key: Cache key

        Returns:
            True if deleted, False if not found

        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> int:
        """Clear all cache entries.

        Returns:
            Number of entries cleared

        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def _evict_lru(self) -> None:
        """Evict least recently used entry."""
        if not self._cache:
            return

        # First, evict expired entries (preserve OrderedDict)
        expired = [k for k, v in self._cache.items() if v.is_expired]
        for k in expired:
            del self._cache[k]

        if len(self._cache) < self.max_size:
            return

        # Find and evict LRU entry (oldest accessed_at)
        lru_key = min(self._cache, key=lambda k: self._cache[k].accessed_at)
        del self._cache[lru_key]

    def stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Cache statistics dict

        """
        total_hits = sum(e.hits for e in self._cache.values())
        expired = sum(1 for e in self._cache.values() if e.is_expired)

        return {
            "entries": len(self._cache),
            "max_size": self.max_size,
            "total_hits": total_hits,
            "expired": expired,
        }


class FileCache:
    """File-based persistent cache."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        max_size_mb: int = 100,
        default_ttl: int = 86400,
    ):
        """Initialize file cache.

        Args:
            cache_dir: Cache directory
            max_size_mb: Maximum cache size in MB
            default_ttl: Default TTL in seconds

        """
        self.cache_dir = cache_dir or get_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.default_ttl = default_ttl
        # In-memory index: {filename: (size, mtime)} — avoids disk scan in _ensure_space
        self._index: dict[str, tuple[int, float]] = {}
        self._total_size: int = 0
        self._lock = threading.Lock()
        self._rebuild_index()

    def _get_path(self, key: str) -> Path:
        """Get file path for cache key."""
        # Use first 2 chars for subdirectory to avoid too many files in one dir
        subdir = self.cache_dir / key[:2]
        subdir.mkdir(exist_ok=True)
        return subdir / f"{key}.cache"

    def _serialize_entry(self, entry: CacheEntry) -> dict[str, Any]:
        """Serialize a CacheEntry to a JSON-safe dict."""
        value = entry.value
        # Handle numpy arrays and other non-JSON-native types
        if np is not None and isinstance(value, np.ndarray):
            value = {
                "__type__": "ndarray",
                "data": value.tolist(),
                "dtype": str(value.dtype),
            }
        elif not isinstance(value, str | int | float | bool | type(None) | list | dict):
            value = str(value)
        return {
            "key": entry.key,
            "value": value,
            "created_at": entry.created_at,
            "accessed_at": entry.accessed_at,
            "ttl_seconds": entry.ttl_seconds,
            "hits": entry.hits,
        }

    def _deserialize_entry(self, data: dict[str, Any]) -> CacheEntry:
        """Deserialize a dict to a CacheEntry."""
        value = data.get("value")
        # Handle numpy array serialization
        if isinstance(value, dict) and value.get("__type__") == "ndarray" and np is not None:
            value = np.array(value.get("data"), dtype=value.get("dtype", "float32"))
        return CacheEntry(
            key=data["key"],
            value=value,
            created_at=data.get("created_at", time.time()),
            accessed_at=data.get("accessed_at", time.time()),
            ttl_seconds=data.get("ttl_seconds", self.default_ttl),
            hits=data.get("hits", 0),
        )

    def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None

        """
        path = self._get_path(key)

        if not path.exists():
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            entry = self._deserialize_entry(data)

            if entry.is_expired:
                with self._lock:
                    path.unlink(missing_ok=True)
                    self._index.pop(str(path), None)
                return None

            entry.touch()
            # Update file mtime for LRU ordering without full rewrite
            os.utime(path, None)

            return entry.value
        # Read path touches the filesystem (unlink/utime → OSError) and may
        # parse/validate cached values (→ ValueError/TypeError). Narrow the
        # blind Exception so real programming errors are not masked (BLE001).
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"[CACHE] Failed to read {key}: {e}")
            with self._lock:
                path.unlink(missing_ok=True)
                self._index.pop(str(path), None)
            return None

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Optional custom TTL

        """
        with self._lock:
            self._ensure_space()

            entry = CacheEntry(
                key=key,
                value=value,
                ttl_seconds=ttl_seconds or self.default_ttl,
            )

            path = self._get_path(key)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self._serialize_entry(entry), f)
                # Update in-memory index
                st = path.stat()
                old_size = self._index.get(str(path), (0, 0))[0]
                self._index[str(path)] = (st.st_size, st.st_mtime)
                self._total_size += st.st_size - old_size
            except (OSError, ValueError) as e:
                logger.warning(f"[CACHE] Failed to write {key}: {e}")

    def delete(self, key: str) -> bool:
        """Delete entry from cache."""
        path = self._get_path(key)
        with self._lock:
            if path.exists():
                size = self._index.get(str(path), (0, 0))[0]
                path.unlink()
                self._index.pop(str(path), None)
                self._total_size -= size
                return True
            return False

    def clear(self) -> int:
        """Clear all cache entries."""
        count = 0
        for path in self.cache_dir.rglob("*.cache"):
            path.unlink()
            count += 1
        self._index.clear()
        self._total_size = 0
        return count

    def _rebuild_index(self) -> None:
        """Scan disk once and populate the in-memory index."""
        self._index.clear()
        self._total_size = 0
        vanished = 0
        for path in self.cache_dir.rglob("*.cache"):
            try:
                st = path.stat()
                key = str(path)
                self._index[key] = (st.st_size, st.st_mtime)
                self._total_size += st.st_size
            except FileNotFoundError:
                # The file was evicted between rglob and stat. Benign race with a
                # concurrent eviction; counted and reported once after the scan.
                vanished += 1
                continue

        if vanished:
            logger.warning(
                "%d cache file(s) disappeared while rebuilding the index; the tracked "
                "total size may understate what is on disk.",
                vanished,
            )

    def _ensure_space(self) -> None:
        """Ensure cache is within size limits using in-memory index."""
        if self._total_size < self.max_size_bytes:
            return

        # Sort by mtime (oldest first) for LRU eviction
        sorted_entries = sorted(self._index.items(), key=lambda x: x[1][1])
        target = int(self.max_size_bytes * 0.8)

        for filepath, (size, _) in sorted_entries:
            if self._total_size < target:
                break
            try:
                Path(filepath).unlink()
                self._total_size -= size
                del self._index[filepath]
            except FileNotFoundError:
                self._total_size -= size
                self._index.pop(filepath, None)
            except OSError as exc:
                logger.warning(
                    "Cannot evict cache file %s (%s); it stays on disk and the tracked "
                    "cache size now overstates the space actually reclaimable.",
                    filepath,
                    exc,
                )

    def cleanup_expired(self) -> int:
        """Remove expired entries.

        Returns:
            Number of entries removed

        """
        removed = 0
        for path in list(self._index.keys()):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                entry = self._deserialize_entry(data)
                if entry.is_expired:
                    Path(path).unlink(missing_ok=True)
                    size = self._index.pop(path, (0, 0))[0]
                    self._total_size -= size
                    removed += 1
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                self._index.pop(path, None)
                removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        """Get cache statistics using in-memory index."""
        # Clean up any stale index entries first
        stale_keys = [k for k in self._index if not Path(k).exists()]
        for k in stale_keys:
            self._total_size -= self._index.pop(k, (0, 0))[0]

        expired = 0
        for path in list(self._index.keys()):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                entry = self._deserialize_entry(data)
                if entry.is_expired:
                    expired += 1
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Cannot read cache entry %s (%s); it is not counted in the expiry "
                    "statistics and will never be reported as expired.",
                    path,
                    exc,
                )

        return {
            "entries": len(self._index),
            "total_size_mb": round(self._total_size / (1024 * 1024), 2),
            "max_size_mb": self.max_size_bytes // (1024 * 1024),
            "index_stale": len(stale_keys),
            "expired": expired,
        }


class ResultCache:
    """High-level caching interface for workflow results."""

    def __init__(
        self,
        memory_cache: MemoryCache | None = None,
        file_cache: FileCache | None = None,
        enabled: bool = True,
    ):
        """Initialize result cache.

        Args:
            memory_cache: Memory cache instance
            file_cache: File cache instance
            enabled: Whether caching is enabled

        """
        self.memory_cache = memory_cache or MemoryCache()
        self.file_cache = file_cache or FileCache()
        self.enabled = enabled
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def get_cached_result(
        self,
        prompt: str,
        recipe: str,
        model: str = "",
    ) -> str | None:
        """Get cached result for prompt+recipe combination.

        Args:
            prompt: The input prompt
            recipe: The recipe content
            model: Model identifier

        Returns:
            Cached result or None

        """
        if not self.enabled:
            return None

        key = compute_cache_key(prompt, recipe, model)

        # Try memory cache first
        result = self.memory_cache.get(key)
        if result is not None:
            with self._lock:
                self._hits += 1
            logger.debug(f"[CACHE] Memory hit: {key[:16]}...")
            return result  # type: ignore[no-any-return]

        # Try file cache
        result = self.file_cache.get(key)
        if result is not None:
            with self._lock:
                self._hits += 1
            # Promote to memory cache
            self.memory_cache.set(key, result)
            logger.debug(f"[CACHE] File hit: {key[:16]}...")
            return result  # type: ignore[no-any-return]

        with self._lock:
            self._misses += 1
        return None

    def cache_result(
        self,
        prompt: str,
        recipe: str,
        result: str,
        model: str = "",
        ttl_seconds: int | None = None,
    ) -> None:
        """Cache a result.

        Args:
            prompt: The input prompt
            recipe: The recipe content
            result: The result to cache
            model: Model identifier
            ttl_seconds: Optional custom TTL

        """
        if not self.enabled:
            return

        key = compute_cache_key(prompt, recipe, model)

        # Store in both caches
        self.memory_cache.set(key, result, ttl_seconds)
        self.file_cache.set(key, result, ttl_seconds)

        logger.debug(f"[CACHE] Stored: {key[:16]}...")

    def invalidate(
        self,
        prompt: str,
        recipe: str,
        model: str = "",
    ) -> None:
        """Invalidate a cached result.

        Args:
            prompt: The input prompt
            recipe: The recipe content
            model: Model identifier

        """
        key = compute_cache_key(prompt, recipe, model)
        self.memory_cache.delete(key)
        self.file_cache.delete(key)

    def clear(self) -> dict[str, int]:
        """Clear all caches.

        Returns:
            Count of cleared entries per cache

        """
        return {
            "memory": self.memory_cache.clear(),
            "file": self.file_cache.clear(),
        }

    def stats(self) -> dict[str, Any]:
        """Get combined cache statistics."""
        with self._lock:
            hit_rate = round(self._hits / max(1, self._hits + self._misses) * 100, 1)
            hits = self._hits
            misses = self._misses
        return {
            "enabled": self.enabled,
            "hits": hits,
            "misses": misses,
            "hit_rate": hit_rate,
            "memory": self.memory_cache.stats(),
            "file": self.file_cache.stats(),
        }


# Global cache instance
_result_cache: ResultCache | None = None


def get_result_cache(enabled: bool = True) -> ResultCache:
    """Get or create the global result cache.

    Args:
        enabled: Whether caching is enabled

    Returns:
        ResultCache instance

    """
    global _result_cache
    if _result_cache is None:
        _result_cache = ResultCache(enabled=enabled)
    elif _result_cache.enabled != enabled:
        # The singleton used to be returned as-is, which silently DISCARDED
        # an explicit `enabled` argument: whichever caller happened to build
        # the cache first won, and every later caller got that setting
        # regardless of what it asked for. A caller that requested a disabled
        # cache could be handed an enabled one (and vice versa) with no
        # signal. Honour the request instead.
        logger.debug(
            "[CACHE] get_result_cache(enabled=%s) differs from existing "
            "singleton (enabled=%s); applying requested setting.",
            enabled,
            _result_cache.enabled,
        )
        _result_cache.enabled = enabled
    return _result_cache


if __name__ == "__main__":
    # Demo cache functionality
    cache = ResultCache()

    # Cache a result
    prompt = "Analyze the codebase"
    recipe = "You are a research-planner agent..."
    result = "Analysis result: The codebase has 10 modules..."

    cache.cache_result(prompt, recipe, result)
    logger.info("Cached result")

    # Retrieve from cache
    cached = cache.get_cached_result(prompt, recipe)
    logger.info(f"Retrieved: {cached[:50]}...")  # type: ignore[index]

    # Check stats
    logger.info(f"\nStats: {json.dumps(cache.stats(), indent=2)}")

    # Cleanup
    cache.clear()


# ============================================================================
# TURBOQUANT INTEGRATION - Phase 2A: Quantized Cache
# ============================================================================

# Disabled by default — 3-bit quantization is lossy and corrupts string cache values.
# Only enable for numeric/vector workloads where lossy compression is acceptable.
# See core/turboquant.py for detailed limitations documentation.
TURBOQUANT_CACHE_ENABLED = os.environ.get("TURBOQUANT_CACHE_ENABLED", "false").lower() == "true"

# Metric counter for monitoring TurboQuant string-skip events
_turboquant_string_skips: int = 0


@dataclass
class QuantizedCacheEntry(CacheEntry):
    """Cache entry with TurboQuant 3-bit compression."""

    compressed_value: bytes | None = None
    rotation_seed: int = 0
    original_size: int = 0
    compression_ratio: float = 1.0
    is_compressed: bool = False
    original_shape: tuple[int, ...] = (1,)


class QuantizedMemoryCache(MemoryCache):
    """Memory cache with optional TurboQuant compression.

    Provides 5.3x larger effective cache with same memory footprint.

    .. warning::
        TurboQuant 3-bit quantization is LOSSY and CORRUPTS string values.
        String values are automatically stored uncompressed when TurboQuant
        is enabled. Only numeric and vector data should be compressed.
        Use ``force=True`` on ``put()`` to override this guard (not recommended
        for string data).
    """

    def __init__(self, max_size: int = 100, default_ttl: int = 86400, use_turboquant: bool = True):
        super().__init__(max_size, default_ttl)
        self.use_turboquant = use_turboquant and TURBOQUANT_CACHE_ENABLED
        self._compression_stats = {"hits": 0, "misses": 0, "savings_bytes": 0}

        if self.use_turboquant:
            logger.warning(
                "TurboQuant cache compression ENABLED. "
                "3-bit quantization is LOSSY — only suitable for numeric/vector data. "
                "String values will be stored uncompressed automatically. "
                "See core/turboquant.py for limitations."
            )

    def set(  # type: ignore[override]
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """Set value in cache, bypassing TurboQuant compression for strings.

        Overrides MemoryCache.set() to ensure that str/bytes/bytearray
        values are never passed through TurboQuant's lossy 3-bit
        quantization.  Strings and bytes are stored uncompressed directly
        via the parent class; all other types are delegated to ``put()``
        which handles compression for eligible numeric/vector data.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl_seconds: Optional custom TTL (mapped to ``put(ttl=…)``).

        """
        if isinstance(value, str):
            logger.warning(
                f"[CACHE] Skipping TurboQuant for str value (key={key}) — "
                "stored uncompressed per VULN-002 remediation."
            )
            # Store directly via the parent class — no compression path.
            super().set(key, value, ttl_seconds=ttl_seconds)
        else:
            # Delegate to put() which has the full compression logic
            # for numeric/vector types and its own str/bytes guard.
            self.put(key, value, ttl=ttl_seconds)

    def _should_compress(self, value: Any) -> bool:
        """Determine if value should be compressed."""
        if not self.use_turboquant:
            return False
        if not turboquant_available():
            return False
        size = sys.getsizeof(value)
        return size > 1024  # Only compress values > 1KB

    def get(self, key: str) -> Any | None:
        """Get value from cache, decompressing if needed."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._compression_stats["misses"] += 1
                return None

            if entry.is_expired:
                del self._cache[key]
                self._compression_stats["misses"] += 1
                return None

            entry.touch()
            self._compression_stats["hits"] += 1

            if isinstance(entry, QuantizedCacheEntry) and entry.is_compressed:
                if entry.compressed_value is not None:
                    try:
                        shape = entry.original_shape
                        decompressed = simple_turboquant_decompress(
                            entry.compressed_value,
                            entry.rotation_seed,
                            shape,
                            bits=3,
                        )
                        # If the original value was a scalar (int/float),
                        # extract and return it as a Python scalar
                        if shape == (1,):
                            return float(decompressed.flat[0])
                        # Otherwise return the ndarray with original shape
                        return decompressed
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Decompression failed for key {key}: {e}")
                        return None
                return None

            return entry.value

    def put(self, key: str, value: Any, ttl: int | None = None, *, force: bool = False) -> None:
        """Store value in cache, compressing if beneficial.

        String and bytes values are ALWAYS stored uncompressed because 3-bit
        quantization corrupts such data irreversibly. The ``force`` parameter
        is DEPRECATED — even with ``force=True``, string/bytes compression is
        blocked at the TurboQuant level (``compress()`` raises ``TypeError``).

        Args:
            key: Cache key.
            value: Value to store.
            ttl: Time-to-live in seconds (uses default if None).
            force: DEPRECATED — has no effect for str/bytes values.
                   Retained for API compatibility.

        """
        _ = force  # deprecated no-op; read so vulture doesn't flag the kept param
        global _turboquant_string_skips

        with self._lock:
            # ═══ SECURITY GUARD ═══════════════════════════════════════════════
            # TurboQuant MUST NEVER compress str, bytes, or bytearray values.
            # Quantization of string data produces corrupted output. This guard
            # is ENFORCED at both the cache layer AND the TurboQuant level:
            # - QuantizedMemoryCache.put() stores str/bytes uncompressed
            # - TurboQuantCompressor.compress() raises TypeError for non-ndarray
            # These dual guards ensure the security invariant is always maintained
            # regardless of how the cache is accessed.
            # ═══════════════════════════════════════════════════════════════════
            if isinstance(value, str | bytes | bytearray):
                _turboquant_string_skips += 1
                logger.debug(
                    f"Bypassing TurboQuant for {type(value).__name__} value "
                    f"(key={key}) — stored uncompressed per security policy."
                )
                entry = CacheEntry(key=key, value=value, ttl_seconds=ttl or self.default_ttl)
            elif self._should_compress(value) and turboquant_available() and np is not None:
                try:
                    if isinstance(value, int | float):
                        arr = np.array([value], dtype=np.float32)
                    else:
                        # All compressible types must be ndarray-compatible
                        # str/bytes/bytearray are already handled by the guard above
                        arr = np.asarray(value, dtype=np.float32)

                    shape = arr.shape
                    compressed, seed = simple_turboquant_compress(arr, bits=3)
                    entry = QuantizedCacheEntry(
                        key=key,
                        value=None,
                        compressed_value=compressed,
                        rotation_seed=seed,
                        original_size=arr.nbytes,
                        compression_ratio=3.0,  # Approximate
                        is_compressed=True,
                        original_shape=shape,
                        ttl_seconds=ttl or self.default_ttl,
                    )
                    self._compression_stats["savings_bytes"] += arr.nbytes - len(compressed)
                # TurboQuant intentionally raises TypeError for non-ndarray
                # input and ValueError on bad shapes — those are the documented,
                # expected compression failures; anything else should surface.
                except (TypeError, ValueError) as e:
                    logger.warning(f"Compression failed for key {key}: {e}")
                    entry = CacheEntry(key=key, value=value, ttl_seconds=ttl or self.default_ttl)
            else:
                entry = CacheEntry(key=key, value=value, ttl_seconds=ttl or self.default_ttl)

            if key not in self._cache and len(self._cache) >= self.max_size:
                self._evict_lru()

            self._cache[key] = entry

    def get_stats(self) -> dict:
        """Get compression statistics."""
        return {
            **self._compression_stats,
            "size": len(self._cache),
            "compression_enabled": self.use_turboquant,
        }
