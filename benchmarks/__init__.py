"""Beagle Benchmark Suite.

This package contains performance benchmarks for Beagle components:

- workflow_benchmarks: Workflow execution, tracking, estimation
- memory_benchmarks: L0-L3 memory operations
- rag_benchmarks: RAG search, embedding, chunking

Run all benchmarks:
    python -m benchmarks.run_benchmarks

Run individual benchmark:
    python -m benchmarks.workflow_benchmarks
    python -m benchmarks.memory_benchmarks
    python -m benchmarks.rag_benchmarks
"""

__all__ = [
    "memory_benchmarks",
    "rag_benchmarks",
    "run_benchmarks",
    "workflow_benchmarks",
]
