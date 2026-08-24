"""Sections 14.1-14.3: Memory management verification tests."""

from __future__ import annotations

import sys

import numpy as np

from beagle.cost_tracker import estimate_tokens_agnostic


class TestTurboQuantRoundtrip:
    """Section 14.1: TurboQuant roundtrip accuracy + benchmark."""

    def test_roundtrip_cosine_similarity(self):
        from beagle.core.turboquant import TurboQuantCompressor

        compressor = TurboQuantCompressor(bits=3)
        original = np.random.randn(100, 768).astype(np.float32)
        compressed, seed = compressor.compress(original)
        decompressed = compressor.decompress(compressed, seed, original.shape)
        from numpy.linalg import norm

        sims = [
            np.dot(original[i], decompressed[i])
            / (norm(original[i]) * norm(decompressed[i]) + 1e-8)
            for i in range(len(original))
        ]
        assert np.mean(sims) > 0.85, f"avg_sim {np.mean(sims):.3f} < 0.85"

    def test_compression_ratio(self):
        from beagle.core.turboquant import TurboQuantCompressor

        compressor = TurboQuantCompressor(bits=3)
        original = np.random.randn(500, 768).astype(np.float32)
        original_size = sys.getsizeof(original.tobytes())
        compressed, _seed = compressor.compress(original)
        compressed_size = sys.getsizeof(compressed)
        ratio = original_size / max(compressed_size, 1)
        assert ratio > 2.0, f"ratio {ratio:.1f}x < 2x"


class TestTokenEstimation:
    """Section 14.3: Context window token estimation."""

    def test_estimate_basic(self):
        tokens = estimate_tokens_agnostic("hello world " * 50)
        assert 20 < tokens < 300

    def test_estimate_empty(self):
        tokens = estimate_tokens_agnostic("")
        assert tokens == 0 or tokens < 5

    def test_estimate_longer_more_tokens(self):
        short = estimate_tokens_agnostic("hi")
        long = estimate_tokens_agnostic("x" * 4000)
        assert long > short
