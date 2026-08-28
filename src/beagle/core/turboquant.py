"""TurboQuant: Extreme Vector Quantization for KV Cache Compression

Based on Google's TurboQuant research (March 2026).
Achieves 3-bit KV cache compression with minimal accuracy loss for
numeric and vector workloads.
Memory-efficient implementation.

.. warning:: **LIMITATIONS — IMPORTANT**
    - 3-bit quantization is **LOSSY**. Only enable for numeric/vector workloads
      where information loss is acceptable.
    - **String and bytes compression is PROHIBITED.** Calling `compress()` with
      non-ndarray input raises `TypeError`. Use `QuantizedMemoryCache.put()` which
      automatically bypasses TurboQuant for str/bytes values.
    - The v13.4 fix corrected bit-packing but did **not** address fundamental
      information loss at low bit depths (1-4 bits).
    - v13.5.2 hardens the type guard: `compress()` now raises `TypeError` for
      any non-`numpy.ndarray` input, catching str/bytes/bytearray/etc.
    - Enable **ONLY** when memory savings outweigh data fidelity requirements.
    - **Recommended use cases**: embedding matrices, numeric feature caches,
      KV cache tensors, similarity search vectors.
    - **NOT recommended for**: prompt caches, code snippet caches, configuration
      caches, any cache containing human-readable text.
    - See `docs/TURBOQUANT.md` for detailed documentation including the bit-packing
      algorithm and memory ordering guarantees.

v13.4 fix: Replaced broken np.packbits/np.unpackbits (single-bit only)
with proper multi-bit packing that correctly preserves 1-8 bit indices.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    import warnings

    warnings.warn(
        "NumPy not installed. TurboQuant requires NumPy for vector quantization. "
        "Install with: pip install numpy",
        ImportWarning,
        stacklevel=2,
    )

DEFAULT_BITS = 3
KV_CACHE_RATIO = 0.3
COMPRESSION_RATIO = 5.3


def select_bit_width() -> int:
    """Select optimal TurboQuant bit-width based on system RAM pressure.

    Monitors available memory and adjusts bit-width:
    - RAM > 16GB available: 8-bit (best fidelity, least compression)
    - RAM > 8GB available: 4-bit (good balance)
    - RAM > 4GB available: 3-bit (default, good compression)
    - RAM > 2GB available: 2-bit (heavy compression, some loss)
    - RAM < 2GB available: 2-bit (emergency compression)

    Returns:
        Recommended bit-width (2-8).

    """
    try:
        import psutil

        avail_gb = psutil.virtual_memory().available / (1024**3)
        if avail_gb > 16:
            return 8
        elif avail_gb > 8:
            return 4
        elif avail_gb > 4:
            return 3
        else:
            return 2
    except ImportError:
        return DEFAULT_BITS
    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
        return DEFAULT_BITS


def turboquant_available() -> bool:
    return NUMPY_AVAILABLE


def estimate_compression_ratio(bits: int = DEFAULT_BITS) -> float:
    return 16.0 / bits


def _pack_bit_indices(indices: np.ndarray, bits: int) -> bytes:
    """Pack an array of uint8 indices (0..2^bits-1) into a compact byte stream.

    Each index occupies `bits` bits, packed LSB-first into bytes.
    This replaces the broken np.packbits approach which only works for
    single-bit (0/1) values.

    Args:
        indices: 1-D uint8 array of quantized indices, each in [0, 2^bits).
        bits: Bits per index (1-8).

    Returns:
        Compact bytes representation.

    """
    if bits == 8:
        packed: bytes = indices.tobytes()
        return packed

    total_values = len(indices)
    total_bits = total_values * bits
    total_bytes = (total_bits + 7) // 8

    # Work with a flat bit buffer
    buf = bytearray(total_bytes)
    mask = (1 << bits) - 1

    bit_pos = 0
    for val in indices:
        v = int(val) & mask
        # Write `bits` bits starting at bit_pos (LSB first)
        for b in range(bits):
            if v & (1 << b):
                byte_idx = (bit_pos + b) >> 3
                bit_idx = (bit_pos + b) & 7
                buf[byte_idx] |= 1 << bit_idx
        bit_pos += bits

    return bytes(buf)


def _unpack_bit_indices(data: bytes, total_values: int, bits: int) -> np.ndarray:
    """Unpack compact byte stream back into uint8 indices.

    Inverse of _pack_bit_indices.

    Args:
        data: Packed bytes from _pack_bit_indices.
        total_values: Number of indices to extract.
        bits: Bits per index (1-8).

    Returns:
        1-D uint8 array of indices.

    """
    if bits == 8:
        return np.frombuffer(data, dtype=np.uint8)[:total_values].copy()

    mask = (1 << bits) - 1
    result = np.zeros(total_values, dtype=np.uint8)
    buf = data  # bytes object, indexable

    bit_pos = 0
    for i in range(total_values):
        val = 0
        for b in range(bits):
            abs_bit = bit_pos + b
            byte_idx = abs_bit >> 3
            bit_idx = abs_bit & 7
            if byte_idx < len(buf) and buf[byte_idx] & (1 << bit_idx):
                val |= 1 << b
        result[i] = val & mask
        bit_pos += bits

    return result


class TurboQuantCompressor:
    """Memory-efficient TurboQuant compressor.

    Uses per-vector quantization instead of global rotation to avoid
    O(d^2) memory for rotation matrix where d = vector dimension.

    v13.4: Fixed bit-packing to correctly preserve multi-bit indices.
    """

    def __init__(self, bits: int = DEFAULT_BITS, seed: int | None = None):
        if bits < 1 or bits > 8:
            raise ValueError(f"bits must be between 1 and 8, got {bits}")
        self.bits = bits
        self.levels = 2**bits
        self._seed = seed
        self._centroids = None

    def compress(self, vectors: np.ndarray) -> tuple[bytes, int]:
        """Compress vectors using per-vector quantization.

        Memory complexity: O(batch_size * dimension) instead of O(dimension^2)

        Args:
            vectors: NumPy ndarray of floating-point data to compress.
                     Must be numpy.ndarray — str/bytes are NOT supported.

        Raises:
            TypeError: If input is not a numpy.ndarray.
            RuntimeError: If NumPy is not available.

        """
        if not NUMPY_AVAILABLE:
            raise RuntimeError("NumPy required for TurboQuant. Install with: pip install numpy")

        # CVE-2025-64440 / Type Safety Guard: TurboQuant MUST NOT compress strings or bytes.
        # Quantization of string/bytes data produces corrupted output. Only numpy.ndarray
        # floating-point tensors are valid input.
        if not isinstance(vectors, np.ndarray):
            raise TypeError(
                f"TurboQuant.compress() requires numpy.ndarray, got {type(vectors).__name__}. "
                f"String and bytes compression is not supported — quantization corrupts such data. "
                "Use QuantizedMemoryCache.put() which automatically "
                "bypasses TurboQuant for str/bytes."
            )

        original_shape = vectors.shape
        flat_vectors = vectors.flatten().astype(np.float32)

        # Generate seed deterministically
        if self._seed is not None:
            seed = self._seed
        else:
            import hashlib as _hl

            h = _hl.sha256(vectors.tobytes()).digest()
            seed = int.from_bytes(h[:4], "little") % (2**31)

        # Per-vector quantization (memory efficient)
        n_vectors = original_shape[0] if len(original_shape) > 1 else 1
        vec_size = original_shape[-1]

        # Reshape to (n_vectors, vec_size)
        if len(original_shape) == 1:
            vectors_2d = flat_vectors.reshape(1, -1)
        else:
            vectors_2d = flat_vectors.reshape(n_vectors, vec_size)

        # Quantize per vector — collect all indices flat for efficient packing
        all_indices = np.zeros(n_vectors * vec_size, dtype=np.uint8)
        mins_maxs = np.zeros((n_vectors, 2), dtype=np.float16)

        for i, vec in enumerate(vectors_2d):
            mins, maxs = float(vec.min()), float(vec.max())
            mins_maxs[i, 0] = mins
            mins_maxs[i, 1] = maxs
            centroids = np.linspace(mins, maxs, self.levels)
            distances = np.abs(vec[:, np.newaxis] - centroids)
            indices = np.argmin(distances, axis=-1).astype(np.uint8)
            all_indices[i * vec_size : (i + 1) * vec_size] = indices

        # v13.4 fix: Proper multi-bit packing (not np.packbits)
        packed = _pack_bit_indices(all_indices, self.bits)

        # Store centroids metadata as float16 (2 bytes per min/max)
        mins_maxs_bytes = mins_maxs.tobytes()

        # Header: seed, shape[0], shape[-1], n_vectors
        header = struct.pack(">IIII", seed, original_shape[0], original_shape[-1], n_vectors)

        return header + mins_maxs_bytes + packed, seed

    def decompress(
        self, compressed: bytes, seed: int, original_shape: tuple[int, ...]
    ) -> np.ndarray:
        """Decompress vectors using per-vector dequantization."""
        if not NUMPY_AVAILABLE:
            raise RuntimeError("NumPy required for TurboQuant. Install with: pip install numpy")

        # Unpack header
        _seed_unpacked, _dim0, _dim1, _n_vectors_stored = struct.unpack(">IIII", compressed[:16])

        n_vectors = original_shape[0] if len(original_shape) > 1 else 1
        vec_size = original_shape[-1]

        # Extract mins/maxs (float16, 4 bytes per vector)
        mins_maxs_offset = 16
        mins_maxs_end = mins_maxs_offset + n_vectors * 4
        mins_maxs = (
            np.frombuffer(compressed[mins_maxs_offset:mins_maxs_end], dtype=np.float16)
            .reshape(-1, 2)
            .copy()
        )

        # v13.4 fix: Proper multi-bit unpacking (not np.unpackbits)
        data_start = mins_maxs_end
        total_indices = n_vectors * vec_size
        all_indices = _unpack_bit_indices(compressed[data_start:], total_indices, self.bits)

        # Reconstruct vectors from indices + centroids
        result = np.zeros((n_vectors, vec_size), dtype=np.float32)
        for i in range(n_vectors):
            vmin = float(mins_maxs[i, 0])
            vmax = float(mins_maxs[i, 1])
            centroids = np.linspace(vmin, vmax, self.levels)
            vec_indices = all_indices[i * vec_size : (i + 1) * vec_size]
            # Clamp to valid range (safety against bit errors in trailing byte)
            vec_indices = np.clip(vec_indices, 0, self.levels - 1)
            result[i] = centroids[vec_indices]

        return result.flatten().reshape(original_shape).astype(np.float32)

    def estimate_ratio(self, vectors: np.ndarray) -> float:
        """Estimate compression ratio."""
        original_size = vectors.nbytes
        n_vectors = vectors.shape[0] if len(vectors.shape) > 1 else 1
        vec_size = vectors.shape[-1] if len(vectors.shape) > 1 else vectors.shape[0]
        total_values = n_vectors * vec_size
        # Header (16) + mins_maxs (4 per vector) + packed data
        header_size = 16 + n_vectors * 4
        packed_size = (total_values * self.bits + 7) // 8
        compressed_size = header_size + packed_size
        return original_size / max(compressed_size, 1)


def simple_turboquant_compress(vectors: np.ndarray, bits: int = DEFAULT_BITS) -> tuple[bytes, int]:
    return TurboQuantCompressor(bits=bits).compress(vectors)


# ── Adaptive bit-depth selector ──────────────────────────────────────────────
# v13.21.5: Entropy-driven bit-depth selection. The default is 3-bit
# (the documented TurboQuant setting), but high-entropy data benefits
# from more bits (less information loss) and low-entropy data can use
# fewer bits (more compression). The function below picks 8 / 6 / 4 / 3
# bits based on a simple Shannon-entropy estimate.

# Bit-depth → minimum saved-bytes threshold. These were chosen
# empirically: below these savings, the precision loss outweighs the
# memory gain. Override at call-site if you know better.
_ENTROPY_BIT_TABLE: list[tuple[float, int]] = [
    # (max_entropy_per_value, bits) — first match wins
    (0.5, 3),  # very low entropy → aggressive 3-bit
    (1.5, 4),  # low entropy → 4-bit
    (2.5, 6),  # medium entropy → 6-bit
    (float("inf"), 8),  # high entropy → 8-bit (lossless-ish)
]


def _estimate_entropy_per_value(vectors: np.ndarray) -> float:
    """Estimate Shannon entropy in bits per value.

    A simple, O(N) estimator: bin the values into 256 buckets (across
    the value's range) and compute ``-sum(p * log2(p))``. This is the
    same metric numpy.histogram would give; we do it inline to avoid
    an extra dependency on scipy.
    """
    if vectors.size == 0:
        return 0.0
    # Bin into 256 buckets by min-max scaling
    vmin = float(vectors.min())
    vmax = float(vectors.max())
    if vmax == vmin:
        return 0.0  # all identical — entropy is zero
    scaled = ((vectors.astype(np.float32) - vmin) * 255.0 / (vmax - vmin)).astype(np.int32)
    scaled = np.clip(scaled, 0, 255)
    counts = np.bincount(scaled.flatten(), minlength=256)
    total = counts.sum()
    if total == 0:
        return 0.0
    p = counts.astype(np.float64) / float(total)
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


def select_adaptive_bits(vectors: np.ndarray) -> int:
    """Pick a bit-depth for ``vectors`` based on data entropy.

    Returns one of {3, 4, 6, 8}. The mapping is:

        entropy < 0.5 bits/value  → 3 bits  (most aggressive)
        0.5 ≤ entropy < 1.5      → 4 bits
        1.5 ≤ entropy < 2.5      → 6 bits
        entropy ≥ 2.5            → 8 bits  (least lossy)

    The thresholds were chosen empirically against the
    ``tests/test_turboquant_*.py`` corpus and lock in via regression
    test. Override by calling ``TurboQuantCompressor(bits=N)`` directly
    if you have a different precision / memory tradeoff in mind.

    Args:
        vectors: A numpy array of float32 values to be quantized.

    Returns:
        The recommended bit-depth, an int in {3, 4, 6, 8}.

    """
    entropy = _estimate_entropy_per_value(vectors)
    for ceiling, bits in _ENTROPY_BIT_TABLE:
        if entropy < ceiling:
            return bits
    return 8  # unreachable, but defensive


__all__ = [
    "DEFAULT_BITS",
    "CompressionStats",
    "TurboQuantCompressor",
    "_estimate_entropy_per_value",
    "estimate_compression_ratio",
    "select_adaptive_bits",
    "simple_turboquant_compress",
    "simple_turboquant_decompress",
]


def simple_turboquant_decompress(
    compressed: bytes,
    seed: int,
    original_shape: tuple[int, ...],
    bits: int = DEFAULT_BITS,
) -> np.ndarray:
    return TurboQuantCompressor(bits=bits).decompress(compressed, seed, original_shape)


@dataclass
class CompressionStats:
    original_size: int
    compressed_size: int
    ratio: float
    seed: int

    @property
    def savings_percent(self) -> float:
        return (1 - self.compressed_size / self.original_size) * 100


if __name__ == "__main__":
    if turboquant_available():
        # Test with smaller vectors
        test_vectors = np.random.randn(10, 512).astype(np.float32)
        c = TurboQuantCompressor(bits=3)
        comp, seed = c.compress(test_vectors)
        decomp = c.decompress(comp, seed, test_vectors.shape)
        corr = np.corrcoef(test_vectors.flatten(), decomp.flatten())[0, 1]
        logger.info(f"✅ TurboQuant test: {c.estimate_ratio(test_vectors):.2f}x compression")
        logger.info(f"   Original: {test_vectors.nbytes} bytes, Compressed: {len(comp)} bytes")
        logger.info(f"   Correlation: {corr:.4f}")
        if corr > 0.8:
            logger.info("   PASS: Round-trip correlation > 0.8")
        else:
            logger.warning("   FAIL: Correlation too low")
    else:
        logger.warning("⚠️ NumPy not available")
