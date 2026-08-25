"""RAG Performance Benchmarks.

Benchmarks for Retrieval-Augmented Generation operations.
"""

import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))


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


class RAGBenchmarks:
    """Benchmark suite for RAG operations."""

    def __init__(self):
        self.results: list[BenchmarkResult] = []

    def benchmark_embedding_creation(self) -> BenchmarkResult:
        """Benchmark embedding vector creation (mock)."""
        import numpy as np

        def create_embedding():
            # Simulate embedding creation (768 dimensions)
            return np.random.randn(768).tolist()

        result = measure(create_embedding, iterations=1000)
        self.results.append(result)
        return result

    def benchmark_cosine_similarity(self) -> BenchmarkResult:
        """Benchmark cosine similarity calculation."""
        import numpy as np

        # Pre-compute embeddings
        embedding1 = np.random.randn(768)
        embedding2 = np.random.randn(768)

        def cosine_sim():
            dot_product = np.dot(embedding1, embedding2)
            norm1 = np.linalg.norm(embedding1)
            norm2 = np.linalg.norm(embedding2)
            return dot_product / (norm1 * norm2)

        result = measure(cosine_sim, iterations=10000)
        self.results.append(result)
        return result

    def benchmark_batch_cosine_similarity(self) -> BenchmarkResult:
        """Benchmark batch cosine similarity."""
        import numpy as np

        # Pre-compute embeddings (query + 100 candidates)
        query = np.random.randn(768)
        candidates = np.random.randn(100, 768)

        def batch_sim():
            # Compute all similarities at once
            dots = np.dot(candidates, query)
            norms = np.linalg.norm(candidates, axis=1) * np.linalg.norm(query)
            return dots / norms

        result = measure(batch_sim, iterations=1000)
        self.results.append(result)
        return result

    def benchmark_chunk_creation(self) -> BenchmarkResult:
        """Benchmark text chunking."""
        # Simple text chunking simulation
        sample_code = (
            '''
def authenticate(user, password):
    """Authenticate a user."""
    if not user or not password:
        return None
    
    stored_hash = get_stored_hash(user)
    if stored_hash is None:
        return None
    
    if verify_password(password, stored_hash):
        return create_session(user)
    return None

def verify_password(password, stored_hash):
    """Verify password against stored hash."""
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    except Exception:
        return False
'''
            * 10
        )

        def chunk():
            # Simple mock chunking
            lines = sample_code.split("\n")
            chunks = []
            current_chunk = []
            current_size = 0
            max_chunk_size = 500

            for line in lines:
                current_size += len(line)
                current_chunk.append(line)
                if current_size >= max_chunk_size:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                    current_size = 0
            if current_chunk:
                chunks.append("\n".join(current_chunk))
            return chunks

        result = measure(chunk, iterations=100)
        self.results.append(result)
        return result

    def benchmark_ast_parsing(self) -> BenchmarkResult:
        """Benchmark AST parsing."""
        import ast

        sample_code = (
            """
class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
    
    def get_full_name(self):
        return f"{self.name} <{self.email}>"

class Authenticator:
    def __init__(self, db):
        self.db = db
    
    def authenticate(self, user, password):
        stored_hash = self.db.get_hash(user)
        if stored_hash:
            return verify(password, stored_hash)
        return False
    
    def create_session(self, user):
        return Session(user, expires_in=3600)
"""
            * 5
        )

        def parse():
            return ast.parse(sample_code)

        result = measure(parse, iterations=500)
        self.results.append(result)
        return result

    def benchmark_kuzu_query(self) -> BenchmarkResult:
        """Benchmark Kùzu graph query (mock)."""

        # Mock Cypher query construction
        def build_query():
            query = """
            MATCH (c:Code)-[r:DependsOn]->(other:Code)
            WHERE c.file_path = $file_path
            RETURN c, r, other
            LIMIT $limit
            """
            params = {"file_path": "auth.py", "limit": 10}
            return query, params

        result = measure(build_query, iterations=5000)
        self.results.append(result)
        return result

    def benchmark_traversal_depth(self) -> BenchmarkResult:
        """Benchmark graph traversal (mock)."""
        import random

        # Mock graph structure
        nodes = {
            f"node_{i}": {"children": [f"node_{j}" for j in random.sample(range(100), 5)]}
            for i in range(100)
        }

        def traverse(max_depth: int = 2):
            visited = set()
            current = "node_0"
            result = []

            for _ in range(max_depth):
                if current in visited:
                    break
                visited.add(current)
                result.append(current)
                children = nodes.get(current, {}).get("children", [])
                if children:
                    current = random.choice(children)

            return result

        result = measure(traverse, iterations=1000)
        self.results.append(result)
        return result

    def benchmark_hybrid_search_scoring(self) -> BenchmarkResult:
        """Benchmark hybrid search result scoring."""
        import random

        # Mock search results
        vector_results = [
            {"id": f"doc_{i}", "score": random.random(), "content": f"Content {i}"}
            for i in range(50)
        ]

        graph_results = [
            {"id": f"doc_{i}", "depth": random.randint(1, 3)} for i in random.sample(range(100), 30)
        ]

        def hybrid_score():
            # Combine vector and graph scores
            combined = {}

            # Add vector scores
            for r in vector_results:
                combined[r["id"]] = r["score"] * 0.7

            # Add graph scores (depth decreases score)
            for r in graph_results:
                if r["id"] in combined:
                    combined[r["id"]] += 0.3 / r["depth"]
                else:
                    combined[r["id"]] = 0.3 / r["depth"]

            # Sort by combined score
            return sorted(combined.items(), key=lambda x: x[1], reverse=True)

        result = measure(hybrid_score, iterations=1000)
        self.results.append(result)
        return result

    def benchmark_reranking(self) -> BenchmarkResult:
        """Benchmark result reranking."""
        import random

        # Mock initial results
        results = [
            {"id": i, "score": random.random(), "content": f"Document {i}" * 10} for i in range(100)
        ]

        def rerank():
            # Re-score based on content length and position
            for r in results:
                r["final_score"] = (
                    r["score"] * 0.5
                    + min(len(r["content"]) / 1000, 1) * 0.3
                    + (1 - r["id"] / 100) * 0.2
                )
            return sorted(results, key=lambda x: x["final_score"], reverse=True)

        result = measure(rerank, iterations=500)
        self.results.append(result)
        return result

    def run_all(self) -> dict[str, Any]:
        """Run all RAG benchmarks."""
        print("Running RAG benchmarks...")

        print("  1. Embedding Creation...")
        self.benchmark_embedding_creation()

        print("  2. Cosine Similarity...")
        self.benchmark_cosine_similarity()

        print("  3. Batch Cosine Similarity...")
        self.benchmark_batch_cosine_similarity()

        print("  4. Chunk Creation...")
        self.benchmark_chunk_creation()

        print("  5. AST Parsing...")
        self.benchmark_ast_parsing()

        print("  6. Kùzu Query Building...")
        self.benchmark_kuzu_query()

        print("  7. Graph Traversal...")
        self.benchmark_traversal_depth()

        print("  8. Hybrid Search Scoring...")
        self.benchmark_hybrid_search_scoring()

        print("  9. Result Reranking...")
        self.benchmark_reranking()

        return {
            "benchmark_type": "rag",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": [r.to_dict() for r in self.results],
        }


def main():
    """Run RAG benchmarks and save results."""
    benchmarks = RAGBenchmarks()
    results = benchmarks.run_all()

    # Save results
    output_path = Path(__file__).parent / "rag_results.json"
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
