# Beagle

**A domain-agnostic autonomous agentic workflow system.** Multi-agent
orchestration, hybrid vector+graph RAG, and MCP integration — designed to run
any structured multi-phase work (research, analysis, operations, code, writing,
planning) rather than being tied to a single domain.

Production software. Proprietary — free for personal, non-commercial use;
a paid licence is required for commercial use (including internal company
use). See [LICENSE](LICENSE).

---

## Highlights

- **Multi-agent workflows** — YAML/TOML-defined phase DAGs executed by a
  LangGraph-backed orchestrator with budgets, caching, circuit breakers, and
  per-node timeouts.
- **Hybrid RAG** — LanceDB vector retrieval + Kùzu graph traversal over your
  own corpora (any text/code corpus; nothing domain-specific is baked in).
- **MCP-native** — exposes its capabilities as MCP tools so any MCP client can
  drive it; plugin architecture for additional MCP servers.
- **Provider-neutral LLM access** — works with most any OpenAI-compatible API.
  No provider presets ship with the product; you choose the endpoint.
- **Swappable connection transports** — plain HTTP by default; hot-swappable
  to alternative backends that implement one common protocol.

## Install

```bash
pip install beagle            # or: uv pip install dist/beagle-*.whl
beagle config init            # seed ~/.config/beagle from code defaults
```

Beagle runs fully configless — every setting has a correct in-code default.
All user-editable configuration lives in ONE place:

```text
~/.config/beagle/              # the ONLY config root (XDG)
├── beagle_core_config/config.toml   # core behaviour/setup SSOT
├── coding_agent_config/             # agents.toml, recipes/, workflows/, metaprompts/
├── beagle_inference_config/         # providers.toml + model fleet cards
├── style_guides/guides/*.toml       # optional doctrine files
├── plugins/<name>/                  # per-plugin config
├── deployments/                     # per-deployment overrides
└── ...
```

The distribution ships **no bundled configuration**: `beagle config init`
writes starter files there from programmatic defaults, and you edit them.

## Provider-neutral LLM configuration

Beagle speaks the OpenAI-compatible chat/completions surface. Point it at
your provider of choice in `~/.config/beagle/beagle_core_config/config.toml`:

```toml
[ollama_cloud]           # historical section name — ANY compatible endpoint works
endpoint = "https://ollama.com"        # or vLLM, LiteLLM, OpenAI, llama.cpp, ...
api_key  = "${MY_PROVIDER_API_KEY}"    # ${ENV} expansion supported

[goose]
default_model = "your-model-name"      # no model presets ship — set your own
provider      = ""                     # e.g. ollama_cloud | openai | vllm | litellm

[model_presets]                        # optional named slots used by workflows
default = "your-primary-model"
coding  = "your-code-model"
```

Preset *categories* exist (default/coding/orchestration/deep_analysis/writing)
so workflows can request a capability rather than a product name — the actual
models behind each slot are entirely yours.

## Connection transports: HTTP default, hot-swappable

Every outbound connection goes through one seam
(`beagle.core.transports`). The built-in transport is plain HTTP:

```bash
BEAGLE_TRANSPORT=... or [connections] transport = "..." in ~/.config/beagle
```

- Selection order: `$BEAGLE_TRANSPORT` env → `[connections].transport` in
  `~/.config/beagle` → `"http"` (built-in default).
- Alternative transports are **auto-detected but never auto-activated**:
  installing a transport wheel lists it in diagnostics; using it requires the
  explicit config step above (an informed decision). Hot-swap at runtime via
  `beagle.core.transports.activate_transport(name)` — in-flight clients finish
  on their old transport, new clients pick up the new one.

### The optional proprietary `beagle-orpheus` wheel

For deployments that want native high-throughput IPC, a separately licensed
wheel (`beagle-orpheus`) provides a FlatBuffers-framed transport over shared-
memory ring buffers (`/run/orpheus_ring`, overridable via `$ORPHEUS_RING_DIR`),
compiled to C for speed:

- **Not included by default.** beagle never requires it, bundles it, or
  activates it implicitly.
- Free for evaluation / limited single use; paid license required for
  production/business use (see the wheel's LICENSE).
- Download the wheel alongside the beagle wheel in [`dist/`](dist/), then:

```bash
pip install ./beagle_orpheus-<ver>-<platform>.whl   # registers beagle.transports
# detected automatically; activate explicitly:
#   export BEAGLE_TRANSPORT=orpheus   OR   [connections] transport = "orpheus"
```

## MCP servers & plugins

ONE stdio surface, registered in goose as `Beagle`:

```bash
/opt/beagle/beagle_venv/bin/python3 -m beagle.infrastructure.mcp_beagle_server
```

The unified server (`src/beagle/infrastructure/mcp_beagle_server.py`) absorbs
the RAG group (`rag_*`, `graph_*`, renamed `rag_health_check` / `rag_get_metrics`)
and the utility group (workflow orchestration, research/code/web tools,
meta-process tuning) plus two management tools: `plugin_status` and
`plugin_reload`.

### [tool] standard — TOML-configured plugins with hotswap

External MCP plugins (OpenClaw and future ones) are declared in the registry
TOML at `<config_root>/plugins/tools.toml` — never hardcoded. Each `[[tool]]`
entry names an entry point (group `beagle.mcp_plugins`) or an importable
module exposing `mcp: FastMCP`; an optional `is_enabled()` self-gate inside
the plugin is ANDed with the registry `enabled` flag. Editing the TOML plus
one `plugin_reload()` call mounts or unmounts a plugin's commands at runtime —
no restart (`[meta].hotswap_enabled = true`). Detection without declaration is
reported by `plugin_status()` and never activates anything.

The OpenClaw task-queue controller ships as its own plugin repo (own root
under Skylon_Ecosystem); once installed into the Beagle venv and listed in
`tools.toml`, its commands are included automatically.

## Development

```bash
uv sync --extra dev
pytest tests/ -x          # targeted selections recommended first
ruff check src tests && ruff format --check src tests
mypy src
```

## License

Beagle is proprietary software, copyright Matthew David Calder Creigh.
See [LICENSE](LICENSE) for the full terms. In short:

1. **Personal, non-commercial use:** free — individuals, personal
   projects, learning and research, and non-profit organizations.
2. **Evaluation:** any entity may evaluate internally for 30 days.
3. **Commercial use:** requires a paid licence under a separate written
   agreement — this includes internal company use, hosted/managed
   services, client work, and bundling into paid products.
4. Contributions are welcomed under the grant in LICENSE §5.
5. The optional `beagle-orpheus` wheel is separately licensed proprietary
   software (evaluation free; production paid).

Commercial licences: <matthewcreigh99@gmail.com>.
