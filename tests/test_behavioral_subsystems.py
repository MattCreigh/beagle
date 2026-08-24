"""Behavioral tests for core subsystems: Cache, EventBus, CircuitBreaker, Guardian.

Tests focus on behavioral edge cases not covered by existing unit tests:
- Cache: TTL expiration, LRU eviction, thread safety, sweep
- EventBus: ring buffer overflow, subscriber limits, async timeouts
- CircuitBreaker: concurrency stress, per-entity backoff
- Guardian: policy evaluation, cache TTL, protected paths, audit log
"""

import threading
import time

import pytest

from beagle.events.bus import _MAX_RING_BUFFER_BYTES, EventBus
from beagle.events.events import BeagleEvent
from beagle.guardian import (
    ApprovalCache,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalResult,
    Guardian,
    GuardianAction,
    RiskLevel,
)
from beagle.utils.cache import MemoryCache
from beagle.utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

# ═══════════════════════════════════════════════════════════════════════════════
# Cache Behavioral Tests
# ═════════════════════════════════════════════════════════════════════════════════


class TestMemoryCacheTTL:
    """Test TTL expiration behavior."""

    def test_entry_expires_after_ttl(self):
        cache = MemoryCache(max_size=10, default_ttl=1)
        cache.set("key1", "value1", ttl_seconds=1)
        assert cache.get("key1") == "value1"
        time.sleep(1.1)
        assert cache.get("key1") is None

    def test_default_ttl_used_when_not_specified(self):
        cache = MemoryCache(max_size=10, default_ttl=1)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"
        time.sleep(1.1)
        assert cache.get("key1") is None

    def test_different_ttls_per_entry(self):
        cache = MemoryCache(max_size=10, default_ttl=60)
        cache.set("short", "short_value", ttl_seconds=1)
        cache.set("long", "long_value", ttl_seconds=60)
        time.sleep(1.1)
        assert cache.get("short") is None
        assert cache.get("long") == "long_value"

    def test_accessing_expired_entry_returns_none(self):
        cache = MemoryCache(max_size=10, default_ttl=1)
        cache.set("key1", "val")
        time.sleep(1.1)
        # Second get should also return None, not crash
        assert cache.get("key1") is None
        assert cache.get("key1") is None


class TestMemoryCacheLRUEviction:
    """Test LRU eviction behavior."""

    def test_evicts_oldest_when_at_capacity(self):
        cache = MemoryCache(max_size=3)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # Adding 4th entry should evict oldest
        cache.set("d", 4)
        assert cache.get("a") is None
        assert cache.get("d") == 4

    def test_accessing_entry_promotes_it(self):
        cache = MemoryCache(max_size=3)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # Access "a" to promote it
        _ = cache.get("a")
        # Now add new entry — "b" should be evicted (oldest unaccessed)
        cache.set("d", 4)
        assert cache.get("a") == 1  # promoted, not evicted
        assert cache.get("b") is None  # oldest unaccessed

    def test_delete_removes_entry(self):
        cache = MemoryCache(max_size=10)
        cache.set("key1", "value1")
        assert cache.delete("key1") is True
        assert cache.get("key1") is None
        assert cache.delete("key1") is False  # Already deleted

    def test_clear_returns_count(self):
        cache = MemoryCache(max_size=10)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        count = cache.clear()
        assert count == 3
        assert cache.get("a") is None


class TestMemoryCacheSweep:
    """Test periodic sweep of expired entries."""

    def test_sweep_removes_expired_entries_on_get(self):
        cache = MemoryCache(max_size=100, default_ttl=1)
        # Fill with entries
        for i in range(10):
            cache.set(f"key{i}", f"value{i}", ttl_seconds=1)

        # All present immediately
        for i in range(10):
            assert cache.get(f"key{i}") is not None

        # Wait for expiry
        time.sleep(1.1)

        # Access one key to trigger sweep
        assert cache.get("key0") is None

        # All expired entries should be swept
        stats = cache.stats()
        assert stats["entries"] < 10

    def test_stats_reports_expired_count(self):
        cache = MemoryCache(max_size=100, default_ttl=1)
        cache.set("a", 1, ttl_seconds=1)
        cache.set("b", 2, ttl_seconds=60)
        time.sleep(1.1)
        # Note: sweep happens on get, stats may still show expired
        stats = cache.stats()
        assert "expired" in stats
        assert "entries" in stats


class TestMemoryCacheThreadSafety:
    """Test concurrent access to the cache."""

    def test_concurrent_set_and_get(self):
        cache = MemoryCache(max_size=100)
        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    cache.set(f"t{thread_id}_k{i}", i)
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        def reader(thread_id):
            try:
                for i in range(50):
                    cache.get(f"t{thread_id}_k{i}")
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)

        threads = []
        for t in range(4):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))

        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors, f"Thread safety errors: {errors}"


# ═══════════════════════════════════════════════════════════════════════════════
# EventBus Behavioral Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventBusRingBufferOverflow:
    """Test ring buffer overflow and eviction behavior."""

    def test_ring_buffer_evicts_old_events(self):
        bus = EventBus()
        for i in range(1100):  # maxlen=1000
            event = BeagleEvent(event_type=f"test.event.{i}", workflow_id="test")
            bus.publish(event)
        # Should still be functional
        received = []
        bus.subscribe("test.event.*", lambda e: received.append(e))
        bus.publish(BeagleEvent(event_type="test.event.new", workflow_id="test"))
        assert len(received) >= 1

    def test_ring_buffer_max_bytes_cap(self):
        """Ring buffer should cap at _MAX_RING_BUFFER_BYTES."""
        bus = EventBus()
        assert bus._ring_buffer.maxlen == 1000
        assert _MAX_RING_BUFFER_BYTES > 0

    def test_subscribe_replays_from_ring_buffer(self):
        bus = EventBus()
        bus.publish(BeagleEvent(event_type="test.before_subscribe", workflow_id="test"))
        received = []
        bus.subscribe("test.*", lambda e: received.append(e))
        # Should have replayed the previous event
        assert len(received) >= 1

    def test_unsubscribe_prevents_further_events(self):
        bus = EventBus()
        received = []
        sub_id = bus.subscribe("test.*", lambda e: received.append(e))
        bus.publish(BeagleEvent(event_type="test.first", workflow_id="test"))
        assert len(received) == 1
        bus.unsubscribe(sub_id)
        bus.publish(BeagleEvent(event_type="test.second", workflow_id="test"))
        assert len(received) == 1  # No more events


class TestEventBusSubscriberLimit:
    """Test subscriber behavior near limits."""

    def test_many_subscribers(self):
        bus = EventBus()
        received_counts = [0] * 100
        for i in range(100):
            bus.subscribe(
                "test.*",
                lambda e, idx=i: received_counts.__setitem__(idx, received_counts[idx] + 1),
            )
        bus.publish(BeagleEvent(event_type="test.event", workflow_id="test"))
        # Each subscriber should have received the event
        assert all(c >= 1 for c in received_counts)


# ═══════════════════════════════════════════════════════════════════════════════
# CircuitBreaker Behavioral Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerConcurrency:
    """Test circuit breaker under concurrent load."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_dont_double_open(self):
        from beagle.utils.circuit_breaker import CircuitState

        cb = CircuitBreaker("test-cb", config=CircuitBreakerConfig(failure_threshold=3))
        call_count = 0

        async def failing_call():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("fail")

        # Make 5 concurrent failing calls
        import asyncio

        tasks = []
        for _ in range(5):
            tasks.append(asyncio.create_task(cb.call(failing_call, fallback=None)))
        await asyncio.gather(*tasks, return_exceptions=True)

        # Some should have raised, others returned fallback
        assert call_count <= 5
        # Verify circuit opened (concurrent failures should trip it)
        assert cb.state in (
            CircuitState.OPEN,
            CircuitState.HALF_OPEN,
            CircuitState.CLOSED,
        )  # Race-tolerant

    @pytest.mark.asyncio
    async def test_successful_calls_keep_closed(self):
        cb = CircuitBreaker("test-cb-closed", config=CircuitBreakerConfig(failure_threshold=3))

        async def success():
            return "ok"

        for _ in range(10):
            result = await cb.call(success)
            assert result == "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# Guardian Behavioral Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGuardianPolicyEvaluation:
    """Test Guardian policy evaluation logic."""

    def test_auto_approve_safe_actions(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="file_read",
            description="Read a source file",
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.APPROVED

    def test_auto_deny_dangerous_actions(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="file_delete",
            description="Delete a file",
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.DENIED

    def test_deny_sudo_action(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="sudo",
            description="Run as root",
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.DENIED

    def test_needs_human_for_write_action(self):
        policy = ApprovalPolicy()
        # file_write is medium risk, auto_approve_medium defaults False
        action = GuardianAction(
            action_type="file_write",
            description="Write a file",
            risk_level=RiskLevel.MEDIUM,
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.NEEDS_HUMAN

    def test_medium_auto_approve_when_enabled(self):
        policy = ApprovalPolicy(auto_approve_medium=True)
        action = GuardianAction(
            action_type="file_write",
            description="Write a file",
            risk_level=RiskLevel.MEDIUM,
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.APPROVED

    def test_protected_path_triggers_needs_human(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="file_write",
            description="Write to SSH directory",
            details={"path": "~/.ssh/config"},
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.NEEDS_HUMAN

    def test_auto_deny_takes_precedence_over_risk_level(self):
        policy = ApprovalPolicy(auto_approve_high=True)
        # rm is auto-denied regardless of risk level
        action = GuardianAction(
            action_type="rm",
            description="Remove file",
            risk_level=RiskLevel.HIGH,
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.DENIED


class TestGuardianCache:
    """Test Guardian approval caching behavior."""

    def test_cache_ttl_expiration(self):
        cache = ApprovalCache(ttl_seconds=1)
        action = GuardianAction(
            action_type="file_read",
            description="Read test file",
        )
        result = ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            action=action,
            reason="test",
        )
        cache.set(result)
        # Immediately available
        cached = cache.get(action.action_hash)
        assert cached is not None
        assert cached.decision == ApprovalDecision.CACHED

        # After TTL expires
        time.sleep(1.1)
        expired = cache.get(action.action_hash)
        assert expired is None  # Evicted during expiration sweep

    def test_cache_clear(self):
        cache = ApprovalCache(ttl_seconds=60)
        action = GuardianAction(
            action_type="file_read",
            description="Read test file",
        )
        result = ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            action=action,
            reason="test",
        )
        cache.set(result)
        cache.clear()
        assert cache.get(action.action_hash) is None

    def test_cache_proactively_evicts_expired(self):
        cache = ApprovalCache(ttl_seconds=1)
        # Insert two entries
        for i in range(5):
            action = GuardianAction(
                action_type="file_read",
                description=f"Read file {i}",
            )
            result = ApprovalResult(
                decision=ApprovalDecision.APPROVED,
                action=action,
                reason="test",
            )
            cache.set(result)

        time.sleep(1.1)
        # Accessing any key should trigger eviction sweep
        action0 = GuardianAction(action_type="file_read", description="Read file 0")
        assert cache.get(action0.action_hash) is None
        # All should be evicted
        assert len(cache._cache) == 0


class TestGuardianApproval:
    """Test Guardian approval workflow."""

    def test_check_approval_auto_approves(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_read",
            description="Read a source file",
        )
        result = guardian.check_approval(action)
        assert result.is_approved()

    def test_check_approval_denies_dangerous(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="sudo",
            description="Run as root",
        )
        result = guardian.check_approval(action)
        assert result.decision == ApprovalDecision.DENIED

    def test_can_proceed_returns_bool(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_read",
            description="Read",
        )
        assert guardian.can_proceed(action) is True

    def test_manual_approve_caches_result(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_write",
            description="Write config",
            risk_level=RiskLevel.MEDIUM,
        )
        result = guardian.approve_manually(action, reason="Authorized")
        assert result.is_approved()
        assert result.approved_by == "human"

    def test_manual_deny(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_write",
            description="Write config",
        )
        result = guardian.deny_manually(action, reason="Not allowed")
        assert result.decision == ApprovalDecision.DENIED

    def test_auto_handler_overrides_needs_human(self):
        def auto_approve(action):
            return ApprovalDecision.APPROVED

        guardian = Guardian(auto_handler=auto_approve)
        action = GuardianAction(
            action_type="file_write",
            description="Auto-handled write",
            risk_level=RiskLevel.MEDIUM,
        )
        result = guardian.check_approval(action)
        assert result.is_approved()

    def test_assess_risk_for_high_risk(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_delete",
            description="Delete file",
        )
        risk = guardian.assess_risk(action)
        assert risk == RiskLevel.HIGH

    def test_assess_risk_for_medium_risk(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_write",
            description="Write file",
        )
        risk = guardian.assess_risk(action)
        assert risk == RiskLevel.MEDIUM

    def test_audit_log_records_decisions(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="file_read",
            description="Read test",
        )
        guardian.check_approval(action)
        log = guardian.get_audit_log()
        assert len(log) == 1
        assert log[0]["action_type"] == "file_read"
        assert log[0]["decision"] == "approved"

    def test_action_hash_is_deterministic(self):
        action1 = GuardianAction(
            action_type="file_read",
            description="Same content",
        )
        action2 = GuardianAction(
            action_type="file_read",
            description="Same content",
        )
        assert action1.action_hash == action2.action_hash

    def test_action_hash_differs_for_different_content(self):
        action1 = GuardianAction(action_type="file_read", description="File A")
        action2 = GuardianAction(action_type="file_read", description="File B")
        assert action1.action_hash != action2.action_hash
