#!/usr/bin/env python3
"""Build the third-party license inventory for the vendored ``pi`` fork.

The inventory is derived **only** from ``vendor/pi/package-lock.json`` — the
npm lockfile is the single source of truth for what the fork depends on and at
which exact version. The installed ``node_modules/`` tree is deliberately *not*
consulted: ``npm ci`` prunes it to the current platform's optional native
bindings, so a tree-walk produces a different package set on every OS/arch and
cannot back a reproducible ``--check`` gate.

Every non-link ``node_modules/*`` entry in the lockfile becomes one inventory
row: name, version, dependency scope, declared SPDX license and the *effective*
license Beagle relies on (see license elections below).

Usage:
    python3 generate_license_inventory.py           # (re)write the inventory
    python3 generate_license_inventory.py --check    # fail if it is stale

Exit codes:
    0  inventory written, or (``--check``) the on-disk inventory is current
    1  (``--check``) the inventory is missing or stale
    2  a dependency carries a copyleft-only or unresolvable license — a
       release blocker that must be triaged before the inventory can be built

<invariant>
Beagle and the vendored pi fork are both distributed under the MIT license.
This script resolves every SPDX ``OR`` expression to a permissive option and
treats a strong-copyleft-only or unknown license as a hard error (exit 2) —
such a dependency cannot be redistributed under MIT terms. It must never
silently downgrade that check to a warning.
</invariant>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────
_TOOLS_DIR = Path(__file__).resolve().parent
_PI_ROOT = _TOOLS_DIR.parent
_LOCKFILE = _PI_ROOT / "vendor" / "pi" / "package-lock.json"
_UPSTREAM = _PI_ROOT / "vendor" / "UPSTREAM.txt"
_OUTPUT = _PI_ROOT / "vendor" / "license-inventory.json"

# Paths shown in the inventory + in operator messages, relative to the repo root.
_REPO_REL = "src/beagle/frontends/pi/tools/generate_license_inventory.py"
_OUTPUT_REPO_REL = "src/beagle/frontends/pi/vendor/license-inventory.json"

# ── License policy ───────────────────────────────────────────────────────────
# Permissive identifiers, in the order Beagle prefers to elect them from an
# SPDX ``OR`` expression. Anything not listed here is neither auto-elected nor
# auto-declined — it is reported verbatim and, if it is the only option, is
# treated as unresolved (exit 2).
_PERMISSIVE_PREFERENCE: tuple[str, ...] = (
    "MIT",
    "ISC",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "0BSD",
    "Apache-2.0",
    "BlueOak-1.0.0",
    "Unlicense",
    "CC0-1.0",
    "Python-2.0",
)

# Strong copyleft: Beagle will not elect these, and one of them as the *only*
# option (or anywhere inside an ``AND``) is a release blocker (exit 2). A
# trailing "+" / "-or-later" is stripped before the membership test.
_DECLINED: frozenset[str] = frozenset(
    {
        "GPL-1.0",
        "GPL-2.0",
        "GPL-3.0",
        "LGPL-2.0",
        "LGPL-2.1",
        "LGPL-3.0",
        "AGPL-1.0",
        "AGPL-3.0",
        "SSPL-1.0",
        "OSL-3.0",
        "EUPL-1.1",
    }
)

# <invariant>
# The lockfile omits ``license`` for these packages. Values are transcribed from
# each package's own ``package.json`` on the npm registry at the pinned version
# and must be re-verified whenever the pin in package-lock.json changes.
# </invariant>
_LICENSE_OVERRIDES: dict[str, dict[str, str]] = {
    "ssh2": {
        "license": "MIT",
        "source": "ssh2 package.json (npm); lockfile omits the license field",
    },
    "cpu-features": {
        "license": "MIT",
        "source": "cpu-features package.json (npm); lockfile omits the license field",
    },
    "buildcheck": {
        "license": "MIT",
        "source": "buildcheck package.json (npm); lockfile omits the license field",
    },
    "rechoir": {
        "license": "MIT",
        "source": "rechoir package.json (npm); lockfile omits the license field",
    },
}

# Weak / file-level copyleft: permitted for unmodified dependencies (no
# obligation flows to Beagle's own source) but surfaced in the summary so a
# license audit sees them without reading every row.
_WEAK_COPYLEFT: frozenset[str] = frozenset({"MPL-2.0", "EPL-2.0", "CDDL-1.0"})


def _canonical_id(identifier: str) -> str:
    """Strip an SPDX ``+`` / ``-or-later`` / ``-only`` suffix for set membership."""
    return (
        identifier.strip()
        .removesuffix("+")
        .removesuffix("-or-later")
        .removesuffix("-only")
    )


def _is_declined(identifier: str) -> bool:
    """True when ``identifier`` is a strong-copyleft license Beagle blocks on."""
    return _canonical_id(identifier) in _DECLINED


class LicenseBlocker(Exception):
    """A dependency license cannot be resolved to a permissive option."""


def _read_lockfile() -> dict[str, Any]:
    """Load and minimally validate ``vendor/pi/package-lock.json``.

    Returns:
        The parsed lockfile object.

    Raises:
        SystemExit: the lockfile is absent or not lockfile-format v3.
    """
    if not _LOCKFILE.is_file():
        sys.exit(f"lockfile not found: {_LOCKFILE}")
    data: dict[str, Any] = json.loads(_LOCKFILE.read_text(encoding="utf-8"))
    version = data.get("lockfileVersion")
    if version != 3:
        sys.exit(
            f"unsupported lockfileVersion {version!r} (expected 3); "
            "update generate_license_inventory.py for the new format"
        )
    return data


def _parse_upstream() -> dict[str, str]:
    """Parse the ``key: value`` provenance pins from ``vendor/UPSTREAM.txt``.

    Returns:
        A mapping of the recognised pin keys (``upstream``, ``fork``, ``tag``,
        ``commit``) to their values. Missing keys are simply absent.
    """
    pins: dict[str, str] = {}
    if not _UPSTREAM.is_file():
        return pins
    wanted = {"upstream", "fork", "tag", "commit"}
    for line in _UPSTREAM.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in wanted:
            pins[key.strip()] = value.strip()
    return pins


def _package_name(lock_key: str) -> str:
    """Return the package name for a lockfile ``packages`` key.

    ``node_modules/execa/node_modules/which`` → ``which``;
    ``node_modules/@babel/types`` → ``@babel/types``.
    """
    return lock_key.rsplit("node_modules/", 1)[1]


def _scope(entry: dict[str, Any]) -> str:
    """Classify a lockfile entry as ``production``, ``optional`` or ``development``."""
    if entry.get("dev"):
        return "development"
    if entry.get("optional"):
        return "optional"
    return "production"


def _normalise_declared(raw: object) -> str | None:
    """Reduce the lockfile ``license`` field to a single SPDX string.

    Handles the plain-string, ``{"type": ...}`` and legacy list forms. Returns
    ``None`` when no license is declared.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        value = raw.get("type")
        return str(value).strip() if value else None
    if isinstance(raw, list):
        parts = [_normalise_declared(item) for item in raw]
        joined = " OR ".join(p for p in parts if p)
        return joined or None
    return None


def _split_or(expression: str) -> list[str]:
    """Split a top-level SPDX ``OR`` expression into its options."""
    inner = expression.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    return [opt.strip() for opt in inner.split(" OR ") if opt.strip()]


def _elect(name: str, version: str, declared: str) -> tuple[str, dict[str, str] | None]:
    """Resolve a declared license to the effective one Beagle relies on.

    Args:
        name: Package name (for diagnostics and the election record).
        version: Package version.
        declared: The normalised declared SPDX string.

    Returns:
        ``(effective_license, election_record_or_None)``. The election record is
        present only when an ``OR`` expression was resolved.

    Raises:
        LicenseBlocker: the license is copyleft-only or has no permissive option.
    """
    # Conjunctions: every term applies; a declined term anywhere is a blocker.
    if " AND " in f" {declared} ":
        terms = [t.strip(" ()") for t in declared.split(" AND ")]
        bad = sorted(t for t in terms if _is_declined(t))
        if bad:
            raise LicenseBlocker(
                f"{name}@{version}: declared {declared!r} includes strong-copyleft "
                f"term(s) {bad} under AND; cannot be redistributed under MIT terms"
            )
        return declared, None

    if " OR " not in declared:
        if _is_declined(declared):
            raise LicenseBlocker(
                f"{name}@{version}: declared {declared!r} is strong-copyleft-only; "
                "cannot be redistributed under MIT terms"
            )
        return declared, None

    options = _split_or(declared)
    for preferred in _PERMISSIVE_PREFERENCE:
        if preferred in options:
            declined = sorted(o for o in options if o != preferred)
            return preferred, {
                "package": name,
                "version": version,
                "declared": declared,
                "elected": preferred,
                "declined": ", ".join(declined),
                "reason": (
                    "permissive option elected; Beagle and the pi fork are "
                    "MIT-licensed and do not take the declined option(s), which "
                    "would impose copyleft or unclear terms on redistribution"
                ),
            }
    raise LicenseBlocker(
        f"{name}@{version}: declared {declared!r} offers no permissive option "
        f"from {list(_PERMISSIVE_PREFERENCE)}"
    )


def _build() -> dict[str, Any]:
    """Assemble the full inventory document from the lockfile.

    Raises:
        LicenseBlocker: at least one dependency license is unresolvable.
    """
    lock = _read_lockfile()
    packages: dict[str, dict[str, Any]] = lock.get("packages", {})

    rows: list[dict[str, Any]] = []
    elections: list[dict[str, str]] = []
    overrides_used: list[dict[str, str]] = []
    blockers: list[str] = []

    for key, entry in packages.items():
        if "node_modules/" not in key or entry.get("link"):
            continue
        name = _package_name(key)
        version = str(entry.get("version", ""))
        scope = _scope(entry)

        declared = _normalise_declared(entry.get("license"))
        override_source: str | None = None
        if declared is None and name in _LICENSE_OVERRIDES:
            declared = _LICENSE_OVERRIDES[name]["license"]
            override_source = _LICENSE_OVERRIDES[name]["source"]
            overrides_used.append(
                {"package": name, "version": version, "license": declared, "source": override_source}
            )
        if declared is None:
            blockers.append(f"{name}@{version}: no license declared and no override")
            declared = "UNKNOWN"

        try:
            effective, election = _elect(name, version, declared)
        except LicenseBlocker as exc:
            # Collect every offending package, don't stop at the first.
            blockers.append(str(exc))
            effective, election = declared, None
        if election is not None:
            elections.append(election)

        rows.append(
            {
                "name": name,
                "version": version,
                "scope": scope,
                "license_declared": declared,
                "license_effective": effective,
                "license_source": override_source or "package-lock.json",
                "deprecated": entry.get("deprecated") or None,
                "has_install_script": bool(entry.get("hasInstallScript")),
                "resolved": entry.get("resolved"),
                "integrity": entry.get("integrity"),
            }
        )

    if blockers:
        raise LicenseBlocker(
            "unresolved dependency licenses (fix the override table or the "
            "vendored fork before regenerating):\n  - " + "\n  - ".join(sorted(blockers))
        )

    rows.sort(key=lambda r: (r["name"], r["version"]))
    elections.sort(key=lambda e: e["package"])
    overrides_used.sort(key=lambda o: o["package"])

    by_scope: dict[str, int] = {}
    by_license: dict[str, int] = {}
    for row in rows:
        by_scope[row["scope"]] = by_scope.get(row["scope"], 0) + 1
        by_license[row["license_effective"]] = by_license.get(row["license_effective"], 0) + 1

    deprecated = sorted(
        f"{r['name']}@{r['version']}" for r in rows if r["deprecated"] is not None
    )
    strong_copyleft = sorted(
        lic for lic in by_license if _is_declined(lic) or lic == "UNKNOWN"
    )
    weak_copyleft = sorted(lic for lic in by_license if lic in _WEAK_COPYLEFT)

    lockfile_bytes = _LOCKFILE.read_bytes()
    return {
        "_comment": (
            "Third-party license inventory for the vendored pi fork. Generated "
            f"from vendor/pi/package-lock.json by {_REPO_REL}. Do not hand-edit; "
            "run the generator. The beagle-pi-license.yml workflow fails if this "
            "file has drifted from the lockfile."
        ),
        "generator": _REPO_REL,
        "upstream": _parse_upstream(),
        "lockfile": {
            "path": "src/beagle/frontends/pi/vendor/pi/package-lock.json",
            "lockfile_version": lock["lockfileVersion"],
            "sha256": hashlib.sha256(lockfile_bytes).hexdigest(),
        },
        "summary": {
            "third_party_packages": len(rows),
            "by_scope": dict(sorted(by_scope.items())),
            "by_effective_license": dict(
                sorted(by_license.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "strong_copyleft_present": bool(strong_copyleft),
            "strong_copyleft_licenses": strong_copyleft,
            "weak_copyleft_licenses": weak_copyleft,
            "deprecated_packages": deprecated,
            "packages_with_install_scripts": sum(
                1 for r in rows if r["has_install_script"]
            ),
        },
        "license_elections": elections,
        "license_overrides": overrides_used,
        "packages": rows,
    }


def _serialise(document: dict[str, Any]) -> str:
    """Render the inventory as canonical, newline-terminated JSON."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. See the module docstring for exit-code semantics."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed inventory is current instead of writing it",
    )
    args = parser.parse_args(argv)

    try:
        rendered = _serialise(_build())
    except LicenseBlocker as exc:
        print(f"license blocker:\n{exc}", file=sys.stderr)
        return 2

    if args.check:
        if not _OUTPUT.is_file():
            print(f"license inventory missing: {_OUTPUT}", file=sys.stderr)
            print(f"regenerate with: python3 {_REPO_REL}", file=sys.stderr)
            return 1
        if _OUTPUT.read_text(encoding="utf-8") != rendered:
            print("license inventory is stale (lockfile changed?).", file=sys.stderr)
            print(f"regenerate with: python3 {_REPO_REL}", file=sys.stderr)
            return 1
        document = json.loads(rendered)
        print(
            "license inventory current: "
            f"{document['summary']['third_party_packages']} packages, "
            f"{len(document['summary']['by_effective_license'])} distinct effective licenses"
        )
        return 0

    # Atomic write: temp file in the same directory, then rename.
    tmp = _OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(_OUTPUT)
    document = json.loads(rendered)
    print(
        f"wrote {_OUTPUT_REPO_REL}: "
        f"{document['summary']['third_party_packages']} packages, "
        f"{len(document['summary']['by_effective_license'])} distinct effective licenses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
