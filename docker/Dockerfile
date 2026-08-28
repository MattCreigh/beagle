# ════════════════════════════════════════════════════════════
# Beagle — single shippable production image (wheel-based)
#
# Maintained in-repo adaptation of the beagle-dockeriser output.
# Build input: the wheel produced by `make build` (dist/beagle-*.whl).
# The image contains NO source tree and NO credentials — all operator
# configuration is mounted at runtime from ~/.config/beagle.
# ════════════════════════════════════════════════════════════

# ── Stage 1: Builder ──────────────────────────────────────────
# D-19 (release-readiness audit 2026-08-28): base aligned to the runtime
# floor — .python-version, CI and the deployed venv all run 3.13. The
# site-packages path below is resolved via sysconfig instead of a hardcoded
# "3.12" literal so it can never drift from the actual interpreter.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy the pre-built wheel from dist/ and install it bare (--no-deps) so the
# exact pins can be read back from its own metadata (SSOT chain:
# pyproject.toml → wheel METADATA → this build).
COPY dist/ /tmp/dist/
RUN uv pip install --system --prefix=/install --no-deps \
    /tmp/dist/beagle-*-py3-none-any.whl

# Torch comes from the CPU index at the wheel's OWN exact pin. A
# local-version variant (+cpu) satisfies an ==X.Y.Z pin, and installing it
# BEFORE the full resolve means the resolver can never drag in the
# multi-GB CUDA build that a plain PyPI resolution would pick.
# Single-tool extraction (no pipeline → no pipefail concern). The base python
# (not /install/bin/python) reads the installed dist-info under /install.
RUN set -eux; \
    META="$(find /install -path '*/beagle-*.dist-info/METADATA' | head -n1)"; \
    TORCH_PIN="$(awk '/^Requires-Dist: torch==/ { print $2; exit }' "$META")"; \
    echo "torch pin from wheel metadata: $TORCH_PIN"; \
    uv pip install --system --prefix=/install "$TORCH_PIN" \
        --index-url https://download.pytorch.org/whl/cpu

# Full dependency resolution — torch is already satisfied, CPU variant kept.
RUN uv pip install --system --prefix=/install \
    /tmp/dist/beagle-*-py3-none-any.whl && \
    rm -rf /tmp/dist

# ── Stage 1b: Node runtime (pi frontend) ──────────────────────
# The bundled pi frontend (in the wheel) requires node >= 22.19. Debian slim
# does not ship a recent enough node, so pull it from the official node image.
FROM node:22-bookworm-slim AS node-runtime

# ── Stage 2: Runner ───────────────────────────────────────────
FROM python:3.13-slim AS runner

# apt package versions intentionally track the moving debian-slim base;
# pinning here would break on every base-image refresh.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/*

# pi frontend runtime: node from the dedicated stage (>=22.19 required).
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node-runtime /usr/local/bin/npm /usr/local/bin/npm
RUN ln -s /usr/local/bin/node /usr/bin/node || true

# Non-root runtime user
RUN groupadd --gid 1000 beagle_user && \
    useradd --uid 1000 --gid beagle_user \
    --shell /bin/bash --create-home beagle_user

# Application directories (RAG corpus, state, output)
RUN mkdir -p /app/data/rag /app/state /app/output && \
    chown -R beagle_user:beagle_user /app

COPY --from=builder /install /usr/local

ENV \
    BEAGLE_EXECUTION_ENV=docker \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_ROOT=/app \
    BEAGLE_DATA_ROOT=/app/data \
    BEAGLE_KNOWLEDGE_DIR=/app/data/rag \
    BEAGLE_KUZU_PATH=/app/data/rag

# A2A federation port
EXPOSE 8420

STOPSIGNAL SIGTERM

# D-16 (release-readiness audit 2026-08-28): the health check must exercise
# the purpose-built module (206 LOC) rather than `import beagle`, which
# passed even when every subsystem was down. The compose `start_period`
# absorbs model load so a slow cold start isn't marked unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -m beagle.infrastructure.health_check || exit 1

USER beagle_user
WORKDIR /app

ENTRYPOINT ["beagle"]
