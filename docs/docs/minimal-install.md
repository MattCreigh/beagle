# Minimal-Setup Install

This is the **fewest-setup-steps** install. It is not the smallest footprint —
RAG, graph search, and embeddings are core and are not optional. "Minimal" here
means: a fresh venv, `pip install beagle`, the bundled defaults, and a running
workflow. No config-root override, no external services required.

## What you get

- The `beagle` package with the bundled doctrine SSOT
  (`default_config/style_guides/guides/`) and bundled default config.
- Local CPU-only embeddings via `sentence-transformers` (no external service).
- RAG, graph search, and the orchestrator — all core, all present.

## What you do NOT need

- A custom config-root override — the bundled defaults are used until you
  set one.
- A running external embedding daemon — embeddings run locally on CPU.
- A running frontend or third-party harness — the orchestrator runs headless.
  Any compatible frontend is optional.

## Steps

```bash
# 1. Fresh venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the built wheel (or `pip install -e .` for a source checkout)
pip install beagle

# 3. Confirm the core imports work with no external service
python -c "import sentence_transformers, torch; print('ok')"
python -c "from beagle.infrastructure.services.embedding import get_local_embedder; print('ok')"

# 4. Run a workflow
beagle run --workflow research --query "your question"
```

## Optional extras

The base install is deliberately lean. Add extras only when you need them:

- `pip install beagle[tui]` — the reactive Textual dashboard.
- `pip install beagle[governance]` — the Casbin RBAC policy engine.

## Notes

- RAG, graph search, and embeddings are **core** and are **not** optional.
- Embeddings need **no external service** — they run locally on CPU.
- The bundled defaults give a clean install a working doctrine SSOT and
  config until you populate a canonical config root.
