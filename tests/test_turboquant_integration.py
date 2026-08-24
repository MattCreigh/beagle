"""Integration tests for TurboQuant compression functionality."""

import sys

sys.path.insert(0, "/home/server/Projects/beagle")


import pytest


class TestTurboQuantBasic:
    """Basic TurboQuant functionality tests."""

    def test_import_turboquant_module(self):
        """Test that turboquant module can be imported."""
        from beagle.core import turboquant

        assert turboquant is not None

    def test_turboquant_available_function(self):
        """Test turboquant_available returns proper boolean."""
        from beagle.core.turboquant import turboquant_available

        result = turboquant_available()
        assert isinstance(result, bool)

    def test_constants_defined(self):
        """Test that module constants are defined."""
        from beagle.core.turboquant import (
            COMPRESSION_RATIO,
            DEFAULT_BITS,
            KV_CACHE_RATIO,
        )

        assert DEFAULT_BITS == 3
        assert KV_CACHE_RATIO == 0.3
        assert COMPRESSION_RATIO == 5.3

    def test_estimate_compression_ratio(self):
        """Test compression ratio estimation."""
        from beagle.core.turboquant import estimate_compression_ratio

        # Test default bits
        ratio_default = estimate_compression_ratio()
        assert ratio_default == pytest.approx(16.0 / 3, rel=0.01)

        # Test 4-bit
        ratio_4bit = estimate_compression_ratio(bits=4)
        assert ratio_4bit == pytest.approx(4.0, rel=0.01)

        # Test 8-bit
        ratio_8bit = estimate_compression_ratio(bits=8)
        assert ratio_8bit == pytest.approx(2.0, rel=0.01)


class TestTurboQuantCompressor:
    """Test TurboQuantCompressor class."""

    def test_compressor_initialization(self):
        """Test compressor can be initialized with different bit settings."""
        from beagle.core.turboquant import TurboQuantCompressor

        # Default init
        c1 = TurboQuantCompressor()
        assert c1.bits == 3
        assert c1.levels == 8

        # Custom bits
        c2 = TurboQuantCompressor(bits=4)
        assert c2.bits == 4
        assert c2.levels == 16

        # Custom seed
        c3 = TurboQuantCompressor(bits=3, seed=42)
        assert c3._seed == 42

    def test_invalid_bit_settings(self):
        """Test that invalid bit settings raise errors."""
        from beagle.core.turboquant import TurboQuantCompressor

        with pytest.raises(ValueError, match="bits must be between 1 and 8"):
            TurboQuantCompressor(bits=0)

        with pytest.raises(ValueError, match="bits must be between 1 and 8"):
            TurboQuantCompressor(bits=9)

    @pytest.mark.skipif(
        not pytest.importorskip("numpy", reason="NumPy not available"),
        reason="NumPy required",
    )
    def test_compress_decompress_roundtrip(self):
        """Test that compress/decompress preserves data approximately."""
        import numpy as np

        from beagle.core.turboquant import TurboQuantCompressor

        # Small test vectors
        original = np.random.randn(5, 128).astype(np.float32)

        compressor = TurboQuantCompressor(bits=3)
        compressed, seed = compressor.compress(original)

        # Decompress
        decompressed = compressor.decompress(compressed, seed, original.shape)

        # Check shape preserved
        assert decompressed.shape == original.shape

        # Check reasonable approximation (3-bit quantization will have error)
        correlation = np.corrcoef(original.flatten(), decompressed.flatten())[0, 1]
        assert correlation > 0.8  # Should have reasonable correlation

    @pytest.mark.skipif(
        not pytest.importorskip("numpy", reason="NumPy not available"),
        reason="NumPy required",
    )
    def test_compression_actually_compresses(self):
        """Test that compressed data is smaller than original."""
        import numpy as np

        from beagle.core.turboquant import TurboQuantCompressor

        original = np.random.randn(10, 512).astype(np.float32)
        original_size = original.nbytes

        compressor = TurboQuantCompressor(bits=3)
        compressed, _seed = compressor.compress(original)

        # Compressed should be smaller
        assert len(compressed) < original_size

        # Check compression ratio
        ratio = compressor.estimate_ratio(original)
        assert ratio > 1.0

    @pytest.mark.skipif(
        not pytest.importorskip("numpy", reason="NumPy not available"),
        reason="NumPy required",
    )
    def test_multiple_vectors(self):
        """Test compression with multiple vectors."""
        import numpy as np

        from beagle.core.turboquant import TurboQuantCompressor

        # Test 1D array
        vec_1d = np.random.randn(256).astype(np.float32)
        compressor = TurboQuantCompressor(bits=4)
        comp_1d, seed_1d = compressor.compress(vec_1d)
        decomp_1d = compressor.decompress(comp_1d, seed_1d, vec_1d.shape)
        assert decomp_1d.shape == vec_1d.shape

        # Test 2D array
        vec_2d = np.random.randn(8, 64).astype(np.float32)
        comp_2d, seed_2d = compressor.compress(vec_2d)
        decomp_2d = compressor.decompress(comp_2d, seed_2d, vec_2d.shape)
        assert decomp_2d.shape == vec_2d.shape


class TestSimpleAPI:
    """Test simple convenience functions."""

    def test_simple_functions_exist(self):
        """Test that simple API functions are available."""
        from beagle.core.turboquant import (
            simple_turboquant_compress,
            simple_turboquant_decompress,
        )

        assert callable(simple_turboquant_compress)
        assert callable(simple_turboquant_decompress)

    @pytest.mark.skipif(
        not pytest.importorskip("numpy", reason="NumPy not available"),
        reason="NumPy required",
    )
    def test_simple_api_roundtrip(self):
        """Test simple API roundtrip."""
        import numpy as np

        from beagle.core.turboquant import (
            simple_turboquant_compress,
            simple_turboquant_decompress,
        )

        data = np.random.randn(4, 32).astype(np.float32)
        compressed, seed = simple_turboquant_compress(data, bits=3)
        decompressed = simple_turboquant_decompress(compressed, seed, data.shape, bits=3)

        assert decompressed.shape == data.shape


class TestCompressionStats:
    """Test CompressionStats dataclass."""

    def test_compression_stats_creation(self):
        """Test creating CompressionStats instances."""
        from beagle.core.turboquant import CompressionStats

        stats = CompressionStats(
            original_size=1000,
            compressed_size=200,
            ratio=5.0,
            seed=42,
        )

        assert stats.original_size == 1000
        assert stats.compressed_size == 200
        assert stats.ratio == 5.0
        assert stats.seed == 42
        assert stats.savings_percent == pytest.approx(80.0, rel=0.01)

    def test_compression_stats_no_compression(self):
        """Test stats when no compression achieved."""
        from beagle.core.turboquant import CompressionStats

        stats = CompressionStats(
            original_size=1000,
            compressed_size=1000,
            ratio=1.0,
            seed=42,
        )

        assert stats.savings_percent == pytest.approx(0.0, rel=0.01)


class TestIntegrationWithCache:
    """Test TurboQuant integration with cache module."""

    def test_cache_imports_turboquant(self):
        """Test that cache module can import turboquant."""
        from beagle.utils.cache import (
            TURBOQUANT_AVAILABLE,
            TURBOQUANT_CACHE_ENABLED,
        )
        from beagle.utils.cache import (
            turboquant_available as cache_turboquant_available,
        )

        # Should not raise
        assert isinstance(TURBOQUANT_AVAILABLE, bool)
        assert isinstance(TURBOQUANT_CACHE_ENABLED, bool)
        assert callable(cache_turboquant_available)

    def test_cache_fallback_functions_work(self):
        """Test that cache compressor functions work with real data."""
        from beagle.utils.cache import (
            COMPRESSION_RATIO,
            simple_turboquant_compress,
            simple_turboquant_decompress,
        )

        try:
            import numpy as np
        except ImportError:
            pytest.skip("NumPy required for cache compression test")
            return

        import numpy as np

        # Create real test data for compression
        data = np.random.randn(4, 32).astype(np.float32)
        result_compress = simple_turboquant_compress(data, bits=3)
        assert isinstance(result_compress, tuple)
        assert len(result_compress) == 2

        compressed, seed = result_compress
        result_decompress = simple_turboquant_decompress(compressed, seed, data.shape, bits=3)
        assert result_decompress is not None
        assert result_decompress.shape == data.shape

        # Should always have a ratio
        assert COMPRESSION_RATIO >= 1.0


class TestContextOptimizerIntegration:
    """Test TurboQuant integration with context optimizer."""

    def test_context_optimizer_can_enable_turboquant(self):
        """Test that context optimizer can initialize with turboquant."""
        from beagle.context.context_optimizer import (
            get_optimizer,
            reset_optimizer,
        )

        reset_optimizer()

        # Should not raise
        optimizer = get_optimizer(enable_turboquant=True)
        assert optimizer is not None
        assert isinstance(optimizer.enable_turboquant, bool)

        reset_optimizer()

    def test_compression_stats_tracking(self):
        """Test that context optimizer tracks compression stats."""
        from beagle.context.context_optimizer import (
            ContextOptimizer,
            reset_optimizer,
        )

        reset_optimizer()

        optimizer = ContextOptimizer()
        stats = optimizer.get_compression_stats()

        assert "turboquant_enabled" in stats
        assert "total_tokens_saved" in stats
        assert isinstance(stats["turboquant_enabled"], bool)

        reset_optimizer()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
