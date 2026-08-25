# Troubleshooting

Common issues, their causes, and how to fix them.

## Installation

### `pip install -e .` fails with "Microsoft Visual C++ 14.0 is required"

You're on Windows. Beagle's `sentence-transformers` dependency
needs a C++ compiler to build `numpy`-derived wheels.

**Fix:** Install the prebuilt wheel instead:

```bash
pip install --only-binary=numpy,sentence-transformers -e .
```

Or use `uv pip install` which prefers prebuilt wheels.

### `ModuleNotFoundError: No module named 're2'`

`google-re2` is a **mandatory** dependency. Beagle fails closed
without it (no secret scrubbing → no secret loads).

**Fix:**

```bash
pip install google-re2
```

If `pip install google-re2` fails, you're probably on a Python
version newer than `re2`'s published wheels support. Check
[the re2 PyPI page](https://pypi.org/project/google-re2/) for
supported Python versions; Beagle requires Python 3.12+.

## RAG / LanceDB / Kùzu

### `rag_search` returns `{"status": "error", "message": "Missing dependencies: lancedb, kuzu"}`

Either the optional dependencies aren't installed, or the
imports are failing. Check:

```bash
python3 -c "import lancedb; print(lancedb.__version__)"
python3 -c "import kuzu; print(kuzu.__version__)"
```

If either fails, install via:

```bash
pip install -e ".[dev]"  # dev extras include lancedb & kuzu
```

### `rag_search` returns an empty result with no error

Check that the index is populated:

```bash
ls -la knowledge_graph/  # should contain Kùzu files
ls -la .lancedb/         # should contain LanceDB files
```

If empty, re-run the ingestion:

```bash
beagle run cast_ingest  # uses the CAST pipeline
```

### `kuzu: Cannot find library: kuzu_shared`

On some Linux distros, Kùzu's bundled `.so` file is not
extracted properly. Try:

```bash
pip install --force-reinstall kuzu
```

## Orchestrator

### `beagle run` hangs at "Restored from previous checkpoint"

The checkpoint restore is waiting on a lock. Either:

- A previous run is still running (check `ps aux | grep beagle`)
- A previous run crashed leaving a stale lock. Clear it:

  ```bash
  rm -f <config_root>/locks/*.lock
  ```

### `Workflow ID: ... Budget: $X.XX` log line is missing

The orchestrator is failing before it can log the workflow ID.
This usually means:

- The config file is invalid → run `beagle config validate`
- A mandatory directory is missing → run `beagle health`
- A secrets file is missing → check the configured secrets location

## MCP servers

### `Connection refused` when an MCP client tries to connect

The MCP server hasn't been started. Use the dockerised launcher:

```bash
docker compose up mcp_rag_server
```

Or start it directly:

```bash
python3 -m beagle.infrastructure.mcp_rag_server
```

### `additionalProperties: false` missing from a tool's schema

You're running a tool that was added without the schema hardener.
Check that `hardening/mcp_schema_hardener.harden_mcp_tool_schemas`
is called after all `@mcp.tool()` registrations.

## Performance

### Workflow runs slowly

Check the cost-tracker output. Common causes:

- High `max_hops` on `rag_search` (1 is fastest, 3 is most thorough)
- Long prompts (the `compress_context` step is the slowest)
- Model resolution picking an expensive model — see
  `beagle config show` for the active model

### Memory usage grows during long workflows

The KV cache pool is unbounded. If you're running multi-hour
workflows, periodically call `clear_kv_pool()` from the
`/health` endpoint or restart the daemon.

## Logs

### Logs are too noisy

Set `BEAGLE_LOG_LEVEL=WARNING` to suppress INFO logs.

To suppress a specific module:

```python
import logging
logging.getLogger("Beagle.observability.metrics").setLevel(logging.ERROR)
```

### Logs are missing timestamps

The default formatter includes millisecond timestamps. If
they're missing, you may have configured a custom
`logging.Formatter` that's stripping them. See
`beagle/observability/logging.py` for the
canonical setup.

## Still stuck?

1. Run `beagle doctor --json` and check the output.
2. Search the [docs/](../) directory.
3. Check the [CHANGELOG](../../CHANGELOG.md) — your issue may
   already be fixed in a newer release.
4. Open a GitHub issue with the output of `beagle doctor --json`
   and the relevant log lines.
