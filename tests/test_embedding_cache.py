"""Tests for EmbeddingCache — SHA-256 keyed, disk-backed LRU embedding cache.

Validates:
- Basic get/put round-trip
- Cache misses return None
- get_or_compute batches uncached chunks
- LRU eviction behaviour (cap/max)
- Thread safety (concurrent reads)
- Stats counters (hits, misses, evictions)
- Singleton get_embedding_cache()
- Corrupt entry recovery
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import numpy as np
import pytest

from beagle.context.embedding_cache import (
    EmbeddingCache,
    get_cache_stats,
    get_embedding_cache,
)


class TestEmbeddingCacheRoundTrip:
    """Test basic put / get / miss behaviour."""

    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache = EmbeddingCache(
            cache_dir=Path(self.tmpdir.name) / "ecache",
            max_entries=500,
        )

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_put_and_get(self):
        """Embedding round-trips through put → get."""
        chunk = "The quick brown fox jumps over the lazy dog."
        emb = np.random.randn(768).astype(np.float32)

        self.cache.put(chunk, emb)
        result = self.cache.get(chunk)

        assert result is not None
        assert np.allclose(result, emb, atol=1e-6)

    def test_miss_returns_none(self):
        """Uncached chunk returns None."""
        result = self.cache.get("never-before-seen-text")
        assert result is None

    def test_different_chunks_different_keys(self):
        """Two distinct texts produce independent cache entries."""
        a = "alpha text"
        b = "beta text"
        ea = np.array([1.0, 2.0], dtype=np.float32)
        eb = np.array([3.0, 4.0], dtype=np.float32)

        self.cache.put(a, ea)
        self.cache.put(b, eb)

        assert np.allclose(self.cache.get(a), ea)  # type: ignore[arg-type]
        assert np.allclose(self.cache.get(b), eb)  # type: ignore[arg-type]

    def test_same_chunk_overwrites(self):
        """Re-put with same text overwrites."""
        chunk = "overwrite me"
        e1 = np.array([0.1], dtype=np.float32)
        e2 = np.array([9.9], dtype=np.float32)

        self.cache.put(chunk, e1)
        self.cache.put(chunk, e2)
        result = self.cache.get(chunk)

        assert np.allclose(result, e2)  # type: ignore[arg-type]


class TestGetOrCompute:
    """Test batch get_or_compute path."""

    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache = EmbeddingCache(cache_dir=Path(self.tmpdir.name) / "ec")

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_all_uncached_computes_all(self):
        """When nothing is cached, compute_fn receives all chunks."""
        chunks = ["hello", "world", "test"]
        call_log: list[list[str]] = []

        def fake_embed(batch: list[str]) -> np.ndarray:
            call_log.append(batch)
            return np.array([[hash(s) % 100 / 100.0] for s in batch], dtype=np.float32)

        result = self.cache.get_or_compute(chunks, fake_embed)

        assert len(call_log) == 1
        assert call_log[0] == chunks
        assert result.shape == (3, 1)

    def test_partially_cached_only_computes_uncached(self):
        """Pre-cached chunks skip the compute_fn."""
        cached = "already-here"
        uncached = "needs-work"
        self.cache.put(cached, np.array([9.9], dtype=np.float32))

        call_log: list[list[str]] = []

        def fake_embed(batch: list[str]) -> np.ndarray:
            call_log.append(batch)
            return np.array([[hash(s) % 50 / 50.0] for s in batch], dtype=np.float32)

        result = self.cache.get_or_compute([cached, uncached], fake_embed)

        assert len(call_log) == 1
        assert call_log[0] == [uncached]  # only uncached passed to fn
        # cached value preserved
        assert np.allclose(result[0], [9.9], atol=1e-6)

    def test_all_cached_skips_compute(self):
        """When every chunk is cached, compute_fn is never called."""
        chunks = ["a", "b"]
        for c in chunks:
            self.cache.put(c, np.array([1.0], dtype=np.float32))

        def never_called(_batch: list[str]) -> np.ndarray:
            pytest.fail("compute_fn should not be called")

        result = self.cache.get_or_compute(chunks, never_called)
        assert result.shape == (2, 1)


class TestLRUEviction:
    """Test capacity-bound eviction."""

    def test_eviction_triggers_at_limit(self):
        """When entries exceed EVICT_AT, oldest entries are purged."""
        cache = EmbeddingCache(
            cache_dir=Path(tempfile.mkdtemp()) / "evict_test",
            max_entries=5,
        )
        # Override the eviction threshold for testing
        # Put 14 entries (above EVICT_AT=12 default, but since max=5...)
        # Actually the eviction trigger is separate from max - let's test it directly
        import beagle.context.embedding_cache as ec

        old_evict_at = ec.EVICT_AT
        old_evict_batch = ec.EVICT_BATCH
        ec.EVICT_AT = 6
        ec.EVICT_BATCH = 3

        try:
            for i in range(10):
                cache.put(f"item-{i}", np.array([float(i)], dtype=np.float32))

            # After 10 puts with max_entries=5, should have evicted oldest
            # but at most max_entries remain
            count = 0
            for i in range(10):
                if cache.get(f"item-{i}") is not None:
                    count += 1
            assert count <= 5
        finally:
            ec.EVICT_AT = old_evict_at
            ec.EVICT_BATCH = old_evict_batch


class TestThreadSafety:
    """Test concurrent access."""

    def test_concurrent_reads_same_key(self):
        """Multiple threads reading the same cached key return consistent results."""
        tmpdir = tempfile.TemporaryDirectory()
        cache = EmbeddingCache(cache_dir=Path(tmpdir.name) / "ts")

        chunk = "shared-chunk"
        emb = np.array([3.14, 2.71], dtype=np.float32)
        cache.put(chunk, emb)

        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(50):
                    r = cache.get(chunk)
                    assert r is not None
                    assert np.allclose(r, emb, atol=1e-6)
            except Exception as exc:  # ruff: ignore[BLE001]
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestStats:
    """Test hit/miss/eviction counters."""

    def test_stats_increment(self):
        """Hits and misses are tracked correctly."""
        tmpdir = tempfile.TemporaryDirectory()
        cache = EmbeddingCache(cache_dir=Path(tmpdir.name) / "stats")

        cache.get("no-such-chunk")  # miss
        cache.get("also-missing")  # miss

        stats = get_cache_stats()
        assert stats["misses"] >= 2

        cache.put("real", np.array([0.0], dtype=np.float32))
        cache.get("real")  # hit

        stats = get_cache_stats()
        assert stats["hits"] >= 1


class TestSingleton:
    """Test the module-level singleton."""

    def test_get_embedding_cache_returns_same_instance(self):
        """get_embedding_cache() is idempotent."""
        c1 = get_embedding_cache()
        c2 = get_embedding_cache()
        assert c1 is c2


class TestCorruptRecovery:
    """Test graceful handling of corrupt cache files."""

    def test_corrupt_file_handled_gracefully(self):
        """A corrupt .npy file triggers a miss, not an exception."""
        tmpdir = tempfile.TemporaryDirectory()
        cache = EmbeddingCache(cache_dir=Path(tmpdir.name) / "corrupt")
        chunk = "garbage-in"

        # Manually write a non-.npy file at the expected path
        import hashlib

        key = hashlib.sha256(chunk.encode()).hexdigest()
        cache_path = Path(tmpdir.name) / "corrupt" / key[:2] / f"{key[2:]}.npy"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("not a numpy array")

        # Should NOT raise — just a miss
        result = cache.get(chunk)
        assert result is None


# ---------------------------------------------------------------------------
# Regression tests for D11 (Fable 5 DD 2026-06-11) — pseudo-embedding fallback
# used Python's builtin `hash()`, salted per-process since 3.3, so the same
# text produced different vectors on every restart. Replaced with
# hashlib.blake2b → 32-bit seed → numpy.RandomState.
# ---------------------------------------------------------------------------


class TestPseudoEmbeddingDeterminism_D11:
    """Regression tests for the per-process-hash defect (D11)."""

    def _adapter(self, dim=16):
        """Build an EmbeddingAdapter with the real embedder forced off."""
        from beagle.context.embedding_adapter import EmbeddingAdapter

        a = EmbeddingAdapter(dimension=dim)
        a._available = False  # force pseudo-embed path
        a._embedder = None
        return a

    def test_same_text_same_vector_across_calls(self):
        a = self._adapter()
        v1 = a._pseudo_embed_batch(["hello world"])[0]
        v2 = a._pseudo_embed_batch(["hello world"])[0]
        assert np.array_equal(v1, v2), "Same text must produce same vector"

    def test_same_text_same_vector_across_instances(self):
        """Two EmbeddingAdapter instances must agree (cross-process simulation)."""
        a1 = self._adapter()
        a2 = self._adapter()
        v1 = a1._pseudo_embed_batch(["hello world"])[0]
        v2 = a2._pseudo_embed_batch(["hello world"])[0]
        assert np.array_equal(v1, v2), "Two instances must agree on the vector for the same text"

    def test_different_text_different_vector(self):
        a = self._adapter()
        v1 = a._pseudo_embed_batch(["hello world"])[0]
        v2 = a._pseudo_embed_batch(["goodbye world"])[0]
        assert not np.array_equal(v1, v2), "Different text must produce different vectors"

    def test_empty_string_handled(self):
        a = self._adapter()
        v = a._pseudo_embed_batch([""])[0]
        assert v.shape == (16,)
        assert np.isfinite(v).all()

    def test_unicode_handled(self):
        a = self._adapter()
        v1 = a._pseudo_embed_batch(["λ 😀 test"])[0]
        v2 = a._pseudo_embed_batch(["λ 😀 test"])[0]
        assert np.array_equal(v1, v2), "Unicode strings must also be deterministic"

    def test_high_bit_digest_does_not_raise(self):
        """A digest with the high bit set must not break the 32-bit seed mask."""
        a = self._adapter()
        # Stress test with 1000 random texts to surface any seed-related
        # ValueError ("Seed must be between 0 and 2**32 - 1").
        import random

        random.seed(42)
        texts = [
            "".join(random.choices("abcdefghijklmnop ", k=random.randint(1, 100)))
            for _ in range(1000)
        ]
        vectors = a._pseudo_embed_batch(texts)
        assert len(vectors) == 1000
        assert all(np.isfinite(v).all() for v in vectors)
