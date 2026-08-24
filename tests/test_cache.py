"""Tests for Result Caching Layer.

Comprehensive tests for:
- MemoryCache (LRU, TTL, thread safety)
- FileCache (persistence, expiration)
- ResultCache (factory, enable/disable)
- QuantizedMemoryCache (compression)
- Cache key computation
"""

from __future__ import annotations

import hashlib

# Add project root to path
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from beagle.utils.cache import (  # ruff: ignore[E402]
    CacheEntry,
    compute_cache_key,
    get_cache_dir,
    get_result_cache,
)


class TestCacheEntry:
    """Test CacheEntry dataclass."""

    def test_cache_entry_creation(self):
        """CacheEntry can be created with default values."""
        entry = CacheEntry(key="test_key", value={"data": "test"})

        assert entry.key == "test_key"
        assert entry.value == {"data": "test"}
        assert entry.hits == 0
        assert entry.ttl_seconds == 86400  # 24 hours default
        assert entry.created_at > 0
        assert entry.accessed_at > 0

    def test_cache_entry_is_expired(self):
        """is_expired returns True when TTL elapsed."""
        entry = CacheEntry(
            key="test",
            value="data",
            ttl_seconds=1,
            created_at=time.time() - 10,  # Created 10 seconds ago
        )

        assert entry.is_expired is True

    def test_cache_entry_not_expired(self):
        """is_expired returns False when TTL not elapsed."""
        entry = CacheEntry(key="test", value="data", ttl_seconds=3600)

        assert entry.is_expired is False

    def test_cache_entry_touch(self):
        """touch updates accessed_at and increments hits."""
        entry = CacheEntry(key="test", value="data")
        old_accessed = entry.accessed_at

        time.sleep(0.01)  # Small delay
        entry.touch()

        assert entry.accessed_at > old_accessed
        assert entry.hits == 1


class TestComputeCacheKey:
    """Test content-addressable cache key computation."""

    def test_cache_key_consistent(self):
        """Same inputs produce same key."""
        key1 = compute_cache_key("prompt", "recipe", "model")
        key2 = compute_cache_key("prompt", "recipe", "model")

        assert key1 == key2
        assert len(key1) == 64  # SHA256 hex digest length

    def test_cache_key_different_prompt(self):
        """Different prompts produce different keys."""
        key1 = compute_cache_key("prompt1", "recipe", "model")
        key2 = compute_cache_key("prompt2", "recipe", "model")

        assert key1 != key2

    def test_cache_key_different_recipe(self):
        """Different recipes produce different keys."""
        key1 = compute_cache_key("prompt", "recipe1", "model")
        key2 = compute_cache_key("prompt", "recipe2", "model")

        assert key1 != key2

    def test_cache_key_different_model(self):
        """Different models produce different keys."""
        key1 = compute_cache_key("prompt", "recipe", "model1")
        key2 = compute_cache_key("prompt", "recipe", "model2")

        assert key1 != key2

    def test_cache_key_empty_model(self):
        """Empty model is valid."""
        key = compute_cache_key("prompt", "recipe", "")

        assert len(key) == 64

    def test_cache_key_special_characters(self):
        """Handles special characters in inputs."""
        key = compute_cache_key("hello\nworld", "recipe\x00data", "模型")

        assert len(key) == 64

    def test_cache_key_is_sha256(self):
        """Key is valid SHA256 hex digest."""

        prompt, recipe, model = "test_prompt", "test_recipe", "test_model"
        expected = hashlib.sha256(f"{prompt}|||{recipe}|||{model}".encode()).hexdigest()
        actual = compute_cache_key(prompt, recipe, model)

        assert actual == expected


class TestGetCacheDir:
    """Test cache directory path resolution."""

    def test_cache_dir_returns_path(self):
        """get_cache_dir returns a Path object."""
        result = get_cache_dir()

        assert isinstance(result, Path)

    def test_cache_dir_is_string(self):
        """get_cache_dir returns a valid string path."""
        result = str(get_cache_dir())

        assert isinstance(result, str)
        assert len(result) > 0


class TestMemoryCache:
    """Test MemoryCache implementation."""

    def test_memory_cache_creation(self):
        """MemoryCache can be created with defaults."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()

        assert cache.max_size == 100
        assert cache.default_ttl == 86400
        assert len(cache._cache) == 0

    def test_memory_cache_set_get(self):
        """Basic set and get operations work."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1")

        result = cache.get("key1")

        assert result == "value1"

    def test_memory_cache_get_missing(self):
        """get returns None for missing keys."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()

        result = cache.get("nonexistent")

        assert result is None

    def test_memory_cache_set_overwrite(self):
        """set overwrites existing values."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1")
        cache.set("key1", "value2")

        result = cache.get("key1")

        assert result == "value2"

    def test_memory_cache_lru_eviction(self):
        """LRU eviction removes oldest entries when full."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(max_size=3)

        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("d", 4)  # Should evict "a"

        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3
        assert cache.get("d") == 4

    def test_memory_cache_ttl_expiration(self):
        """Entries expire after TTL."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(default_ttl=1)  # 1 second TTL

        cache.set("key1", "value1")
        time.sleep(1.5)

        result = cache.get("key1")

        assert result is None

    def test_memory_cache_delete(self):
        """delete removes entries."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1")
        cache.delete("key1")

        result = cache.get("key1")

        assert result is None

    def test_memory_cache_clear(self):
        """clear removes all entries."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()

        assert len(cache._cache) == 0

    def test_memory_cache_contains(self):
        """Contains check via get works correctly."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()
        cache.set("key1", "value1")

        assert cache.get("key1") is not None
        assert cache.get("key2") is None

    def test_memory_cache_stats(self):
        """Stats method returns correct count."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()

        stats = cache.stats()
        assert stats["entries"] == 0

        cache.set("a", 1)
        cache.set("b", 2)

        stats = cache.stats()
        assert stats["entries"] == 2

    def test_memory_cache_thread_safety(self):
        """Concurrent access doesn't corrupt cache."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(max_size=1000)
        errors = []
        iterations = 100

        def writer_thread():
            try:
                for i in range(iterations):
                    cache.set(f"key_{threading.current_thread().name}_{i}", i)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        def reader_thread():
            try:
                for i in range(iterations):
                    cache.get(f"key_{i}")
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = [threading.Thread(target=writer_thread, name=f"writer_{i}") for i in range(5)] + [
            threading.Thread(target=reader_thread, name=f"reader_{i}") for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestFileCache:
    """Test FileCache implementation."""

    def test_file_cache_creation(self):
        """FileCache can be created."""
        from beagle.utils.cache import FileCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = FileCache(cache_dir=Path(tmpdir))

            assert str(cache.cache_dir) == tmpdir
            assert cache.default_ttl == 86400

    def test_file_cache_set_get(self):
        """Basic set and get with file persistence."""
        from beagle.utils.cache import FileCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = FileCache(cache_dir=Path(tmpdir))
            cache.set("key1", {"data": "value1"})

            result = cache.get("key1")

            assert result == {"data": "value1"}

    def test_file_cache_persistence(self):
        """Data persists across cache instances."""
        from beagle.utils.cache import FileCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache1 = FileCache(cache_dir=Path(tmpdir))
            cache1.set("key1", "persistent_value")

            # Create new instance
            cache2 = FileCache(cache_dir=Path(tmpdir))
            result = cache2.get("key1")

            assert result == "persistent_value"

    def test_file_cache_ttl_expiration(self):
        """File entries expire after TTL."""
        from beagle.utils.cache import FileCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = FileCache(cache_dir=Path(tmpdir), default_ttl=1)
            cache.set("key1", "value1")

            time.sleep(1.5)

            result = cache.get("key1")

            assert result is None

    def test_file_cache_delete(self):
        """delete removes file."""
        from beagle.utils.cache import FileCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = FileCache(cache_dir=Path(tmpdir))
            cache.set("key1", "value1")
            cache.delete("key1")

            result = cache.get("key1")

            assert result is None

    def test_file_cache_clear(self):
        """clear removes all files."""
        from beagle.utils.cache import FileCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = FileCache(cache_dir=Path(tmpdir))
            cache.set("a", 1)
            cache.set("b", 2)
            cache.clear()

            assert len(cache._index) == 0


class TestResultCache:
    """Test ResultCache factory and operations."""

    def test_get_result_cache_enabled(self):
        """get_result_cache returns cache when enabled."""
        cache = get_result_cache(enabled=True)

        assert cache is not None

    def test_get_result_cache_disabled(self):
        """get_result_cache returns disabled cache when disabled."""
        cache = get_result_cache(enabled=False)

        # A disabled cache should return None for get_cached_result
        result = cache.get_cached_result("prompt", "recipe", "model")
        assert result is None

    def test_result_cache_compute_key_and_store(self):
        """Can compute key and store result."""
        cache = get_result_cache(enabled=True)

        # Use cache_result and get_cached_result
        cache.cache_result("test_prompt", "test_recipe", "test_result", "test_model")

        result = cache.get_cached_result("test_prompt", "test_recipe", "test_model")

        assert result == "test_result"


class TestQuantizedMemoryCache:
    """Test QuantizedMemoryCache with compression."""

    def test_quantized_cache_creation(self):
        """QuantizedMemoryCache can be created."""
        from beagle.utils.cache import QuantizedMemoryCache

        cache = QuantizedMemoryCache()

        assert cache.max_size == 100
        assert cache.default_ttl == 86400

    def test_quantized_cache_set_get(self):
        """Quantized cache can store and retrieve."""
        from beagle.utils.cache import QuantizedMemoryCache

        cache = QuantizedMemoryCache()
        cache.set("key1", [1, 2, 3, 4, 5])

        result = cache.get("key1")

        # Should decompress back to original
        assert result is not None
        assert len(result) == 5


class TestCacheIntegration:
    """Integration tests for cache system."""

    def test_cache_workflow(self):
        """Complete cache workflow works end-to-end."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(max_size=10)

        # Store multiple entries
        for i in range(10):
            cache.set(f"key_{i}", f"value_{i}")

        # Verify all stored
        for i in range(10):
            assert cache.get(f"key_{i}") == f"value_{i}"

        # Add one more (should evict oldest)
        cache.set("key_10", "value_10")

        assert cache.get("key_0") is None
        assert cache.get("key_10") == "value_10"

    def test_cache_with_complex_objects(self):
        """Cache handles complex nested objects."""
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()

        complex_obj = {
            "metrics": {"tokens": 1000, "cost": 0.05},
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi!"},
            ],
            "metadata": {"model": "gpt-4", "temperature": 0.7},
        }

        cache.set("complex", complex_obj)
        result = cache.get("complex")

        assert result == complex_obj
        assert result["metrics"]["tokens"] == 1000
        assert len(result["messages"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
