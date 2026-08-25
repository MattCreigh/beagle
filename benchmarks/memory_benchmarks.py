"""Memory Hierarchy Benchmarks.

Benchmarks for L0-L3 memory operations.
"""

import json
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from beagle.memory import MemoryLevel
from beagle.memory.hierarchical_memory import MemoryEntry


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


class MemoryBenchmarks:
    """Benchmark suite for memory operations."""

    def __init__(self):
        self.results: list[BenchmarkResult] = []

    def benchmark_l0_context_add(self) -> BenchmarkResult:
        """Benchmark adding messages to working context."""
        # Simulate working context with simple list
        context = []
        context_tokens = 0

        def add_message():
            nonlocal context_tokens
            msg = {
                "role": "user",
                "content": "This is a test message for benchmarking",
                "tokens": 10,
            }
            context.append(msg)
            context_tokens += msg["tokens"]
            return context

        result = measure(add_message, iterations=10000)
        self.results.append(result)
        return result

    def benchmark_l0_context_get(self) -> BenchmarkResult:
        """Benchmark retrieving context."""
        # Pre-populate context
        context = []
        for i in range(100):
            context.append(
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"Message {i}" * 10,
                    "tokens": 100,
                }
            )

        def get_context():
            return context.copy()

        result = measure(get_context, iterations=5000)
        self.results.append(result)
        return result

    def benchmark_l1_episode_store(self) -> BenchmarkResult:
        """Benchmark storing episodes to SQLite."""
        # Mock session memory - we'll just benchmark SQLite insert
        import sqlite3
        import tempfile
        import uuid

        with tempfile.TemporaryDirectory() as tmpdir:
            conn = sqlite3.connect(str(Path(tmpdir) / "bench.db"))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    summary TEXT,
                    started_at REAL
                )
            """)
            conn.commit()

            def store_episode():
                conn.execute(
                    "INSERT INTO episodes VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), "bench-session", "Benchmark episode", time.time()),
                )
                conn.commit()

            result = measure(store_episode, iterations=500)
            conn.close()
            self.results.append(result)
            return result

    def benchmark_l1_episode_retrieve(self) -> BenchmarkResult:
        """Benchmark retrieving episodes from SQLite."""
        import sqlite3
        import tempfile
        import uuid

        with tempfile.TemporaryDirectory() as tmpdir:
            conn = sqlite3.connect(str(Path(tmpdir) / "bench.db"))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    summary TEXT,
                    started_at REAL
                )
            """)

            # Pre-populate
            for i in range(100):
                conn.execute(
                    "INSERT INTO episodes VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), "bench-session", f"Episode {i}", time.time()),
                )
            conn.commit()

            def get_episodes():
                cursor = conn.execute(
                    "SELECT * FROM episodes WHERE session_id = ?", ("bench-session",)
                )
                return list(cursor.fetchall())

            result = measure(get_episodes, iterations=1000)
            conn.close()
            self.results.append(result)
            return result

    def benchmark_l1_episode_search(self) -> BenchmarkResult:
        """Benchmark searching episodes."""
        import sqlite3
        import tempfile
        import uuid

        with tempfile.TemporaryDirectory() as tmpdir:
            conn = sqlite3.connect(str(Path(tmpdir) / "bench.db"))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    summary TEXT,
                    started_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_summary ON episodes(summary)")

            # Pre-populate with searchable content
            for i in range(500):
                conn.execute(
                    "INSERT INTO episodes VALUES (?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        f"session-{i % 10}",
                        f"Episode about authentication and security topic {i}",
                        time.time(),
                    ),
                )
            conn.commit()

            def search():
                cursor = conn.execute(
                    "SELECT * FROM episodes WHERE summary LIKE ? LIMIT 10", ("%authentication%",)
                )
                return list(cursor.fetchall())

            result = measure(search, iterations=100)
            conn.close()
            self.results.append(result)
            return result

    def benchmark_memory_entry_creation(self) -> BenchmarkResult:
        """Benchmark MemoryEntry model creation."""
        import uuid

        def create_entry():
            MemoryEntry(
                id=str(uuid.uuid4()),
                level=MemoryLevel.EPISODIC,
                content="Test memory entry",
                metadata={"source": "benchmark"},
            )

        result = measure(create_entry, iterations=10000)
        self.results.append(result)
        return result

    def benchmark_session_episode_creation(self) -> BenchmarkResult:
        """Benchmark SessionEpisode model creation."""
        # Simple mock episode creation benchmark
        import uuid

        def create_episode():
            return {
                "id": str(uuid.uuid4()),
                "session_id": "bench-session",
                "messages": [],
                "summary": "Test episode",
                "started_at": time.time(),
            }

        result = measure(create_episode, iterations=10000)
        self.results.append(result)
        return result

    def benchmark_sqlite_write(self) -> BenchmarkResult:
        """Benchmark raw SQLite write operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bench.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    data TEXT,
                    timestamp REAL
                )
            """)
            conn.commit()

            def write_entry():
                conn.execute(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    (f"id-{time.time()}", "test data", time.time()),
                )
                conn.commit()

            result = measure(write_entry, iterations=1000)
            conn.close()
            self.results.append(result)
            return result

    def benchmark_sqlite_read(self) -> BenchmarkResult:
        """Benchmark raw SQLite read operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bench.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    data TEXT,
                    timestamp REAL
                )
            """)

            # Pre-populate
            for i in range(1000):
                conn.execute(
                    "INSERT INTO entries VALUES (?, ?, ?)", (f"id-{i}", f"data-{i}", time.time())
                )
            conn.commit()

            def read_entries():
                cursor = conn.execute("SELECT * FROM entries LIMIT 100")
                return list(cursor.fetchall())

            result = measure(read_entries, iterations=1000)
            conn.close()
            self.results.append(result)
            return result

    def run_all(self) -> dict[str, Any]:
        """Run all memory benchmarks."""
        print("Running memory benchmarks...")

        print("  1. L0 Context Add...")
        self.benchmark_l0_context_add()

        print("  2. L0 Context Get...")
        self.benchmark_l0_context_get()

        print("  3. L1 Episode Store...")
        self.benchmark_l1_episode_store()

        print("  4. L1 Episode Retrieve...")
        self.benchmark_l1_episode_retrieve()

        print("  5. L1 Episode Search...")
        self.benchmark_l1_episode_search()

        print("  6. Memory Entry Creation...")
        self.benchmark_memory_entry_creation()

        print("  7. Session Episode Creation...")
        self.benchmark_session_episode_creation()

        print("  8. SQLite Write...")
        self.benchmark_sqlite_write()

        print("  9. SQLite Read...")
        self.benchmark_sqlite_read()

        return {
            "benchmark_type": "memory",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": [r.to_dict() for r in self.results],
        }


def main():
    """Run memory benchmarks and save results."""
    benchmarks = MemoryBenchmarks()
    results = benchmarks.run_all()

    # Save results
    output_path = Path(__file__).parent / "memory_results.json"
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
