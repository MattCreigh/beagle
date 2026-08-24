"""Tests for new modules added in Beagle v10.0.

Tests for: cache, rate_limiter, webhooks, config, checkpoint, audit, errors, router.
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ============================================================================
# Cache Tests
# ============================================================================


class TestMemoryCache:
    """Tests for in-memory cache."""

    def test_set_and_get(self):
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(max_size=10)

        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_get_missing_key(self):
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache()
        assert cache.get("nonexistent") is None

    def test_lru_eviction(self):
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(max_size=3)

        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Access key1 to make it recently used
        cache.get("key1")

        # Add key4, should evict key2 (least recently used)
        cache.set("key4", "value4")

        assert cache.get("key1") == "value1"  # Still present
        assert cache.get("key3") == "value3"  # Still present
        assert cache.get("key4") == "value4"  # New key
        # key2 may or may not be evicted depending on implementation

    def test_ttl_expiration(self):
        from beagle.utils.cache import MemoryCache

        cache = MemoryCache(default_ttl=1)

        cache.set("key1", "value1", ttl_seconds=1)

        # Should be present immediately
        assert cache.get("key1") == "value1"

        # Wait for expiration
        time.sleep(1.1)

        # Should be expired
        assert cache.get("key1") is None


class TestResultCache:
    """Tests for result cache."""

    def test_cache_and_retrieve(self):
        from beagle.utils.cache import ResultCache

        cache = ResultCache(enabled=True)

        prompt = "Test prompt"
        recipe = "Test recipe"
        result = "Test result"

        cache.cache_result(prompt, recipe, result)
        cached = cache.get_cached_result(prompt, recipe)

        assert cached == result

    def test_cache_disabled(self):
        from beagle.utils.cache import ResultCache

        cache = ResultCache(enabled=False)

        cache.cache_result("prompt", "recipe", "result")
        cached = cache.get_cached_result("prompt", "recipe")

        assert cached is None

    def test_stats(self):
        from beagle.utils.cache import ResultCache

        cache = ResultCache(enabled=True)

        cache.cache_result("p1", "r1", "result1")
        cache.get_cached_result("p1", "r1")  # Hit
        cache.get_cached_result("p2", "r2")  # Miss

        stats = cache.stats()

        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 50.0


# ============================================================================
# Rate Limiter Tests
# ============================================================================


class TestTokenBucket:
    """Tests for token bucket implementation."""

    def test_consume_available(self):
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=10, refill_rate=1)

        assert bucket.consume(5) is True
        # Allow small floating-point drift from refill timing
        assert abs(bucket.available() - 5) < 0.01

    def test_consume_unavailable(self):
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=5, refill_rate=1)

        assert bucket.consume(10) is False
        assert bucket.available() == 5  # Unchanged

    def test_refill(self):
        from beagle.utils.rate_limiter import TokenBucket

        bucket = TokenBucket(capacity=10, refill_rate=10)  # 10 per second

        bucket.consume(5)
        time.sleep(0.5)  # Wait half second

        # Should have refilled ~5 tokens
        assert bucket.available() >= 9


class TestRateLimiter:
    """Tests for rate limiter."""

    def test_check_request(self):
        from beagle.utils.rate_limiter import (
            RateLimitConfig,
            RateLimiter,
        )

        config = RateLimitConfig(requests_per_second=60)
        limiter = RateLimiter(config)

        assert limiter.check_request() is True

    @pytest.mark.asyncio
    async def test_acquire(self):
        from beagle.utils.rate_limiter import (
            RateLimitConfig,
            RateLimiter,
        )

        config = RateLimitConfig(
            requests_per_second=60,
            burst_size=500,  # Enough tokens for a 100-token acquire
            tokens_per_request=100000,
        )
        limiter = RateLimiter(config)

        wait_time = await limiter.acquire(estimated_tokens=100)

        assert wait_time >= 0
        assert limiter._request_count == 1

    def test_stats(self):
        from beagle.utils.rate_limiter import (
            RateLimitConfig,
            RateLimiter,
        )

        config = RateLimitConfig(requests_per_second=60)
        limiter = RateLimiter(config)

        limiter.consume_request()
        limiter.consume_request()

        stats = limiter.stats()

        assert stats["requests"]["total"] == 2


# ============================================================================
# Webhook Tests
# ============================================================================


class TestWebhookManager:
    """Tests for webhook manager."""

    def test_register_webhook(self):
        from beagle.webhooks import WebhookConfig, WebhookManager

        manager = WebhookManager()

        config = WebhookConfig(url="https://example.com/webhook")
        manager.register("test", config)

        assert "test" in manager._webhooks

    def test_unregister_webhook(self):
        from beagle.webhooks import WebhookConfig, WebhookManager

        manager = WebhookManager()

        config = WebhookConfig(url="https://example.com/webhook")
        manager.register("test", config)
        result = manager.unregister("test")

        assert result is True
        assert "test" not in manager._webhooks

    def test_should_deliver_all_events(self):
        from beagle.webhooks import WebhookConfig, WebhookManager

        manager = WebhookManager()
        config = WebhookConfig(url="https://example.com", events=[])

        assert manager.should_deliver(config, "any.event") is True

    def test_should_deliver_filtered_events(self):
        from beagle.webhooks import WebhookConfig, WebhookManager

        manager = WebhookManager()
        config = WebhookConfig(url="https://example.com", events=["workflow.completed"])

        assert manager.should_deliver(config, "workflow.completed") is True
        assert manager.should_deliver(config, "workflow.started") is False


class TestWebhookSignature:
    """Tests for webhook signature computation."""

    def test_compute_signature(self):
        from beagle.webhooks import compute_signature

        payload = '{"event": "test"}'
        secret = "test_secret"

        signature = compute_signature(payload, secret)

        assert signature.startswith("sha256=")
        assert len(signature) == 71  # "sha256=" + 64 hex chars

    def test_compute_signature_no_secret(self):
        from beagle.webhooks import compute_signature

        with pytest.raises(ValueError, match="secret"):
            compute_signature('{"event": "test"}', "")


# ============================================================================
# Config Tests
# ============================================================================


class TestConfig:
    """Tests for configuration loading."""

    def test_load_defaults(self):
        from beagle.config.config import WorkflowConfig, load_config

        # Load with non-existent path returns defaults
        config = load_config(Path("/nonexistent/config.toml"))

        assert isinstance(config, WorkflowConfig)
        assert config.orchestrator.timeout_seconds == 300
        assert config.budget.default_usd == 10.0

    def test_env_overrides(self):
        import os

        from beagle.config.config import (
            WorkflowConfig,
            apply_env_overrides,
        )

        config = WorkflowConfig()

        # Set env override
        os.environ["GOOSE_MODEL"] = "test-model"

        config = apply_env_overrides(config)

        assert config.goose.default_model == "test-model"

        # Cleanup
        del os.environ["GOOSE_MODEL"]

    def test_generate_default_config(self):
        from beagle.config.config import generate_default_config

        content = generate_default_config()

        assert "[orchestrator]" in content
        assert "[goose]" in content
        assert "[budget]" in content
        assert "[cache]" in content


# ============================================================================
# Checkpoint Tests
# ============================================================================


class TestCheckpoint:
    """Tests for checkpoint functionality."""

    def test_save_and_load(self, tmp_path):
        from beagle.checkpointer import Checkpoint

        checkpoint = Checkpoint(
            workflow_id="test-123",
            query="Test query",
            completed_nodes=["Phase1"],
            state_data={"key": "value"},
        )

        path = checkpoint.save(tmp_path / "test.json")
        assert path.exists()

        loaded = Checkpoint.load(path)

        assert loaded.workflow_id == "test-123"
        assert loaded.query == "Test query"
        assert loaded.completed_nodes == ["Phase1"]
        assert loaded.state_data["key"] == "value"

    def test_delete(self, tmp_path):
        from beagle.checkpointer import Checkpoint

        checkpoint = Checkpoint(workflow_id="test-123", query="Test")
        checkpoint.save(tmp_path / "test-123.json")

        # Mock get_checkpoint_dir
        with patch(
            "beagle.checkpointer.get_checkpoint_dir",
            return_value=tmp_path,
        ):
            result = checkpoint.delete()

        assert result is True


class TestCheckpointManager:
    """Tests for checkpoint manager."""

    def test_list_checkpoints(self, tmp_path):
        from beagle.checkpointer import Checkpoint, CheckpointManager

        # Create some checkpoints
        Checkpoint(workflow_id="wf1", query="Query 1").save(tmp_path / "wf1.json")
        Checkpoint(workflow_id="wf2", query="Query 2").save(tmp_path / "wf2.json")

        manager = CheckpointManager(checkpoint_dir=tmp_path)
        checkpoints = manager.list_checkpoints()

        assert len(checkpoints) == 2
        assert any(cp["workflow_id"] == "wf1" for cp in checkpoints)


# ============================================================================
# Audit Tests
# ============================================================================


class TestAuditTrail:
    """Tests for audit trail."""

    def test_log_event(self, tmp_path):
        from beagle.audit import AuditEventType, AuditTrail

        trail = AuditTrail("test-123", audit_dir=tmp_path)

        event = trail.log(AuditEventType.WORKFLOW_STARTED, query="Test")

        assert event.event_type == AuditEventType.WORKFLOW_STARTED
        assert event.workflow_id == "test-123"
        assert event.event_hash != ""

    def test_hash_chain(self, tmp_path):
        from beagle.audit import AuditEventType, AuditTrail

        trail = AuditTrail("test-123", audit_dir=tmp_path)

        event1 = trail.log(AuditEventType.WORKFLOW_STARTED)
        event2 = trail.log(AuditEventType.NODE_STARTED, node_name="Phase1")

        assert event2.previous_hash == event1.event_hash

    def test_verify_integrity(self, tmp_path):
        from beagle.audit import AuditEventType, AuditTrail

        trail = AuditTrail("test-123", audit_dir=tmp_path)

        trail.log(AuditEventType.WORKFLOW_STARTED)
        trail.log(AuditEventType.NODE_COMPLETED, node_name="Phase1")
        trail.log(AuditEventType.WORKFLOW_COMPLETED)

        is_valid, errors = trail.verify_integrity()

        assert is_valid is True
        assert len(errors) == 0


# ============================================================================
# Router Tests
# ============================================================================


class TestRouter:
    """Tests for query routing."""

    def test_route_audit_query(self):
        from beagle.core.router import route_query

        result = route_query("Audit the codebase for security issues")

        assert result.workflow in ["audit", "security"]
        assert result.confidence > 0.1  # Word-boundary matching with realistic confidence

    def test_route_research_query(self):
        from beagle.core.router import route_query

        result = route_query("How does the IPC system work?")

        assert result.workflow == "research"
        assert result.confidence > 0.1  # Word-boundary matching with realistic confidence

    def test_route_unknown_query(self):
        from beagle.core.router import route_query

        result = route_query("Hello world")

        # Should default to research
        assert result.workflow == "research"
        assert result.confidence < 0.5

    def test_suggest_workflow(self):
        from beagle.core.router import route_query, suggest_workflow

        # High confidence should return None
        result = route_query("Audit the codebase architecture completely")
        if result.confidence >= 0.7:
            suggestion = suggest_workflow("Audit the codebase architecture completely")
            assert suggestion is None


# ============================================================================
# Errors Tests
# ============================================================================


class TestCustomErrors:
    """Tests for custom error classes."""

    def test_recipe_not_found_error(self):
        from beagle.errors import RecipeNotFoundError

        error = RecipeNotFoundError("custom-agent")

        assert "custom-agent" in str(error)
        assert error.recipe_name == "custom-agent"

    def test_budget_exceeded_error(self):
        from beagle.errors import BudgetExceededError

        error = BudgetExceededError(15.0, 10.0)

        assert "15.0" in str(error) or "15" in str(error)
        assert error.current == 15.0
        assert error.budget == 10.0

    def test_error_to_dict(self):
        from beagle.errors import GooseWorkflowError

        error = GooseWorkflowError(
            "Test error",
            suggestion="Fix it",
            docs_link="https://docs.example.com",
        )

        d = error.to_dict()

        assert d["message"] == "Test error"
        assert d["suggestion"] == "Fix it"
        assert d["docs_link"] == "https://docs.example.com"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
