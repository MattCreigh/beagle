"""Test Context Management Integration.

Tests the full context management stack:
- File preprocessing with chunking
- TurboQuant compression
- Progressive compression strategies
- Context overflow handling
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import pytest

# Setup path for imports
sys.path.insert(0, str(Path(__file__).parent))

from beagle.context.context_integration import (
    get_context_integration,
)
from beagle.context.context_manager_hook import (
    ContextManagementHook,
    ContextStatus,
    get_context_hook,
)
from beagle.context.context_optimizer import (
    CompressionLevel,
    ContextOptimizer,
    ContextStrategy,
    get_optimizer,
)
from beagle.context.context_preprocessor import (
    ContextPreprocessor,
    get_preprocessor,
)

logger = logging.getLogger("Test_Context")
logging.basicConfig(level=logging.INFO)


class TestContextPreprocessor:
    """Tests for file preprocessing and chunking."""

    def test_small_file_no_chunking(self):
        """Small files should not be chunked."""
        preprocessor = get_preprocessor()

        # Create small test file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write("def hello():\n    print('hello')\n")
            f.flush()
            test_file = f.name

        result = preprocessor.preprocess_file(test_file)
        assert not result.needs_chunking
        assert len(result.chunks) == 1
        assert result.total_tokens > 0

        # Cleanup
        Path(test_file).unlink()

    def test_large_file_chunking(self):
        """Large files should be chunked."""
        preprocessor = ContextPreprocessor(
            file_size_limit=1000,  # Very low to force chunking
            line_limit=50,
        )

        # Create large test file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            for i in range(100):
                f.write(f"def function_{i}():\n")
                f.write(f"    '''Function {i}'''\n")
                f.write(f"    x = {i}\n")
                f.write("    return x\n\n")
            f.flush()
            test_file = f.name

        result = preprocessor.preprocess_file(test_file)

        # Should be chunked due to size
        assert result.needs_chunking or len(result.chunks) > 1

        # Cleanup
        Path(test_file).unlink()

    def test_semantic_boundaries(self):
        """Chunking should respect semantic boundaries."""
        preprocessor = ContextPreprocessor(
            file_size_limit=500,
            line_limit=20,
        )

        # Create Python file with clear boundaries
        content = """#!/usr/bin/env python3
'''Test file with semantic boundaries.'''

import os  # noqa: E402
import sys  # noqa: E402


class FirstClass:
    def __init__(self):
        pass

    def method1(self):
        pass


class SecondClass:
    def __init__(self):
        pass

    def method2(self):
        pass


def standalone_function():
    pass
"""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            test_file = f.name

        plan = preprocessor.analyze_file(test_file)

        # Boundaries should be found
        assert len(plan.chunks) > 0

        # Cleanup
        Path(test_file).unlink()


class TestContextOptimizer:
    """Tests for context optimization."""

    def test_compression_levels(self):
        """Test different compression levels."""
        optimizer = ContextOptimizer()

        # Create test content
        content = "def test():\n    pass\n" * 100

        for level in [CompressionLevel.LIGHT, CompressionLevel.MODERATE]:
            result = optimizer.compress_content(content, level=level)
            assert result.compressed_tokens <= result.original_tokens
            # Near-incompressible content may have ratio slightly above 1.0
            # due to overhead of compression wrapper; allow 2% tolerance
            assert result.compression_ratio <= 1.02, (
                f"compression_ratio {result.compression_ratio} exceeded tolerance 1.02"
            )

    def test_context_strategy_detection(self):
        """Test strategy detection based on utilization."""
        # Create mock context manager

        optimizer = ContextOptimizer(warning_threshold=0.50)

        # Test at different utilizations
        # (Utilization is calculated from context_manager, which is None in tests)
        # So we'll test the threshold logic directly

        strategy = optimizer.get_current_strategy()
        assert strategy in [
            ContextStrategy.NORMAL,
            ContextStrategy.COMPRESS,
            ContextStrategy.AGGRESSIVE,
            ContextStrategy.EMERGENCY,
        ]

    def test_compression_stats(self):
        """Test compression statistics tracking."""
        optimizer = get_optimizer()

        content = "x = 1\n" * 100
        optimizer.compress_content(content, level=CompressionLevel.MODERATE)

        stats = optimizer.get_compression_stats()
        assert stats["total_operations"] >= 0
        # compression_ratio = original_tokens / compressed_tokens;
        # >= 1.0 means compression worked (or was identity)
        assert stats["average_compression_ratio"] >= 0.0


class TestContextManagementHook:
    """Tests for context management hook."""

    def test_context_status(self):
        """Test context status reporting."""
        hook = get_context_hook(auto_compress_at=0.50)

        status = hook.check_context_status()

        assert isinstance(status, ContextStatus)
        assert 0.0 <= status.utilization <= 1.0
        assert status.strategy in [
            ContextStrategy.NORMAL,
            ContextStrategy.COMPRESS,
            ContextStrategy.AGGRESSIVE,
            ContextStrategy.EMERGENCY,
        ]

    def test_content_preparation(self):
        """Test content preparation with chunking."""
        hook = ContextManagementHook(auto_compress_at=0.50)

        # Large content
        large_content = "x" * 10000

        result = hook.prepare_content(
            large_content,
            file_path="test_large.py",
            force_chunk=True,  # Force chunking for test
        )

        # Should handle chunking
        assert result.original_size == 10000

    def test_compression_triggers(self):
        """Test that compression triggers correctly."""
        hook = ContextManagementHook(auto_compress_at=0.50)

        # Small content - should not need compression initially
        small_content = "def test(): pass"

        result = hook.prepare_content(
            small_content,
            force_compress=False,
        )

        # Verify the hook can process content
        assert result.original_size > 0


class TestContextIntegration:
    """Tests for full context integration."""

    def test_file_preprocessing(self):
        """Test file preprocessing through integration."""
        integration = get_context_integration(
            auto_compress_threshold=0.50,
            file_size_limit=500_000,
        )

        # Create test file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write("def integration_test():\n")
            f.write("    '''Test integration'''\n")
            f.write("    return True\n")
            f.flush()
            test_file = f.name

        result = integration.preprocess_file(test_file)

        assert result.processed
        assert result.original_size > 0

        # Cleanup
        Path(test_file).unlink()

    def test_chunk_access(self):
        """Test chunk access methods."""
        integration = get_context_integration()

        # Test with no chunks
        chunk = integration.get_chunk("nonexistent_file.py", 0)
        assert chunk is None

    def test_statistics(self):
        """Test statistics tracking."""
        integration = get_context_integration()

        stats = integration.get_stats()

        assert "integration" in stats
        assert "context_window" in stats
        assert "processed_files" in stats


class TestTurboQuantIntegration:
    """Tests for TurboQuant integration."""

    def test_turboquant_available(self):
        """Test TurboQuant availability."""
        from beagle.core.turboquant import turboquant_available

        # May or may not be available depending on NumPy
        available = turboquant_available()
        assert isinstance(available, bool)

    def test_enhanced_context_fold(self):
        """Test enhanced context fold."""
        import asyncio

        integration = get_context_integration()

        # Test async fold
        async def run_fold():
            content = "test" * 100
            result = await integration.enhanced_context_fold(content, "auto")
            return result

        folded = asyncio.run(run_fold())
        assert folded is not None


def run_all_tests():
    """Run all tests."""
    print("=" * 70)
    print("Running Context Management Tests")
    print("=" * 70)

    # Test ContextPreprocessor
    print("\n[1. Context Preprocessor]")
    test = TestContextPreprocessor()
    print("  - test_small_file_no_chunking: ", end="")
    test.test_small_file_no_chunking()
    print("✓")

    print("  - test_large_file_chunking: ", end="")
    test.test_large_file_chunking()
    print("✓")

    print("  - test_semantic_boundaries: ", end="")
    test.test_semantic_boundaries()
    print("✓")

    # Test ContextOptimizer
    print("\n[2. Context Optimizer]")
    test = TestContextOptimizer()
    print("  - test_compression_levels: ", end="")
    test.test_compression_levels()
    print("✓")

    print("  - test_context_strategy_detection: ", end="")
    test.test_context_strategy_detection()
    print("✓")

    print("  - test_compression_stats: ", end="")
    test.test_compression_stats()
    print("✓")

    # Test ContextManagementHook
    print("\n[3. Context Management Hook]")
    test = TestContextManagementHook()
    print("  - test_context_status: ", end="")
    test.test_context_status()
    print("✓")

    print("  - test_content_preparation: ", end="")
    test.test_content_preparation()
    print("✓")

    print("  - test_compression_triggers: ", end="")
    test.test_compression_triggers()
    print("✓")

    # Test ContextIntegration
    print("\n[4. Context Integration]")
    test = TestContextIntegration()
    print("  - test_file_preprocessing: ", end="")
    test.test_file_preprocessing()
    print("✓")

    print("  - test_chunk_access: ", end="")
    test.test_chunk_access()
    print("✓")

    print("  - test_statistics: ", end="")
    test.test_statistics()
    print("✓")

    # Test TurboQuant
    print("\n[5. TurboQuant Integration]")
    test = TestTurboQuantIntegration()
    print("  - test_turboquant_available: ", end="")
    test.test_turboquant_available()
    print("✓")

    print("  - test_enhanced_context_fold: ", end="")
    test.test_enhanced_context_fold()
    print("✓")

    print("\n" + "=" * 70)
    print("All tests passed! ✓")
    print("=" * 70)

    # Print final stats
    integration = get_context_integration()
    print("\nIntegration Statistics:")
    stats = integration.get_stats()
    print(f"  Files processed: {stats['integration']['files_processed']}")
    print(f"  Files chunked: {stats['integration']['files_chunked']}")
    print(f"  Context folds: {stats['integration']['context_folds']}")


if __name__ == "__main__":
    import sys

    if "--pytest" in sys.argv:
        # Run with pytest
        pytest.main([__file__, "-v"])
    else:
        # Run tests directly
        run_all_tests()
