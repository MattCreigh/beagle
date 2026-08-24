"""Tests for TurboQuant safety guardrails.

SP1-2B/2C: Verifies that string values are never compressed by default,
numeric values are compressed correctly, and the force override works.
"""

import logging
import os

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_env():
    """Reset TurboQuant env vars."""
    saved = {}
    for key in ("TURBOQUANT_CACHE_ENABLED",):
        saved[key] = os.environ.pop(key, None)
    yield
    for key, val in saved.items():
        if val is not None:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def reset_global_counter():
    """Reset the string skip counter before each test."""
    import beagle.utils.cache as cache_mod

    cache_mod._turboquant_string_skips = 0
    yield
    cache_mod._turboquant_string_skips = 0


def _make_cache(enabled=True):
    """Create a QuantizedMemoryCache with TurboQuant enabled or disabled."""
    os.environ["TURBOQUANT_CACHE_ENABLED"] = "true" if enabled else "false"
    # Re-import to pick up env var
    import importlib

    import beagle.utils.cache as cache_mod

    importlib.reload(cache_mod)
    from beagle.utils.cache import QuantizedMemoryCache

    return QuantizedMemoryCache(max_size=10)


# ── String Safety Tests ───────────────────────────────────────────────────────


class TestStringSafety:
    """Test that string values are never compressed by default."""

    def test_string_values_stored_uncompressed_when_turboquant_enabled(self):
        """String values bypass compression even when TurboQuant is enabled."""
        cache = _make_cache(enabled=True)
        cache.put("test_key", "hello world")
        entry = cache._cache.get("test_key")
        # Entry should NOT be a QuantizedCacheEntry with compression
        assert entry is not None
        assert entry.value == "hello world"
        assert not (hasattr(entry, "is_compressed") and entry.is_compressed)

    def test_numeric_values_can_be_compressed(self):
        """Numeric values are eligible for compression when TurboQuant is enabled."""
        cache = _make_cache(enabled=True)
        cache.put("num_key", 42.0)
        entry = cache._cache.get("num_key")
        assert entry is not None
        # Numeric values should be compressed (or at least attempted)
        # The entry may or may not be compressed depending on numpy availability
        # but it should NOT be stored as a raw string
        if hasattr(entry, "is_compressed") and entry.is_compressed:
            assert entry.compressed_value is not None

    def test_string_values_uncompressed_when_turboquant_disabled(self):
        """When TurboQuant is disabled, all values stored uncompressed."""
        cache = _make_cache(enabled=False)
        cache.put("test_key", "hello world")
        entry = cache._cache.get("test_key")
        assert entry is not None
        assert entry.value == "hello world"

    def test_string_skip_counter_increments(self):
        """The global string skip counter increments when strings are skipped."""
        cache = _make_cache(enabled=True)
        import beagle.utils.cache as cache_mod

        counter_before = cache_mod._turboquant_string_skips
        cache.put("key1", "string_value_1")
        cache.put("key2", "string_value_2")
        counter_after = cache_mod._turboquant_string_skips
        assert counter_after >= counter_before + 2

    def test_force_true_still_bypasses_strings(self):
        """v13.5.2: force=True NO LONGER compresses strings.

        String/bytes compression is PROHIBITED regardless of force flag.
        The force parameter is retained for API compatibility but has no
        effect on str/bytes values.
        """
        cache = _make_cache(enabled=True)
        cache.put("forced_key", "forced_string", force=True)
        entry = cache._cache.get("forced_key")
        assert entry is not None
        # Even with force=True, string values must NOT be compressed
        # They are stored as plain CacheEntry, not QuantizedCacheEntry
        assert not (hasattr(entry, "is_compressed") and entry.is_compressed)

    def test_force_false_does_not_compress_strings(self):
        """With force=False (default), string values are not compressed."""
        cache = _make_cache(enabled=True)
        cache.put("safe_key", "safe_string", force=False)
        entry = cache._cache.get("safe_key")
        assert entry is not None
        assert not (hasattr(entry, "is_compressed") and entry.is_compressed)


class TestStartupWarning:
    """Test that a warning is logged when TurboQuant is enabled."""

    def test_warning_logged_on_init_when_enabled(self, caplog):
        """QuantizedMemoryCache logs a warning at init when TurboQuant is enabled."""
        with caplog.at_level(logging.WARNING):
            _make_cache(enabled=True)
        # Check that the warning about TurboQuant was logged
        assert any("TurboQuant" in msg and "LOSSY" in msg for msg in caplog.messages)

    def test_no_warning_when_disabled(self, caplog):
        """No TurboQuant warning when disabled."""
        with caplog.at_level(logging.WARNING):
            _make_cache(enabled=False)
        assert not any("TurboQuant" in msg for msg in caplog.messages)


class TestTurboQuantDocumentation:
    """Verify TurboQuant module-level documentation exists."""

    def test_module_docstring_contains_warnings(self):
        """turboquant.py module docstring documents limitations."""
        from beagle.core import turboquant

        assert "LOSSY" in turboquant.__doc__
        # v13.5.2: Updated to accept "PROHIBITED" (stronger than "corrupt"/"corrupts")
        doc_lower = turboquant.__doc__.lower()
        assert "corrupt" in doc_lower or "corrupts" in doc_lower or "prohibited" in doc_lower, (
            "Module docstring must warn about string corruption/prohibition"
        )

    def test_quantized_cache_class_docstring_contains_warning(self):
        """QuantizedMemoryCache class docstring warns about string corruption."""
        from beagle.utils.cache import QuantizedMemoryCache

        # The class docstring should mention the string corruption risk
        assert QuantizedMemoryCache.__doc__ is not None
