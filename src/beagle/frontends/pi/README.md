# Vendored `pi` frontend

Beagle's interactive terminal frontend is a vendored fork of
[`earendil-works/pi`](https://github.com/earendil-works/pi) — an MIT-licensed
TUI coding agent. This directory holds the vendored tree plus the Beagle-side
tooling that keeps it auditable.

## Layout

```text
src/beagle/frontends/pi/
├── __init__.py                     # namespace marker (no runtime Python)
├── README.md                       # this file
├── tools/
│   └── generate_license_inventory.py   # license manifest generator (stdlib only)
└── vendor/
    ├── UPSTREAM.txt                # exact upstream ref that was vendored
    ├── license-inventory.json      # generated third-party license manifest
    └── pi/                         # pristine upstream checkout
```

`vendor/pi/` is a **verbatim** checkout of a fork commit (see `vendor/UPSTREAM.txt`
for the repo, tag, and SHA). Nothing in this repository edits it in place — the
only writer is the re-sync procedure below. Keeping it pristine is what makes a
re-sync a clean directory swap rather than a merge.

## Not bundled into the wheel

The Beagle wheel does **not** carry `pi` or its `node_modules`. The vendored tree
is repo-only, for three reasons:

- `vendor/pi/node_modules/` and `vendor/pi/dist/` are git-ignored (upstream
  `.gitignore`), so the tree on its own is not a runnable artifact — it needs
  `npm ci` + `npm run build`.
- Bundling ~150 MB of JavaScript, including per-platform native binaries for
  macOS and Windows, into a CPU-only Python wheel is the wrong distribution
  shape. Beagle wheels stay lean and are attached to GitHub Releases.
- The frontend is launched as a separate process, not imported. It does not
  need to sit on `sys.path`.

An operator who wants the frontend builds it from this tree (or installs the
fork's own published package) and points Beagle at the resulting binary.

## Working with the vendored tree

```bash
cd src/beagle/frontends/pi/vendor/pi
npm ci --ignore-scripts        # node >= 22.19 (see package.json "engines")
npm run build
npm test
```

Always pass `--ignore-scripts`:

- the root `package.json` has a `prepare: husky` script that, run from inside
  this repository, would repoint Beagle's own git hooks;
- several dependencies (`ssh2`, `cpu-features`, `esbuild`, …) run native build
  scripts on install. `vendor/license-inventory.json` lists which
  (`summary.packages_with_install_scripts`).

The `.npmrc` in `vendor/pi/` sets `min-release-age=2` — npm's supply-chain
cooldown, which refuses to resolve a dependency version published less than two
days ago. Leave it in place; it does not affect `npm ci` (which installs the
pinned lockfile verbatim), only lockfile updates.

## License inventory

`vendor/license-inventory.json` is a generated manifest of every third-party npm
package the fork pins, with its declared SPDX license and the *effective* license
Beagle relies on. It is built purely from `vendor/pi/package-lock.json`, so it is
deterministic on any platform.

```bash
# regenerate after any change to vendor/pi/package-lock.json
python3 src/beagle/frontends/pi/tools/generate_license_inventory.py

# verify the committed manifest is current (CI runs this)
python3 src/beagle/frontends/pi/tools/generate_license_inventory.py --check
```

Beagle and the `pi` fork are both MIT-licensed. The generator resolves SPDX `OR`
expressions to a permissive option and exits non-zero if a dependency is
strong-copyleft-only or carries an unresolvable license — such a dependency
could not be redistributed under MIT terms. Every resolution is recorded in
`license_elections`. The one that matters:

- **`node-forge`** is `(BSD-3-Clause OR GPL-2.0)`. Beagle elects **BSD-3-Clause**;
  the GPL-2.0 option would impose copyleft on redistribution.

Four packages (`ssh2`, `cpu-features`, `buildcheck`, `rechoir`) omit the license
field from the lockfile; their values are transcribed from each package's own
`package.json` (all MIT) in the generator's override table and must be
re-verified whenever those pins change.

## Re-syncing from upstream

1. In a checkout of the fork, check out the new tag and note its commit SHA.
2. Replace `vendor/pi/` wholesale with the new tree (delete, copy, re-add). Do
   not merge — the point of a pristine vendor is that this step is mechanical.
3. Update every field in `vendor/UPSTREAM.txt` (tag, commit, date, dep counts).
4. Regenerate the license inventory and review the diff — new packages, license
   changes, and any new `license_elections` entry all need a human look.
5. `cd vendor/pi && npm ci --ignore-scripts && npm run build && npm test`.
6. Record the bump in the repository `CHANGELOG.md`.
