"""Workflow Performance Benchmarks.

Benchmarks for core workflow execution performance.
"""

import json
import statistics

# Add parent to path
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from beagle.preflight import PreFlightEstimator
from beagle.tracking import TrackingDatabase


@dataclass
class BenchmarkResult:
    """Result of a benchmark run."""

    name: str
    iterations: int
    mean_time_ms: float
    median_time_ms: float
    p99_time_ms: float
    min_time_ms: float
    max_time_ms: float
    ops_per_second: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "iterations": self.iterations,
            "mean_time_ms": round(self.mean_time_ms, 2),
            "median_time_ms": round(self.median_time_ms, 2),
            "p99_time_ms": round(self.p99_time_ms, 2),
            "min_time_ms": round(self.min_time_ms, 2),
            "max_time_ms": round(self.max_time_ms, 2),
            "ops_per_second": round(self.ops_per_second, 2),
        }


def measure(func, *args, iterations: int = 100, **kwargs) -> BenchmarkResult:
    """Measure execution time of a function."""
    times = []

    for _ in range(iterations):
        start = time.perf_counter()
        func(*args, **kwargs)
        elapsed = (time.perf_counter() - start) * 1000  # Convert to ms
        times.append(elapsed)

    return BenchmarkResult(
        name=func.__name__,
        iterations=iterations,
        mean_time_ms=statistics.mean(times),
        median_time_ms=statistics.median(times),
        p99_time_ms=sorted(times)[int(len(times) * 0.99)],
        min_time_ms=min(times),
        max_time_ms=max(times),
        ops_per_second=1000 / statistics.mean(times),
    )


async def measure_async(func, *args, iterations: int = 100, **kwargs) -> BenchmarkResult:
    """Measure execution time of an async function."""
    times = []

    for _ in range(iterations):
        start = time.perf_counter()
        await func(*args, **kwargs)
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    return BenchmarkResult(
        name=func.__name__,
        iterations=iterations,
        mean_time_ms=statistics.mean(times),
        median_time_ms=statistics.median(times),
        p99_time_ms=sorted(times)[int(len(times) * 0.99)],
        min_time_ms=min(times),
        max_time_ms=max(times),
        ops_per_second=1000 / statistics.mean(times),
    )


class WorkflowBenchmarks:
    """Benchmark suite for workflow operations."""

    def __init__(self):
        self.results: list[BenchmarkResult] = []

    def benchmark_preflight_estimator(self) -> BenchmarkResult:
        """Benchmark preflight cost estimation."""
        estimator = PreFlightEstimator(budget_usd=25.0)

        # Mock DAG nodes
        nodes = [
            {"name": "planner", "skill": "develop_planner", "model": "glm-5.1:cloud"},
            {"name": "researcher", "skill": "research", "model": "glm-5.1:cloud"},
            {"name": "implementer", "skill": "develop_implementer", "model": "glm-5.1:cloud"},
            {"name": "tester", "skill": "develop_tester", "model": "glm-5.1:cloud"},
        ]

        def estimate():
            estimator.estimate(workflow_name="develop", dag_nodes=nodes)

        result = measure(estimate, iterations=1000)
        self.results.append(result)
        return result

    def benchmark_tracking_database(self) -> BenchmarkResult:
        """Benchmark tracking database operations."""
        import tempfile
        import time as time_module

        from beagle.tracking.models import WorkflowRun

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrackingDatabase(Path(tmpdir) / "bench.db")

            def db_write():
                run = WorkflowRun(
                    id=f"bench-{time_module.time()}",
                    workflow_name="develop",
                    query="Benchmark test",
                    mode="read-write",
                    started_at=time_module.time(),
                )
                db.insert_workflow_run(run)
                run.success = True
                run.completed_at = time_module.time()
                db.update_workflow_run(run)

            result = measure(db_write, iterations=500)
            self.results.append(result)
            return result

    def benchmark_tracking_read(self) -> BenchmarkResult:
        """Benchmark tracking database read operations."""
        import tempfile
        import time as time_module

        from beagle.tracking.models import WorkflowRun

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrackingDatabase(Path(tmpdir) / "bench.db")

            # Pre-populate with runs
            for i in range(100):
                run = WorkflowRun(
                    id=f"bench-{i}",
                    workflow_name="develop",
                    query=f"Benchmark test {i}",
                    mode="read-write",
                    started_at=time_module.time(),
                )
                db.insert_workflow_run(run)
                run.success = True
                run.completed_at = time_module.time()
                db.update_workflow_run(run)

            def db_read():
                db.get_workflow_runs(limit=10)

            result = measure(db_read, iterations=500)
            self.results.append(result)
            return result

    def benchmark_node_run_creation(self) -> BenchmarkResult:
        """Benchmark NodeRun model creation."""
        import time as time_module
        import uuid

        from beagle.tracking.models import NodeRun

        def create_node():
            NodeRun(
                id=str(uuid.uuid4()),
                workflow_run_id="bench-run",
                node_name="planner",
                skill_name="develop_planner",
                model="glm-5.1:cloud",
                started_at=time_module.time(),
            )

        result = measure(create_node, iterations=10000)
        self.results.append(result)
        return result

    def benchmark_workflow_run_creation(self) -> BenchmarkResult:
        """Benchmark WorkflowRun model creation."""
        import time as time_module
        import uuid

        from beagle.tracking.models import WorkflowRun

        def create_run():
            WorkflowRun(
                id=str(uuid.uuid4()),
                workflow_name="develop",
                query="Benchmark test workflow",
                mode="read-write",
                started_at=time_module.time(),
            )

        result = measure(create_run, iterations=10000)
        self.results.append(result)
        return result

    def run_all(self) -> dict[str, Any]:
        """Run all workflow benchmarks."""
        print("Running workflow benchmarks...")

        print("  1. Preflight Estimator...")
        self.benchmark_preflight_estimator()

        print("  2. Tracking Database Write...")
        self.benchmark_tracking_database()

        print("  3. Tracking Database Read...")
        self.benchmark_tracking_read()

        print("  4. NodeRun Creation...")
        self.benchmark_node_run_creation()

        print("  5. WorkflowRun Creation...")
        self.benchmark_workflow_run_creation()

        return {
            "benchmark_type": "workflow",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": [r.to_dict() for r in self.results],
        }


def main():
    """Run workflow benchmarks and save results."""
    benchmarks = WorkflowBenchmarks()
    results = benchmarks.run_all()

    # Save results
    output_path = Path(__file__).parent / "workflow_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to {output_path}")
    print("\nBenchmark Results:")
    print("-" * 80)
    print(f"{'Benchmark':<40} {'Mean (ms)':<12} {'P99 (ms)':<12} {'Ops/s':<12}")
    print("-" * 80)
    for r in benchmarks.results:
        print(
            f"{r.name:<40} {r.mean_time_ms:<12.4f} {r.p99_time_ms:<12.4f} {r.ops_per_second:<12.1f}"
        )
    print("-" * 80)


if __name__ == "__main__":
    main()
