# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Scan src/beagle for hardcoded tunable defaults (detector / reporter).

A tunable default — a numeric or string literal at a constructor keyword, or
a module-level SCREAMING_CASE constant — should live in code constants or the
typed config schema (``beagle/config/schema.py``). This tool FINDS literals;
an optional ``--registry TOML`` marks known-accepted sites, in which case any
uncovered finding fails the run (exit 1). Without a registry the scan is
report-only (exit 0): the bundled classification registry was retired when
all user-editable configuration moved to ``~/.config/beagle`` (XDG) — the
package ships zero bundled config.

Two finding kinds:

- ``kwarg-default``: a literal default on any ``__init__`` keyword parameter.
- ``module-const``: a module-level ``NAME = literal`` where NAME is SCREAMING_CASE.

Registry rows match by file substring plus an optional symbol list ("*"
matches every symbol in the file). Statuses are informational for humans;
the gated mode only cares about coverage.

Usage:
    python3 scripts/check_hardcoded_defaults.py [--src DIR] [--registry FILE]
    python3 scripts/check_hardcoded_defaults.py --json   # machine-readable
    python3 scripts/check_hardcoded_defaults.py --selftest

Exit codes: 0 clean/report-only (or selftest passed), 1 findings in gated
mode (or selftest failed), 2 usage error.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SRC = "src/beagle"


@dataclass(frozen=True)
class Finding:
    """One hardcoded literal this gate wants classified or moved."""

    kind: str  # "kwarg-default" | "module-const"
    file: str  # path relative to CWD
    line: int
    symbol: str  # function name for kwargs, constant name for module consts
    detail: str  # parameter name, or the literal value


@dataclass
class Registry:
    """Allowlist rows loaded from defaults_registry.toml."""

    rows: list[dict[str, object]] = field(default_factory=list)

    def covers(self, rel_path: str, symbol: str) -> bool:
        """Return True when some row matches this file and symbol.

        Args:
            rel_path: Repo-relative file path of the finding.
            symbol: Function or constant name of the finding.

        Returns:
            True when a row's ``file`` is a substring of rel_path and its
            ``symbols`` list is absent, contains "*", or contains symbol.
        """
        for row in self.rows:
            entry_file = str(row.get("file", ""))
            if entry_file and entry_file not in rel_path:
                continue
            symbols = row.get("symbols", [])
            if not isinstance(symbols, list):
                continue
            if not symbols or "*" in symbols or symbol in [str(s) for s in symbols]:
                return True
        return False


def _literal_constant(node: ast.expr) -> bool:
    """Return True when node is an int/float/non-empty-str literal."""
    if not isinstance(node, ast.Constant):
        return False
    if isinstance(node.value, bool):  # bool is an int subclass; flags are policy
        return False
    if isinstance(node.value, (int, float)):
        return True
    return isinstance(node.value, str) and node.value != ""


def scan_file(path: Path) -> list[Finding]:
    """Scan one Python file for unclassified tunable literals.

    Args:
        path: Absolute or CWD-relative path of the file to scan.

    Returns:
        Findings in source order. Unparsable files are skipped silently —
        ruff/mypy own syntax errors; this gate owns literals only.
    """
    findings: list[Finding] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return findings
    rel = str(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != "__init__":
                continue
            all_args = node.args.args + node.args.kwonlyargs
            defaults = node.args.defaults + node.args.kw_defaults
            pairs = [
                (a, d)
                for a, d in zip(all_args[len(all_args) - len(defaults) :], defaults, strict=True)
                if d
            ]
            for arg, default in pairs:
                if _literal_constant(default):
                    findings.append(
                        Finding(
                            kind="kwarg-default",
                            file=rel,
                            line=node.lineno,
                            symbol=node.name,
                            detail=f"{arg.arg}={ast.unparse(default)}",
                        )
                    )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.isupper()
                    and "_" in target.id
                    and _literal_constant(value)
                ):
                    findings.append(
                        Finding(
                            kind="module-const",
                            file=rel,
                            line=node.lineno,
                            symbol=target.id,
                            detail=ast.unparse(value)[:40],
                        )
                    )
    return findings


def load_registry(path: Path) -> Registry:
    """Load the allowlist registry TOML.

    Args:
        path: Path to defaults_registry.toml.

    Returns:
        A Registry whose rows come from the optional ``[[entry]]`` tables.
        A missing file yields an empty registry (everything fails).
    """
    reg = Registry()
    if not path.exists():
        return reg
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entry", [])
    if isinstance(entries, list):
        reg.rows = [row for row in entries if isinstance(row, dict)]
    return reg


def collect(src_root: Path) -> list[Finding]:
    """Run the scan across every .py under src_root."""
    findings: list[Finding] = []
    for py in sorted(src_root.rglob("*.py")):
        findings.extend(scan_file(py))
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to sys.argv[1:].

    Returns:
        Process exit code (0 clean/selftest-ok, 1 findings/selftest-fail,
        2 usage).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", default=DEFAULT_SRC, help="Source root to scan")
    parser.add_argument(
        "--registry",
        default=None,
        help="Optional allowlist TOML; omit for a report-only scan "
        "(the bundled classification registry is retired — user config "
        "lives in ~/.config/beagle, nothing ships in the package)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON output")
    parser.add_argument("--selftest", action="store_true", help="Verify detection works")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    src_root = Path(args.src)
    if not src_root.is_dir():
        parser.error(f"--src {src_root} is not a directory")

    findings = collect(src_root)
    if args.registry:
        reg = load_registry(Path(args.registry))
        findings = [f for f in findings if not reg.covers(f.file, f.symbol)]
    if args.as_json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.file}:{f.line}: {f.kind} {f.symbol} ({f.detail})")
        mode = "gated" if args.registry else "report-only"
        summary = "CLEAN" if not findings else f"{len(findings)} TUNABLE LITERALS"
        print(f"check_hardcoded_defaults[{mode}]: {summary}")
    return (1 if findings else 0) if args.registry else 0


def _selftest() -> int:
    """Prove the scanner detects both finding kinds on a planted violation.

    Returns:
        0 when both kinds are detected in a temp tree, 1 otherwise.
    """
    snippet = (
        "MAX_RETRIES = 5\n"
        "\n"
        "class Thing:\n"
        "    def __init__(self, timeout_s: float = 30.0) -> None:\n"
        "        self.timeout_s = timeout_s\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "src"
        pkg = root / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "bad.py").write_text(snippet, encoding="utf-8")
        found = {(f.kind, f.symbol) for f in collect(root)}
    ok = {("module-const", "MAX_RETRIES"), ("kwarg-default", "__init__")} <= found
    print("selftest:", "OK" if ok else f"FAILED (found={sorted(found)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
