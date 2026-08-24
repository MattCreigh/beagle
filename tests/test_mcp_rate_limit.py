"""Tests for the shared MCP rate limiter (mcp_rate_limit.py)."""

import asyncio

import pytest

from beagle.utils.mcp_rate_limit import RateLimiter, RateLimitExceeded


class TestRateLimiter:
    """Unit tests for the sliding-window RateLimiter."""

    async def test_under_budget_passes(self):
        """119 calls should all succeed (one under the 120 limit)."""
        limiter = RateLimiter(max_calls=120, window_seconds=60.0)
        for _ in range(119):
            await limiter.check()
        # All 119 passed — no exception raised

    async def test_over_budget_raises(self):
        """121 calls should fail on the 121st with RateLimitExceeded."""
        limiter = RateLimiter(max_calls=120, window_seconds=60.0)
        for _ in range(120):
            await limiter.check()
        with pytest.raises(RateLimitExceeded):
            await limiter.check()

    async def test_window_slides(self):
        """Window expiry allows calls to continue after timestamps age out."""
        limiter = RateLimiter(max_calls=60, window_seconds=0.5)  # small window for speed
        # Fill the budget
        for _ in range(60):
            await limiter.check()
        # Budget exhausted
        with pytest.raises(RateLimitExceeded):
            await limiter.check()
        # Sleep long enough for the window to slide
        await asyncio.sleep(0.6)
        # Now another 60 should succeed
        for _ in range(60):
            await limiter.check()
        # And the 61st should fail
        with pytest.raises(RateLimitExceeded):
            await limiter.check()

    async def test_concurrent_safe(self):
        """200 concurrent calls; count successes vs RateLimitExceeded."""
        limiter = RateLimiter(max_calls=120, window_seconds=60.0)
        successes = 0
        failures = 0
        lock = asyncio.Lock()

        async def call():
            nonlocal successes, failures
            try:
                await limiter.check()
                async with lock:
                    successes += 1
            except RateLimitExceeded:
                async with lock:
                    failures += 1

        tasks = [asyncio.create_task(call()) for _ in range(200)]
        await asyncio.gather(*tasks)

        assert successes + failures == 200
        assert successes <= 120  # at most 120 succeed
        assert failures > 0  # some must fail since we sent 200
