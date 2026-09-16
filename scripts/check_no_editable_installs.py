#!/usr/bin/env python3
# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Editable-install gate: a released artefact must never resolve into a source tree.

House policy is NON-EDITABLE at every layer — ``pipx install -e .`` is retired,
wheels only (see ``beagle_environment.toml`` [environment.python_install_policy]).
The policy was stated but never ENFORCED, so 25 editable installs accumulated
across the project venvs and were only found by a manual sweep.

Two distinct mechanisms mark an install as editable, and a gate must catch both:

  1. ``<pkg>.dist-info/direct_url.json`` carrying ``{"dir_info": {"editable": true}}``
     — the PEP 610 marker a modern installer writes.
  2. A ``*.pth`` whose name contains ``editable`` — ``__editable__.*.pth`` (the
     setuptools static form) or ``_editable_impl_*.pth`` (the uv/Hatch form).
     Each appends a source ``src/`` directory to ``sys.path``.

Why mechanism (2) matters even when (1) is absent: the ``.pth`` is what actually
puts ``src/`` on ``sys.path``, which makes mypy resolve the package as a LIBRARY
and silently suppress errors in followed modules. A wheel does not mask; only the
``.pth`` does. A stale or orphaned ``.pth`` is therefore a live defect even if the
distribution metadata no longer claims to be editable.

Read-only: never mutates a venv.

Usage:
    python3 scripts/check_no_editable_installs.py
    python3 scripts/check_no_editable_installs.py --root /home/server/Projects
    python3 scripts/check_no_editable_installs.py --venv /path/to/.venv
    python3 scripts/check_no_editable_installs.py --json
    python3 scripts/check_no_editable_installs.py --selftest

Exit codes: 0 clean, 1 editable install found, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Directories never worth walking: VCS metadata, dependency caches, and the
# interpreter's own bundled wheels (a vendored pip/setuptools wheel is not a
# project install and must not be reported as one).
_PRUNE = frozenset({".git", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"})

# An editable marker can only meaningfully exist inside an installed
# environment, so restrict the walk to that subtree. This keeps a scan of a
# whole projects tree cheap.
_SITE_MARKER = "site-packages"


@dataclass(frozen=True)
class Finding:
    """One editable install, with the evidence that proved it."""

    venv: str
    mechanism: str  # "direct_url" | "pth"
    detail: str
    target: str

    def as_dict(self) -> dict[str, str]:
        return {
            "venv": self.venv,
            "mechanism": self.mechanism,
            "detail": self.detail,
            "target": self.target,
        }


def _venv_root_of(site_packages: Path) -> Path:
    """Derive the environment root from a ``.../lib/pythonX.Y/site-packages`` path.

    Returns the site-packages parent when the path does not follow the layout,
    so an unusual environment is still reported under a stable name.
    """
    for parent in site_packages.parents:
        if (parent / "pyvenv.cfg").is_file():
            return parent
    return site_packages


def _is_editable_direct_url(path: Path) -> tuple[bool, str]:
    """True when a PEP 610 ``direct_url.json`` declares an editable install."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, ""
    if not isinstance(data, dict):
        return False, ""
    dir_info = data.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable") is True:
        return True, str(data.get("url", ""))
    return False, ""


def scan_site_packages(site_packages: Path) -> list[Finding]:
    """Return every editable marker directly inside one site-packages dir."""
    findings: list[Finding] = []
    venv = str(_venv_root_of(site_packages))
    try:
        entries = sorted(site_packages.iterdir())
    except OSError:
        return findings
    for entry in entries:
        name = entry.name
        if name.endswith(".dist-info") and entry.is_dir():
            ok, url = _is_editable_direct_url(entry / "direct_url.json")
            if ok:
                findings.append(
                    Finding(venv, "direct_url", f"{name}/direct_url.json", url)
                )
        elif name.endswith(".pth") and "editable" in name.lower():
            target = ""
            try:
                target = entry.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            findings.append(Finding(venv, "pth", name, target))
    return findings


def scan_tree(root: Path) -> list[Finding]:
    """Walk ``root`` and scan every ``site-packages`` directory found."""
    findings: list[Finding] = []
    seen: set[Path] = set()
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE]
        if Path(dirpath).name != _SITE_MARKER:
            continue
        resolved = Path(dirpath)
        if resolved in seen:
            continue
        seen.add(resolved)
        findings.extend(scan_site_packages(resolved))
    return findings


def default_roots() -> list[Path]:
    """Host locations that hold project environments, filtered to those present."""
    home = Path.home()
    candidates = [
        home / "Projects",
        home / ".local" / "share",
        home / ".cache" / "pipx",
        Path("/opt"),
        Path("/Projects"),
    ]
    return [c for c in candidates if c.is_dir()]


def render(findings: list[Finding], as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {
                "status": "violation" if findings else "clean",
                "count": len(findings),
                "findings": [f.as_dict() for f in findings],
            },
            indent=2,
        )
    if not findings:
        return "no editable installs found"
    lines = [f"{len(findings)} editable install(s) found:"]
    for f in findings:
        lines.append(f"  [{f.mechanism}] {f.venv}")
        lines.append(f"      {f.detail} -> {f.target}")
    lines.append("")
    lines.append("Remedy: build a wheel and install it non-editable, e.g.")
    lines.append("  uv build --wheel")
    lines.append("  uv pip install --force-reinstall --no-deps dist/<pkg>.whl --python <venv>/bin/python3")
    return "\n".join(lines)


def _selftest() -> int:
    """Prove both mechanisms are detected, and that a clean venv is not flagged."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "venv" / "lib" / "python3.13" / "site-packages"
        site.mkdir(parents=True)
        (Path(tmp) / "venv" / "pyvenv.cfg").write_text("", encoding="utf-8")

        # Mechanism 1 only: PEP 610 marker, no .pth.
        d1 = site / "alpha-1.0.0.dist-info"
        d1.mkdir()
        (d1 / "direct_url.json").write_text(
            json.dumps({"url": "file:///src/alpha", "dir_info": {"editable": True}}),
            encoding="utf-8",
        )
        # A non-editable direct_url must NOT be flagged.
        d2 = site / "beta-1.0.0.dist-info"
        d2.mkdir()
        (d2 / "direct_url.json").write_text(
            json.dumps({"url": "file:///dist/beta.whl", "archive_info": {}}),
            encoding="utf-8",
        )
        # Mechanism 2 only: an orphaned .pth, metadata silent.
        (site / "_editable_impl_gamma.pth").write_text("/src/gamma", encoding="utf-8")
        (site / "a1_coverage.pth").write_text("import coverage", encoding="utf-8")

        found = scan_site_packages(site)
        by_detail = {f.detail for f in found}
        if "alpha-1.0.0.dist-info/direct_url.json" not in by_detail:
            failures.append("missed the PEP 610 editable marker")
        if "_editable_impl_gamma.pth" not in by_detail:
            failures.append("missed the _editable_impl_*.pth form")
        if "beta-1.0.0.dist-info/direct_url.json" in by_detail:
            failures.append("false positive on a wheel direct_url")
        if any(f.detail == "a1_coverage.pth" for f in found):
            failures.append("false positive on a non-editable .pth")
        if len(found) != 2:
            failures.append(f"expected 2 findings, got {len(found)}")

        # The tree walk must reach a nested site-packages, not just the top one.
        walked = scan_tree(Path(tmp))
        if len(walked) != 2:
            failures.append(f"scan_tree expected 2 findings, got {len(walked)}")

    if failures:
        for f in failures:
            print(f"SELFTEST FAIL: {f}", file=sys.stderr)
        return 1
    print("selftest passed: both editable mechanisms detected, wheels and normal .pth ignored")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        metavar="DIR",
        help="tree to walk for site-packages dirs (repeatable; default: standard host locations)",
    )
    parser.add_argument(
        "--venv",
        action="append",
        default=None,
        metavar="DIR",
        help="scan one environment root directly (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--selftest", action="store_true", help="verify the detector, then exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    findings: list[Finding] = []
    if args.venv:
        for raw in args.venv:
            venv = Path(raw).expanduser()
            found = list(venv.glob("lib/python*/site-packages"))
            if not found:
                print(f"error: no site-packages under {venv}", file=sys.stderr)
                return 2
            for site in found:
                findings.extend(scan_site_packages(site))
    else:
        roots = [Path(r).expanduser() for r in args.root] if args.root else default_roots()
        if not roots:
            print("error: no scan roots resolved", file=sys.stderr)
            return 2
        for root in roots:
            if not root.is_dir():
                print(f"warning: skipping missing root {root}", file=sys.stderr)
                continue
            findings.extend(scan_tree(root))

    # De-duplicate: a venv reached from two roots must not be reported twice.
    unique = {(f.venv, f.mechanism, f.detail): f for f in findings}
    ordered = [unique[k] for k in sorted(unique)]

    print(render(ordered, args.json))
    return 1 if ordered else 0


if __name__ == "__main__":
    sys.exit(main())
