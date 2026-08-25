# beagle-dockeriser

> **Containerise the Beagle agentic workflow system with a single command.**

`beagle-dockeriser` is a multi-phase CI/CD pipeline that takes the [Beagle](https://github.com/MattCreigh/beagle) source code and produces a production-ready Docker image. It validates code quality, builds a Python wheel, auto-generates a multi-stage `Dockerfile` and `docker-compose.yaml`, and optionally builds and tags the image—all in one pass.

---

## Table of Contents

- [What This Does](#what-this-does)
- [Quick Start](#quick-start)
- [Pipeline Phases](#pipeline-phases)
- [Installation](#installation)
- [Usage](#usage)
  - [Full Deployment](#full-deployment)
  - [Generate Only (Skip Build)](#generate-only-skip-build)
  - [Validate Only](#validate-only)
  - [Status Check](#status-check)
- [Generated Artifacts](#generated-artifacts)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Docker Compose Reference](#docker-compose-reference)
- [Health Checks](#health-checks)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)
- [License](#license)

---

## What This Does

You have the Beagle codebase. You want it running in Docker. `beagle-dockeriser` automates every step:

| Step | What Happens |
|------|-------------|
| **Validate** | Runs `ruff`, `vulture`, and `pytest` against the source. Catches lint, dead code, and test failures before they reach the container. |
| **Build wheel** | Uses `uv build` to produce a clean `.whl` file from the source. |
| **Generate Dockerfile** | Produces a **multi-stage** Dockerfile: a builder stage installs the wheel, and a slim runner stage copies only what's needed. |
| **Generate compose** | Produces a `docker-compose.yaml` pre-configured with volume mounts, health checks, port binding, and logging. |
| **Build image** | Runs `docker build` against the generated Dockerfile (optional). |
| **.dockerignore** | Auto-generates a `.dockerignore` so secrets, venvs, and `.git` are never sent to the Docker daemon. |

The result: a < 200 MB production image running a **non-root user**, ready to deploy on any Docker host.

---

## Quick Start

```bash
# 1. Navigate to the containerisation directory
cd beagle_containerisation

# 2. Deploy — this runs the full pipeline
python3 -m beagle_dockeriser deploy

# 3. Start the container
docker compose -f docker-compose.yaml up -d

# 4. Verify
docker ps --filter name=beagle-factory
curl http://127.0.0.1:8420/health
```

That's it. The deploy command handles everything from validation to image build.

---

## Pipeline Phases

`beagle-dockeriser` runs through five sequential phases. Each phase can also be run independently.

### Phase 1 — Validate

```bash
python3 -m beagle_dockeriser validate
```

- Runs `ruff check` (linting)
- Runs `vulture` (dead code detection)
- Runs `pytest -n auto` (full test suite with parallel execution)
- Scans for vestigial files (leftover build artifacts)

If any check fails, the pipeline stops. You must fix issues before the image is built.

### Phase 2 — Build Wheel

```bash
python3 -m beagle_dockeriser generate --skip-validate
```

- Cleans `dist/`
- Runs `uv build` to produce a wheel
- Locates and validates the `.whl` file

### Phase 3 — Generate Dockerfile

Produces a multi-stage Dockerfile:

- **Stage 1 (Builder)**: Copies the wheel, installs it into `/install` using `uv pip install`
- **Stage 2 (Runner)**: Copies installed packages from builder, creates non-root user, sets environment, exposes port, configures health check

### Phase 4 — Generate Compose

Produces `docker-compose.yaml` with:

- Single service `beagle-factory`
- Volume mounts for RAG data, checkpoints, and secrets
- Port binding on `127.0.0.1:8420`
- JSON-file logging with rotation
- Health check integration

### Phase 5 — Build Image (optional)

```bash
python3 -m beagle_dockeriser deploy
```

Runs `docker build -t beagle-factory:v{VERSION} .` against the generated Dockerfile. If you only want the artifacts without building, use `generate` instead of `deploy`.

---

## Installation

### Prerequisites

- **Python 3.12+**
- **uv** — install via `brew install uv` or `pip install uv`
- **Docker** — for building and running images
- **git** — for source access

The dockeriser tool is part of the containerisation directory. No separate installation needed:

```bash
git clone https://github.com/MattCreigh/beagle-dockeriser.git
# or: it lives inside your Beagle checkout at beagle_containerisation/
cd beagle-dockeriser
```

### Verify

```bash
python3 -m beagle_dockeriser status
```

Expected output: project version, paths, prerequisites, and phase states.

---

## Usage

### Full Deployment

Runs all five phases in sequence:

```bash
python3 -m beagle_dockeriser deploy
```

### Generate Only (Skip Build)

Creates Dockerfile, docker-compose.yaml, and .dockerignore without building the image:

```bash
python3 -m beagle_dockeriser generate
```

### Validate Only

Runs the quality gate without touching any artifacts:

```bash
python3 -m beagle_dockeriser validate
```

### Status Check

```bash
python3 -m beagle_dockeriser status
```

Displays:

- Project version and identity
- Paths (source, dist, output)
- Prerequisites found or missing
- Which artifacts exist and their sizes

---

## Generated Artifacts

After running `generate` or `deploy`, you'll have:

```
beagle_containerisation/
├── Dockerfile              ← Multi-stage production Dockerfile
├── docker-compose.yaml     ← Single-service compose config
├── .dockerignore           ← Prevents secrets/git from entering build context
└── beagle_dockeriser/        ← The tool itself
```

The `Dockerfile` uses:

- **Base image:** `python:3.12-slim`
- **User:** `beagle_user` (UID 1000, non-root)
- **Entrypoint:** `beagle` (the Beagle CLI)
- **Stop signal:** `SIGTERM`
- **Exposed port:** `8420` (A2A federation)
- **Health check:** Python import of `beagle`

---

## Configuration

Constants are defined in `beagle_dockeriser/constants.py`. Key values:

| Constant | Default | Purpose |
|----------|---------|---------|
| `PROJECT_NAME` | `beagle` | Package name |
| `PROJECT_VERSION` | From pyproject.toml | Tagged version |
| `DOCKER_BASE_IMAGE` | `python:3.12-slim` | Runner base |
| `CONTAINER_PORT` | `8420` | A2A federation port |
| `CONTAINER_USER` | `beagle_user` | Non-root user |
| `ENTRYPOINT` | `["beagle"]` | Container command |

Environment variables set inside the container:

| Variable | Value | Meaning |
|----------|-------|---------|
| `BEAGLE_EXECUTION_ENV` | `docker` | Tells Beagle it's inside a container |
| `PYTHONDONTWRITEBYTECODE` | `1` | No `.pyc` files |
| `PYTHONUNBUFFERED` | `1` | Real-time log output |
| `WORKSPACE_ROOT` | `/app` | Application root |
| `BEAGLE_DATA_ROOT` | `/app/data` | Data volume root |

---

## How It Works

```
                    ┌──────────────┐
                    │   You type   │
                    │  "deploy"    │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  PHASE 1     │
                    │  Validate    │──── ruff, vulture, pytest
                    └──────┬───────┘
                           │ PASS
                    ┌──────▼───────┐
                    │  PHASE 2     │
                    │  Build Wheel │──── uv build → dist/*.whl
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  PHASE 3     │
                    │  Dockerfile  │──── Multi-stage Dockerfile
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  PHASE 4     │
                    │  Compose     │──── docker-compose.yaml
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  PHASE 5     │
                    │  Build Image │──── docker build
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   DONE ✓     │
                    │  Image ready │
                    └──────────────┘
```

The orchestrator (`pipeline.py`) runs phases sequentially. Each phase receives a `PipelineState` object and returns it mutated. If any phase fails, the pipeline stops and reports errors.

---

## Docker Compose Reference

The generated `docker-compose.yaml`:

```yaml
services:
  beagle-factory:
    image: beagle-factory:v13.14.7
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
      - GOOSE_PROVIDER=${GOOSE_PROVIDER:-ollama_cloud}
      - GOOSE_MODEL=${GOOSE_MODEL:-glm-5}
    env_file:
      - .env
```

**Volume mounts explained:**

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `./data/rag/` | `/app/data/rag` | RAG vector & graph databases |
| `./data/checkpoints/` | `/home/beagle_user/.cache/...` | Agent checkpoints |
| `~/.config/goose/secrets.yaml:ro` | `/home/beagle_user/.config/...` | API keys, read-only |

---

## Health Checks

The container includes a built-in health check:

```
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import beagle; print('ok')" || exit 1
```

Docker Compose also provides an external health check via the A2A port.

---

## Troubleshooting

### "Module not found: beagle_dockeriser"

Set `PYTHONPATH` to include the containerisation directory:

```bash
export PYTHONPATH="$(pwd):$PYTHONPATH"
python3 -m beagle_dockeriser deploy
```

### "uv: command not found"

Install uv:

```bash
brew install uv
# or
pip install uv
```

### "docker: command not found"

Docker must be installed and the daemon running:

```bash
docker info
```

### Validation fails

If ruff, vulture, or pytest fail:

1. Check the error output
2. Fix the issues in the source code
3. Re-run `python3 -m beagle_dockeriser validate`

Common issues:

- **ruff**: Run `ruff check --fix` to auto-correct
- **vulture**: Remove or mark false-positive dead code
- **pytest**: Check that all tests pass with `pytest -n auto`

### Build context too large

The `.dockerignore` is auto-generated and should exclude everything except `dist/`. If the build context is still large:

```bash
python3 -m beagle_dockeriser generate    # re-generate .dockerignore
docker build --no-cache -t beagle-factory:latest .
```

---

## Project Structure

```
beagle_containerisation/
├── beagle_dockeriser/          # The pipeline tool
│   ├── __init__.py
│   ├── __main__.py           # python3 -m beagle_dockeriser entry
│   ├── cli.py                # Typer CLI (deploy, validate, generate, status)
│   ├── constants.py          # All project identity & path constants
│   ├── models.py             # Dataclasses: WheelSpec, DockerfileSpec, etc.
│   ├── pipeline.py           # Sequential phase orchestrator
│   └── phases/
│       ├── validate.py       # Phase 1: ruff, vulture, pytest
│       ├── build.py          # Phase 2: uv build → wheel
│       ├── dockerfile.py     # Phase 3: Generate Dockerfile
│       ├── compose.py        # Phase 4: Generate docker-compose.yaml
│       └── build_push.py     # Phase 5: docker build
├── Dockerfile                # Generated artifact
├── docker-compose.yaml       # Generated artifact
├── .dockerignore             # Generated artifact
├── .gitignore
├── PLAN.md                   # Full architecture & design document
├── README.md                 # This file
├── sup                      # Codebase dump for LLM context hydration
└── gup                      # Git sync: commit + pull + push
```

---

## License

This project is part of the Beagle ecosystem. See the main project for license details.

---

*Built with the Beagle dockeriser. Containerise your agentic workflow.*
