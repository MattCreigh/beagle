#!/usr/bin/env bash
# Rebuild the vendored webui frontend bundle into vendor/webui-prebuild/dist.
#
# Only the frontend build (vite) is used — the Express server from upstream is
# NOT shipped; its role is replaced by server.py. Requires node >= 20 + npm.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC="$REPO_ROOT/src/beagle/frontends/webui/vendor/webui"
DST="$REPO_ROOT/src/beagle/frontends/webui/vendor/webui-prebuild/dist"

cd "$SRC"
npm install
npx vite build
rm -rf "$DST"
mkdir -p "$DST"
cp -r dist/index.html dist/assets "$DST"/
echo "webui bundle rebuilt -> $DST"
