"""Section 9.1: Secret cache TTL + clear_secret_cache() tests.

Validates that the secret cache enforces TTL expiry,
clear_secret_cache() works, and get_cache_ttl() is configurable.
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

from beagle.secrets_loader import (
    _secret_cache,
    clear_cache,
    clear_secret_cache,
    get_cache_ttl,
    load_secret,
)


class TestSecretCacheTTL:
    """Secret cache respects TTL expiry."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def test_default_ttl_is_300(self):
        """Default cache TTL is 300 seconds."""
        with patch.dict(os.environ, {}, clear=True):
            assert get_cache_ttl() == 300

    def test_ttl_from_env_var(self):
        """BEAGLE_SECRET_CACHE_TTL environment variable overrides default."""
        with patch.dict(os.environ, {"BEAGLE_SECRET_CACHE_TTL": "60"}):
            assert get_cache_ttl() == 60

    def test_ttl_zero_disables_caching(self):
        """TTL=0 means no caching — every call hits the source."""
        with patch.dict(os.environ, {"BEAGLE_SECRET_CACHE_TTL": "0"}):
            assert get_cache_ttl() == 0

    def test_ttl_negative_clamps_to_zero(self):
        """Negative TTL values are clamped to 0."""
        with patch.dict(os.environ, {"BEAGLE_SECRET_CACHE_TTL": "-10"}):
            assert get_cache_ttl() == 0

    def test_ttl_invalid_env_falls_back(self):
        """Non-numeric BEAGLE_SECRET_CACHE_TTL falls back to default."""
        with patch.dict(os.environ, {"BEAGLE_SECRET_CACHE_TTL": "not_a_number"}):
            assert get_cache_ttl() == 300

    def test_cached_entry_has_timestamp(self):
        """Cached entries store (value, timestamp) tuples."""
        with patch.dict(os.environ, {"TEST_SECRET_910": "abc123"}, clear=False):
            load_secret("TEST_SECRET_910", allow_file=False)
        cache_key = "TEST_SECRET_910:True:False"
        assert cache_key in _secret_cache
        entry = _secret_cache[cache_key]
        assert isinstance(entry, tuple)
        assert len(entry) == 2
        assert entry[0] == "abc123"
        assert isinstance(entry[1], float)
        assert entry[1] > 0

    def test_cache_hit_within_ttl(self):
        """Within TTL, cached value is returned without re-fetch."""
        with patch.dict(os.environ, {"TEST_911": "cached_value"}, clear=False):
            result1 = load_secret("TEST_911", allow_file=False)
            result2 = load_secret("TEST_911", allow_file=False)
        assert result1 == "cached_value"
        assert result2 == "cached_value"

    def test_cache_expiry_re_fetches(self):
        """After TTL expires, the secret is re-fetched from source."""
        with patch.dict(os.environ, {"TEST_912": "old_value"}, clear=False):
            load_secret("TEST_912", allow_file=False)

        # Manually backdate the cache entry to simulate expiry
        cache_key = "TEST_912:True:False"
        if cache_key in _secret_cache:
            value, _ = _secret_cache[cache_key]
            _secret_cache[cache_key] = (value, time.monotonic() - 600)

        # Now change the env var
        with patch.dict(os.environ, {"TEST_912": "new_value"}, clear=False):
            result = load_secret("TEST_912", allow_file=False)
        assert result == "new_value"

    def test_ttl_zero_always_fetches(self):
        """When TTL=0, every load_secret call fetches fresh value."""
        with patch.dict(
            os.environ, {"BEAGLE_SECRET_CACHE_TTL": "0", "TEST_913": "val1"}, clear=False
        ):
            load_secret("TEST_913", allow_file=False)
        # Cache should remain empty due to TTL=0
        cache_key = "TEST_913:True:False"
        assert cache_key not in _secret_cache or get_cache_ttl() == 0


class TestClearSecretCache:
    """clear_secret_cache() clears all cached entries."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def test_clear_cache_empties_all_entries(self):
        """clear_cache() removes all cached secrets."""
        with patch.dict(os.environ, {"TEST_920": "secret"}, clear=False):
            load_secret("TEST_920", allow_file=False)
        assert len(_secret_cache) > 0
        clear_cache()
        assert len(_secret_cache) == 0

    def test_clear_secret_cache_alias_works(self):
        """clear_secret_cache is an alias for clear_cache."""
        assert clear_secret_cache is clear_cache

    def test_clear_cache_allows_re_fetch(self):
        """After clearing, the next load re-fetches the secret."""
        with patch.dict(os.environ, {"TEST_921": "first"}, clear=False):
            result1 = load_secret("TEST_921", allow_file=False)

        clear_cache()

        with patch.dict(os.environ, {"TEST_921": "second"}, clear=False):
            result2 = load_secret("TEST_921", allow_file=False)

        assert result1 == "first"
        assert result2 == "second"

    def test_clear_cache_idempotent(self):
        """Calling clear_cache() on an empty cache is a no-op."""
        clear_cache()
        clear_cache()
        assert len(_secret_cache) == 0


# ---------------------------------------------------------------------------
# Regression tests for D8 (Fable 5 DD 2026-06-11) — load_secret coerced a
# YAML null (None) to the literal string "None", which is truthy and
# looked like a real (wrong) secret. These tests lock in the fix.
# ---------------------------------------------------------------------------


class TestLoadSecretNullCoercion_D8:
    """Regression tests for the None-coerced-to-'None' defect (D8)."""

    def test_none_value_coerces_to_empty_string(self, monkeypatch, tmp_path):
        """A YAML null in secrets.yaml must become '' not 'None'."""
        import stat as _stat

        from beagle import secrets_loader

        # Create a secrets.yaml with explicit nulls
        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("NULLY_KEY: null\nGOOD_KEY: 'real-value'\n")
        # _check_file_permissions requires 0o600
        secrets_file.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
        monkeypatch.setattr(secrets_loader, "_SECRETS_PATH", secrets_file)
        # Clear cache to ensure fresh lookup
        with secrets_loader._secret_cache_lock:
            secrets_loader._secret_cache.clear()

        # No env var set → falls through to file
        monkeypatch.delenv("NULLY_KEY", raising=False)
        result = secrets_loader.load_secret("NULLY_KEY", allow_env=False, allow_file=True)
        assert result == "", f"Expected '' for null YAML value, got {result!r}"
        assert result != "None", "Must not be the literal string 'None'"

    def test_good_value_still_loads(self, monkeypatch, tmp_path):
        """Real values must still pass through unchanged."""
        import stat as _stat

        from beagle import secrets_loader

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("GOOD_KEY: 'real-value'\n")
        # _check_file_permissions requires 0o600 (S01 strict-secrets doctrine).
        # Test files are created with the runner's umask (typically 0o022),
        # so explicitly tighten mode here.
        secrets_file.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
        monkeypatch.setattr(secrets_loader, "_SECRETS_PATH", secrets_file)
        with secrets_loader._secret_cache_lock:
            secrets_loader._secret_cache.clear()

        monkeypatch.delenv("GOOD_KEY", raising=False)
        result = secrets_loader.load_secret("GOOD_KEY", allow_env=False, allow_file=True)
        assert result == "real-value"

    def test_missing_key_returns_empty_string(self, monkeypatch, tmp_path):
        """A key that's not in the file must return '', not 'None'."""
        import stat as _stat

        from beagle import secrets_loader

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("OTHER_KEY: 'x'\n")
        secrets_file.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
        monkeypatch.setattr(secrets_loader, "_SECRETS_PATH", secrets_file)
        with secrets_loader._secret_cache_lock:
            secrets_loader._secret_cache.clear()

        monkeypatch.delenv("MISSING_KEY", raising=False)
        result = secrets_loader.load_secret("MISSING_KEY", allow_env=False, allow_file=True)
        assert result == ""
        assert result != "None"
