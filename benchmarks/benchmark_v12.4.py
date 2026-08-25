#!/usr/bin/env python3
"""Performance benchmarks for Beagle v12.4.

Measures caching performance improvements from Phase 4.
"""

import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from beagle.security import scrub_secrets, validate_query


def benchmark_scrub_secrets(iterations=100):
    """Benchmark secret scrubbing with caching."""
    test_data = "API_KEY=sk-1234567890abcdef and password=secret123"

    # Warm up cache
    for _ in range(10):
        scrub_secrets(test_data)

    # Measure
    start = time.perf_counter()
    for _ in range(iterations):
        scrub_secrets(test_data)
    elapsed = time.perf_counter() - start

    return elapsed, iterations / elapsed


def benchmark_validate_query(iterations=100):
    """Benchmark query validation with caching."""
    test_query = "Analyze the codebase for security vulnerabilities"

    # Warm up cache
    for _ in range(10):
        validate_query(test_query)

    # Measure
    start = time.perf_counter()
    for _ in range(iterations):
        validate_query(test_query)
    elapsed = time.perf_counter() - start

    return elapsed, iterations / elapsed


def main():
    print("=" * 60)
    print("Beagle v12.4 Performance Benchmarks")
    print("=" * 60)
    print()

    # Scrub secrets benchmark
    print("🔐 Secret Scrubbing (100 iterations)")
    elapsed, ops_sec = benchmark_scrub_secrets(100)
    print(f"   Time: {elapsed:.4f}s")
    print(f"   Ops/sec: {ops_sec:.1f}")
    print(f"   Avg per call: {elapsed / 100 * 1000:.3f}ms")
    print()

    # Validate query benchmark
    print("✅ Query Validation (100 iterations)")
    elapsed, ops_sec = benchmark_validate_query(100)
    print(f"   Time: {elapsed:.4f}s")
    print(f"   Ops/sec: {ops_sec:.1f}")
    print(f"   Avg per call: {elapsed / 100 * 1000:.3f}ms")
    print()

    print("=" * 60)
    print("✅ All benchmarks completed successfully")
    print("=" * 60)


if __name__ == "__main__":
    main()
