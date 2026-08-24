"""Tests for TurboQuant roundtrip accuracy and performance benchmark.

Verifies that TurboQuant 3-bit compression preserves data within acceptable
tolerance across various matrix sizes, and achieves 2x+ compression ratio.
"""

import sys

import numpy as np
import pytest

from beagle.core.turboquant import (
    TurboQuantCompressor,
    estimate_compression_ratio,
    simple_turboquant_compress,
    simple_turboquant_decompress,
    turboquant_available,
)


@pytest.mark.skipif(not turboquant_available(), reason="NumPy required")
class TestTurboQuantRoundtrip:
    """Roundtrip accuracy tests for TurboQuant compression."""

    def test_small_matrix_roundtrip(self):
        """5x128 matrix should preserve >0.85 cosine similarity."""
        compressor = TurboQuantCompressor(bits=3)
        original = np.random.randn(5, 128).astype(np.float32)
        compressed, seed = compressor.compress(original)
        decompressed = compressor.decompress(compressed, seed, original.shape)

        assert decompressed.shape == original.shape
        avg_sim = self._avg_cosine_similarity(original, decompressed)
        assert avg_sim > 0.85, f"Cosine similarity {avg_sim:.3f} below 0.85"

    def test_medium_matrix_roundtrip(self):
        """100x768 matrix (typical embedding) should preserve >0.85."""
        compressor = TurboQuantCompressor(bits=3)
        original = np.random.randn(100, 768).astype(np.float32)
        compressed, seed = compressor.compress(original)
        decompressed = compressor.decompress(compressed, seed, original.shape)

        avg_sim = self._avg_cosine_similarity(original, decompressed)
        assert avg_sim > 0.85, f"Cosine similarity {avg_sim:.3f} below 0.85"

    def test_large_matrix_roundtrip(self):
        """500x768 matrix roundtrip should preserve >0.80."""
        compressor = TurboQuantCompressor(bits=3)
        original = np.random.randn(500, 768).astype(np.float32)
        compressed, seed = compressor.compress(original)
        decompressed = compressor.decompress(compressed, seed, original.shape)

        avg_sim = self._avg_cosine_similarity(original, decompressed)
        assert avg_sim > 0.80, f"Cosine similarity {avg_sim:.3f} below 0.80"

    def test_positive_values_roundtrip(self):
        """All-positive vectors (like softmax outputs) should roundtrip well."""
        compressor = TurboQuantCompressor(bits=3)
        original = np.abs(np.random.randn(20, 256).astype(np.float32)) + 0.01
        compressed, seed = compressor.compress(original)
        decompressed = compressor.decompress(compressed, seed, original.shape)

        avg_sim = self._avg_cosine_similarity(original, decompressed)
        assert avg_sim > 0.85, f"Cosine similarity {avg_sim:.3f} below 0.85"

    def test_simple_api_roundtrip(self):
        """simple_turboquant_compress/decompress should roundtrip correctly."""
        original = np.random.randn(50, 256).astype(np.float32)
        compressed, seed = simple_turboquant_compress(original, bits=3)
        decompressed = simple_turboquant_decompress(compressed, seed, original.shape, bits=3)

        avg_sim = self._avg_cosine_similarity(original, decompressed)
        assert avg_sim > 0.85, f"Cosine similarity {avg_sim:.3f} below 0.85"

    @staticmethod
    def _avg_cosine_similarity(original: np.ndarray, decompressed: np.ndarray) -> float:
        """Compute average cosine similarity across all vectors."""
        from numpy.linalg import norm

        similarities = []
        for i in range(len(original)):
            denom = norm(original[i]) * norm(decompressed[i]) + 1e-8
            cos_sim = float(np.dot(original[i], decompressed[i]) / denom)
            similarities.append(cos_sim)
        return float(np.mean(similarities))


@pytest.mark.skipif(not turboquant_available(), reason="NumPy required")
class TestTurboQuantCompressionRatio:
    """Performance and compression ratio tests."""

    def test_compression_ratio_meets_minimum(self):
        """Compression ratio should be at least 2x for typical matrices."""
        original = np.random.randn(500, 768).astype(np.float32)
        original_size = sys.getsizeof(original.tobytes())

        compressor = TurboQuantCompressor(bits=3)
        compressed, _seed = compressor.compress(original)
        compressed_size = sys.getsizeof(compressed)

        ratio = original_size / max(compressed_size, 1)
        assert ratio > 2.0, f"Compression ratio {ratio:.1f}x below 2x minimum"

    def test_compression_ratio_estimate_function(self):
        """estimate_compression_ratio should return reasonable values."""
        for bits in [1, 2, 3, 4, 8]:
            ratio = estimate_compression_ratio(bits=bits)
            assert ratio > 0, f"Ratio for {bits}-bit should be positive"
            # More bits = less compression
            if bits > 1:
                prev_ratio = estimate_compression_ratio(bits=bits - 1)
                assert ratio <= prev_ratio, (
                    f"{bits}-bit ratio {ratio:.1f}x should be <= "
                    f"{bits - 1}-bit ratio {prev_ratio:.1f}x"
                )

    def test_compression_preserves_shape(self):
        """Compress/decompress should preserve matrix shape."""
        for shape in [(10, 128), (50, 256), (100, 768)]:
            original = np.random.randn(*shape).astype(np.float32)
            compressor = TurboQuantCompressor(bits=3)
            compressed, seed = compressor.compress(original)
            decompressed = compressor.decompress(compressed, seed, original.shape)
            assert decompressed.shape == original.shape, (
                f"Shape mismatch: {decompressed.shape} != {original.shape}"
            )

    def test_bit_width_tradeoff(self):
        """Higher bit widths should produce better fidelity."""
        original = np.random.randn(100, 256).astype(np.float32)

        similarities = {}
        for bits in [2, 3, 4]:
            compressor = TurboQuantCompressor(bits=bits)
            compressed, seed = compressor.compress(original)
            decompressed = compressor.decompress(compressed, seed, original.shape)
            avg_sim = float(
                np.mean(
                    [
                        np.dot(original[i], decompressed[i])
                        / (np.linalg.norm(original[i]) * np.linalg.norm(decompressed[i]) + 1e-8)
                        for i in range(len(original))
                    ]
                )
            )
            similarities[bits] = avg_sim

        # Higher bit width should produce better similarity
        assert similarities[4] > similarities[2], (
            f"4-bit ({similarities[4]:.3f}) should be better than 2-bit ({similarities[2]:.3f})"
        )
