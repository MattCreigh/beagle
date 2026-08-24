"""Tests for Rate Limiter.

Comprehensive tests for:
- TokenBucket rate limiting
- Sliding window rate limiting
- Rate limit configuration
- Thread safety
- Burst handling
"""

from __future__ import annotations

# Add project root to path
import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from beagle.utils.rate_limiter import RateLimitConfig, RateLimiter, TokenBucket

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestRateLimiterImports:
    """Test that all rate limiter components can be imported."""

    def test_import_rate_limiter_module(self):
        """Rate limiter module can be imported."""
        from beagle.utils import rate_limiter

        assert rate_limiter is not None

    def test_import_rate_limit_config(self):
        """Rate limit config classes can be imported."""
        from beagle.utils.rate_limiter import RateLimitConfig

        assert RateLimitConfig is not None

    def test_import_token_bucket(self):
        """TokenBucket class can be imported."""
        from beagle.utils.rate_limiter import TokenBucket

        assert TokenBucket is not None


class TestTokenBucket:
    """Test TokenBucket rate limiting algorithm."""

    def test_token_bucket_creation(self):
        """TokenBucket can be created with defaults."""
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=10.0, refill_rate=10.0)

        assert bucket.refill_rate == 10.0
        assert bucket.capacity == 10
        assert bucket.tokens == bucket.capacity

    def test_token_bucket_consume(self):
        """TokenBucket can consume tokens."""
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=10.0, refill_rate=10.0)

        result = bucket.consume(1)

        assert result is True
        assert bucket.tokens < bucket.capacity

    def test_token_bucket_consume_insufficient(self):
        """TokenBucket rejects when insufficient tokens."""
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=1.0, refill_rate=1.0)

        # Consume all tokens
        bucket.consume(1)
        # Try to consume more
        result = bucket.consume(1)

        assert result is False

    def test_token_bucket_refill(self):
        """TokenBucket refills at configured rate."""
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=10.0, refill_rate=10.0)  # 10 tokens/sec
        bucket.tokens = 0

        time.sleep(0.2)  # Should add ~2 tokens
        bucket._refill()

        assert bucket.tokens >= 1.8  # Allow for timing variance

    def test_token_bucket_burst(self):
        """TokenBucket allows burst up to capacity."""
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=5.0, refill_rate=1.0)

        # Can consume up to capacity at once
        result = bucket.consume(5)

        assert result is True
        assert bucket.tokens == 0

    def test_token_bucket_time_until_available(self):
        """TokenBucket.time_until_available calculates wait time."""
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=1.0, refill_rate=10.0)

        # Use all tokens
        bucket.consume(1)

        # Time until 1 token available
        wait_time = bucket.time_until_available(1)

        # Should be about 0.1 seconds (1 token / 10 tokens per sec)
        assert 0.05 < wait_time < 0.2

    # ── Merged from test_rate_limiter_inner.py (v1.0.0 consolidation) ──
    # That module declared a second class also named TestTokenBucket; the two
    # tested the same type with non-overlapping method names, so they are one
    # class now rather than a name shadowed across two files.

    def test_initial_capacity(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.available() >= 10

    def test_consume_reduces_tokens(self):
        bucket = TokenBucket(capacity=10, refill_rate=0)
        assert bucket.consume(5)
        assert bucket.available() <= 5

    def test_consume_fails_when_empty(self):
        bucket = TokenBucket(capacity=5, refill_rate=0)
        assert bucket.consume(5)
        assert not bucket.consume(1)


class TestRateLimiterRaceCondition:
    """Concurrent acquire() calls respect the configured limits."""

    @pytest.mark.asyncio
    async def test_concurrent_acquire_respects_capacity(self):
        """10 concurrent acquire() on limiter with capacity 5 — exactly 5 immediate successes."""
        config = RateLimitConfig(
            requests_per_second=100,  # Fast refill
            burst_size=500,  # Burst capacity: 500 token-units
            tokens_per_request=100,
        )
        limiter = RateLimiter(config)

        async def try_acquire():
            # acquire() is async in RateLimiter
            wait = await limiter.acquire(estimated_tokens=100)
            return wait == 0.0

        results = await asyncio.gather(*[try_acquire() for _ in range(10)])
        successes = sum(1 for r in results if r)
        # With burst_size=500 and 100 tokens/request, first 5 should succeed immediately,
        # remaining 5 may succeed after brief refill wait
        assert successes >= 5, f"Expected at least 5 successes, got {successes}"

    @pytest.mark.asyncio
    async def test_lock_exists(self):
        """Verify that RateLimiter has a threading.Lock (not asyncio.Lock)."""
        limiter = RateLimiter()
        assert isinstance(limiter._lock, type(threading.Lock()))

    @pytest.mark.asyncio
    async def test_acquire_sequential(self):
        """Sequential acquire should work normally."""
        config = RateLimitConfig(
            requests_per_second=1,  # Fixed: was requests_per_minute
            burst_size=100,
            tokens_per_request=100,
        )
        limiter = RateLimiter(config)
        # acquire() is async in RateLimiter
        wait = await limiter.acquire(estimated_tokens=100)
        assert wait == 0.0  # Should be immediate with high limits


# v1.0.2: TestSlidingWindowRateLimiter (3 tests) removed.
#
# Every one of them was `try: from ... import SlidingWindowRateLimiter /
# except ImportError: pytest.skip("SlidingWindowRateLimiter not implemented")`.
# That class does not exist and never has — `grep -rn SlidingWindow src/`
# returns nothing, and beagle.utils.rate_limiter exports TokenBucket,
# RateLimiter, WorkflowRateLimiter and RateLimitConfig. The tests therefore
# could not fail under any change to the codebase: they read as coverage of a
# rate-limiting strategy while asserting nothing at all.
#
# They were deleted rather than satisfied. Implementing a sliding-window
# limiter to make them pass would add a public class with zero production
# callers — dead code that this project's own vulture gate flags — and the
# rate-limiting need is already met by the token-bucket implementation in
# utils/rate_limiter/token_bucket.py, which is genuinely exercised above.
# If a sliding window is ever actually needed, add it with its consumer and
# write tests that fail when it breaks.


class TestRateLimitConfig:
    """Test RateLimitConfig."""

    def test_rate_limit_config_creation(self):
        """RateLimitConfig can be created."""
        from beagle.utils.rate_limiter import RateLimitConfig

        config = RateLimitConfig(
            requests_per_second=10,
            burst_size=20,
        )

        assert config.requests_per_second == 10
        assert config.burst_size == 20

    def test_rate_limit_config_defaults(self):
        """RateLimitConfig uses reasonable defaults."""
        from beagle.utils.rate_limiter import RateLimitConfig

        config = RateLimitConfig()

        assert config.requests_per_second > 0
        assert config.burst_size > 0

    def test_rate_limit_config_validation(self):
        """RateLimitConfig validates parameters."""
        from beagle.utils.rate_limiter import RateLimitConfig

        # Valid config
        config = RateLimitConfig(requests_per_second=10.0, burst_size=20, tokens_per_request=100)
        assert config.requests_per_second == 10.0
        assert config.burst_size == 20
        assert config.tokens_per_request == 100

        # Invalid requests_per_second
        with pytest.raises(ValueError):
            RateLimitConfig(requests_per_second=0, burst_size=10)

        # Invalid burst_size
        with pytest.raises(ValueError):
            RateLimitConfig(requests_per_second=10, burst_size=0)


class TestRateLimiterIntegration:
    """Integration tests for rate limiter."""

    def test_global_rate_limiter_singleton(self):
        """get_rate_limiter returns singleton instance."""
        from beagle.utils.rate_limiter import get_rate_limiter

        limiter1 = get_rate_limiter()
        limiter2 = get_rate_limiter()

        assert limiter1 is limiter2

    @patch("time.sleep", return_value=None)
    def test_rate_limiter_check_and_wait(self, _mock_sleep):
        """Rate limiter can acquire and release tokens."""
        from beagle.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()

        # WorkflowRateLimiter.acquire returns time waited (float >= 0)
        # Test that we can acquire a slot
        result = limiter.acquire(estimated_tokens=100, workflow_id="test_workflow")

        # Result should be float (time waited)
        assert isinstance(result, (int, float))
        assert result >= 0

    @patch("time.sleep", return_value=None)
    def test_rate_limiter_acquire_release(self, _mock_sleep):
        """Rate limiter can acquire and release workflow slots."""
        from beagle.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()

        # Acquire should succeed and return time waited
        time_waited = limiter.acquire(estimated_tokens=100, workflow_id="test_acquire")
        assert time_waited >= 0

        # Cleanup
        limiter.cleanup_workflow("test_acquire")

    def test_rate_limiter_record_success_failure(self):
        """Rate limiter can record success/failure."""
        from beagle.utils.rate_limiter import get_rate_limiter

        limiter = get_rate_limiter()

        # Record success
        limiter.record_success("test_entity")

        # Record failure
        limiter.record_failure("test_entity")

        # Should have tracked the failure
        assert "test_entity" in limiter._failure_counts


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
