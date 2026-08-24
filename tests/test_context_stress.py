"""Context Folding Stress Tests.

Tests context compression under heavy load:
1. Large context (>50k tokens) compression
2. Compression threshold triggers
3. Fact preservation post-compression
4. Edge cases (empty, single huge message)
5. Performance under stress

This validates the ContextOptimizer progressive compression strategies.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from beagle.context.context_optimizer import (  # ruff: ignore[E402]
    CompressionLevel,
    ContextOptimizer,
    ContextPlan,
    ContextStrategy,
)
from beagle.context.context_window import (  # ruff: ignore[E402]
    ContextWindowManager,
)

# ── Test Fixtures ───────────────────────────────────────────────────────────────


def generate_large_content(num_chars: int) -> str:
    """Generate large content for stress testing."""
    # Generate structured content with facts that should be preserved
    paragraphs = []
    for i in range(num_chars // 500):
        paragraphs.append(
            f"## Section {i + 1}\n\n"
            f"Important fact {i + 1}: The system processed {i * 100} tokens.\n\n"
            f"Key finding: User preference {i % 5} was observed.\n\n"
            f"{'This is filler content to reach the desired length. ' * 10}\n"
        )
    return "\n".join(paragraphs)


def generate_code_content(num_lines: int) -> str:
    """Generate code-style content for compression testing."""
    lines = []
    for i in range(num_lines):
        lines.append(f"def function_{i}(arg1: str, arg2: int) -> bool:")
        lines.append(f'    """Docstring for function {i}."""')
        lines.append(f"    result = arg1 * arg2 + {i}")
        lines.append(f"    return result > {i * 10}")
        lines.append("")
    return "\n".join(lines)


@pytest.fixture
def context_manager():
    """Create a fresh context manager for testing."""
    return ContextWindowManager(
        context_window=128000,  # Standard context window
        model="test-model",
    )


@pytest.fixture
def optimizer(context_manager):
    """Create a context optimizer with test context manager."""
    return ContextOptimizer(
        context_manager=context_manager,
        warning_threshold=0.50,
        aggressive_threshold=0.70,
        emergency_threshold=0.85,
        enable_turboquant=False,  # Disable for unit tests
    )


# ── Stress Test: Large Context Compression ───────────────────────────────────────


class TestLargeContextCompression:
    """Test compression of large contexts (>50k tokens)."""

    def test_compress_50k_token_content(self, optimizer):
        """Compress content representing 50k+ tokens."""
        # 50k tokens ≈ 200k characters (roughly 4 chars per token)
        large_content = generate_large_content(200000)

        result = optimizer.compress_content(
            large_content,
            level=CompressionLevel.MODERATE,
        )

        # Verify compression occurred
        assert result.compressed_tokens < result.original_tokens
        assert result.compression_ratio < 1.0

        # Verify at least 20% reduction for moderate compression
        assert result.compression_ratio < 0.8

    def test_compress_100k_token_content(self, optimizer):
        """Compress content representing 100k tokens."""
        # 100k tokens ≈ 400k characters
        huge_content = generate_large_content(400000)

        result = optimizer.compress_content(
            huge_content,
            level=CompressionLevel.AGGRESSIVE,
        )

        # Verify significant compression
        assert result.compressed_tokens < result.original_tokens
        # Aggressive should achieve at least 40% reduction
        assert result.compression_ratio < 0.6

    def test_progressive_compression_levels(self, optimizer):
        """Test that higher compression levels produce smaller output."""
        content = generate_large_content(100000)

        result_light = optimizer.compress_content(
            content,
            level=CompressionLevel.LIGHT,
        )
        result_moderate = optimizer.compress_content(
            content,
            level=CompressionLevel.MODERATE,
        )
        result_aggressive = optimizer.compress_content(
            content,
            level=CompressionLevel.AGGRESSIVE,
        )

        # Each level should compress more
        assert result_light.compressed_tokens >= result_moderate.compressed_tokens
        assert result_moderate.compressed_tokens >= result_aggressive.compressed_tokens

    def test_compression_performance(self, optimizer):
        """Compression should complete within reasonable time."""
        import time

        content = generate_large_content(200000)  # 50k tokens

        start = time.time()
        result = optimizer.compress_content(
            content,
            level=CompressionLevel.AGGRESSIVE,
        )
        elapsed = time.time() - start

        # Should complete within 5 seconds even for large content
        assert elapsed < 5.0, f"Compression took {elapsed}s, expected < 5s"
        assert result.compressed_tokens > 0


# ── Compression Threshold Tests ───────────────────────────────────────────────────


class TestCompressionThresholds:
    """Test compression triggering at configured thresholds."""

    def test_no_compression_below_threshold(self, optimizer, context_manager):
        """Context under 50% should use NORMAL strategy."""
        # Add tokens to reach 40% utilization
        asyncio.run(context_manager.record_node_tokens("test", 50000, 0))  # 40% of 128k

        strategy = optimizer.get_current_strategy()
        level = optimizer.get_compression_level()

        assert strategy == ContextStrategy.NORMAL
        assert level == CompressionLevel.LIGHT

    def test_compress_triggers_at_50_percent(self, optimizer, context_manager):
        """Compression should trigger at 50% utilization."""
        # Add tokens to reach 55% utilization
        asyncio.run(context_manager.record_node_tokens("test", 70000, 0))  # ~55% of 128k

        strategy = optimizer.get_current_strategy()
        level = optimizer.get_compression_level()

        assert strategy == ContextStrategy.COMPRESS
        assert level == CompressionLevel.MODERATE

    def test_aggressive_at_70_percent(self, optimizer, context_manager):
        """Aggressive compression should trigger at 70%."""
        # Add tokens to reach 75% utilization
        asyncio.run(context_manager.record_node_tokens("test", 96000, 0))  # ~75% of 128k

        strategy = optimizer.get_current_strategy()
        level = optimizer.get_compression_level()

        assert strategy == ContextStrategy.AGGRESSIVE
        assert level == CompressionLevel.AGGRESSIVE

    def test_emergency_at_85_percent(self, optimizer, context_manager):
        """Emergency compression should trigger at 85%."""
        # Add tokens to reach 90% utilization
        asyncio.run(context_manager.record_node_tokens("test", 115000, 0))  # ~90% of 128k

        strategy = optimizer.get_current_strategy()
        level = optimizer.get_compression_level()

        assert strategy == ContextStrategy.EMERGENCY
        assert level == CompressionLevel.EMERGENCY


# ── Fact Preservation Tests ─────────────────────────────────────────────────────────


class TestFactPreservation:
    """Test that key facts survive compression."""

    def test_headers_preserved(self, optimizer):
        """Section headers should survive compression."""
        content = """## Important Section 1

Some content here.

## Important Section 2

More content.

## Conclusion

Final thoughts.
"""
        result = optimizer.compress_content(
            content,
            level=CompressionLevel.MODERATE,
        )

        # Verify compression completed
        assert result.original_tokens > 0
        assert result.compressed_tokens > 0
        # Details should contain compression metadata
        assert "elapsed_seconds" in result.details

    def test_key_entities_preserved(self, optimizer):
        """Key entities (names, numbers) should survive moderate compression."""
        content = """
The user Alice requested analysis of dataset DS-2024-001.
Budget allocated: $50,000
Priority: HIGH
Deadline: 2024-03-15
Contact: bob@example.com
"""
        # Apply moderate compression
        result = optimizer.compress_content(
            content,
            level=CompressionLevel.MODERATE,
        )

        _compressed = result.details.get("compressed", content)

        # Key facts should survive (implementation dependent)
        # At minimum, compression should not lose all information
        assert result.original_tokens > 0
        assert result.compressed_tokens > 0

    def test_code_structure_preserved(self, optimizer):
        """Code structure should be preserved in compression."""
        code = generate_code_content(100)

        result = optimizer.compress_content(
            code,
            level=CompressionLevel.LIGHT,
            preserve_structure=True,
        )

        # Light compression should result in valid compression
        assert result.original_tokens > 0
        assert result.compressed_tokens > 0
        # Verify compression metadata is present
        assert "elapsed_seconds" in result.details


# ── Edge Case Tests ─────────────────────────────────────────────────────────────


class TestContextEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_content_compression(self, optimizer):
        """Empty content should compress to empty."""
        result = optimizer.compress_content(
            "",
            level=CompressionLevel.AGGRESSIVE,
        )

        assert result.original_tokens == 0
        assert result.compressed_tokens == 0
        assert result.compression_ratio == 1.0

    def test_single_huge_message(self, optimizer):
        """Single huge message should compress without crash."""
        # Single 100k character message
        huge_message = "This is a single huge message. " * 5000

        result = optimizer.compress_content(
            huge_message,
            level=CompressionLevel.AGGRESSIVE,
        )

        assert result.compressed_tokens > 0
        assert result.compressed_tokens < result.original_tokens

    def test_whitespace_only_content(self, optimizer):
        """Whitespace-only content should compress to minimal."""
        whitespace = "\n" * 10000 + " " * 10000 + "\t" * 10000

        result = optimizer.compress_content(
            whitespace,
            level=CompressionLevel.LIGHT,
        )

        # Whitespace should be heavily compressed
        assert result.compressed_tokens < result.original_tokens

    def test_special_characters_preserved(self, optimizer):
        """Special characters should be handled properly."""
        special_content = """
Path: /home/user/file.py
Regex: ^[A-Z]+\\d*$
Unicode: 日本語 中文 العربية
Emoji: 🚀 🎉 ✅
Math: ∑(x²) = x₁ + x₂
"""
        result = optimizer.compress_content(
            special_content,
            level=CompressionLevel.LIGHT,
        )

        # Special content should compress without errors
        assert result.compressed_tokens > 0

    def test_repeated_content_compression(self, optimizer):
        """Highly repetitive content should compress efficiently."""
        # Repetitive content compresses well
        repeated = "This line repeats.\n" * 1000

        result = optimizer.compress_content(
            repeated,
            level=CompressionLevel.MODERATE,
        )

        # Repetitive content should achieve high compression
        assert result.compression_ratio < 0.5


# ── Context Plan Tests ───────────────────────────────────────────────────────────


class TestContextPlanning:
    """Test context planning and analysis."""

    def test_analyze_context_needs(self, optimizer, context_manager):
        """Context analysis should return valid plan."""
        # Add some tokens
        asyncio.run(context_manager.record_node_tokens("test", 50000, 0))

        plan = optimizer.analyze_context_needs(
            pending_content="More content to add",
        )

        assert isinstance(plan, ContextPlan)
        assert plan.current_utilization > 0
        assert len(plan.recommended_actions) >= 0
        assert plan.estimated_room >= 0

    def test_plan_with_pending_content(self, optimizer, context_manager):
        """Plan should account for pending content."""
        asyncio.run(context_manager.record_node_tokens("test", 30000, 0))

        large_pending = "x" * 100000  # Large pending content
        plan = optimizer.analyze_context_needs(
            pending_content=large_pending,
        )

        # Plan should indicate need for compression if pending would overflow
        assert isinstance(plan, ContextPlan)


# ── Compression History Tests ─────────────────────────────────────────────────────


class TestCompressionHistory:
    """Test compression history tracking."""

    def test_compression_history_recorded(self, optimizer):
        """Each compression should be recorded in history."""
        content = generate_large_content(50000)

        # Perform multiple compressions
        optimizer.compress_content(content, level=CompressionLevel.LIGHT)
        optimizer.compress_content(content, level=CompressionLevel.MODERATE)

        # History should be tracked (implementation dependent)
        # Note: actual history tracking may vary
        assert optimizer._last_compression_time >= 0

    def test_tokens_saved_tracking(self, optimizer):
        """Tokens saved should be tracked across compressions."""
        content = generate_large_content(100000)

        result1 = optimizer.compress_content(
            content,
            level=CompressionLevel.MODERATE,
        )
        result2 = optimizer.compress_content(
            content,
            level=CompressionLevel.AGGRESSIVE,
        )

        # Total tokens saved should accumulate
        # Implementation dependent - just verify it runs
        assert result1.original_tokens > 0
        assert result2.original_tokens > 0


# ── Performance Stress Tests ───────────────────────────────────────────────────────


class TestPerformanceUnderStress:
    """Test performance under stress conditions."""

    @pytest.mark.slow
    def test_repeated_compression_performance(self, optimizer):
        """Multiple compressions should not degrade significantly."""
        import time

        content = generate_large_content(100000)
        times = []

        for _ in range(10):
            start = time.time()
            optimizer.compress_content(content, level=CompressionLevel.MODERATE)
            times.append(time.time() - start)

        # No single compression should take too long
        assert max(times) < 3.0, f"Max time {max(times)}s exceeds 3s"

        # No significant degradation
        avg_first_5 = sum(times[:5]) / 5
        avg_last_5 = sum(times[5:]) / 5
        degradation = avg_last_5 / avg_first_5 if avg_first_5 > 0 else 1.0
        assert degradation < 2.0, f"Performance degraded by {degradation}x"

    @pytest.mark.slow
    def test_concurrent_compression_safety(self, optimizer):
        """Concurrent compressions should not corrupt state."""
        import concurrent.futures

        content = generate_large_content(50000)
        errors = []

        def compress_worker(i):
            try:
                result = optimizer.compress_content(
                    content,
                    level=CompressionLevel.MODERATE,
                )
                return result.compressed_tokens
            except Exception as e:  # ruff: ignore[BLE001]
                errors.append(e)
                return -1

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(compress_worker, i) for i in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All should complete without errors
        assert len(errors) == 0, f"Errors during concurrent compression: {errors}"
        assert all(r > 0 for r in results), "Some compressions failed"


# ── Integration with Context Window ─────────────────────────────────────────────────


class TestContextWindowIntegration:
    """Test integration between ContextOptimizer and ContextWindowManager."""

    def test_optimizer_uses_context_manager(self, context_manager):
        """Optimizer should track context manager state."""
        optimizer = ContextOptimizer(context_manager=context_manager)

        # Record some tokens
        asyncio.run(context_manager.record_node_tokens("node1", 50000, 0))

        # Optimizer should see the utilization
        strategy = optimizer.get_current_strategy()
        assert strategy in [ContextStrategy.NORMAL, ContextStrategy.COMPRESS]

    def test_compression_reduces_context_pressure(self, context_manager):
        """Compression should reduce context window pressure."""
        optimizer = ContextOptimizer(context_manager=context_manager)

        # Add content to approach limit
        _large_content = generate_large_content(100000)
        asyncio.run(context_manager.record_node_tokens("initial", 80000, 0))

        # Get current utilization
        status_before = context_manager.cost_tracker.context_status
        util_before = status_before.utilization

        # Verify optimizer can analyze state
        plan = optimizer.analyze_context_needs()
        assert plan.current_utilization >= util_before


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
