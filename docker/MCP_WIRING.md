# Dev-Stack MCP Wiring

How `code-server`, `open-webui`, `goose`, `beagle` and `openclaw` connect,
with **ollama** as the embedding backend and **beagle as the primary agentic
inference plane**.

```
                     ┌─────────────────────────── compose network ───────────────────────────┐
                     │                                                                        │
  code-server ───────┤──► openclaw :8791/mcp        todos · metaevents · task queue           │
  (.vscode/mcp.json) ├──► beagle-rag :8421/mcp     hybrid RAG search   [Bearer BEAGLE_MCP_TOKEN]
                     │                                                                        │
  open-webui ────────┤──► openclaw / beagle-rag     (MCP-aware builds; else mcpo bridge)      │
                     ├──► ollama :11434/v1          base chat LLM (OpenAI-compatible)         │
                     │                                                                        │
  goose ─────────────┤──► openclaw / beagle-rag     same MCP surfaces, bearer auth            │
                     │                                                                        │
  beagle ────────────┤──► ollama :11434              embeddings (OLLAMA_EMBED_MODEL)           │
   orchestrator      └── A2A :8420 (ed25519-signed)  internal sub-agent plane                  │
```

## Ports & trust (all internal-network only)

| Surface | Port | Auth |
|---|---|---|
| `beagle-rag` MCP | 8421 | **mandatory bearer** (`BEAGLE_MCP_TOKEN`, fail-closed) |
| `openclaw` MCP | 8791 | network isolation only until plugin ships native HTTP auth |
| ollama API | 11434 | internal |
| beagle A2A | 8420 | ed25519-signed, orchestration plane |

Generate the token once per stack: `openssl rand -hex 32 > .env` entry
`BEAGLE_MCP_TOKEN=…`. Per `MCP_TRUST.md`, HTTP MCP always requires auth —
which is exactly what the rag/utility servers enforce and why `openclaw`
must stay off the host network until its gateway gains a token gate.

## Consumers

### open-webui
* **Tools/MCP:** Settings → External Tools → add
  `http://openclaw:8791/mcp` and `http://beagle-rag:8421/mcp`
  (Bearer token header). Requires an MCP-capable open-webui release;
  older builds use the `mcpo` bridge pattern instead.
* **Chat models:** Add Connection → `http://ollama:11434` (OpenAI-compatible
  `/v1`). Base chat hits ollama directly; anything agentic (workflows,
  RAG-grounded answers, task creation) routes through beagle/openclaw tools —
  beagle stays the primary inference plane, ollama is embeddings + raw chat.

### code-server
Mount [`examples/mcp.json`](examples/mcp.json) as `.vscode/mcp.json` in the
workspace volume. Both servers appear in agent mode; creating a todo or
metaevent is one tool call away.

### goose
Merge [`examples/goose-extensions.yaml`](examples/goose-extensions.yaml)
into `~/.config/goose/config.yaml` inside whatever context runs goose
(beagle container or host), then `goose mcp list` / a session smoke-test.

### Todos & metaevents — one surface, three entry points
open-webui, code-server and goose all reach the SAME openclaw tool set
(task/todo/metaevent creation backed by the Orpheus ring queue). There is
no per-consumer configuration beyond pointing at `http://openclaw:8791/mcp`.

## Embedding backend swap (ollama → llama.cpp)

Beagle's embedder speaks **Ollama's native `/api/*` protocol**
(`OLLAMA_BASE_URL`, default model `nomic-embed-code`). `llama-server`
exposes OpenAI-style `/v1/embeddings`, so a swap needs either:

1. an API-translating sidecar (ollama-shim), or
2. native OpenAI-provider support in the embedder config.

Until one of those lands, llama.cpp is documented as an *alternative*, not a
drop-in: keep `ollama` for embeddings, run llama.cpp alongside for chat
models if desired (open-webui can talk to it via `/v1`).

## Build & operate

```bash
make build                                  # beagle wheel → dist/
cp dist/beagle-*.whl …                      # stack host inputs
# plugin wheel (from the beagle-openclaw repo):
uv build --out-dir wheels/                  # → wheels/beagle_openclaw-*.whl

scripts/integrate_dev_stack.sh -d /path/to/dev_stack   # copies fragment+inputs
cd /path/to/dev_stack && docker compose up -d --build

docker exec ollama ollama pull nomic-embed-code        # embedding model
```

`OPENCLAW_IMAGE` may point at a pre-built registry image to skip the build.
