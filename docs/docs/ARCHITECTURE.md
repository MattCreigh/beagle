# Beagle Architecture

## System Overview

Beagle is an enterprise multi-agent orchestration engine. It provides hierarchical
agent delegation, structural state sharing, semantic retrieval over local
codebases, real-time cost tracking, and enterprise-grade security through an
agent-to-agent protocol with cryptographic signatures and role-based access
control.

The runtime is built on a graph execution backend and is exposed to a host
orchestrator (such as an interactive AI harness) over the Model Context
Protocol (MCP).

---

## Architecture Layers

```text
┌──────────────────────────────────────┐
│         Host Orchestrator            │
│      User-facing session layer       │
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│      Task Multiplexer                │
│  Task routing / workflow selection   │
└──────────────────┬───────────────────┘
                   │ MCP Protocol
                   ▼
┌──────────────────────────────────────┐
│     Beagle Workflow Engine           │
│  Router · Steering · Deep Forks      │
│  A2A Protocol · MCP Servers · RAG    │
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│      Observability & Security        │
│  Distributed tracing · Semantic      │
│  firewall                            │
└──────────────────────────────────────┘
```

```text
Host Orchestrator ──▶ Task Multiplexer ──▶ Beagle Workflow Engine ──▶ Observability & Security
                        │  MCP protocol    │
                        └──────────────────┘
```

---

## Core Subsystems

### Orchestration Layer

| Component | Module | Description |
|---|---|---|
| **AutonomousOrchestrator** | `src/beagle/core/autonomous_orchestrator.py` | Top-level workflow executor with signal handling |
| **Router** | `src/beagle/core/router.py` | Model selection via adaptive quantization |
| **Steering Manager** | `src/beagle/steering/` | Constraint propagation: registry to state to prompt |
| **Preflight Estimator** | `src/beagle/preflight/` | Pre-execution cost and time estimation |

### Execution Layer

| Component | Module | Description |
|---|---|---|
| **Deep Forks** | `src/beagle/core/graph.py` | Structural sharing via persistent map and vector data structures (zero-copy state branching) |
| **A2A Protocol** | `src/beagle/core/a2a_protocol.py` | Agent-to-agent protocol with signature verification and role-based access control |
| **A2A Bridges** | `src/beagle/bridges/` | Integrations for graph execution backend and other agent frameworks |
| **VIGIL Validator** | `src/beagle/security/vigil.py` | Verify-before-commit tool output validation |
| **MicroVM Sandbox** | `src/beagle/core/sandbox.py` | Isolated code execution with timeout and resource limits |
| **Context Preprocessor** | `src/beagle/context/context_preprocessor.py` | Token-aware context window management with adaptive chunking |
| **Daemon and Scheduler** | `src/beagle/daemon/` | Background services: scheduler, watcher, triggers |

### Infrastructure Layer

| Component | Module | Description |
|---|---|---|
| **MCP RAG Server** | `src/beagle/infrastructure/mcp_rag_server.py` | Hybrid vector and graph semantic search |
| **MCP OpenClaw Server** | `src/beagle/infrastructure/mcp_openclaw_server.py` | Task queue: create, monitor, cancel workflows |
| **MCP Utility Server** | `src/beagle/infrastructure/mcp_utility_server.py` | Consolidated utility (workflow, code tools, web search) |
| **MCP Security** | `src/beagle/infrastructure/mcp_security.py` | Token verification middleware, transport hardening |
| **CPU Governor** | `src/beagle/infrastructure/cpu_governor.py` | Host resource policy enforcement |

### Memory and Context

| Component | Module | Description |
|---|---|---|
| **Memory Index** | `src/beagle/memory/memory_index.py` | Three-layer memory (semantic, RAG detail, session) with configurable token budget |
| **Hierarchical Memory** | `src/beagle/memory/hierarchical_memory.py` | Tiered memory hierarchy |
| **AutoDream** | `src/beagle/memory/autodream.py` | Background consolidation: prune, merge, refresh |
| **Post-Compaction Rehydration** | `src/beagle/context/post_compaction_rehydration.py` | Restores context from memory after compaction events |
| **Recipe-Agent Bridge** | `src/beagle/context/recipe_agent_bridge.py` | Connects agent recipes to context injection |

### Observability and Security

| Component | Module | Description |
|---|---|---|
| **OpenTelemetry** | `src/beagle/utils/tracing.py` | Distributed tracing with OTLP export |
| **Semantic Firewall** | `src/beagle/security/firewall.py` | Pattern matching plus model-based prompt injection detection |
| **Cost Governance** | `src/beagle/cost_tracker.py`, `src/beagle/tracking/` | Per-model, per-workflow cost tracking with budget limits |
| **Constraint Registry** | `src/beagle/infrastructure/constraint_registry.py` | Policy engine with priority-based constraint resolution |
| **Secrets Loader** | `src/beagle/secrets_loader.py` | Vault to environment to file secret chain with caching and rotation |
| **Audit Logger** | `src/beagle/infrastructure/audit_logger.py` | Structured audit trail for security-relevant events |

---

## Data Flow

```text
User Query
    │
    ▼
CLI (Typer) ──→ AutonomousOrchestrator
    │               │
    │               ├── PreflightEstimator (cost/time budget)
    │               ├── SteeringManager (apply constraints)
    │               └── Router (select model)
    │                       │
    │                       ▼
    │               Workflow Execution (graph state)
    │               │       │
    │               │       ├── Deep Fork (branch state)
    │               │       ├── A2A (inter-agent comms)
    │               │       └── Context Preprocessor (manage tokens)
    │               │
    │               ▼
    │          Result Aggregation
    │               │
    ▼               ▼
Output (Rich formatted) ──→ Cost Report ──→ Trace Export
```

---

## Configuration

Beagle reads TOML configuration files. The default is `config.toml` in the
project root, with optional override at the user's configuration directory.

Key configuration sections: `[orchestrator]`, `[router]`, `[rag]`,
`[security]`, `[tracing]`, `[memory]`.

### Memory Index Token Budget

The memory index token budget controls how much context the semantic layer
can occupy. Configuration priority:

1. **Environment variable** (highest priority)
2. **Config file** under `[memory] index_token_budget`
3. **Default**: 2000 (minimum: 500, values below 500 are clamped)

```toml
[memory]
index_token_budget = 3000
```

### Adaptive Quantization Cache

Adaptive 3-bit key/value compression is disabled by default. Enable through
the corresponding environment variable.

String and bytes values are never compressed — both `put()` and `set()` bypass
quantization for non-numeric types.

### Embedding Service

The configured embedding endpoint may expose different API paths depending on
whether the runtime base URL points to a local or remote endpoint. The path
is selected automatically based on the base URL.

See `config.toml` for the full schema.

---

## MCP Protocol Servers

Beagle exposes ONE Model Context Protocol server, registered in goose as `beagle`:

```
python3 -m beagle.infrastructure.mcp_beagle_server
```

The unified surface (`src/beagle/infrastructure/mcp_beagle_server.py`) absorbs:

1. **RAG group** — semantic code search (vector retrieval plus graph traversal);
   aggregate health/metrics are exposed as `rag_health_check` / `rag_get_metrics`
2. **Utility group** — workflow orchestration, code analysis, web research,
   meta-process tuning
3. **[tool]-standard plugins** — declared in `<config_root>/plugins/tools.toml`,
   mounted/unmounted at runtime via `plugin_reload()` (hotswap; no restart).
   The OpenClaw task-queue plugin (create/monitor/cancel/schedule tasks) is the
   first-class example.

---

## Workflow Templates

Workflows are defined in TOML format under `src/beagle/metaprompts/`. Built-in
templates cover three families:

| Template Family | Purpose |
|---|---|
| `research` | Deep research and analysis |
| `develop` | Feature implementation and code generation |
| `self-improvement` | Self-improvement and optimization cycles |

Custom workflows can be added by placing TOML templates in the same directory.

---

## Security Model

- **Semantic Firewall**: Regex pattern matching plus optional model-based safety evaluation
- **A2A Protocol**: Cryptographic signatures, role-based access control with wildcard permissions
- **MicroVM Sandbox**: Isolated execution for untrusted code with timeout and resource limits
- **Constraint Propagation**: Policy constraints flow from registry to state to prompt

---

## Further Reading

| Topic | Document |
|-------|----------|
| Steering System | [docs/STEERING.md](STEERING.md) — Mid-workflow directive injection |
| API Reference | [docs/API.md](API.md) — MCP tool reference |
| Observability | [docs/OBSERVABILITY.md](OBSERVABILITY.md) — Events, tracing, metrics |
| Security | [docs/SECURITY.md](SECURITY.md) — Input validation, sandboxing, A2A |
| Configuration | `beagle config schema` — Full config schema with types and constraints |
| Agent Profiles | Per-agent model and provider profile assignments |

### Provider Decoupling

Beagle uses a **deterministic fallback chain** for model and provider resolution.
Each step is consulted in order; the first match wins.

1. **Per-agent profile** — an agent may declare an explicit profile override
2. **Default agent profile** — the catch-all profile for any agent without an override
3. **Global LLM defaults** — used when no per-agent profile is defined
4. **Legacy global section** — kept for backward compatibility
5. **Hardcoded safe defaults** — fail-closed if no configuration is supplied

The resolution layer returns an immutable profile object carrying the
provider, model, and temperature. A separate resolver handles
complexity-based routing to higher- or lower-capability models. All file
outputs produced by the engine pass through a lint-before-write stage that
runs the project's quality gates against a staged payload before the file is
committed to disk.
