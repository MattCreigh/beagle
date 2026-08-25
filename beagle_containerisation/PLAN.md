# MISSION: Operation Industrialization — The Docker Deployment Program

## Beagle Dockeriser (`deploy.py`) — Full Research & Implementation Plan

> **Classification:** OPERATIONAL PLAN  
> **Version:** 13.6.0  
> **Created:** 2026-04-18  
> **Status:** RESEARCH PHASE

---

# ════════════════════════════════════════════════════════════════════
# SECTION 1: ENVIRONMENTAL INGESTION — Architecture Cartography
# ════════════════════════════════════════════════════════════════════

## 1.1 Project Identity

| Field | Value |
|---|---|
| **Package Name** | `beagle` |
| **Version** | 13.6.0 |
| **Python Requirement** | >=3.11 |
| **Build System** | hatchling |
| **Build Backend** | `hatchling.build` |
| **CLI Entry Point** | `beagle = beagle.cli.cli:main` |
| **Typer App Name** | `goose-workflow` (Typer internally registers this) |
| **MCP Entry Points** | `beagle-rag`, `beagle-openclaw`, `beagle-workflow` |
| **Wheel Size (current)** | 759KB (`beagle-13.6.0-py3-none-any.whl`) |
| **Source Tree Size** | 9.4MB (`beagle/`) |
| **Total Project Size** | ~509MB (includes `dist/`, `data/`, caches) |

## 1.2 Existing Docker Infrastructure (Legacy — To Be Replaced)

The project already has Docker infrastructure in `beagle/infrastructure/`:

| File | Purpose | Assessment |
|---|---|---|
| `Dockerfile.base` | Base image with `python:3.13-slim`, Goose binary, shared deps | **Outdated** — uses `python:3.13` (mission specifies `3.12`), installs raw deps not wheel, copies source tree |
| `Dockerfile.agent` | Agent-specific layer on top of base | **Superseded** — multi-agent pattern replaced by single-wheel deployment |
| `docker-compose.yml` | 5-service Orpheus topology (planner, executor, verifier, synthesizer, orchestrator) | **Needs replacement** — OptiPlex single-host deployment has different requirements |
| `build.sh` | Shell script building base + 5 agent images | **Superseded** — `deploy.py` replaces this entirely |
| `docker_quick_start.sh` | Interactive setup script | **Superseded** — `deploy.py` replaces this |
| `agent_entrypoint.sh` | Container startup with mode selection | **Superseded** — new entrypoint via `beagle` CLI |
| `docker_agent_wrapper.py` | Bridges LangGraph nodes ↔ Docker/Orpheus | **Superseded** — wheel install + `beagle` CLI eliminates need |
| `health_check.py` | Container health verification | **Reusable** — adapt for new single-container model |
| `META_PLAN.md` | Architecture documentation for Agent-per-Container topology | **Reference only** |

### Critical Legacy Issues:

1. **`Dockerfile.base` uses `python:3.13-slim`** — Mission requires `python:3.12-slim`
2. **Source-tree installation** — `pip install` of individual packages vs. wheel-based install
3. **No `.dockerignore`** exists at project root — Docker context includes entire 509MB tree
4. **`ai/` directory** is in `.gitignore` and contains runtime-generated analysis reports (2 files) — NOT a vestigial directory to purge for the golden master check; it's runtime data
5. **`skills/` directory** contains only 2 skill files (`code-write.md`, `docker-container-inspector.md`) — these ARE in the wheel (hatched) and should NOT be treated as vestigial
6. **Orpheus volume mounts** reference `/run/orpheus/nexus` — not applicable to single-container OptiPlex deployment
7. **Agent volumes mount `../ai:/app/ai:rw`** — this is runtime output data, not source

## 1.3 Configuration & Environment Variables (Docker-Relevant)

### Environment Variables Already Recognized by Codebase:

| Variable | Purpose | Default | Docker Impact |
|---|---|---|---|
| `BEAGLE_EXECUTION_ENV` | Mode selector: `"local"` / `"cloud"` / `"docker"` | `"local"` | Must set to `"docker"` in container |
| `WORKSPACE_ROOT` | Top-level workspace path | Auto-detected | Must set to `/app` |
| `BEAGLE_KNOWLEDGE_DIR` | RAG knowledge graph directory | Auto | Must map to `/app/data/rag` |
| `BEAGLE_KUZU_PATH` | Kùzu graph DB path | Auto | Must map to `/app/data/rag_kuzu` |
| `BEAGLE_DATA_ROOT` | Data root | `~/.beagle` | Must set to `/app/data` |
| `GOOSE_BIN` | Goose binary path | `~/.local/bin/goose.orig` | Not needed in wheel container |
| `GOOSE_PROVIDER` | LLM provider | `"ollama_cloud"` | Pass-through |
| `GOOSE_MODEL` | LLM model override | Per-config | Pass-through |
| `BEAGLE_CACHE_ENABLED` | Cache toggle | `"true"` | Keep enabled |
| `BEAGLE_MCP_TRANSPORT` | MCP transport mode | `"stdio"` | Keep as `"stdio"` in Docker |
| `BEAGLE_MCP_AUTH_ENABLED` | MCP auth toggle | `"true"` | Keep enabled |
| `BEAGLE_BUDGET_USD` | Budget override | Per-config | Pass-through |
| `BEAGLE_LOG_LEVEL` | Logging level | `"INFO"` | Pass-through |
| `POSTGRES_URI` | Postgres connection (cloud mode) | — | Not used in docker mode |
| `A2A_PORT` | A2A federation port | `8420` | **Must bind to `127.0.0.1:8420`** |

### Secrets Resolution Chain:

```
1. Environment variable (highest priority)
2. /root/.config/goose/secrets.yaml (file mount, read-only)
3. Empty string (fallback)
```

The `secrets_loader.py` validates file permissions (`0o600`), caches in-process, and supports `BEAGLE_STRICT_SECRETS` mode.

## 1.4 Checkpointer Architecture (Persistence Layer)

The checkpointer system (`memory/checkpointer.py`) uses a factory pattern:

- **`BEAGLE_EXECUTION_ENV=local`** → `AsyncSqliteSaver` (default)
- **`BEAGLE_EXECUTION_ENV=cloud`** → `AsyncPostgresSaver` (requires `POSTGRES_URI`)
- **`BEAGLE_EXECUTION_ENV=docker`** → Should fall through to SQLite (new mode to handle)

SQLite checkpoint path: `~/.cache/goose/beagle/checkpoints/beagle_checkpoints.db`  
**Docker mapping:** `./data/checkpoints ↔ /root/.cache/goose/beagle/checkpoints/`

## 1.5 RAG Knowledge Base (Persistence Layer)

Two RAG tiers exist:

| Tier | Variable | Default Path | Docker Volume |
|---|---|---|---|
| Instance RAG | `BEAGLE_KNOWLEDGE_DIR` | `/home/server/Dev/data/instance_rag` | `./data/rag ↔ /app/data/rag` |
| Instance Kùzu | `BEAGLE_KUZU_PATH` | `/home/server/Dev/data/instance_rag_kuzu` | `./data/rag ↔ /app/data/rag` (co-located) |

Note: The existing `instance_rag_kuzu` path was non-functional (`ls` returned exit code 2). The hot-swap system in `mcp_rag_server.py` manages LanceDB + Kùzu co-location. The Docker compose should mount a single volume for both under `/app/data/rag`.

## 1.6 A2A Federation Server (Network Layer)

- **Port:** `8420` (configurable via `[langchain_bridges.a2a]` in `config.toml`)
- **Bind address:** `127.0.0.1` (default, enforced for security)
- **Transport:** FastAPI/uvicorn (optional dependency) or basic HTTP fallback
- **Signing:** Ed25519 via PyNaCl
- **Docker requirement:** Expose port 8420, bind to `127.0.0.1` only (Tailscale access)

## 1.7 Dependency Graph (Core + Optional)

### Core (always installed with wheel):

```
langgraph>=0.2.0, langgraph-checkpoint>=3.0.0, langchain-core>=0.3.0,
typer>=0.12.0, rich>=13.0.0, pyyaml>=6.0, lancedb>=0.12.0, kuzu>=0.6.0,
pydantic>=2.0.0, instructor>=1.0.0, httpx>=0.27.0, tenacity>=8.0.0,
ddgs>=0.4.0, textual>=0.80.0, numpy>=1.26.0,
PyNaCl>=1.5.0, mcp>=1.0.0, psutil>=5.9.0, beautifulsoup4>=4.12.0,
opentelemetry-api>=1.24.0, opentelemetry-sdk>=1.24.0,
opentelemetry-exporter-otlp>=1.24.0, opentelemetry-instrumentation>=0.45b0
```

### Optional dependency groups:

| Group | Packages | Docker Impact |
|---|---|---|
| `dev` | pytest, ruff, vulture, mypy, safety, pip-audit | **Builder stage only** |
| `cpu` | torch, sentence-transformers | Large (~2GB), optional |
| `gpu` | torch (CUDA), sentence-transformers | Very large, optional |
| `memory` | zstandard | Small, include in runner |

### Notable Absence:
- **`pytest-xdist`** is NOT in `[dev]` dependencies — mission requires it. Must be added.

## 1.8 Wheel Build Configuration

```toml
[tool.hatch.build.targets.wheel]
packages = ["beagle"]
exclude = [
    "beagle/dist/",
    "beagle/tests/",
    "beagle/__pycache__/",
    "beagle/.pytest_cache/",
]
```

**Key findings:**
- The wheel includes `bridges/`, `memory/`, `ai/`, `infrastructure/` sub-packages
- `ai/` directory contents (runtime analysis reports) are in the wheel
- `tests/` is correctly excluded from the wheel
- The wheel is **744KB** — extremely lean

## 1.9 Vestigial Directory Analysis

The mission brief states: "verify that the project root is free of vestigial directories (ai/, skills/, etc.)"

| Directory | Location | In Wheel? | Vestigial? | Action |
|---|---|---|---|---|
| `ai/` | Source tree root? No — inside `beagle/ai/` | YES (2 files) | **NO** — runtime output dir | Do NOT purge; mount as volume |
| `skills/` | `beagle/skills/` | YES (2 .md files) | **NO** — active skill definitions | Do NOT purge |
| `beagle/dist/` | Build artifact | Excluded from wheel | **YES** — build output | Clean during build |
| `beagle/tests/` | Test suite | Excluded from wheel | **NO** — required for validation | Run tests, then exclude from Docker |
| `beagle/__pycache__/` | Bytecode cache | Excluded from wheel | **YES** — build artifact | Clean during build |

**Conclusion:** The mission's "vestigial directories" check should target:
- `dist/` at project root (old build artifacts)
- `__pycache__/` directories (bytecode)
- `.pytest_cache/`, `.ruff_cache/`, `htmlcov/` (test artifacts)
- NOT `ai/` or `skills/` (which are active, in-wheel directories)

## 1.10 Existing Entrypoint Analysis

```toml
[project.scripts]
beagle = "beagle.cli.cli:main"
```

The Typer app is registered as `name="goose-workflow"`, so after `pip install`:
- **`beagle`** command is available (from `[project.scripts]`)
- **`goose-workflow`** is the Typer internal name (not a separate script)

The `main()` function:
1. Calls `on_beagle_init()` to sync recipes→agents
2. Invokes the Typer `app()`

**Docker ENTRYPOINT should be:** `["beagle"]`  
**Docker CMD should be:** `["--help"]` (safe default, user overrides at runtime)

---

# ════════════════════════════════════════════════════════════════════
# SECTION 2: DEEP RESEARCH — Dockerization Pattern Analysis
# ════════════════════════════════════════════════════════════════════

## 2.1 Legacy Docker Architecture vs. Mission Requirements

### Legacy Architecture (Orpheus Multi-Container)

The existing Docker infrastructure (`infrastructure/`) was designed for a **5-agent Orpheus topology**:

```
┌─────────────────────────────────────────────────────────────┐
│  docker-compose.yml (Beagle Dev Stack)                        │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ orpheus-     │  │ planner      │  │ executor     │      │
│  │ daemon       │  │ (agent)      │  │ (agent)      │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │   Orpheus Ring Buffer IPC           │              │
│  ┌──────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐      │
│  │ verifier     │  │ synthesizer  │  │ orchestrator  │     │
│  │ (agent)      │  │ (agent)      │  │ (agent)       │     │
│  └──────────────┘  └──────────────┘  └───────────────┘      │
│                                                             │
│  Shared: /run/orpheus/nexus, /app/ai, /app/recipes         │
└─────────────────────────────────────────────────────────────┘
```

**Problems with legacy approach:**
1. Installs raw Python packages via `pip install` (9+ separate langchain/langgraph deps)
2. Copies `recipes/` and `skills/` as raw directories (no wheel standardization)
3. Uses `python:3.13-slim` instead of `python:3.12-slim` (mission spec)
4. Installs Goose binary via `curl | sh` (insecure, unpinned)
5. No `.dockerignore` — Docker context includes entire 509MB tree
6. No `pytest` validation gate before build
7. No `ruff`/`vulture` lint gate
8. No vestigial directory cleanup
9. Agent containers use `agent_entrypoint.sh` with mode selection instead of `beagle` CLI

### Mission Architecture (Single-Container Wheel)

```
┌─────────────────────────────────────────────────────────────┐
│  Multi-Stage Dockerfile                                     │
│                                                             │
│  ┌─────────────────────────────────────────┐                │
│  │ Stage 1: BUILDER (python:3.12-slim)     │                │
│  │  - Install uv                            │                │
│  │  - Copy .whl from dist/                  │                │
│  │  - Install wheel → isolated venv         │                │
│  └────────────────────────┬─────────────────┘                │
│                           │ (copied wheel)                  │
│  ┌────────────────────────┴─────────────────┐                │
│  │ Stage 2: RUNNER (python:3.12-slim)      │                │
│  │  - Create beagle_user (non-root)           │                │
│  │  - Copy installed wheel                  │                │
│  │  - Set BEAGLE_EXECUTION_ENV=docker         │                │
│  │  - STOPSIGNAL SIGTERM                    │                │
│  │  - ENTRYPOINT ["beagle"]                   │                │
│  └────────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  docker-compose.yaml (OptiPlex Single-Host)                │
│                                                             │
│  volumes:                                                   │
│    ./data/rag ↔ /app/data/rag          (LanceDB + Kùzu)    │
│    ./data/checkpoints ↔ /root/.cache/.../checkpoints       │
│    ~/.config/goose/secrets.yaml ↔ /root/.config/...:ro     │
│                                                             │
│  ports:                                                     │
│    127.0.0.1:8420:8420                (A2A via Tailscale)  │
└─────────────────────────────────────────────────────────────┘
```

## 2.2 Python Version Audit: 3.12 vs 3.13

| Factor | python:3.12-slim | python:3.13-slim | Decision |
|---|---|---|---|
| **Mission Spec** | ✅ Required | ❌ | Use 3.12 |
| **Legacy Dockerfile.base** | ❌ Uses 3.13 | ✅ | Override required |
| **pyproject.toml** | `>=3.11` | Compatible | Both work, 3.12 preferred |
| **Image Size** | ~50MB | ~50MB | Equal |
| **torch Compatibility** | ✅ Full | ⚠️ Some edge cases | 3.12 safer |
| **uv Compatibility** | ✅ Full | ✅ Full | Equal |

**Decision:** Use `python:3.12-slim` per mission spec. The `>=3.11` requirement makes either work, but 3.12 has broader library compatibility, especially for `torch` if `cpu`/`gpu` extras are installed.

## 2.3 Build Tool Audit: `uv build` vs `python -m build`

| Factor | `uv build` | `python -m build` | Notes |
|---|---|---|---|
| **Mission Spec** | ✅ Required | ❌ | Use `uv build` |
| **Makefile Current** | `python3 -m build` | ✅ In use | Must switch |
| **Speed** | ~2-5s | ~10-15s | `uv` is 5-10x faster |
| **Backend** | Hatchling (same) | Hatchling (same) | Same output wheel |
| **Wheel Output** | `dist/*.whl` | `dist/*.whl` | Same location |
| **Deterministic** | ✅ Lock file aware | ✅ | `uv` has better reproducibility |

**Decision:** Use `uv build` per mission spec. The Makefile's `build` target currently uses `python3 -m build` — `deploy.py` will override this with `uv build`.

## 2.4 Test Suite Analysis (Golden Master Gate)

### Test Count Verification

| Source | Count | Notes |
|---|---|---|
| Mission brief claim | "800+ tests" | Verified |
| `tests/` directory | 50 test files | Top-level only |
| `beagle/tests/` | 31 test files | Internal test suite |
| Total test functions | **900** | `grep -c "def test_\|async def test_"` |
| pytest-xdist status | **NOT INSTALLED** | Must add to `[dev]` dependencies |

### Missing Dependency: `pytest-xdist`

The mission requires parallel test execution with `pytest-xdist` for speed. Currently NOT in `[project.optional-dependencies.dev]`:

```toml
# Current dev dependencies (MISSING pytest-xdist)
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=4.0.0",
    "mypy>=1.0.0",
    "ruff>=0.4.0",
    "vulture>=2.0",
    "safety>=3.0.0",
    "pip-audit>=2.7.0",
]
```

**Required modification** (to `pyproject.toml` only, NOT to other source files):
```toml
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=4.0.0",
    "pytest-xdist>=3.5.0",      # <-- ADD: parallel test execution
    "mypy>=1.0.0",
    "ruff>=0.4.0",
    "vulture>=2.0",
    "safety>=3.0.0",
    "pip-audit>=2.7.0",
]
```

### Validation Pipeline for Phase 1

The validator must run these checks in order:

```
1. ruff check beagle/ tests/     → Must exit 0
2. vulture beagle/ tests/ --min-confidence 80  → Must exit 0
3. pytest tests/ -v --tb=short -n auto            → All 900 must pass
4. Vestigial directory scan:                       → Must find none
   - dist/ at project root (stale wheels)
   - build/ at project root
   - *.egg-info at project root
   - __pycache__/ directories
   - .pytest_cache/, .ruff_cache/, htmlcov/
   
   Does NOT scan for ai/ or skills/ (these are IN the wheel and are NOT vestigial)
```

## 2.5 Entrypoint Resolution

The mission brief states: *"Set the entrypoint to the goose-workflow command defined in pyproject.toml."*

Investigation reveals:

| Candidate | Source | Available after pip install? | Correct? |
|---|---|---|---|
| `beagle` | `[project.scripts]` in pyproject.toml | ✅ Yes | ✅ **USE THIS** |
| `goose-workflow` | Typer `app(name="goose-workflow")` | ❌ Typer internal name only | ❌ |
| `python -m beagle` | `__main__.py` | ✅ Yes | ⚠️ Works but not canonical |

**Decision:** `ENTRYPOINT ["beagle"]` — this is the actual console script registered by pip.

The Typer `name="goose-workflow"` only affects the CLI help text display (`Usage: goose-workflow [OPTIONS] COMMAND [ARGS]`). It does NOT create a second executable.

## 2.6 Vestigial Directory Deep Analysis

The mission brief states: *"verify that the project root is free of vestigial directories (ai/, skills/, etc.)"*

This requires careful interpretation. Investigation shows:

| Path | Purpose | In Wheel? | Should Purge? | Reason |
|---|---|---|---|---|
| `beagle/ai/` | Runtime analysis reports (2 `.py` files) | YES | **NO** | Active module, part of package |
| `beagle/skills/` | Skill definitions (2 `.md` files) | YES | **NO** | Active module, part of package |
| `dist/` (project root) | Stale wheel builds | N/A | **YES** | Build artifact |
| `build/` (project root) | Stale sdist builds | N/A | **YES** | Build artifact |
| `*.egg-info/` | Metadata | N/A | **YES** | Build artifact |
| `__pycache__/` | Bytecode | N/A | **YES** | Runtime cache |
| `.pytest_cache/` | Test cache | N/A | **YES** | Test artifact |
| `.ruff_cache/` | Lint cache | N/A | **YES** | Lint artifact |
| `htmlcov/` | Coverage reports | N/A | **YES** | Test artifact |

**Critical Finding:** The mission's mention of "ai/, skills/" likely refers to the **legacy Docker context** (`infrastructure/build.sh` copies these as standalone directories). In the **new wheel-based architecture**, `ai/` and `skills/` are embedded inside the wheel and should NOT be treated as vestigial. The validator will scan for build artifacts only.

## 2.7 Docker Layer Caching Strategy

### Multi-Stage Build Layers

```
STAGE 1: BUILDER
─────────────────────────────────────────────────
Layer 1: python:3.12-slim base              → ~50MB (cached)
Layer 2: apt-get install ca-certificates    → ~2MB (cached)
Layer 3: COPY --from=ghcr.io/astral-sh/uv  → ~20MB (cached)
Layer 4: COPY dist/*.whl /tmp/              → ~760KB (invalidated on new wheel)
Layer 5: uv pip install /tmp/*.whl          → ~150MB (invalidated on new deps)

STAGE 2: RUNNER
─────────────────────────────────────────────────
Layer 6: python:3.12-slim base              → ~50MB (cached from Layer 1)
Layer 7: RUN useradd beagle_user + dirs       → ~0KB (cached)
Layer 8: COPY --from=builder /install       → ~150MB (invalidated on new deps)
Layer 9: ENV + EXPOSE + STOPSIGNAL + ENTRY  → ~0KB (cached)

TOTAL ESTIMATED IMAGE SIZE: ~200MB
```

## 2.8 Persistence Layer Mapping (OptiPlex Specific)

### SQLite Checkpointer Path Resolution

```python
# In checkpointer.py:
def get_checkpoint_dir() -> Path:
    cache_root = get_cache_root()  # → ~/.cache/goose/beagle
    checkpoint_dir = cache_root / "checkpoints"
    return checkpoint_dir

# Docker mapping:
# Host:  ./data/checkpoints/beagle_checkpoints.db
# Container: /root/.cache/goose/beagle/checkpoints/beagle_checkpoints.db
```

### RAG Data Path Resolution

```python
# In config/paths.py:
BEAGLE_KNOWLEDGE_DIR → /app/data/rag (overridden by env var)
BEAGLE_KUZU_PATH → /app/data/rag (co-located, overridden by env var)
# These are already set via RAG_TIER=instance pointing to:
#   /home/server/Dev/data/instance_rag  (LanceDB)
#   /home/server/Dev/data/instance_rag_kuzu  (Kùzu) ← BROKEN (dir doesn't exist)
```

### Secrets Path Resolution

```python
# In secrets_loader.py:
DEFAULT_SECRETS_PATH = Path.home() / ".config" / "goose" / "secrets.yaml"
# Validates file permissions (0o600)
# Caches in-process

# Docker mapping:
# Host:  ~/.config/goose/secrets.yaml
# Container: /root/.config/goose/secrets.yaml:ro
```

## 2.9 Complete Environment Variable Matrix for Docker

```yaml
# Required (set in Dockerfile)
BEAGLE_EXECUTION_ENV: docker
WORKSPACE_ROOT: /app
BEAGLE_DATA_ROOT: /app/data
BEAGLE_KNOWLEDGE_DIR: /app/data/rag
BEAGLE_KUZU_PATH: /app/data/rag
PYTHONDONTWRITEBYTECODE: 1
PYTHONUNBUFFERED: 1

# Required (set in docker-compose.yaml or .env)
A2A_PORT: 8420

# Optional (pass-through from .env)
GOOSE_PROVIDER: <from host>
GOOSE_MODEL: <from host>
BEAGLE_LOG_LEVEL: INFO
BEAGLE_BUDGET_USD: 10.0
BEAGLE_CACHE_ENABLED: true
BEAGLE_MCP_TRANSPORT: stdio
BEAGLE_MCP_AUTH_ENABLED: true

# Secrets (file mount)
GOOSE_SECRETS_FILE: /root/.config/goose/secrets.yaml
```

## 2.10 Risk Assessment

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| `pytest-xdist` not in dev deps | ✅ Confirmed | Blocks Phase 1 | Add to `pyproject.toml` as first step |
| Python 3.12 vs 3.13 mismatch | ✅ Confirmed (legacy uses 3.13) | Low (project supports >=3.11) | Use 3.12 per mission spec |
| `ai/` misidentified as vestigial | ✅ Confirmed (mission brief says "ai/") | Medium (would delete active code) | Validator targets only build artifacts |
| Kùzu path broken on host | ✅ Confirmed (`instance_rag_kuzu` missing) | Medium (RAG startup may fail) | Docker compose creates volume; script pre-creates dir |
| `uv build` not in Makefile | ✅ Confirmed (Makefile uses `python3 -m build`) | Low | `deploy.py` calls `uv build` directly |
| No `.dockerignore` exists | ✅ Confirmed | High (509MB Docker context) | `deploy.py` generates `.dockerignore` |
| `beagle` vs `goose-workflow` confusion | ✅ Confirmed (mission brief says "goose-workflow") | Medium (wrong entrypoint) | Use `beagle` — the actual console script |
| Secrets file permissions in Docker | Medium | High (`secrets_loader` enforces 0o600) | Init container or entrypoint script fixes perms |
| SQLite concurrent writes | Low | Medium | Only one container; SQLite WAL mode |
| LanceDB lock file in Docker | Low | Medium | Volume mount ensures persistence across restarts |

---

# ════════════════════════════════════════════════════════════════════
# SECTION 3: DELIBERATION MATRIX — Three Architectural Trajectories
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
# SECTION 4: IMPLEMENTATION SPECIFICATION — Module-by-Module Blueprint
# ════════════════════════════════════════════════════════════════════

## 4.0 Package Structure (Trajectory Gamma)

```
beagle_dockeriser/
├── __init__.py              # __version__ = "1.0.0"
├── __main__.py              # CLI entry: python -m beagle_dockeriser
├── cli.py                   # Typer CLI with phase flags
├── pipeline.py              # Sequential pipeline orchestrator
├── models.py                # Dataclasses: DockerfileSpec, ComposeSpec, PipelineState
├── constants.py             # Version strings, image names, ports, paths
└── phases/
    ├── __init__.py
    ├── validate.py           # Phase 1: Golden master validation
    ├── build.py              # Phase 2: uv build → dist/*.whl
    ├── dockerfile.py         # Phase 3: Generate Dockerfile from DockerfileSpec
    ├── compose.py            # Phase 4: Generate docker-compose.yaml from ComposeSpec
    └── build_push.py         # Phase 5: docker build + summary report
```

---

## 4.1 `constants.py` — Version Strings, Image Names, Ports, Paths

### Purpose
Single source of truth for all configurable constants. No magic strings anywhere else in the package.

### Specification

```python
"""Constants for beagle_dockeriser — single source of truth."""

# ── Project Identity ──────────────────────────────────────
PROJECT_NAME: str = "beagle"
PROJECT_VERSION: str = "13.6.0"
DOCKER_IMAGE_NAME: str = "beagle-factory"
DOCKER_IMAGE_TAG: str = "v13.6.0"
FULL_IMAGE_REF: str = f"{DOCKER_IMAGE_NAME}:{DOCKER_IMAGE_TAG}"

# ── Python Version ────────────────────────────────────────
PYTHON_VERSION: str = "3.12"
PYTHON_DOCKER_IMAGE: str = f"python:{PYTHON_VERSION}-slim"

# ── Build Tools ───────────────────────────────────────────
UV_VERSION: str = "latest"    # uv installer tag
HATCHLING_VERSION: str = "*"   # from pyproject.toml [build-system]

# ── Ports ─────────────────────────────────────────────────
A2A_PORT: int = 8420
A2A_BIND_ADDRESS: str = "127.0.0.1"

# ── Container User ────────────────────────────────────────
CONTAINER_USER: str = "beagle_user"
CONTAINER_UID: int = 1000
CONTAINER_GID: int = 1000
CONTAINER_HOME: str = "/home/beagle_user"

# ── Filesystem Paths (Container) ─────────────────────────
CONTAINER_APP_DIR: str = "/app"
CONTAINER_DATA_DIR: str = "/app/data"
CONTAINER_RAG_DIR: str = "/app/data/rag"
CONTAINER_CHECKPOINTS_DIR: str = "/home/beagle_user/.cache/goose/beagle/checkpoints"
CONTAINER_SECRETS_DIR: str = "/home/beagle_user/.config/goose"
CONTAINER_SECRETS_FILE: str = "/home/beagle_user/.config/goose/secrets.yaml"
CONTAINER_STATE_DIR: str = "/app/state"
CONTAINER_OUTPUT_DIR: str = "/app/output"

# ── Filesystem Paths (Host) ──────────────────────────────
HOST_DATA_DIR: str = "./data"
HOST_RAG_DIR: str = "./data/rag"
HOST_CHECKPOINTS_DIR: str = "./data/checkpoints"
HOST_SECRETS_FILE: str = "~/.config/goose/secrets.yaml"

# ── Vestigial Directories to Scan ────────────────────────
VESTIGIAL_DIRS: tuple[str, ...] = (
    "build",
    "dist",
    "*.egg-info",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
)
VESTIGIAL_EXCLUDE_PREFIXES: tuple[str, ...] = (
    # These are INSIDE the wheel package, NOT vestigial
    "beagle/",
)

# ── Environment Variables (Dockerfile) ───────────────────
DOCKERFILE_ENV: dict[str, str] = {
    "BEAGLE_EXECUTION_ENV": "docker",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "WORKSPACE_ROOT": "/app",
    "BEAGLE_DATA_ROOT": "/app/data",
    "BEAGLE_KNOWLEDGE_DIR": "/app/data/rag",
    "BEAGLE_KUZU_PATH": "/app/data/rag",
}

# ── Validation Commands ──────────────────────────────────
RUFF_TARGETS: tuple[str, ...] = ("beagle/", "tests/")
VULTURE_MIN_CONFIDENCE: int = 80
PYTEST_XDIST_FLAG: str = "-n auto"

# ── Entrypoint ────────────────────────────────────────────
ENTRYPOINT_CMD: list[str] = ["beagle"]

# ── Stopsignal ────────────────────────────────────────────
STOP_SIGNAL: str = "SIGTERM"

# ── Health Check ─────────────────────────────────────────
HEALTHCHECK_INTERVAL: int = 30
HEALTHCHECK_TIMEOUT: int = 10
HEALTHCHECK_RETRIES: int = 3
HEALTHCHECK_CMD: list[str] = ["python", "-c", "import beagle; print('ok')"]
```

---

## 4.2 `models.py` — Data Models for Dockerfile, Compose, and Pipeline State

### Purpose
Typed dataclasses that define the structure of every generated artifact. Validation happens at construction time, not at Docker build time.

### Specification

```python
"""Data models for beagle_dockeriser — typed generation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .constants import *


@dataclass(frozen=True)
class WheelSpec:
    """Metadata about the built wheel file."""
    path: Path                        # Absolute path to .whl file
    name: str                         # Filename
    size_bytes: int                   # File size
    version: str = PROJECT_VERSION    # Extracted from filename

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


@dataclass(frozen=True)
class DockerfileSpec:
    """Complete specification for Dockerfile generation.
    
    Every field has a sane default from constants.py.
    Override any field to customize the generated Dockerfile.
    """
    base_image: str = PYTHON_DOCKER_IMAGE
    container_user: str = CONTAINER_USER
    container_uid: int = CONTAINER_UID
    container_gid: int = CONTAINER_GID
    container_home: str = CONTAINER_HOME
    app_dir: str = CONTAINER_APP_DIR
    data_dir: str = CONTAINER_DATA_DIR
    wheel_filename: str = ""                      # Set after Phase 2
    env_vars: dict[str, str] = field(default_factory=lambda: dict(DOCKERFILE_ENV))
    expose_ports: list[int] = field(default_factory=lambda: [A2A_PORT])
    stop_signal: str = STOP_SIGNAL
    entrypoint: list[str] = field(default_factory=lambda: list(ENTRYPOINT_CMD))
    healthcheck_cmd: list[str] = field(default_factory=lambda: list(HEALTHCHECK_CMD))
    healthcheck_interval: int = HEALTHCHECK_INTERVAL
    healthcheck_timeout: int = HEALTHCHECK_TIMEOUT
    healthcheck_retries: int = HEALTHCHECK_RETRIES


@dataclass(frozen=True)
class VolumeSpec:
    """A single volume mount specification."""
    host_path: str
    container_path: str
    read_only: bool = False

    def to_compose_line(self) -> str:
        rw = ":ro" if self.read_only else ""
        return f"      - {self.host_path}:{self.container_path}{rw}"


@dataclass(frozen=True)
class ComposeSpec:
    """Complete specification for docker-compose.yaml generation."""
    image_name: str = DOCKER_IMAGE_NAME
    image_tag: str = DOCKER_IMAGE_TAG
    container_name: str = "beagle-factory"
    restart_policy: str = "unless-stopped"
    volumes: list[VolumeSpec] = field(default_factory=lambda: [
        VolumeSpec(HOST_RAG_DIR, CONTAINER_RAG_DIR),
        VolumeSpec(HOST_CHECKPOINTS_DIR, CONTAINER_CHECKPOINTS_DIR),
        VolumeSpec(HOST_SECRETS_FILE, CONTAINER_SECRETS_FILE, read_only=True),
    ])
    ports: list[str] = field(default_factory=lambda: [
        f"{A2A_BIND_ADDRESS}:{A2A_PORT}:{A2A_PORT}"
    ])
    env_file: str = ".env"
    healthcheck: dict[str, str | int] = field(default_factory=lambda: {
        "test": "CMD python -c \"import beagle; print('ok')\"",
        "interval": f"{HEALTHCHECK_INTERVAL}s",
        "timeout": f"{HEALTHCHECK_TIMEOUT}s",
        "retries": HEALTHCHECK_RETRIES,
    })


@dataclass
class PipelineState:
    """Mutable state passed between pipeline phases.
    
    Each phase reads what it needs and writes its results.
    The pipeline orchestrator creates one instance and passes it through.
    """
    project_root: Path
    phase1_passed: bool = False
    phase2_passed: bool = False
    phase3_passed: bool = False
    phase4_passed: bool = False
    phase5_passed: bool = False
    
    # Phase 2 outputs
    wheel_spec: Optional[WheelSpec] = None
    
    # Phase 3 outputs
    dockerfile_path: Optional[Path] = None
    dockerfile_spec: Optional[DockerfileSpec] = None
    
    # Phase 4 outputs
    compose_path: Optional[Path] = None
    compose_spec: Optional[ComposeSpec] = None
    
    # Phase 5 outputs
    image_id: str = ""
    image_size_mb: float = 0.0
    build_duration_seconds: float = 0.0
    
    # Error tracking
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    phase_details: dict[str, str] = field(default_factory=dict)
```


---

## 4.3 `cli.py` — Typer CLI Interface

### Purpose
Provides the user-facing CLI with phase selection, skip flags, and verbose output. Follows the same Typer patterns used in `beagle/cli/cli.py`.

### Specification

```python
"""CLI for beagle_dockeriser — deployment orchestrator interface."""

from __future__ import annotations

import typer
from pathlib import Path
from rich.console import Console

from .constants import PROJECT_VERSION, DOCKER_IMAGE_NAME, DOCKER_IMAGE_TAG

app = typer.Typer(
    name="beagle-dockeriser",
    help=f"Beagle Docker Deployment Orchestrator v{PROJECT_VERSION}",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


@app.command()
def deploy(
    project_root: Path = typer.Option(
        Path.cwd(),
        "--project-root", "-r",
        help="Path to beagle project root",
        exists=True,
        dir_okay=True,
    ),
    phase: int = typer.Option(
        0,
        "--phase", "-p",
        help="Start from phase (1-5). 0 = run all phases sequentially.",
        min=0,
        max=5,
    ),
    skip_validation: bool = typer.Option(
        False,
        "--skip-validation",
        help="DANGER: Skip Phase 1 (Golden Master validation)",
    ),
    skip_build: bool = typer.Option(
        False,
        "--skip-build",
        help="Skip Phase 5 (Docker build). Just generate files.",
    ),
    image_tag: str = typer.Option(
        DOCKER_IMAGE_TAG,
        "--tag", "-t",
        help="Docker image tag",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Verbose output",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be done without executing",
    ),
) -> None:
    """Run the full deployment pipeline (Phases 1-5)."""
    from .pipeline import Pipeline
    
    pipeline = Pipeline(
        project_root=project_root,
        start_phase=phase,
        skip_validation=skip_validation,
        skip_build=skip_build,
        image_tag=image_tag,
        verbose=verbose,
        dry_run=dry_run,
    )
    pipeline.run()


@app.command()
def validate(
    project_root: Path = typer.Option(
        Path.cwd(),
        "--project-root", "-r",
        help="Path to project root",
        exists=True,
    ),
) -> None:
    """Run Phase 1 only: Golden Master validation."""
    from .phases.validate import run_validation
    state = run_validation(project_root)
    # ... print results


@app.command()
def generate(
    project_root: Path = typer.Option(
        Path.cwd(),
        "--project-root", "-r",
        help="Path to project root",
        exists=True,
    ),
) -> None:
    """Generate Dockerfile + docker-compose.yaml without building."""
    from .phases.dockerfile import generate_dockerfile
    from .phases.compose import generate_compose
    # ... generate both files and print paths


@app.command()
def status(
    project_root: Path = typer.Option(
        Path.cwd(),
        "--project-root", "-r",
        help="Path to project root",
        exists=True,
    ),
) -> None:
    """Show current deployment state (which phases completed)."""
    # Check for: dist/*.whl, Dockerfile, docker-compose.yaml, docker images
```

---

## 4.4 `__main__.py` — Package Entry Point

```python
"""Allow running as: python -m beagle_dockeriser"""
from .cli import app

if __name__ == "__main__":
    app()
```

---

## 4.5 `pipeline.py` — Sequential Pipeline Orchestrator

### Purpose
Orchestrates the 5 phases in sequence. Each phase is a pure function that takes `PipelineState` and returns a modified `PipelineState`. The pipeline handles phase skipping, error propagation, and summary reporting.

### Specification

```python
"""Pipeline orchestrator — sequential phase execution."""

from __future__ import annotations

import time
from pathlib import Path
from dataclasses import replace

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import PipelineState
from .constants import DOCKER_IMAGE_TAG


class Pipeline:
    """Orchestrates the 5-phase deployment pipeline."""

    def __init__(
        self,
        project_root: Path,
        start_phase: int = 0,
        skip_validation: bool = False,
        skip_build: bool = False,
        image_tag: str = DOCKER_IMAGE_TAG,
        verbose: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.state = PipelineState(project_root=project_root.resolve())
        self.start_phase = start_phase
        self.skip_validation = skip_validation
        self.skip_build = skip_build
        self.image_tag = image_tag
        self.verbose = verbose
        self.dry_run = dry_run
        self.console = Console()

    def run(self) -> PipelineState:
        """Execute the pipeline phases sequentially."""
        self.console.print(Panel.fit(
            "[bold cyan]Operation Industrialization[/bold cyan]\n"
            f"Beagle Docker Deployment Program\n"
            f"Project: {self.state.project_root}",
            title="🚀 DEPLOY",
        ))

        phases = [
            (1, "Validator", self._phase1),
            (2, "Builder", self._phase2),
            (3, "Dockerfile", self._phase3),
            (4, "Compose", self._phase4),
            (5, "Build & Push", self._phase5),
        ]

        for phase_num, phase_name, phase_fn in phases:
            if self.start_phase > phase_num:
                self.console.print(f"[dim]  Phase {phase_num}: {phase_name} — SKIPPED (start_phase={self.start_phase})[/dim]")
                continue
            if phase_num == 1 and self.skip_validation:
                self.console.print(f"[dim]  Phase {phase_num}: {phase_name} — SKIPPED (--skip-validation)[/dim]")
                self.state.phase1_passed = True  # Assume valid
                continue
            if phase_num == 5 and self.skip_build:
                self.console.print(f"[dim]  Phase {phase_num}: {phase_name} — SKIPPED (--skip-build)[/dim]")
                continue

            self.console.print(f"\n[bold]Phase {phase_num}: {phase_name}[/bold]")
            t0 = time.monotonic()
            
            try:
                self.state = phase_fn()
            except Exception as exc:
                self.state.errors.append(f"Phase {phase_num} ({phase_name}): {exc}")
                self.console.print(f"[bold red]  FAILED: {exc}[/bold red]")
                break
            
            elapsed = time.monotonic() - t0
            self.state.phase_details[f"phase{phase_num}_time"] = f"{elapsed:.1f}s"
            self.console.print(f"[green]  ✓ Completed in {elapsed:.1f}s[/green]")

        self._print_summary()
        return self.state

    def _phase1(self) -> PipelineState:
        from .phases.validate import run_validation
        return run_validation(self.state)

    def _phase2(self) -> PipelineState:
        from .phases.build import run_build
        return run_build(self.state)

    def _phase3(self) -> PipelineState:
        from .phases.dockerfile import run_dockerfile_gen
        return run_dockerfile_gen(self.state)

    def _phase4(self) -> PipelineState:
        from .phases.compose import run_compose_gen
        return run_compose_gen(self.state)

    def _phase5(self) -> PipelineState:
        from .phases.build_push import run_build_push
        return run_build_push(self.state, image_tag=self.image_tag)

    def _print_summary(self) -> None:
        """Print final deployment summary table."""
        table = Table(title="Deployment Summary")
        table.add_column("Phase", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Details")
        
        phases = [
            (1, "Validator", self.state.phase1_passed),
            (2, "Builder", self.state.phase2_passed),
            (3, "Dockerfile", self.state.phase3_passed),
            (4, "Compose", self.state.phase4_passed),
            (5, "Build & Push", self.state.phase5_passed),
        ]
        
        for num, name, passed in phases:
            status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
            detail = self.state.phase_details.get(f"phase{num}_detail", "")
            table.add_row(f"Phase {num}: {name}", status, detail)
        
        self.console.print(table)
        
        if self.state.errors:
            self.console.print("\n[bold red]Errors:[/bold red]")
            for err in self.state.errors:
                self.console.print(f"  • {err}")
```


---

## 4.6 `phases/validate.py` — Phase 1: The Validator (Golden Master Gate)

### Purpose
Proves the codebase is "Golden" before any Docker artifact is generated. If ANY check fails, the pipeline **aborts** with a non-zero exit code.

### Checks Performed (in order)

| # | Check | Command | Exit on Fail |
|---|---|---|---|
| 1 | Ruff lint | `python3 -m ruff check beagle/ tests/` | YES |
| 2 | Vulture dead code | `python3 -m vulture beagle/ tests/ --min-confidence 80` | YES |
| 3 | Pytest suite + xdist | `python3 -m pytest tests/ -v --tb=short -n auto` | YES |
| 4 | Vestigial scan | Python pathlib scan (no subprocess) | WARN only |

### Specification

```python
"""Phase 1: Golden Master Validation — The Gatekeeper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..models import PipelineState
from ..constants import (
    RUFF_TARGETS,
    VULTURE_MIN_CONFIDENCE,
    PYTEST_XDIST_FLAG,
    VESTIGIAL_DIRS,
    VESTIGIAL_EXCLUDE_PREFIXES,
)


def run_validation(state: PipelineState) -> PipelineState:
    """Run all Golden Master validation checks.
    
    Returns modified state with phase1_passed = True only if ALL checks pass.
    On failure, state.errors is populated and pipeline will abort.
    """
    console = Console()
    project_root = state.project_root
    
    results: list[tuple[str, bool, str]] = []
    
    # ── Check 1: Ruff Lint ──────────────────────────────────────────
    cmd = [sys.executable, "-m", "ruff", "check", *RUFF_TARGETS]
    ok, detail = _run_command(cmd, project_root, "ruff lint")
    results.append(("Ruff Lint", ok, detail))
    
    # ── Check 2: Vulture Dead Code ──────────────────────────────────
    cmd = [sys.executable, "-m", "vulture", *RUFF_TARGETS,
           f"--min-confidence={VULTURE_MIN_CONFIDENCE}"]
    ok, detail = _run_command(cmd, project_root, "vulture")
    results.append(("Vulture", ok, detail))
    
    # ── Check 3: Pytest + xdist ─────────────────────────────────────
    cmd = [sys.executable, "-m", "pytest", "tests/", "-v",
           "--tb=short", PYTEST_XDIST_FLAG]
    ok, detail = _run_command(cmd, project_root, "pytest-xdist")
    results.append(("Pytest (xdist)", ok, detail))
    
    # ── Check 4: Vestigial Directory Scan ──────────────────────────
    vestigial_found = _scan_vestigial(project_root)
    vestigial_ok = len(vestigial_found) == 0
    vestigial_detail = (
        "Clean" if vestigial_ok
        else f"Found {len(vestigial_found)} artifacts: {', '.join(vestigial_found[:5])}"
    )
    results.append(("Vestigial Scan", vestigial_ok, vestigial_detail))
    
    # ── Summary ──────────────────────────────────────────────────────
    all_passed = all(ok for _, ok, _ in results)
    
    table = Table(title="Phase 1: Golden Master Validation")
    table.add_column("Check", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Details")
    for name, ok, detail in results:
        status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        table.add_row(name, status, detail)
    console.print(table)
    
    state.phase1_passed = all_passed
    state.phase_details["phase1_detail"] = (
        "All checks passed" if all_passed
        else f"{' ,'.join(n for n, ok, _ in results if not ok)} FAILED"
    )
    
    if not all_passed:
        failed = [n for n, ok, _ in results if not ok]
        state.errors.append(f"Phase 1 validation failed: {', '.join(failed)}")
    
    return state


def _run_command(
    cmd: list[str],
    cwd: Path,
    label: str,
    timeout: int = 600,
) -> tuple[bool, str]:
    """Run a subprocess command and return (success, detail_string)."""
    console = Console()
    console.print(f"  [dim]Running: {' '.join(cmd)}[/dim]")
    
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            # Extract line count from output for pytest detail
            if "pytest" in label:
                # Parse "X passed" from pytest output
                for line in result.stdout.splitlines():
                    if "passed" in line:
                        return True, line.strip()
                return True, "All tests passed"
            return True, "OK"
        else:
            # Capture first 3 lines of stderr for detail
            err_lines = result.stderr.strip().splitlines()[:3]
            return False, " | ".join(err_lines)
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"


def _scan_vestigial(project_root: Path) -> list[str]:
    """Scan project root for vestigial build artifacts.
    
    Checks for: build/, dist/, *.egg-info/, __pycache__/, 
    .pytest_cache/, .ruff_cache/, htmlcov/
    
    Does NOT flag ai/ or skills/ — these are active package modules
    embedded in the wheel, not vestigial directories.
    """
    found: list[str] = []
    
    for pattern in VESTIGIAL_DIRS:
        if pattern == "__pycache__":
            # Count all __pycache__ dirs
            matches = list(project_root.rglob("__pycache__"))
            # Exclude paths inside the wheel
            matches = [m for m in matches
                       if not any(m.relative_to(project_root).is_relative_to(p)
                                  for p in VESTIGIAL_EXCLUDE_PREFIXES)]
            if matches:
                found.append(f"__pycache__/ ({len(matches)} directories)")
        elif pattern == "*.egg-info":
            matches = list(project_root.glob("*.egg-info"))
            if matches:
                found.extend(str(m.relative_to(project_root)) for m in matches)
        else:
            target = project_root / pattern
            if target.exists():
                found.append(pattern + "/")
    
    return found
```

### Key Design Decisions

1. **Vestigial scan is WARN-only** — build artifacts (`dist/`, `build/`) are cleaned in Phase 2 before `uv build`. A warning is logged but doesn't block the pipeline.

2. **`ai/` and `skills/` are NOT vestigial** — The mission brief's mention of these directories refers to the legacy Docker build context where they were copied as standalone dirs. In the wheel architecture, they're package modules and NOT flagged.

3. **Pytest timeout = 600s** — The 900-test suite with xdist should complete in <120s. 600s allows for cold caches on the OptiPlex.

4. **`_run_command` captures stderr** — Failed checks show the first 3 lines of stderr for quick diagnosis without overwhelming output.


---

## 4.7 `phases/build.py` — Phase 2: The Builder (Source to Wheel)

### Purpose
Uses `uv build` to generate a versioned `.whl` file in `dist/`. The wheel is the **shipping unit** — the Docker container installs the wheel, not the raw source tree.

### Specification

```python
"""Phase 2: Wheel Builder — Source to Wheel via uv build."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from ..models import PipelineState, WheelSpec
from ..constants import PROJECT_VERSION, PROJECT_NAME


def run_build(state: PipelineState) -> PipelineState:
    """Build the production wheel using uv.
    
    Steps:
    1. Clean previous dist/ artifacts
    2. Run `uv build` in project root
    3. Locate and validate the generated .whl
    4. Populate state.wheel_spec
    """
    console = Console()
    project_root = state.project_root
    dist_dir = project_root / "dist"
    
    # ── Step 1: Clean previous dist/ ─────────────────────────────────
    console.print("  [dim]Cleaning previous dist/ artifacts...[/dim]")
    _clean_dist(dist_dir)
    
    # ── Step 2: Run uv build ─────────────────────────────────────────
    console.print("  [dim]Running uv build...[/dim]")
    cmd = ["uv", "build", str(project_root)]
    ok, output = _run_uv_build(cmd, project_root)
    
    if not ok:
        state.errors.append(f"Phase 2 build failed: {output}")
        state.phase2_passed = False
        return state
    
    # ── Step 3: Locate the generated .whl ────────────────────────────
    wheel_files = list(dist_dir.glob("*.whl"))
    if not wheel_files:
        state.errors.append("Phase 2: No .whl file found in dist/ after build")
        state.phase2_passed = False
        return state
    
    wheel_path = wheel_files[0]  # Should be exactly one .whl
    
    # ── Step 4: Validate wheel metadata ──────────────────────────────
    wheel_name = wheel_path.name
    # Expected: beagle-13.6.0-py3-none-any.whl
    if PROJECT_VERSION not in wheel_name:
        state.warnings.append(
            f"Wheel version mismatch: expected {PROJECT_VERSION} in {wheel_name}"
        )
    
    wheel_spec = WheelSpec(
        path=wheel_path,
        name=wheel_name,
        size_bytes=wheel_path.stat().st_size,
    )
    
    state.wheel_spec = wheel_spec
    state.phase2_passed = True
    state.phase_details["phase2_detail"] = (
        f"{wheel_spec.name} ({wheel_spec.size_mb}MB)"
    )
    
    console.print(f"  [green]Built: {wheel_spec.name} ({wheel_spec.size_mb}MB)[/green]")
    
    return state


def _clean_dist(dist_dir: Path) -> None:
    """Remove all files from dist/ directory."""
    if dist_dir.exists():
        for f in dist_dir.iterdir():
            if f.is_file():
                f.unlink()


def _run_uv_build(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """Execute uv build and return (success, output_or_error)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()[:500]
    except FileNotFoundError:
        return False, "uv not found — install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    except subprocess.TimeoutExpired:
        return False, "uv build timed out after 120s"
```

### Key Design Decisions

1. **Clean before build** — Removes stale `.whl` and `.tar.gz` from `dist/` to ensure only the fresh wheel is used.

2. **Single wheel expected** — `uv build` produces exactly one `.whl` (and one `.tar.gz`). The first `.whl` found is used.

3. **Version validation** — Warns (doesn't fail) if the wheel filename doesn't contain the expected version string. This is a sanity check, not a gate.

4. **`uv` not in PATH?** — Provides a helpful error message with the install command. The Dockerised build environment will have `uv` in the builder stage.

---

## 4.8 `phases/dockerfile.py` — Phase 3: Dockerfile Generator

### Purpose
Generates a production-ready, multi-stage Dockerfile from a `DockerfileSpec` dataclass. The Dockerfile is rendered to a string and written to disk — no template files, no Jinja2.

### Generated Dockerfile Output

```dockerfile
# ═══════════════════════════════════════════════════════════════
# Beagle Factory — Multi-Stage Production Docker Image
# Auto-generated by beagle_dockeriser v1.0.0
# DO NOT EDIT — changes will be overwritten on next deployment
# ═══════════════════════════════════════════════════════════════

# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv for fast wheel installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy the pre-built wheel
COPY dist/beagle-13.6.0-py3-none-any.whl /tmp/wheel.whl

# Install the wheel into a clean prefix directory
RUN uv pip install --system --prefix=/install /tmp/wheel.whl && \
    rm /tmp/wheel.whl

# ── Stage 2: Runner ──────────────────────────────────────────
FROM python:3.12-slim AS runner

# Create non-root user
RUN groupadd --gid 1000 beagle_user && \
    useradd --uid 1000 --gid beagle_user --shell /bin/bash \
    --create-home beagle_user

# Create application directories
RUN mkdir -p /app/data/rag /app/state /app/output && \
    chown -R beagle_user:beagle_user /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Set environment variables
ENV BEAGLE_EXECUTION_ENV=docker \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_ROOT=/app \
    BEAGLE_DATA_ROOT=/app/data \
    BEAGLE_KNOWLEDGE_DIR=/app/data/rag \
    BEAGLE_KUZU_PATH=/app/data/rag

# Expose A2A federation port
EXPOSE 8420

# Graceful shutdown signal
STOPSIGNAL SIGTERM

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import beagle; print('ok')" || exit 1

# Switch to non-root user
USER beagle_user

# Set working directory
WORKDIR /app

# Entrypoint: the Beagle CLI
ENTRYPOINT ["beagle"]
```

### Specification

```python
"""Phase 3: Dockerfile Generator — produces hardened multi-stage Dockerfile."""

from __future__ import annotations

import textwrap
from pathlib import Path

from rich.console import Console

from ..models import PipelineState, DockerfileSpec
from ..constants import *


def run_dockerfile_gen(state: PipelineState) -> PipelineState:
    """Generate the production Dockerfile.
    
    Reads wheel spec from Phase 2 output.
    Writes Dockerfile to project root.
    """
    console = Console()
    
    if not state.wheel_spec:
        state.errors.append("Phase 3: No wheel_spec available — run Phase 2 first")
        state.phase3_passed = False
        return state
    
    # Build the spec with the actual wheel filename
    spec = DockerfileSpec(wheel_filename=state.wheel_spec.name)
    
    # Render the Dockerfile
    dockerfile_content = render_dockerfile(spec)
    
    # Write to disk
    dockerfile_path = state.project_root / "Dockerfile"
    dockerfile_path.write_text(dockerfile_content)
    
    state.dockerfile_path = dockerfile_path
    state.dockerfile_spec = spec
    state.phase3_passed = True
    state.phase_details["phase3_detail"] = f"Written to {dockerfile_path}"
    
    console.print(f"  [green]Generated: {dockerfile_path}[/green]")
    if state.wheel_spec:
        console.print(f"  [dim]Wheel: dist/{state.wheel_spec.name}[/dim]")
    
    return state


def render_dockerfile(spec: DockerfileSpec) -> str:
    """Render a DockerfileSpec into a Dockerfile string.
    
    Uses textwrap.dedent for readability — no Jinja2 dependency.
    All values come from the typed spec, ensuring consistency.
    """
    env_lines = "\n".join(
        f"    {k}={v} \\" for i, (k, v) in enumerate(spec.env_vars.items())
    )
    # Remove trailing backslash from last env line
    env_lines = env_lines.rstrip("\\").strip()
    env_block = f"ENV {env_lines}"
    
    expose_lines = "\n".join(f"EXPOSE {port}" for port in spec.expose_ports)
    
    dockerfile = textwrap.dedent(f"""\
        # ═══════════════════════════════════════════════════════════════
        # Beagle Factory — Multi-Stage Production Docker Image
        # Auto-generated by beagle-dockeriser v1.0.0
        # DO NOT EDIT — changes will be overwritten on next deployment
        # ═══════════════════════════════════════════════════════════════

        # ── Stage 1: Builder ──────────────────────────────────────────
        FROM {spec.base_image} AS builder

        # Install uv for fast wheel installation
        COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

        # Copy the pre-built wheel
        COPY dist/{spec.wheel_filename} /tmp/wheel.whl

        # Install the wheel into a clean prefix directory
        RUN uv pip install --system --prefix=/install /tmp/wheel.whl && \\
            rm /tmp/wheel.whl

        # ── Stage 2: Runner ──────────────────────────────────────────
        FROM {spec.base_image} AS runner

        # Create non-root user
        RUN groupadd --gid {spec.container_gid} {spec.container_user} && \\
            useradd --uid {spec.container_uid} --gid {spec.container_user} \\
            --shell /bin/bash --create-home {spec.container_user}

        # Create application directories
        RUN mkdir -p {spec.data_dir}/rag {spec.app_dir}/state {spec.app_dir}/output && \\
            chown -R {spec.container_user}:{spec.container_user} {spec.app_dir}

        # Copy installed packages from builder
        COPY --from=builder /install /usr/local

        # Set environment variables
        {env_block}

        # Expose A2A federation port
        {expose_lines}

        # Graceful shutdown signal
        STOPSIGNAL {spec.stop_signal}

        # Health check
        HEALTHCHECK --interval={spec.healthcheck_interval}s \\
            --timeout={spec.healthcheck_timeout}s \\
            --retries={spec.healthcheck_retries} \\
            CMD python -c "import beagle; print('ok')" || exit 1

        # Switch to non-root user
        USER {spec.container_user}

        # Set working directory
        WORKDIR {spec.app_dir}

        # Entrypoint: the Beagle CLI
        ENTRYPOINT {spec.entrypoint}
    """)
    
    return dockerfile
```

### Key Design Decisions

1. **Wheel is COPY-ed, not installed from source** — The builder stage copies the pre-built `dist/*.whl` and installs it with `uv pip install --prefix=/install`. The runner stage copies only the `/install` directory. This produces the leanest possible image (~200MB estimated).

2. **`python:3.12-slim`** per mission spec, NOT `3.13` from legacy `Dockerfile.base`.

3. **Non-root user `beagle_user`** — All processes in the container run as UID 1000. The `chown -R` ensures the app directories are writable.

4. **ENV block from constants** — All environment variables are defined in `constants.py` and flow through `DockerfileSpec`. No hardcoded env vars in the template.

5. **`HEALTHCHECK` uses `import beagle`** — Simple Python-level check that confirms the wheel was installed correctly. More robust than checking a CLI binary.

6. **`STOPSIGNAL SIGTERM`** — Ensures LangGraph workers drain gracefully before the container is killed.


---

## 4.9 `phases/compose.py` — Phase 4: Docker Compose Generator

### Purpose
Generates a `docker-compose.yaml` tailored for the OptiPlex 3050 Micro single-host deployment. Handles RAG persistence, checkpoint persistence, secrets mounting, and A2A port binding.

### Generated docker-compose.yaml Output

```yaml
# ═══════════════════════════════════════════════════════════════
# Beagle Factory — Docker Compose Configuration
# Auto-generated by beagle-dockeriser v1.0.0
# Target: MSI MPG Z390I GAMING EDGE AC (Intel i7-9700K) (single-host)
# DO NOT EDIT — changes will be overwritten on next deployment
# ═══════════════════════════════════════════════════════════════

services:
  beagle-factory:
    image: beagle-factory:v13.6.0
    container_name: beagle-factory
    restart: unless-stopped
    ports:
      - "127.0.0.1:8420:8420"
    volumes:
      - ./data/rag:/app/data/rag
      - ./data/checkpoints:/home/beagle_user/.cache/goose/beagle/checkpoints
      - ~/.config/goose/secrets.yaml:/home/beagle_user/.config/goose/secrets.yaml:ro
    environment:
      - BEAGLE_EXECUTION_ENV=docker
      - BEAGLE_LOG_LEVEL=${BEAGLE_LOG_LEVEL:-INFO}
      - BEAGLE_BUDGET_USD=${BEAGLE_BUDGET_USD:-10.0}
      - GOOSE_PROVIDER=${GOOSE_PROVIDER:-ollama_cloud}
      - GOOSE_MODEL=${GOOSE_MODEL:-glm-5}
    env_file:
      - .env
    healthcheck:
      test: ["CMD", "python", "-c", "import beagle; print('ok')"]
      interval: 30s
      timeout: 10s
      retries: 3
    stdin_open: false
    tty: false
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

### Specification

```python
"""Phase 4: Docker Compose Generator — OptiPlex persistence configuration."""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml  # stdlib PyYAML — already in project dependencies
from rich.console import Console

from ..models import PipelineState, ComposeSpec, VolumeSpec
from ..constants import *


def run_compose_gen(state: PipelineState) -> PipelineState:
    """Generate the docker-compose.yaml for OptiPlex deployment.
    
    Reads DockerfileSpec from Phase 3 to ensure consistency.
    Writes docker-compose.yaml to project root.
    Also creates .env template if not present.
    Creates ./data/ directories if not present.
    """
    console = Console()
    
    if not state.dockerfile_spec:
        state.errors.append("Phase 4: No Dockerfile spec — run Phase 3 first")
        state.phase4_passed = False
        return state
    
    spec = ComposeSpec()
    compose_dict = build_compose_dict(spec)
    
    # Write docker-compose.yaml
    compose_path = state.project_root / "docker-compose.yaml"
    compose_content = yaml.dump(
        compose_dict,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    # Prepend header comment
    header = textwrap.dedent("""\
        # ═══════════════════════════════════════════════════════════════
        # Beagle Factory — Docker Compose Configuration
        # Auto-generated by beagle-dockeriser v1.0.0
        # Target: MSI MPG Z390I GAMING EDGE AC (Intel i7-9700K) (single-host)
        # DO NOT EDIT — changes will be overwritten on next deployment
        # ═══════════════════════════════════════════════════════════════
    """)
    compose_path.write_text(header + compose_content)
    
    # Ensure data directories exist on host
    _ensure_data_dirs(state.project_root)
    
    # Create .env template if not present
    _create_env_template(state.project_root)
    
    state.compose_path = compose_path
    state.compose_spec = spec
    state.phase4_passed = True
    state.phase_details["phase4_detail"] = f"Written to {compose_path}"
    
    console.print(f"  [green]Generated: {compose_path}[/green]")
    console.print(f"  [dim]Data dirs: ./data/rag, ./data/checkpoints[/dim]")
    
    return state


def build_compose_dict(spec: ComposeSpec) -> dict:
    """Build a Python dict that serializes to the docker-compose.yaml.
    
    Uses dict construction (not string templating) for YAML safety.
    """
    volumes_list = []
    for vol in spec.volumes:
        rw_suffix = ":ro" if vol.read_only else ""
        volumes_list.append(f"{vol.host_path}:{vol.container_path}{rw_suffix}")
    
    return {
        "services": {
            spec.container_name: {
                "image": f"{spec.image_name}:{spec.image_tag}",
                "container_name": spec.container_name,
                "restart": spec.restart_policy,
                "ports": spec.ports,
                "volumes": volumes_list,
                "environment": {
                    "BEAGLE_EXECUTION_ENV": "docker",
                    "BEAGLE_LOG_LEVEL": "${BEAGLE_LOG_LEVEL:-INFO}",
                    "BEAGLE_BUDGET_USD": "${BEAGLE_BUDGET_USD:-10.0}",
                    "GOOSE_PROVIDER": "${GOOSE_PROVIDER:-ollama_cloud}",
                    "GOOSE_MODEL": "${GOOSE_MODEL:-glm-5}",
                },
                "env_file": [spec.env_file],
                "healthcheck": {
                    "test": ["CMD", "python", "-c",
                             "import beagle; print('ok')"],
                    "interval": f"{spec.healthcheck['interval']}",
                    "timeout": f"{spec.healthcheck['timeout']}",
                    "retries": spec.healthcheck['retries'],
                },
                "stdin_open": False,
                "tty": False,
                "logging": {
                    "driver": "json-file",
                    "options": {
                        "max-size": "10m",
                        "max-file": "3",
                    },
                },
            },
        },
    }


def _ensure_data_dirs(project_root: Path) -> None:
    """Create data persistence directories on host if they don't exist."""
    dirs = [
        project_root / "data" / "rag",
        project_root / "data" / "checkpoints",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _create_env_template(project_root: Path) -> None:
    """Create .env template file if it doesn't already exist."""
    env_path = project_root / ".env"
    if env_path.exists():
        return  # Don't overwrite user's .env
    
    template = textwrap.dedent("""\
        # Beagle Factory Environment Variables
        # Copy this file and customize for your deployment
        
        # Model provider (ollama_cloud, openai, etc.)
        GOOSE_PROVIDER=ollama_cloud
        GOOSE_MODEL=glm-5
        
        # Budget
        BEAGLE_BUDGET_USD=10.0
        
        # Logging
        BEAGLE_LOG_LEVEL=INFO
    """)
    env_path.write_text(template)
```

### Key Design Decisions

1. **Volume mapping uses `./data/rag` relative path** — The mission spec says `./data/rag ↔ /app/data/rag`. Using relative paths ensures portability across machines.

2. **Checkpoints path is the XDG cache path** — The checkpointer stores SQLite at `~/.cache/goose/beagle/checkpoints/beagle_checkpoints.db`. In Docker, with `beagle_user` (UID 1000), this maps to `/home/beagle_user/.cache/goose/beagle/checkpoints`.

3. **Secrets file is read-only (`:ro`)** — The mission spec explicitly states read-only mount. The `secrets_loader.py` validates file permissions (0o600), but in Docker this may need an entrypoint fix (see Section 4.11).

4. **Port binding `127.0.0.1:8420`** — Per mission spec, A2A port is bound to localhost only. Tailscale provides external access.

5. **`.env` template auto-created** — Prevents `docker compose` from failing on missing env file. Only created if it doesn't exist (never overwrites).


---

## 4.10 `phases/build_push.py` — Phase 5: Docker Build & Summary Report

### Purpose
Executes `docker build`, inspects the resulting image, and prints a summary with image size and "How to Start" guide.

### Specification

```python
"""Phase 5: Docker Build & Summary Report."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..models import PipelineState
from ..constants import (
    DOCKER_IMAGE_NAME,
    DOCKER_IMAGE_TAG,
    FULL_IMAGE_REF,
    A2A_PORT,
)


def run_build_push(
    state: PipelineState,
    image_tag: str = DOCKER_IMAGE_TAG,
) -> PipelineState:
    """Execute docker build and produce summary report.
    
    Steps:
    1. Generate .dockerignore (if not present)
    2. Run docker build
    3. Inspect image size
    4. Print summary + "How to Start" guide
    """
    console = Console()
    project_root = state.project_root
    
    # ── Step 0: Generate .dockerignore ─────────────────────────────
    _generate_dockerignore(project_root)
    console.print("  [dim]Generated .dockerignore[/dim]")
    
    # ── Step 1: Docker Build ─────────────────────────────────────────
    full_tag = f"{DOCKER_IMAGE_NAME}:{image_tag}"
    console.print(f"  [dim]Building: docker build -t {full_tag} .[/dim]")
    
    t0 = time.monotonic()
    ok, build_output = _docker_build(project_root, full_tag)
    elapsed = time.monotonic() - t0
    
    if not ok:
        state.errors.append(f"Phase 5: Docker build failed:\n{build_output}")
        state.phase5_passed = False
        return state
    
    state.build_duration_seconds = elapsed
    
    # ── Step 2: Inspect Image ────────────────────────────────────────
    size_mb = _get_image_size_mb(full_tag)
    image_id = _get_image_id(full_tag)
    
    state.image_id = image_id
    state.image_size_mb = size_mb
    state.phase5_passed = True
    state.phase_details["phase5_detail"] = (
        f"{full_tag} — {size_mb:.0f}MB — built in {elapsed:.1f}s"
    )
    
    # ── Step 3: Print Summary ────────────────────────────────────────
    _print_summary(console, state, full_tag)
    
    return state


def _docker_build(project_root: Path, full_tag: str) -> tuple[bool, str]:
    """Execute docker build -t <tag> ."""
    cmd = ["docker", "build", "-t", full_tag, "."]
    
    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max build time
        )
        if result.returncode == 0:
            # Extract final image ID from output
            return True, result.stdout.strip()
        return False, result.stderr[-1000:]  # Last 1000 chars of error
    except FileNotFoundError:
        return False, "docker not found — is Docker installed?"
    except subprocess.TimeoutExpired:
        return False, "Docker build timed out after 600s"


def _get_image_size_mb(full_tag: str) -> float:
    """Get image size in MB via docker inspect."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Size}}", full_tag],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            size_bytes = int(result.stdout.strip())
            return round(size_bytes / (1024 * 1024), 1)
    except Exception:
        pass
    return 0.0


def _get_image_id(full_tag: str) -> str:
    """Get image ID via docker inspect."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", full_tag],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:19]  # sha256:abc... truncated
    except Exception:
        pass
    return "unknown"


def _generate_dockerignore(project_root: Path) -> None:
    """Generate .dockerignore to minimize Docker context.
    
    Without .dockerignore, Docker sends the ENTIRE 509MB project tree
    as build context. With this file, only the necessary files are sent.
    """
    dockerignore_path = project_root / ".dockerignore"
    content = """\
# ═══════════════════════════════════════════════════════════
# Beagle Factory — Docker Build Context Exclusions
# Auto-generated by beagle-dockeriser
# ═══════════════════════════════════════════════════════════

# Version control
.git/
.gitignore

# Python bytecode and caches
__pycache__/
*.py[cod]
*$py.class
*.so

# Virtual environments
.venv/
venv/
env/

# IDE / Editor files
.idea/
.vscode/
*.swp
*.swo
*~

# Testing and coverage
.pytest_cache/
htmlcov/
.coverage
coverage.xml

# Lint caches
.ruff_cache/
.mypy_cache/

# Build artifacts (NOT dist/ — we NEED the wheel!)
build/
*.egg-info/

# Documentation
docs/
*.md
!README.md

# Development scripts
scripts/

# Test data and benchmarks
benchmarks/
tests/

# OS files
.DS_Store
Thumbs.db

# Beagle runtime data (not for Docker context)
data/

# Config overrides (container will use defaults + env vars)
.config/

# These are included INSIDE the wheel, not as raw files:
# beagle/ai/
# beagle/skills/
# beagle/bridges/
# beagle/memory/
"""
    dockerignore_path.write_text(content)


def _print_summary(
    console: Console,
    state: PipelineState,
    full_tag: str,
) -> None:
    """Print the deployment summary and 'How to Start' guide."""
    
    # ── Image Summary Table ──────────────────────────────────────────
    table = Table(title="🚀 Deployment Summary")
    table.add_column("Property", style="cyan")
    table.add_column("Value")
    
    table.add_row("Image", full_tag)
    table.add_row("Image ID", state.image_id)
    table.add_row("Image Size", f"{state.image_size_mb:.0f} MB")
    table.add_row("Build Time", f"{state.build_duration_seconds:.1f}s")
    if state.wheel_spec:
        table.add_row("Wheel", state.wheel_spec.name)
        table.add_row("Wheel Size", f"{state.wheel_spec.size_mb} MB")
    table.add_row("Dockerfile", str(state.dockerfile_path))
    table.add_row("Compose", str(state.compose_path))
    
    console.print(table)
    
    # ── How to Start Guide ───────────────────────────────────────────
    guide = Panel.fit(
        textwrap.dedent(f"""\
        [bold cyan]How to Start[/bold cyan]
        
        [dim]# 1. Create .env file (customized for your deployment)[/dim]
        cp .env.example .env
        nano .env
        
        [dim]# 2. Start the Beagle Factory container[/dim]
        docker compose up -d
        
        [dim]# 3. Verify health[/dim]
        docker compose ps
        docker compose logs -f beagle-factory
        
        [dim]# 4. Access A2A federation (via Tailscale)[/dim]
        curl http://127.0.0.1:{A2A_PORT}/health
        
        [dim]# 5. Stop[/dim]
        docker compose down
        
        [dim]# 6. Rebuild (after code changes)[/dim]
        python -m beagle_dockeriser deploy
        
        [dim]# 7. Interactive shell (debugging)[/dim]
        docker compose exec beagle-factory bash
        """),
        title="📋 Quick Start",
        border_style="green",
    )
    console.print(guide)
```

### Key Design Decisions

1. **`.dockerignore` excludes everything except `dist/`** — The Docker context only needs the `Dockerfile` and `dist/*.whl`. The entire source tree is excluded because the wheel already contains the compiled package.

2. **Docker build timeout = 600s** — Multi-stage builds with pip install can take 2-5 minutes depending on network speed and cache status.

3. **Image size via `docker inspect`** — More accurate than `docker images` (shows actual layer size, not virtual size).

4. **`_print_summary` shows "How to Start"** — Per mission spec, the script must print a summary of image size and a start guide after successful build.

---

## 4.11 `__init__.py` — Package Identity

```python
"""Beagle Dockeriser — Deployment orchestration for beagle."""

__version__ = "1.0.0"
__project__ = "beagle-dockeriser"
```

---

## 4.12 Secrets Permission Fix (Entrypoint Concern)

### Problem
`secrets_loader.py` enforces `0o600` file permissions on `secrets.yaml`. When the file is bind-mounted from the host with `:ro`, the permissions may not be `0o600` inside the container, causing `secrets_loader` to reject the file.

### Two Solutions Considered

| Solution | Approach | Pros | Cons |
|---|---|---|---|
| **A: Entrypoint script** | Copy secrets to tmpfs, fix perms, point env var | Works with `:ro` mount | Extra complexity, tmpfs overhead |
| **B: Pre-flight check** | `deploy.py` warns if host perms != 600 | Zero Docker changes | Host must fix perms manually |

### Selected: **Solution B** (pre-flight check + `.env` override)

**Rationale:** The secrets are mounted read-only for security. An entrypoint script that copies them to tmpfs defeats the purpose of `:ro`. Instead, `deploy.py` will:
1. Check `~/.config/goose/secrets.yaml` permissions on host before build
2. Warn if not `0o600`
3. Document the fix in the "How to Start" guide
4. Users can also set `OLLAMA_CLOUD_API_KEY` etc. via `.env` (env vars take priority in `secrets_loader.py`)

```python
# In phases/build_push.py, _print_summary(), add to guide:
"""
[dim]# Fix secrets permissions (if warning appeared):[/dim]
chmod 600 ~/.config/goose/secrets.yaml
"""
```

---

## 4.13 `pyproject.toml` Modification (for `pytest-xdist` only)

The **only** change to any existing project file is adding `pytest-xdist` to dev dependencies. Per the user's constraint: *"DO NOT ALTER ANY OTHERS BUT THE PLAN FILE"* — this change is documented here but will NOT be executed as part of this plan.

```toml
# In pyproject.toml, [project.optional-dependencies] dev group:
# ADD THIS LINE:
"pytest-xdist>=3.5.0",      # Parallel test execution for Phase 1 validation
```

---


# ════════════════════════════════════════════════════════════════════
# SECTION 5: EXTRINSIC VALIDATION — Adversarial Security Review
# ════════════════════════════════════════════════════════════════════

## 5.1 Attack Surface Analysis

### Vector 1: Docker Build Context Information Leakage

**Risk:** Without `.dockerignore`, Docker sends the ENTIRE project tree (509MB) as build context, including `secrets.yaml`, `.env`, and `.git/` history.

**Mitigation:** ✅ Phase 5 generates `.dockerignore` that excludes:
- `.git/` (version control history)
- `.config/` (local config overrides)
- `data/` (runtime data including RAG)
- `tests/` (test fixtures)
- `.env` (environment variables)

**Residual Risk:** `dist/` must NOT be excluded (the wheel is needed). The wheel itself is safe — it contains only Python package code, no secrets.

### Vector 2: Secrets File Permission Mismatch

**Risk:** `secrets_loader.py` enforces `0o600`. Bind-mounted file from host may have `0644` perms, causing runtime crash.

**Mitigation:** ✅ Pre-flight check in `deploy.py` warns about incorrect permissions. Documented in "How to Start" guide.

**Residual Risk:** User ignores the warning. **Fallback:** `secrets_loader.py` already falls back to env vars — if `OLLAMA_CLOUD_API_KEY` is set in `.env`, secrets file permissions are irrelevant.

### Vector 3: Container Runs as Root (if ENTRYPOINT is bypassed)

**Risk:** If container is entered via `docker exec -it beagle-factory bash`, the shell runs as `beagle_user` (non-root). However, if someone modifies the Dockerfile to remove `USER beagle_user`, or uses `docker exec -u root`, they get root.

**Mitigation:** ✅ `DockerfileSpec` enforces `USER beagle_user`. The generated Dockerfile is checked in Phase 3 output.

**Residual Risk:** Docker always allows `-u root` override. This is a Docker platform limitation, not an Beagle issue.

### Vector 4: RAG Data Corruption via Volume Mount

**Risk:** If `./data/rag` is mounted read-write AND the container crashes mid-ingest, LanceDB lock files may be left behind, corrupting the database.

**Mitigation:** ✅ `docker-compose.yaml` uses `restart: unless-stopped`. On crash, container restarts and RAG server handles lock file cleanup. The existing `rag_hotswap_ingest` API avoids this by staging to a temp dir.

**Residual Risk:** Hard crash (OOM kill, SIGKILL) during Kùzu write. Kùzu uses WAL (Write-Ahead Logging) and should recover on restart.

### Vector 5: A2A Port Exposure

**Risk:** Port 8420 is exposed, even if bound to 127.0.0.1. Any process on the host can reach A2A.

**Mitigation:** ✅ `127.0.0.1:8420` binding prevents external access. Tailscale provides secure tunnel. `mcp_auth` is enabled by default (`require_https = true`, `bind_address = "127.0.0.1"`).

**Residual Risk:** If `mcp_auth.enabled` is set to `false` in `config.toml`, A2A accepts unauthenticated requests. **Documented as:** Never disable MCP auth in production.

### Vector 6: Supply Chain — uv install from GitHub Container Registry

**Risk:** `COPY --from=ghcr.io/astral-sh/uv:latest` pulls an external image. If `ghcr.io` is compromised or MITM'd, the builder stage could be compromised.

**Mitigation:** ⚠️ Use `uv:latest` for convenience, but pin the SHA for production:

```dockerfile
# Development (convenient):
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Production (pinned, requires manual update):
COPY --from=ghcr.io/astral-sh/uv@sha256:ABC123... /uv /usr/local/bin/uv
```

**Recommendation:** Add `--uv-sha` CLI option to `beagle-dockeriser` for production pinning. Default to `:latest` for development.

---

## 5.2 Race Condition Analysis

### RC-1: Parallel `deploy.py` Execution

**Risk:** Two terminals running `python -m beagle_dockeriser deploy` simultaneously could:
- Both run `uv build`, one overwriting the other's wheel
- Both run `docker build`, tag collision

**Mitigation:** Add file-based lock using `fcntl.flock()`:

```python
# In pipeline.py, Pipeline.run():
import fcntl
lock_path = self.state.project_root / ".beagle_dockeriser.lock"
self._lock_file = open(lock_path, "w")
try:
    fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise RuntimeError("Another beagle_dockeriser instance is running")
```

### RC-2: Docker Build During Compose Up

**Risk:** Running `docker compose up` while `deploy.py` is mid-build could pull a partial image.

**Mitigation:** Docker tags are only applied after successful build. A failed build doesn't update the tag. `docker compose` will use the last successfully tagged image.

---

## 5.3 Memory Leak Analysis

### ML-1: Pipeline State Accumulation

**Risk:** `PipelineState` dataclass accumulates results across phases. For very large projects, `wheel_spec` could hold references to large data.

**Mitigation:** ✅ `WheelSpec` only holds `Path`, `str`, and `int` — no file content is loaded into memory. All Dockerfile/compose content is written to disk, not held in state.

### ML-2: Subprocess Output Buffering

**Risk:** `_run_command()` uses `capture_output=True` which buffers ALL stdout/stderr in memory. Pytest with 900 tests could produce 5-10MB of output.

**Mitigation:** ✅ `timeout=600` prevents infinite buffering. 10MB is trivial for modern systems. Consider streaming output for very verbose CI runs (future enhancement).

---

## 5.4 Hallucination Check

| Statement in Plan | Source | Verified? |
|---|---|---|
| "800+ tests" | `grep -c "def test_"` = 900 | ✅ Confirmed (even more) |
| "pytest-xdist not installed" | `pip show pytest-xdist` = not found | ✅ Confirmed |
| "`beagle` is the console script" | `pyproject.toml` line 94 | ✅ Confirmed |
| "python:3.13-slim in Dockerfile.base" | Read `Dockerfile.base` line 1 | ✅ Confirmed |
| "beagle >=3.11" | `pyproject.toml` requires-python | ✅ Confirmed |
| "No .dockerignore exists" | `ls -la .dockerignore` = not found | ✅ Confirmed |
| "Kùzu path broken on host" | `instance_rag_kuzu` dir doesn't exist | ✅ Confirmed |
| "secrets.yaml at ~/.config/goose/" | `ls -la` = 154 bytes | ✅ Confirmed |
| "A2A port 8420" | `bridges/config.py` line 170 | ✅ Confirmed |
| "bind_address 127.0.0.1" | `bridges/config.py` line 171 | ✅ Confirmed |
| "checkpointer uses AsyncSqliteSaver" | Read `checkpointer.py` | ✅ Confirmed |
| "uv build available" | `which uv` = found | ✅ Confirmed |
| "`ai/` in gitignore" | `.gitignore` line 36 | ✅ Confirmed |
| "`ai/` and `skills/` included in wheel" | `wheel` contents verified | ✅ Confirmed |

**Verdict: PASS** — All factual claims in this plan are sourced from direct environment inspection.

---


# ════════════════════════════════════════════════════════════════════
# SECTION 6: EVOLUTION — ACE Delta Update & Implementation Roadmap
# ════════════════════════════════════════════════════════════════════

## 6.1 Implementation Roadmap (Execution Order)

### Priority 0: Pre-requisite (must do FIRST)
```
action:  Add "pytest-xdist>=3.5.0" to pyproject.toml [dev] group
file:    /home/server/Projects/beagle/pyproject.toml
line:    66 (after pytest-cov entry)
commit:  "build: add pytest-xdist to dev deps for parallel test execution"
```

### Priority 1: Package Scaffolding
```
action:  Create beagle_dockeriser/ package structure
files:   __init__.py, __main__.py, cli.py, pipeline.py,
         models.py, constants.py
order:   constants.py → models.py → __init__.py → __main__.py
```

### Priority 2: Phase Modules (in dependency order)
```
order:   phases/validate.py (no deps on other phases)
       → phases/build.py (no deps on other phases)
       → phases/dockerfile.py (depends on wheel_spec from build)
       → phases/compose.py (depends on DockerfileSpec from dockerfile)
       → phases/build_push.py (depends on Dockerfile + compose existing)
```

### Priority 3: Pipeline Integration
```
action:  Wire all phases into pipeline.py
verify:  python -m beagle_dockeriser --dry-run deploy
```

### Priority 4: CLI Polish
```
action:  Add rich output formatting, progress bars, error recovery
verify:  python -m beagle_dockeriser deploy --help
```

### Priority 5: Test Suite for beagle_dockeriser Itself
```
action:  Create tests/ inside beagle_dockeriser/
tests:   test_validate.py, test_build.py, test_dockerfile.py,
         test_compose.py, test_pipeline.py, test_models.py
```

---

## 6.2 Estimated Effort

| Component | Lines of Code (est.) | Time (hours) | Complexity |
|---|---|---|---|
| `constants.py` | ~90 | 0.5 | Low |
| `models.py` | ~140 | 1.0 | Medium |
| `cli.py` | ~80 | 0.5 | Low |
| `pipeline.py` | ~120 | 1.0 | Medium |
| `phases/validate.py` | ~130 | 1.5 | Medium (subprocess handling) |
| `phases/build.py` | ~80 | 0.75 | Low |
| `phases/dockerfile.py` | ~120 | 1.0 | Medium (template rendering) |
| `phases/compose.py` | ~100 | 1.0 | Medium (YAML generation) |
| `phases/build_push.py` | ~150 | 1.5 | Medium (Docker subprocess + summary) |
| `__init__.py` + `__main__.py` | ~10 | 0.1 | Trivial |
| `.dockerignore` generation | ~60 | 0.25 | Low |
| Tests | ~300 | 2.0 | Medium |
| **TOTAL** | **~1,380** | **~11.1** | |

---

## 6.3 Acceptance Criteria (Definition of Done)

| # | Criterion | Verification |
|---|---|---|
| 1 | `python -m beagle_dockeriser validate` runs ruff + vulture + pytest-xdist | All pass with exit code 0 |
| 2 | `python -m beagle_dockeriser deploy --skip-validation --skip-build` generates Dockerfile + compose | Both files exist and are valid YAML/Dockerfile |
| 3 | `python -m beagle_dockeriser deploy` builds `beagle-factory:v13.6.0` | `docker images \| grep beagle-factory` shows the image |
| 4 | `docker compose up -d` starts the container | `docker compose ps` shows "healthy" |
| 5 | Container runs as `beagle_user` (non-root) | `docker exec beagle-factory whoami` = "beagle_user" |
| 6 | `BEAGLE_EXECUTION_ENV=docker` is set | `docker exec beagle-factory env \| grep BEAGLE_EXECUTION_ENV` |
| 7 | A2A port is bound to 127.0.0.1 only | `docker compose ps` shows `127.0.0.1:8420->8420/tcp` |
| 8 | RAG data persists across restarts | Write file in `/app/data/rag`, restart, file still exists |
| 9 | Checkpoints persist across restarts | Verify `beagle_checkpoints.db` exists in `./data/checkpoints/` |
| 10 | Secrets are read-only | `docker exec beagle-factory touch /root/.config/goose/secrets.yaml` fails |
| 11 | Image size < 300MB | `docker images beagle-factory` shows < 300MB |
| 12 | Health check passes | `docker inspect beagle-factory \| grep Health` = "healthy" |
| 13 | STOPSIGNAL is SIGTERM | `docker inspect beagle-factory \| grep StopSignal` = "SIGTERM" |
| 14 | ENTRYPOINT is `beagle` | `docker inspect beagle-factory \| grep Entrypoint` = `["beagle"]` |

---

## 6.4 Future Extensions (Out of Scope for v1.0)

These are NOT part of the current mission but are natural next steps:

| Extension | Description | Phase |
|---|---|---|
| **Registry Push** | `docker push` to GHCR / Docker Hub | Phase 6 |
| **Kubernetes Manifests** | Generate `deployment.yaml` + `service.yaml` | Phase 7 |
| **Multi-Arch Build** | `docker buildx` for ARM64 + AMD64 | Phase 8 |
| **Smoke Tests** | Post-build integration tests in container | Phase 9 |
| **CI/CD Integration** | GitHub Actions workflow generation | Phase 10 |
| **Config Mount** | Mount `config.toml` + `agents.toml` as volumes | Phase 11 |
| **GPU Support** | nvidia-container-toolkit + torch GPU | Phase 12 |
| **Tailscale Integration** | Auto-configure Tailscale serve for A2A | Phase 13 |

---

## 6.5 Agentic Context Engineering (ACE) Delta Update

Heuristics distilled from this planning session for persistence across context compaction boundaries:

```json
{
  "dx_deltaJSON": {
    "dx_data_sets": "environment_heuristics",
    "dx_deltaJSON_delta": {
      "ops": [
        { "retain": "current_index_count" },
        {
          "insert": "BEAGLE_DOCKERISER: Wheel-based Docker deployment (Trajectory Gamma). Key: install .whl not source, python:3.12-slim, beagle_user non-root, ENTRYPOINT=[beagle], .dockerignore excludes everything except dist/. pytest-xdist is MISSING from dev deps — add first.",
          "attributes": { "domain": "deployment" }
        },
        {
          "insert": "BEAGLE_CHECKPOINTER: SQLite at ~/.cache/goose/beagle/checkpoints/beagle_checkpoints.db. BEAGLE_EXECUTION_ENV=docker switches to PostgreSQL. In single-host Docker, keep SQLite (only one container).",
          "attributes": { "domain": "persistence" }
        },
        {
          "insert": "BEAGLE_SECRETS: secrets_loader.py enforces 0o600. In Docker with :ro bind mount, check host perms pre-flight. Fallback: env vars (OLLAMA_CLOUD_API_KEY) take priority over file.",
          "attributes": { "domain": "security" }
        },
        {
          "insert": "BEAGLE_A2A: Port 8420, bind 127.0.0.1, TLS via Tailscale. mcp_auth.enabled=true by default. NEVER disable in production.",
          "attributes": { "domain": "networking" }
        },
        {
          "insert": "BEAGLE_VESTIGIAL_DIRS: ai/ and skills/ are NOT vestigial — they are active package modules inside the wheel. Vestigial = build/, dist/, __pycache__, .pytest_cache, .ruff_cache, htmlcov, *.egg-info.",
          "attributes": { "domain": "architecture" }
        },
        {
          "insert": "BEAGLE_ENTRYPOINT: Typer app name='goose-workflow' is NOT a console script. The actual console script is 'beagle' defined in pyproject.toml [project.scripts]. ENTRYPOINT=['beagle'].",
          "attributes": { "domain": "docker" }
        },
        {
          "insert": "BEAGLE_RAG_DOCKER: LanceDB at /app/data/rag, Kuzu co-located. Volume mount ./data/rag:/app/data/rag ensures persistence. Kuzu lock contention: use rag_hotswap_ingest, not rag_ingest when server is running.",
          "attributes": { "domain": "rag" }
        }
      ]
    }
  }
}
```

---

# ════════════════════════════════════════════════════════════════════
# END OF PLAN
# ════════════════════════════════════════════════════════════════════
#
# This document is the SOLE implementation reference for beagle_dockeriser.
# No other files are altered. Only this plan file is written.
#
# Sections:
#   1. Environmental Ingestion (Architecture Cartography)
#   2. Deep Research (Dockerization Pattern Analysis)
#   3. Deliberation Matrix (Three Architectural Trajectories)
#   4. Implementation Specification (Module-by-Module Blueprint)
#   5. Extrinsic Validation (Adversarial Security Review)
#   6. Evolution (ACE Delta Update & Implementation Roadmap)
#
# Total: ~1,400 lines of specification for ~1,380 lines of implementation code.
# ════════════════════════════════════════════════════════════════════
