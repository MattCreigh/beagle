#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# prepare_public_release.sh -- publish Beagle publicly WITHOUT its history.
#
# Strategy (owner decision, 2026-08-25):
#   * PUBLIC repository : exactly one initial commit (full audited tree,
#                         no development history)
#   * PRIVATE _dev mirror : complete history pushed first, so nothing is lost
#
# Usage:
#   scripts/prepare_public_release.sh <dev_remote> <public_remote> [branch]
#
#     dev_remote     URL or path of the private history-mirror repo (_dev)
#     public_remote  URL of the freshly created empty public GitHub repo
#     branch         branch to publish (default: main)
#
# Safety: requires a clean worktree; never rewrites or deletes local
# history; only pushes. Re-runnable.
# ---------------------------------------------------------------------------
set -euo pipefail

DEV_REMOTE="${1:?usage: prepare_public_release.sh <dev_remote> <public_remote> [branch]}"
PUBLIC_REMOTE="${2:?usage: prepare_public_release.sh <dev_remote> <public_remote> [branch]}"
BRANCH="${3:-main}"

[ -n "$(git status --porcelain)" ] && {
  echo "ERROR: worktree not clean -- commit or stash before publishing." >&2; exit 1; }

CURRENT="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT" = "$BRANCH" ] || {
  echo "ERROR: expected to be on '$BRANCH' (currently on '$CURRENT')." >&2; exit 1; }

# D-08 (release-readiness audit 2026-08-28): on ANY exit path (success,
# error, or the orphan-branch step), return to the original branch and delete
# the orphan so the caller is never left stranded. The trap fires even on a
# mid-push abort.
CLEANUP_NEEDED=0
cleanup() {
  status=$?
  if [ "$CLEANUP_NEEDED" = "1" ]; then
    git checkout -q "$BRANCH" 2>/dev/null || true
    git branch -D public-release 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

echo "== 1/4  Mirroring FULL history to the private _dev remote =="
git push "$DEV_REMOTE" --all
git push "$DEV_REMOTE" --tags

echo "== 2/4  Creating orphan branch with a single initial commit =="
VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
git checkout --orphan public-release
CLEANUP_NEEDED=1
git add -A

# D-08 hard guard: the public tree must contain NO proprietary compiled
# artifacts (wheels, sdists, shared objects). A committed wheel is an IP
# leak on publication and defeats the release's own purpose.
if [ -n "$(git ls-files dist/)" ]; then
  echo "ERROR: public release would publish tracked build artifacts under dist/:"
  git ls-files dist/
  echo "Remove them (git rm --cached) before publishing." >&2
  exit 1
fi
if [ -n "$(git ls-files | grep -E '\.(whl|tar\.gz|so)$' || true)" ]; then
  echo "ERROR: public release would publish a binary artifact:"
  git ls-files | grep -E '\.(whl|tar\.gz|so)$' || true
  echo "Remove it before publishing." >&2
  exit 1
fi
if [ -n "$(git ls-files | grep -iE 'orpheus' || true)" ]; then
  echo "ERROR: public release would publish Orpheus-proprietary paths:"
  git ls-files | grep -iE 'orpheus' || true
  echo "Remove them before publishing." >&2
  exit 1
fi

git commit -m "feat: beagle v${VERSION} -- initial public release

Single-commit snapshot of the audited open-source tree.
Development history is maintained in the private _dev mirror."

echo "== 3/4  Pushing squashed tree to the public remote =="
# D-08: --force-with-lease refuses to clobber a remote that moved under us;
# a plain --force can overwrite another operator's push.
git push "$PUBLIC_REMOTE" public-release:main --force-with-lease

echo "== 4/4  Restoring local state =="
git checkout "$BRANCH"
git branch -D public-release
CLEANUP_NEEDED=0

echo "Done: public repo has one commit; full history preserved at $DEV_REMOTE"
