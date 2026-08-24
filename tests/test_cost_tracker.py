"""Tests for cost_tracker module.

Covers:
- TokenUsage dataclass
- CostTracker budget tracking
- _get_pricing fallback logic
- estimate_tokens_agnostic fallback
- Global tracker lifecycle (get, reset)
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_cost_tracker_state():
    """Reset global state before each test to ensure isolation.

    ``_TOKENIZER_STATE`` and ``_tokenizer_cache`` are two halves of one cache and
    must be reset together. This fixture used to null the cache alone, which
    produced a state the module cannot produce itself: sentinel says "tiktoken
    available", cache says None. ``_get_tokenizer_cached`` then skips the import
    (the sentinel is not None) and returns None forever, so tiktoken was
    permanently disabled for the rest of the process.

    Alone, the file passed — nothing had primed the tokenizer, so the sentinel
    was already None. After any earlier test that used it,
    ``test_uses_tiktoken_when_available`` silently took the heuristic path and
    got 11 tokens for "hello world" instead of 2. A fixture that half-resets is
    worse than no fixture, because it manufactures a state the product cannot
    reach on its own.

    Yields:
        None. State is reset both before and after each test.
    """
    from beagle import cost_tracker

    def _reset() -> None:
        cost_tracker._TOKENIZER_STATE = None
        cost_tracker._tokenizer_cache = None
        cost_tracker._global_tracker = None
        cost_tracker._tracker_lock = threading.Lock()

    _reset()
    yield
    _reset()


# ── TokenUsage ──────────────────────────────────────────────────────────────


class TestTokenUsage:
    def test_total_tokens_property(self):
        from beagle.cost_tracker import TokenUsage

        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        assert usage.total_tokens == 1500

    def test_total_tokens_zero(self):
        from beagle.cost_tracker import TokenUsage

        usage = TokenUsage(input_tokens=0, output_tokens=0)
        assert usage.total_tokens == 0

    def test_cost_usd_known_model(self):
        from beagle.cost_tracker import TokenUsage

        usage = TokenUsage(input_tokens=1_000_000, output_tokens=500_000, model="gemini-2.5-pro")
        # gemini-2.5-pro: $1.25/M input, $10.00/M output
        # = 1.25 + 5.00 = $6.25
        assert abs(usage.cost_usd - 6.25) < 0.001

    def test_cost_usd_unknown_model_uses_default(self):
        from beagle.cost_tracker import TokenUsage

        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, model="unknown-xyz")
        # default: $1.00/M input, $4.00/M output = $5.00
        assert abs(usage.cost_usd - 5.00) < 0.001

    def test_timestamp_defaults_to_now(self):
        from beagle.cost_tracker import TokenUsage

        before = time.time()
        usage = TokenUsage(input_tokens=100, output_tokens=50)
        after = time.time()
        assert before <= usage.timestamp <= after


# ── estimate_tokens_agnostic ────────────────────────────────────────────────


class TestEstimateTokensAgnostic:
    def test_empty_string_returns_zero(self):
        from beagle.cost_tracker import estimate_tokens_agnostic

        assert estimate_tokens_agnostic("") == 0

    def test_uses_tiktoken_when_available(self):
        from beagle.cost_tracker import estimate_tokens_agnostic

        # With tiktoken: "hello world" = 2 tokens in cl100k_base
        # Without tiktoken: heuristic estimate (still reasonable)
        result = estimate_tokens_agnostic("hello world")
        import importlib.util as _ilu

        if _ilu.find_spec("tiktoken") is not None:
            assert result == 2  # Exact when tiktoken is available
        else:
            # Heuristic fallback — should be within reasonable range
            assert 1 <= result <= 10

    def test_estimate_tokens_returns_positive_integer(self):
        """estimate_tokens_agnostic returns a positive integer for non-empty input."""
        from beagle.cost_tracker import estimate_tokens_agnostic

        result = estimate_tokens_agnostic("hello world test")
        assert isinstance(result, int)
        assert result >= 1


# ── _get_pricing ────────────────────────────────────────────────────────────


class TestGetPricing:
    def test_known_model(self):
        from beagle.cost_tracker import _get_pricing

        # Use a currently-allowlisted model, not a retired one. Registry keys
        # carry the Ollama Cloud suffix (":cloud" / "-cloud"); a bare name
        # misses the lookup and silently falls through to default pricing,
        # which is exactly the mis-pricing this test exists to catch.
        pricing = _get_pricing("deepseek-v4-pro:cloud")
        assert pricing == {"input": 1.20, "output": 4.80}

    def test_known_model_gemini(self):
        from beagle.cost_tracker import _get_pricing

        pricing = _get_pricing("gemini-2.5-pro")
        assert pricing == {"input": 1.25, "output": 10.00}

    def test_unknown_model_uses_default(self):
        from beagle.cost_tracker import _get_pricing

        pricing = _get_pricing("completely-fake-model-xyz")
        # default: $1.00/M input, $4.00/M output
        assert pricing == {"input": 1.00, "output": 4.00}


# ── CostTracker basics ──────────────────────────────────────────────────────


class TestCostTrackerBasics:
    def test_default_budget(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker()
        assert tracker.budget_usd == 10.0
        assert not tracker.budget_exceeded
        assert tracker.budget_remaining == 10.0

    def test_custom_budget(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=5.0)
        assert tracker.budget_usd == 5.0

    def test_zero_budget_unlimited(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=0)
        assert not tracker.budget_exceeded  # Zero = unlimited

    def test_negative_budget_unlimited(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=-5.0)
        assert not tracker.budget_exceeded

    @pytest.mark.asyncio
    async def test_record_usage_basic(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=100.0)
        usage = await tracker.record_usage(1000, 500, model="gemini-2.5-pro")
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert tracker.total_tokens == 1500
        assert tracker.total_cost_usd > 0

    @pytest.mark.asyncio
    async def test_record_usage_with_node_name(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=100.0)
        await tracker.record_usage(1000, 500, node_name="PlanningPhase")
        await tracker.record_usage(2000, 1000, node_name="ExecutionPhase")
        assert tracker.node_costs["PlanningPhase"] > 0
        assert tracker.node_costs["ExecutionPhase"] > 0
        assert len(tracker.usage_history) == 2

    @pytest.mark.asyncio
    async def test_estimate_from_text_records(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=100.0)
        usage = await tracker.estimate_from_text(
            "test input", "test output", model="gemini-2.5-pro"
        )
        assert usage.input_tokens > 0
        assert usage.output_tokens > 0
        assert tracker.total_tokens > 0


# ── Budget enforcement ────────────────────────────────────────────────────


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_budget_exceeded_sets_flag_and_logs(self, caplog):
        from beagle.cost_tracker import CostTracker

        caplog.set_level(logging.ERROR)
        tracker = CostTracker(budget_usd=0.001)
        await tracker.estimate_from_text("x" * 50_000, "y" * 50_000, model="gemini-2.5-pro")
        # check_budget() must be called explicitly to trigger logging
        result = tracker.check_budget()
        assert result is False
        assert tracker.budget_exceeded
        assert any("exceeded" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_budget_warning_at_threshold(self, caplog):
        from beagle.cost_tracker import CostTracker

        caplog.set_level(logging.WARNING)
        tracker = CostTracker(budget_usd=5.0)
        for _ in range(8):
            await tracker.estimate_from_text(
                "prompt " * 40_000, "response " * 40_000, model="gemini-2.5-pro"
            )
        result = tracker.check_budget(warn_threshold=0.8)
        # Either budget is still OK (True) or a warning was logged
        assert result is True or any("budget" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_check_budget_false_when_exceeded(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=0.001)
        await tracker.estimate_from_text("x" * 50_000, "y" * 50_000, model="gemini-2.5-pro")
        assert tracker.check_budget() is False

    @pytest.mark.asyncio
    async def test_check_budget_true_when_ok(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=100.0)
        await tracker.estimate_from_text("small input", "small output", model="gemini-2.5-pro")
        assert tracker.check_budget() is True


# ── Reporting ───────────────────────────────────────────────────────────────


class TestCostTrackerSummary:
    @pytest.mark.asyncio
    async def test_get_summary_keys(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=5.0)
        await tracker.estimate_from_text("test input", "test output", model="gemini-2.5-pro")
        summary = tracker.get_summary()
        assert "total_tokens" in summary
        assert "total_cost_usd" in summary
        assert "budget_exceeded" in summary
        assert summary["operations"] == 1

    @pytest.mark.asyncio
    async def test_format_report_contains_cost(self):
        from beagle.cost_tracker import CostTracker

        tracker = CostTracker(budget_usd=5.0)
        await tracker.estimate_from_text("test input", "test output", model="gemini-2.5-pro")
        report = tracker.format_report()
        assert "Cost Tracking Report" in report
        assert "$" in report


# ── Global tracker lifecycle ───────────────────────────────────────────────


class TestGlobalTrackerLifecycle:
    def test_get_cost_tracker_creates_singleton(self):
        from beagle.cost_tracker import (
            get_cost_tracker,
            reset_cost_tracker,
        )

        reset_cost_tracker(budget_usd=1.0)
        t1 = get_cost_tracker(budget_usd=99.0)  # budget ignored on reuse
        t2 = get_cost_tracker(budget_usd=99.0)
        assert t1 is t2
        assert t1.budget_usd == 1.0

    def test_reset_cost_tracker_replaces_instance(self):
        from beagle.cost_tracker import (
            get_cost_tracker,
            reset_cost_tracker,
        )

        t1 = get_cost_tracker(budget_usd=5.0)
        reset_cost_tracker(budget_usd=99.0)
        t2 = get_cost_tracker()
        assert t1 is not t2
        assert t2.budget_usd == 99.0
        assert t1.budget_usd == 5.0  # old unchanged

    def test_async_tracker_singleton(self):
        """Async get/reset work via synchronous wrappers."""
        from beagle import cost_tracker as ct

        ct.reset_cost_tracker(budget_usd=3.0)
        t1 = ct.get_cost_tracker(budget_usd=99.0)
        t2 = ct.get_cost_tracker(budget_usd=99.0)
        assert t1 is t2
        assert t1.budget_usd == 3.0
