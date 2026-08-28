# Beagle Container Workflow

Ship Beagle as a single self-contained container — no source tree, no
credentials baked in, CPU-only torch, non-root runtime.

```
make build ──► dist/beagle-<ver>-py3-none-any.whl
                    │
make image-build ──► docker/Dockerfile ──► beagle:<ver> image
                    │                                    │
     standalone: docker/docker-compose.yml    dev stack: scripts/integrate_dev_stack.sh
```

## 1. Build the image

```bash
make image-build                      # tags beagle:<version from pyproject>
# custom name/tag:
make image-build IMAGE=ghcr.io/acme/beagle
```

The Dockerfile installs **CPU-only torch first** so dependency resolution can
never pull the multi-GB CUDA build — the same guarantee as `make install`.

## 2. Run standalone

```bash
make container-up                     # docker compose -f docker/docker-compose.yml up -d
make container-down
```

- A2A federation port `8420` is bound to loopback only.
- Config/secrets are **mounted, never baked**: uncomment the
  `${HOME}/.config/beagle` read-only mount in the compose file to seed the
  container from the host config root (`beagle config init` seeds it).
- RAG corpus and state live in named volumes (`beagle-rag`, `beagle-state`).

## 3. Publish

```bash
# wheels → GitHub Release (see docs):
gh release create v<ver> dist/*.whl

# image → registry:
make image-push REGISTRY=ghcr.io/<owner>
```

## 4. Integrate into an existing dev stack

Wire the service fragment into any Compose-based stack (idempotent,
validates the merged config before finishing):

```bash
make dev-stack-integrate DEV_STACK_DIR=/path/to/dev_stack   # e.g. server_1
```

What it does:
1. Copies `docker/compose.dev-stack.yml` → `<stack>/beagle.compose.yml`
2. Appends an `include:` entry to the stack's main compose file
3. Runs `docker compose config` to prove the merge parses

Then on the stack host:

```bash
docker compose up -d beagle
docker inspect --format '{{.State.Health.Status}}' "$(docker ps -qf name=beagle)"
```

If the stack fronts services with Traefik, attach that network inside
`beagle.compose.yml` — none is forced by default.

## Full dev-stack MCP mesh

The five-way wiring — `code-server` ⇄ `open-webui` ⇄ `goose` ⇄
`beagle` ⇄ `openclaw`, with **ollama as the embedding backend** and beagle
as the primary agentic inference plane — lives in
[`MCP_WIRING.md`](MCP_WIRING.md), including per-consumer config snippets
(`examples/mcp.json` for code-server, `examples/goose-extensions.yaml` for
goose) and the llama.cpp swap caveats.

## Relationship to the legacy multi-agent topology

`src/beagle/infrastructure/` retains the agent-per-container topology
(`Dockerfile.base`, `Dockerfile.agent`, 5-service compose) used by the
planner/executor/verifier/synthesizer stacks — see that directory's README.
This workflow is the single-image path: one wheel, one container, CLI
entrypoint (`beagle`), suitable for shipping and CI.
