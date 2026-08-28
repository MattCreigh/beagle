#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# integrate_dev_stack.sh — wire the Beagle single-container service into
# an existing Compose-based dev stack (e.g. the server_1 dev stack).
#
# Idempotent: re-running never duplicates the fragment or include line.
# Usage:
#   scripts/integrate_dev_stack.sh -d /path/to/dev_stack [-f]
#     -d  target dev-stack directory containing docker-compose.yml
#         (compose.yaml / compose.yml also accepted)
#     -f  force-overwrite an existing beagle.compose.yml
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

usage() { sed -n '2,12p' "$0"; exit "${1:-0}"; }
die()   { echo "ERROR: $*" >&2; exit 1; }

STACK_DIR="" FORCE=0
while getopts ":d:fh" opt; do
  case "$opt" in
    d) STACK_DIR="$OPTARG" ;;
    f) FORCE=1 ;;
    h) usage 0 ;;
    *) usage 1 ;;
  esac
done
[ -n "$STACK_DIR" ] || usage 1
[ -d "$STACK_DIR" ] || die "not a directory: $STACK_DIR"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/docker/compose.dev-stack.yml"
DEST="$STACK_DIR/beagle.compose.yml"

# Mesh support inputs (openclaw service): config gate + extend-Dockerfile.
EXTRA_SRCS=(
  "$REPO_ROOT/docker/examples/openclaw.config.toml|$STACK_DIR/openclaw.config.toml"
  "$REPO_ROOT/docker/Dockerfile.openclaw|$STACK_DIR/Dockerfile.openclaw"
)

# 1/4 — locate the stack's main compose file
MAIN=""
for cand in docker-compose.yml compose.yaml compose.yml docker-compose.yaml; do
  [ -f "$STACK_DIR/$cand" ] && MAIN="$STACK_DIR/$cand" && break
done
[ -n "$MAIN" ] || die "no compose file found in $STACK_DIR"

# 2/4 — copy the service fragment (never clobber without -f)
if [ -e "$DEST" ] && [ "$FORCE" -ne 1 ]; then
  echo "= $DEST already present — leaving it (use -f to overwrite)"
else
  cp -v "$SRC" "$DEST"
fi

# Mesh support inputs: openclaw activation config + extend-Dockerfile.
for pair in "${EXTRA_SRCS[@]}"; do
  src="${pair%%|*}"; dst="${pair##*|}"
  if [ -e "$dst" ] && [ "$FORCE" -ne 1 ]; then
    echo "= $dst already present — leaving it (use -f to overwrite)"
  else
    cp -v "$src" "$dst"
  fi
done

# OpenClaw image build input (plugin wheel). Optional if OPENCLAW_IMAGE points
# at a pre-built registry image instead.
if ls "$REPO_ROOT"/wheels/beagle_openclaw-*.whl >/dev/null 2>&1; then
  mkdir -p "$STACK_DIR/wheels"
  cp -v "$REPO_ROOT"/wheels/beagle_openclaw-*.whl "$STACK_DIR/wheels/"
else
  echo "! openclaw: no wheels/beagle_openclaw-*.whl in repo — build it from the"
  echo "  plugin repo or set OPENCLAW_IMAGE to a prebuilt image."
fi

# 3/4 — ensure the main compose includes the fragment (idempotent)
if grep -Eq '^\s*include:' "$MAIN"; then
  if grep -q 'beagle.compose.yml' "$MAIN"; then
    echo "= include already wired in $(basename "$MAIN")"
  else
    printf '  - path: ./beagle.compose.yml\n' >> "$MAIN"
    echo "+ appended include path to $(basename "$MAIN")"
  fi
else
  printf '\ninclude:\n  - path: ./beagle.compose.yml\n' >> "$MAIN"
  echo "+ added include block to $(basename "$MAIN")"
fi

# 4/4 — validate merged configuration (parse-only, no pull/start)
if command -v docker >/dev/null 2>&1; then
  if docker compose --project-directory "$STACK_DIR" -f "$MAIN" config -q; then
    echo "✓ merged compose config validates"
  else
    die "merged compose config invalid — inspect $MAIN"
  fi
else
  echo "! docker CLI not found — skipped validation"
fi

cat <<NEXT

Next steps:
  1. Provide the image on the stack host:
       make image-build REGISTRY=<registry> && make image-push
     …or build in place:  make image-build
  2. Start it:            docker compose -f "$MAIN" up -d beagle
  3. Verify:              docker inspect --format '{{.State.Health.Status}}' \$(docker ps -qf name=beagle)
NEXT
