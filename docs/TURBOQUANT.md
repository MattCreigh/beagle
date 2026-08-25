# TurboQuant Bit-Packing Documentation

## Overview

TurboQuant implements extreme vector quantization for KV cache compression,
based on Google's TurboQuant research (March 2026). It achieves 3-bit
quantization with minimal accuracy loss for **numeric/vector workloads only**.

## ⚠️ Critical Limitation: No String/Bytes Compression

**As of v13.6.0, TurboQuant PROHIBITS compression of `str`, `bytes`, and
`bytearray` values.** This is enforced at two levels:

1. **`TurboQuantCompressor.compress()`** raises `TypeError` for any
   non-`numpy.ndarray` input.
2. **`QuantizedMemoryCache.put()`** automatically bypasses TurboQuant for
   string/bytes values, storing them uncompressed.

This dual-layer defense ensures the security invariant is always maintained,
regardless of how the cache API is accessed.

1. **`QuantizedMemoryCache.set()`** (v13.6.0) — The inherited `set()` method from
   `MemoryCache` previously bypassed the string guard in `put()`. As of v13.6.0,
   `set()` is overridden to check `isinstance(value, str)` and store strings
   uncompressed via `super().set()`, providing a **triple-layer defense** that
   covers all cache mutation paths.

### Why Strings Are Prohibited

3-bit quantization maps continuous float values to 8 discrete levels (2³ = 8).
When applied to string data (via byte representation), this produces:

- **Corrupted characters**: The 8-level quantization cannot faithfully represent
  the 256 possible byte values, causing data corruption.
- **Irreversible loss**: Unlike numeric data where small quantization errors
  are tolerable, text data has zero tolerance for bit errors.
- **Security risk**: Corrupted cache data can cause crashes, incorrect behavior,
  or injection vulnerabilities when deserialized.

## Bit-Packing Algorithm

### LSB-First Multi-Bit Packing

The `_pack_bit_indices()` function packs an array of uint8 indices (each in
the range [0, 2^bits)) into a compact byte stream using LSB-first bit packing.

### Why Not `np.packbits`?

`np.packbits()` only works for **single-bit** (0/1) values. For multi-bit
indices (2-bit, 3-bit, 4-bit), we need a different approach:

```text
Example: 3-bit packing of indices [5, 3, 6]
Index 5 = 101 (3 bits)
Index 3 = 011 (3 bits)
Index 6 = 110 (3 bits)

LSB-first packing:
Bit position:  0  1  2  3  4  5  6  7  8
Value:          1  0  1  1  1  0  0  1  1

Byte 0: bits 0-7 = 10110110 = 0xB6
Byte 1: bit 8    = 00000001 = 0x01
```

### Multi-Bit Quantization Correctness

The v13.4 fix replaced the broken `np.packbits`/`np.unpackbits` approach
(which could only handle 1-bit values) with proper multi-bit packing that
correctly preserves 1-8 bit indices.

**Key correctness properties:**

1. **Lossless for 8-bit**: When `bits=8`, indices are byte-aligned and
   no packing occurs — `compress()` returns raw bytes.
2. **No cross-contamination**: Each index's bits are written independently
   into the bit buffer using `buf[byte_idx] |= 1 << bit_idx`.
3. **Proper alignment**: The total bit stream length is `total_values * bits`,
   packed into `(total_bits + 7) // 8` bytes.

### Unpacking Symmetry

`_unpack_bit_indices()` is the exact inverse of `_pack_bit_indices()`:

```python
# Round-trip test:
indices = np.array([5, 3, 6, 0, 7, 2, 1, 4], dtype=np.uint8)
packed = _pack_bit_indices(indices, bits=3)
unpacked = _unpack_bit_indices(packed, len(indices), bits=3)
assert np.array_equal(indices, unpacked)  # ✅ Always passes
```

## Memory Efficiency

| Bit Width | Levels | Compression Ratio | Use Case |
|-----------|--------|-------------------|----------|
| 1-bit     | 2      | 16x               | Binary features |
| 2-bit     | 4      | 8x                | Coarse classification |
| 3-bit     | 8      | 5.3x              | KV cache, embeddings |
| 4-bit     | 16     | 4x                | Moderate precision |
| 8-bit     | 256    | 2x                | Full precision quantized |

## Configuration

TurboQuant cache compression is disabled by default. Enable via:

```bash
export TURBOQUANT_CACHE_ENABLED=true
```

Or in code:

```python
from beagle.utils.cache import QuantizedMemoryCache
cache = QuantizedMemoryCache(use_turboquant=True)
```

## API Reference

### TurboQuantCompressor

- `compress(vectors: np.ndarray) -> tuple[bytes, int]` — Compress ndarray.
  **Raises `TypeError`** for non-ndarray input (str, bytes, list, etc.).

- `decompress(compressed: bytes, seed: int, original_shape: tuple) -> np.ndarray`
  — Decompress back to ndarray.

### QuantizedMemoryCache

- `put(key, value, ttl, *, force=False)` — Store value. str/bytes are ALWAYS
  stored uncompressed regardless of `force` parameter.

- `set(key, value, ttl)` — Store value. str values bypass TurboQuant compression
  entirely, stored uncompressed via parent class. Non-str values delegate to `put()`.

- `get(key)` — Retrieve value, decompressing if needed.

## Security Considerations

1. **Type guard is hardcoded**: The `TypeError` check in `compress()` cannot
   be overridden or bypassed via configuration.
2. **Triple-layer defense**: `TurboQuantCompressor.compress()`,
   `QuantizedMemoryCache.put()`, and `QuantizedMemoryCache.set()` all
   independently enforce the no-strings rule.
3. **Audit logging**: Every string bypass is logged and counted via
   `_turboquant_string_skips` for monitoring.
4. **v13.5.2 change**: The `force=True` parameter on `put()` is now deprecated
   and has **no effect** for str/bytes values. It is retained only for API
   backward compatibility.
5. **v13.6.0 change**: `set()` override added — previously inherited from
   `MemoryCache` without string guards, creating a bypass path.
