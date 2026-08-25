#!/usr/bin/env python3
"""Run all benchmarks and generate performance baseline.

Usage:
    python run_benchmarks.py [--output-dir OUTPUT_DIR]
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def run_benchmark(script_path: Path) -> dict | None:
    """Run a benchmark script and return results."""
    print(f"\n{'=' * 60}")
    print(f"Running: {script_path.name}")
    print(f"{'=' * 60}\n")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        # Print output for visibility
        print(result.stdout)
        if result.stderr:
            print(f"STDERR: {result.stderr}")

        # Load results JSON
        results_file = script_path.parent / script_path.name.replace(
            "_benchmarks.py", "_results.json"
        )
        if results_file.exists():
            with open(results_file, encoding="utf-8") as f:
                return json.load(f)

    except subprocess.TimeoutExpired:
        print(f"ERROR: Benchmark {script_path.name} timed out")
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — surface benchmark error
        print(f"ERROR: {e}")

    return None


def generate_performance_doc(results: dict[str, dict]) -> str:
    """Generate PERFORMANCE.md from benchmark results."""

    doc = """# Beagle v12.3 Performance Baseline

## Overview

This document provides performance baselines for Beagle components. These measurements were taken on the reference hardware and serve as a baseline for performance regression testing.

## Test Environment

- **Date**: {date}
- **Python Version**: {python_version}
- **OS**: {os_info}

## Workflow Operations

| Operation | Mean (ms) | P99 (ms) | Ops/s | Notes |
|-----------|-----------|----------|-------|-------|
{workflow_table}

## Memory Operations

| Operation | Mean (ms) | P99 (ms) | Ops/s | Tier |
|-----------|-----------|----------|-------|------|
{memory_table}

## RAG Operations

| Operation | Mean (ms) | P99 (ms) | Ops/s | Notes |
|-----------|-----------|----------|-------|-------|
{rag_table}

## Performance Targets

Based on these baselines, here are the performance targets for production:

### Latency Targets (P99)
- Workflow node execution start: < 100ms
- L0 context add: < 1ms
- L1 episode store: < 10ms
- L1 episode retrieve: < 5ms
- RAG search (full): < 500ms
- Tracking DB write: < 50ms

### Throughput Targets
- Workflow nodes started: > 10/sec
- L0 context operations: > 10,000/sec
- L1 episode operations: > 100/sec
- RAG searches: > 10/sec
- Tracking DB writes: > 20/sec

## Performance Optimization Tips

### L0 Context (Working Memory)
- Keep context size below 100K tokens for optimal performance
- Use hierarchical compaction to preserve important context
- Prune old messages before reaching the 128K limit

### L1 Memory (Episodic)
- Use batch inserts when storing multiple episodes
- Run periodic cleanup to remove old sessions
- Index on session_id for faster retrieval

### L2 Memory (Semantic)
- Pre-compute embeddings for common queries
- Use max_hops=1 for faster searches when relationships are not critical
- Limit top_k to 5-10 for best latency

### Workflow Execution
- Use preflight estimation to validate budget before starting
- Log tracking metrics asynchronously
- Cache steering directive checks

## Regression Testing

To detect performance regressions:

```bash
# Run baseline comparison
python benchmarks/run_benchmarks.py --compare baseline.json

# Flag thresholds
# - Mean latency: +20% is warning, +50% is critical
# - P99 latency: +30% is warning, +100% is critical
# - Throughput: -20% is warning, -50% is critical
```

## Historical Performance

| Version | Date | Workflow Mean | L1 Store Mean | RAG Search Mean |
|---------|------|---------------|---------------|-----------------|
| v12.3.0 | {date} | TBD | {l1_store_mean}ms | TBD |

---
*This document is auto-generated from benchmark results.*
"""

    # Build tables
    workflow_table = ""
    memory_table = ""
    rag_table = ""

    if "workflow" in results:
        for r in results["workflow"]["results"]:
            workflow_table += f"| {r['name']} | {r['mean_time_ms']:.4f} | {r['p99_time_ms']:.4f} | {r['ops_per_second']:.1f} | - |\n"

    if "memory" in results:
        for r in results["memory"]["results"]:
            tier = (
                "L0"
                if "context" in r["name"].lower()
                else "L1"
                if "episode" in r["name"].lower()
                else "Core"
            )
            memory_table += f"| {r['name']} | {r['mean_time_ms']:.4f} | {r['p99_time_ms']:.4f} | {r['ops_per_second']:.1f} | {tier} |\n"

    if "rag" in results:
        for r in results["rag"]["results"]:
            rag_table += f"| {r['name']} | {r['mean_time_ms']:.4f} | {r['p99_time_ms']:.4f} | {r['ops_per_second']:.1f} | - |\n"

    # Get L1 store mean for historical table
    l1_store_mean = "TBD"
    if "memory" in results:
        for r in results["memory"]["results"]:
            if "episode_store" in r["name"]:
                l1_store_mean = f"{r['mean_time_ms']:.2f}"
                break

    import platform

    return doc.format(
        date=datetime.now(UTC).strftime("%Y-%m-%d"),
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        os_info=f"{platform.system()} {platform.release()}",
        workflow_table=workflow_table or "| - | - | - | - | - |",
        memory_table=memory_table or "| - | - | - | - | - |",
        rag_table=rag_table or "| - | - | - | - | - |",
        l1_store_mean=l1_store_mean,
    )


def main():
    parser = argparse.ArgumentParser(description="Run Beagle benchmarks")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent,
        help="Output directory for results",
    )
    parser.add_argument("--skip-workflow", action="store_true", help="Skip workflow benchmarks")
    parser.add_argument("--skip-memory", action="store_true", help="Skip memory benchmarks")
    parser.add_argument("--skip-rag", action="store_true", help="Skip RAG benchmarks")
    args = parser.parse_args()

    benchmarks_dir = Path(__file__).parent
    results: dict[str, dict] = {}

    # Run benchmarks
    if not args.skip_workflow:
        script = benchmarks_dir / "workflow_benchmarks.py"
        results["workflow"] = run_benchmark(script)

    if not args.skip_memory:
        script = benchmarks_dir / "memory_benchmarks.py"
        results["memory"] = run_benchmark(script)

    if not args.skip_rag:
        script = benchmarks_dir / "rag_benchmarks.py"
        results["rag"] = run_benchmark(script)

    # Generate combined results file
    combined_path = args.output_dir / "benchmark_results.json"
    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "results": {k: v for k, v in results.items() if v is not None},
            },
            f,
            indent=2,
        )
    print(f"\nCombined results saved to {combined_path}")

    # Generate PERFORMANCE.md
    perf_doc = generate_performance_doc(results)
    perf_path = benchmarks_dir.parent / "docs" / "PERFORMANCE.md"
    perf_path.parent.mkdir(parents=True, exist_ok=True)
    with open(perf_path, "w", encoding="utf-8") as f:
        f.write(perf_doc)
    print(f"Performance documentation written to {perf_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)

    for bench_type, data in results.items():
        if data:
            print(f"\n{bench_type.upper()}:")
            print("-" * 40)
            for r in data.get("results", []):
                print(f"  {r['name']}: {r['mean_time_ms']:.4f}ms (P99: {r['p99_time_ms']:.4f}ms)")


if __name__ == "__main__":
    main()
