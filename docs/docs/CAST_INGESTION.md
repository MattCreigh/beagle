# CAST Ingestion Pipeline

> **CAST = Code AST.** The pipeline parses a codebase with
> tree-sitter, chunks it, embeds the chunks, and builds a
> hybrid vector + graph index for the RAG subsystem.

This document is the operational guide for the ingestion
pipeline: how to run it, how it works, how to hot-swap data
without taking the MCP server offline.

---

## When to re-ingest

Re-ingest a codebase when:

- The code has changed substantially (more than 10% of files)
- The graph is incomplete (missing call edges)
- You're switching to a different branch
- You want to re-baseline the cost tracker

You do **not** need to re-ingest for:

- Single-file edits (the RAG layer reads from the on-disk
    files at query time, only the vector/graph indexes are
    cached)
- README changes
- Adding new test cases

## How to ingest

### Cold ingest (recommended for first-time setup)

```bash
# Parse + chunk + embed + build graph (full pipeline)
beagle run cast_ingest
```

This runs the full pipeline:

  1. AST parse every Python file in the workspace
  2. Chunk the AST into embeddable units (functions, classes, blocks)
  3. Embed each chunk via the configured embedding model → vector store
  4. Build the call-graph edges → Kùzu
  5. Compute and persist file hashes (for incremental re-ingest)

Duration: ~1-2 minutes per 1,000 LoC on a CPU-only machine.

### Hot-swap ingest (no downtime)

```bash
# Stage the new data, swap atomically, re-init the RAG server
python3 -m beagle.infrastructure.hotswap_ingest \
    /path/to/new/codebase
```

The hot-swap path:

  1. Stages the new LanceDB and Kùzu databases to a temporary
     directory
  2. Releases the RAG server's database connections
  3. Atomically renames the staged directory into place
  4. Triggers a re-initialization in the running RAG server

This is safe to run while queries are in flight — the old
connections are drained before the swap.

### Incremental ingest (file-level)

```bash
# Re-ingest only files changed since the last full ingest
beagle run cast_ingest --incremental
```

Uses the file-hash index from the last full ingest to skip
unchanged files.

## What gets stored

After a full ingest, you'll have:

```text
.lancedb/                    # vector store
  └─ ast_code_chunks/         # one table per tenant
       ├─ data.lance
       └─ ...

knowledge_graph/             # graph store
  ├─ ast_nodes.kuzu
  └─ ast_edges.kuzu

<config_root>/index_state.json  # bookkeeping: file hashes, last ingest time
```

Total disk usage: ~3-5 MB per 1,000 LoC (dominated by the
embedding vectors; the graph is small).

## Verifying the ingest

```bash
# Quick health check
beagle health

# Detailed: query the RAG layer
beagle run research "What is the largest function in src/?" --headless

# Inspect the graph directly
python3 -c "
import kuzu
db = kuzu.Database('knowledge_graph')
conn = kuzu.Connection(db)
result = conn.execute('MATCH (n:Function) RETURN n.name, n.file LIMIT 10')
while result.has_next():
    print(result.get_next())
"
```

## Rollback

If an ingest goes wrong, you can roll back to the previous
LanceDB + Kùzu state:

```bash
# Automatic: a full ingest keeps the previous data in
# `.lancedb.bak` and `knowledge_graph.bak`
mv .lancedb .lancedb.broken
mv .lancedb.bak .lancedb
mv knowledge_graph knowledge_graph.broken
mv knowledge_graph.bak knowledge_graph
```

Then restart the MCP server (or use the hot-swap path's
rollback). The `beagle-sbom.yml` workflow attaches the index
manifest to releases, so you can also rollback to a specific
release's index.

## Concurrency

The ingest pipeline is single-writer. If you need to ingest
multiple codebases in parallel, run them in separate workspaces
(separate `.lancedb/` and `knowledge_graph/` directories) and
configure the MCP server with the path.

## Common failure modes

### "Out of memory" during embedding

The embedder is RAM-hungry. For a 100K-LoC codebase, you need
~4 GiB free. Reduce memory pressure with:

```bash
export OMP_NUM_THREADS=2
export TOKENIZERS_PARALLELISM=false
```

### "Kuzu: IO error: Could not set up database"

The Kùzu database directory is corrupted (usually from a
SIGKILL during a write). Fix: delete `knowledge_graph/` and
re-ingest from scratch.

### "LanceDB: version mismatch"

The on-disk LanceDB format is from a newer LanceDB than Beagle
was built against. Pin the version in `pyproject.toml` or
re-ingest from scratch.
