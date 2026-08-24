"""Tests for circuit_breaker.py"""

import asyncio
import contextlib

import pytest

from beagle.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
    get_circuit_breaker,
    reset_circuit,
)


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions."""

    def test_initial_state_is_closed(self):
        """Circuit starts in CLOSED state."""
        cb = CircuitBreaker("test", CircuitBreakerConfig())
        assert cb.state == CircuitState.CLOSED
        assert cb.is_closed

    @pytest.mark.asyncio
    async def test_opens_after_failure_threshold(self):
        """Circuit opens after consecutive failures reach threshold."""
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))

        for _ in range(3):
            await cb._record_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.is_open

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        """Success resets consecutive failure counter."""
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))

        await cb._record_failure()
        await cb._record_failure()
        await cb._record_success()
        await cb._record_failure()
        await cb._record_failure()

        # Should NOT be open yet - counter was reset by success
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self):
        """Circuit goes to HALF_OPEN after timeout."""
        cb = CircuitBreaker(
            "test",
            CircuitBreakerConfig(
                failure_threshold=2,
                timeout_seconds=0.1,  # 100ms
            ),
        )

        # Open the circuit
        await cb._record_failure()
        await cb._record_failure()
        assert cb.is_open

        # Wait for timeout
        await asyncio.sleep(0.2)

        # Should be able to attempt now (goes to HALF_OPEN)
        can_attempt = await cb._can_attempt()
        assert can_attempt


class TestCircuitBreakerStats:
    """Test circuit breaker statistics tracking."""

    @pytest.mark.asyncio
    async def test_records_successful_calls(self):
        """Statistics track successful calls through call()."""
        cb = CircuitBreaker("test")

        async def success():
            return 42

        async def success2():
            return 43

        await cb.call(success)
        await cb.call(success2)

        assert cb.stats.successful_calls == 2
        assert cb.stats.total_calls == 2

    @pytest.mark.asyncio
    async def test_records_failed_calls(self):
        """Statistics track failed calls."""
        cb = CircuitBreaker("test")
        await cb._record_failure()

        assert cb.stats.failed_calls == 1
        assert cb.stats.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_health_score_calculation(self):
        """Health score reflects failure ratio through call()."""
        cb = CircuitBreaker("test")

        async def succeed():
            return True

        async def fail():
            raise ValueError("fail")

        # 8 successes, 2 failures = 80% health
        for _ in range(8):
            await cb.call(succeed)
        for _ in range(2):
            with contextlib.suppress(ValueError):
                await cb.call(fail)

        health = cb.get_health_report()["health_score"]
        # 8/10 = 0.8 base, with 0.7 weight on failures = 0.86
        assert 0.7 < health <= 1.0


class TestCircuitBreakerCall:
    """Test circuit breaker call protection."""

    @pytest.mark.asyncio
    async def test_successful_call_returns_result(self):
        """Successful async function returns result."""
        cb = CircuitBreaker("test")

        async def func():
            return 42

        result = await cb.call(func)
        assert result == 42

    @pytest.mark.asyncio
    async def test_open_circuit_raises_error(self):
        """Open circuit fast-fails with CircuitBreakerOpenError."""
        cb = CircuitBreaker(
            "test",
            CircuitBreakerConfig(
                failure_threshold=1,
                timeout_seconds=0.1,
            ),
        )

        # Open the circuit
        await cb._record_failure()
        assert cb.is_open

        async def func():
            return 42

        # Should raise immediately without calling func
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await cb.call(func)

        assert exc_info.value.circuit_name == "test"


class TestCircuitBreakerGlobalRegistry:
    """Test global circuit breaker registry."""

    @pytest.mark.asyncio
    async def test_get_circuit_breaker_creates_new(self):
        """get_circuit_breaker creates new instance if not exists."""
        await reset_circuit()

        cb1 = await get_circuit_breaker("new-circuit")
        cb2 = await get_circuit_breaker("new-circuit")

        assert cb1 is cb2  # Same instance
        assert cb1.name == "new-circuit"

    @pytest.mark.asyncio
    async def test_reset_circuit_clears_named(self):
        """reset_circuit with name clears only that circuit."""
        await reset_circuit()

        cb1 = await get_circuit_breaker("circuit-a")
        cb2 = await get_circuit_breaker("circuit-b")

        await reset_circuit("circuit-a")

        cb1_new = await get_circuit_breaker("circuit-a")
        cb2_same = await get_circuit_breaker("circuit-b")

        assert cb1 is not cb1_new  # Was reset
        assert cb2 is cb2_same  # Unchanged


class TestCircuitBreakerEdgeCases:
    """Test edge cases and error conditions."""

    def test_zero_failure_threshold_is_invalid(self):
        """Zero failure threshold should not crash but circuit won't open."""
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=0))
        assert cb.is_closed

    @pytest.mark.asyncio
    async def test_call_with_sync_function(self):
        """Circuit breaker works with sync functions too."""
        cb = CircuitBreaker("test")

        def sync_func():
            return "sync"

        result = await cb.call(sync_func)
        assert result == "sync"

    @pytest.mark.asyncio
    async def test_retry_after_calculation(self):
        """retry_after reflects time until circuit might close."""
        cb = CircuitBreaker(
            "test",
            CircuitBreakerConfig(
                failure_threshold=2,  # Need 2 failures
                timeout_seconds=5.0,
            ),
        )

        # Open the circuit using call()
        async def fail():
            raise ValueError("fail")

        for _ in range(2):
            with contextlib.suppress(ValueError):
                await cb.call(fail)

        assert cb.is_open

        retry_after = cb.get_retry_after()
        assert 0 < retry_after <= 5.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
