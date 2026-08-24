"""Regression tests for the adaptive TurboQuant bit-depth selector.

Locks down the entropy-driven bit-depth table:
  - all-same / near-constant vectors → 3 bits
  - high-cardinality random vectors → 8 bits
  - intermediate entropy → 4 or 6 bits
"""

from __future__ import annotations

import numpy as np

from beagle.core.turboquant import (
    _estimate_entropy_per_value,
    select_adaptive_bits,
)


class TestEntropyEstimator:
    def test_all_zeros_zero_entropy(self):
        v = np.zeros((10, 64), dtype=np.float32)
        assert _estimate_entropy_per_value(v) == 0.0

    def test_all_same_value_zero_entropy(self):
        v = np.full((10, 64), 42.0, dtype=np.float32)
        assert _estimate_entropy_per_value(v) == 0.0

    def test_two_value_low_entropy(self):
        v = np.zeros((10, 64), dtype=np.float32)
        v[:, 32:] = 1.0
        # 50/50 split → 1 bit of entropy
        e = _estimate_entropy_per_value(v)
        assert 0.99 <= e <= 1.01

    def test_uniform_random_high_entropy(self):
        rng = np.random.default_rng(seed=42)
        v = rng.uniform(-1e6, 1e6, (10, 256)).astype(np.float32)
        e = _estimate_entropy_per_value(v)
        # Should be close to 8 bits
        assert e > 6.0

    def test_empty_array(self):
        v = np.array([], dtype=np.float32)
        assert _estimate_entropy_per_value(v) == 0.0


class TestAdaptiveBits:
    def test_constant_data_3_bits(self):
        v = np.full((10, 64), 7.5, dtype=np.float32)
        assert select_adaptive_bits(v) == 3

    def test_two_value_data_3_or_4_bits(self):
        v = np.zeros((10, 64), dtype=np.float32)
        v[:, 32:] = 1.0
        bits = select_adaptive_bits(v)
        assert bits in (3, 4)  # low-entropy regime

    def test_high_cardinality_random_8_bits(self):
        rng = np.random.default_rng(seed=42)
        v = rng.uniform(-1e6, 1e6, (10, 512)).astype(np.float32)
        assert select_adaptive_bits(v) == 8

    def test_returns_int(self):
        rng = np.random.default_rng(seed=1)
        v = rng.standard_normal((10, 64)).astype(np.float32)
        result = select_adaptive_bits(v)
        assert isinstance(result, int)

    def test_returns_one_of_allowed_values(self):
        """Bit-depth is always in {3, 4, 6, 8}."""
        rng = np.random.default_rng(seed=7)
        for _ in range(20):
            v = rng.standard_normal((10, 64)).astype(np.float32)
            bits = select_adaptive_bits(v)
            assert bits in (3, 4, 6, 8)

    def test_empty_array_does_not_crash(self):
        v = np.array([], dtype=np.float32)
        # The selector should fall through to a safe default (3 bits)
        assert select_adaptive_bits(v) == 3
