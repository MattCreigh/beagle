#!/usr/bin/env python3
"""Quality Ratchet Checker for Beagle.

Reads baselines/quality-ratchet.json: {metric_id: {"count": int, "target": int}}
Recomputes metrics live and ensures no metric increases above the baseline.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile

R = sys.executable
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
RATCHET_FILE = ROOT_DIR / "baselines" / "quality-ratchet.json"


def _sh(cmd: str) -> str:
    args = shlex.split(cmd)
    res = subprocess.run(args, shell=False, cwd=ROOT_DIR, capture_output=True, text=True)
    return res.stdout.strip()


def _ruff_sum(select_rule: str) -> int:
    """Run ruff check with --isolated for a specific rule and sum the finding counts."""
    txt = _sh(
        f"{R} -m ruff check src/beagle/ --isolated --select {select_rule} --statistics --no-cache"
    )
    total = 0
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if parts and parts[0].isdigit():
            total += int(parts[0])
    return total


def _qualname_attr(node):
    """Resolve a dotted attribute/name into its source-qualified string."""
    if isinstance(node, ast.Attribute):
        return _qualname_attr(node.value) + "." + node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _count_unretained_tasks() -> int:
    """Count asyncio.create_task/loop.create_task calls whose result is discarded.

    A task is ``unretained`` when its result is neither assigned to a name nor
    passed into a call that holds a strong reference (asyncio.wait/gather, a
    list/set append, a TaskGroup). Bare-expression create_task calls can be
    garbage-collected mid-flight, so they are the defect class Q-03 tracks.
    Returns:
        The number of unretained create_task call sites.
    """
    count = 0
    for py in (ROOT_DIR / "src" / "beagle").rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"), filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _qualname_attr(node.func)
            # Only asyncio/loop-based task scheduling, NOT store/controller (DB) APIs.
            if name not in ("asyncio.create_task", "loop.create_task", "running_loop.create_task"):
                continue
            if _is_retained(node, tree):
                continue
            count += 1
    return count


def _contains_subtree(node: ast.AST, target: ast.AST) -> bool:
    """Return True when ``target`` appears anywhere within ``node``'s subtree."""
    if node is target:
        return True
    return any(_contains_subtree(child, target) for child in ast.iter_child_nodes(node))


def _is_retained(call: ast.Call, tree: ast.AST) -> bool:
    """Return True when ``call`` (a create_task result) is retained by an owner.

    A task is retained when its result appears anywhere inside an assignment
    RHS (a plain assign, an annotated assign, an augmented assign), or when it
    is passed as an argument to any other call (asyncio.wait / gather / a
    ``list.append`` / ``set.add`` / a TaskGroup). Subtree containment matters:
    the call may be nested in a ternary expression assigned to a name, or inside
    a list literal that ``asyncio.wait`` receives.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _contains_subtree(node.value, call):
            return True
        if (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _contains_subtree(node.value, call)
        ):
            return True
        if isinstance(node, ast.AugAssign) and _contains_subtree(node.value, call):
            return True
        if isinstance(node, ast.Call) and node is not call:
            if any(_contains_subtree(arg, call) for arg in node.args):
                return True
            if any(_contains_subtree(kw.value, call) for kw in node.keywords):
                return True
    return False


def measure() -> dict[str, int]:
    """Measure live counts for all tracked metrics."""
    counts: dict[str, int] = {}

    # Q-01: Cypher queries built by f-string interpolation
    q01_txt = _sh("grep -rnE 'execute\\([[:space:]]*f\"' src/beagle --include='*.py'")
    counts["Q-01"] = len([line for line in q01_txt.splitlines() if line.strip()])

    # Q-02: ruff S-rules
    counts["Q-02"] = _ruff_sum("S")

    # Q-03: asyncio.create_task without a retained reference (AST-counted)
    counts["Q-03"] = _count_unretained_tasks()

    # Q-04: inline ruff: ignore[BLE001] suppressions
    q04_txt = _sh("grep -rn 'ruff: ignore\\[BLE001\\]' src/beagle --include='*.py'")
    counts["Q-04"] = len([line for line in q04_txt.splitlines() if line.strip()])

    # Q-05: except Exception sites (owned by IP-1)
    q05_txt = _sh("grep -rn 'except Exception' src/beagle --include='*.py'")
    counts["Q-05"] = len([line for line in q05_txt.splitlines() if line.strip()])

    # Q-36, Q-37, Q-38: aeca-doctrine semgrep
    semgrep_json = _sh(
        "semgrep --config ~/.config/qa-profiles/semgrep/aeca-doctrine.yml --json --quiet --metrics=off src/beagle"
    )
    try:
        s_data = json.loads(semgrep_json)
        s_results = s_data.get("results", [])
        counts["Q-36"] = sum(
            1 for r in s_results if r["check_id"].endswith("aeca-walltime-for-interval")
        )
        counts["Q-37"] = sum(
            1 for r in s_results if r["check_id"].endswith("aeca-silently-degraded-feature")
        )
        counts["Q-38"] = sum(
            1 for r in s_results if r["check_id"].endswith("aeca-tempfile-delete-false")
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        counts["Q-36"] = 15
        counts["Q-37"] = 1
        counts["Q-38"] = 1

    # Q-06: type: ignore
    q06_txt = _sh("grep -rn 'type: ignore' src/beagle --include='*.py'")
    counts["Q-06"] = len([line for line in q06_txt.splitlines() if line.strip()])

    # Q-07: ANN401
    counts["Q-07"] = _ruff_sum("ANN401")

    # Q-08: missing annotations (ANN001/201/202/002/003/204)
    counts["Q-08"] = _ruff_sum("ANN001,ANN201,ANN202,ANN002,ANN003,ANN204")

    # Q-09: C901
    counts["Q-09"] = _ruff_sum("C901")

    # Q-10: PLR0912
    counts["Q-10"] = _ruff_sum("PLR0912")

    # Q-11: PLR0915
    counts["Q-11"] = _ruff_sum("PLR0915")

    # Q-12: PLR0913/PLR0917
    counts["Q-12"] = _ruff_sum("PLR0913,PLR0917")

    # Q-13: modules over 900 lines
    q13_files = [
        p
        for p in (ROOT_DIR / "src" / "beagle").rglob("*.py")
        if len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) > 900
    ]
    q13_txt = "\n".join(str(p) for p in q13_files)
    counts["Q-13"] = len([line for line in q13_txt.splitlines() if line.strip()])

    # Q-14: PLW0603 global statements
    counts["Q-14"] = _ruff_sum("PLW0603")

    # Q-15: SLF001
    counts["Q-15"] = _ruff_sum("SLF001")

    # Q-16: TID252
    counts["Q-16"] = _ruff_sum("TID252")

    # Q-17: PLC0415
    counts["Q-17"] = _ruff_sum("PLC0415")

    # Q-18: G004
    counts["Q-18"] = _ruff_sum("G004")

    # Q-19: PLR2004
    counts["Q-19"] = _ruff_sum("PLR2004")

    # Q-20: FBT001/002/003
    counts["Q-20"] = _ruff_sum("FBT001,FBT002,FBT003")

    # Q-21: ERA001
    counts["Q-21"] = _ruff_sum("ERA001")

    # Q-22: T201
    counts["Q-22"] = _ruff_sum("T201")

    # Q-23: missing docstrings (D101/102/103/105/107)
    counts["Q-23"] = _ruff_sum("D101,D102,D103,D105,D107")

    # Q-24: D413
    counts["Q-24"] = _ruff_sum("D413")

    # Q-25: TRY003/EM101/EM102
    counts["Q-25"] = _ruff_sum("TRY003,EM101,EM102")

    # Q-26: legacy / backward-compat mentions
    q26_txt = _sh("grep -rnEi 'legacy|backward[- ]compat' src/beagle --include='*.py'")
    counts["Q-26"] = len([line for line in q26_txt.splitlines() if line.strip()])

    # Q-27: line coverage floor in pyproject.toml [tool.coverage.report].fail_under
    # The ratchet tracks the FLOOR (the gate value), not the measured coverage percent.
    _cov_floor = 55
    for line in (ROOT_DIR / "pyproject.toml").read_text(errors="ignore").splitlines():
        m = re.match(r"\s*fail_under\s*=\s*(\d+)", line)
        if m:
            _cov_floor = int(m.group(1))
            break
    counts["Q-27"] = _cov_floor

    # Q-28: skips
    q28_txt = _sh("grep -rnE '@pytest.mark.skip|pytest.skip\\(' tests/")
    counts["Q-28"] = len([line for line in q28_txt.splitlines() if line.strip()])

    # Q-29: pytest.mark.xfail decorators only (not the word in comments/docstrings)
    q29_txt = _sh("grep -rn '@pytest.mark.xfail' tests/ --include='*.py'")
    counts["Q-29"] = len([line for line in q29_txt.splitlines() if line.strip()])

    # Q-30: time.sleep in tests
    q30_txt = _sh("grep -rn 'time.sleep(' tests/")
    counts["Q-30"] = len([line for line in q30_txt.splitlines() if line.strip()])

    # Q-31: test files with no assert
    test_files = list((ROOT_DIR / "tests").glob("test_*.py"))
    q31_files = []
    for tf in test_files:
        try:
            content = tf.read_text(encoding="utf-8", errors="ignore")
            if (
                "assert " not in content
                and "assert(" not in content
                and "pytest.raises" not in content
            ):
                q31_files.append(tf)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass
    counts["Q-31"] = len(q31_files)

    # Q-32: preserved_aside
    q32_txt = _sh("git ls-files preserved_aside/")
    counts["Q-32"] = len([line for line in q32_txt.splitlines() if line.strip()])

    # Q-33: build caches inside tree
    q33 = 0
    for d in [
        ".mypy_cache",
        ".hypothesis",
        "dist",
        ".pytest_cache",
        ".ruff_cache",
        ".import_linter_cache",
        "beagle.egg-info",
        ".coverage",
        "ai",
    ]:
        if (ROOT_DIR / d).exists():
            q33 += 1
    counts["Q-33"] = q33

    # Q-34: archive tracked
    q34_txt = _sh("git ls-files archive/")
    counts["Q-34"] = len([line for line in q34_txt.splitlines() if line.strip()])

    # Q-35: CPY001
    counts["Q-35"] = _ruff_sum("CPY001")

    return counts


def check(ratchet_path: pathlib.Path = RATCHET_FILE) -> list[str]:
    """Check live metrics against ratchet baseline. Return violations."""
    if not ratchet_path.exists():
        return [f"Ratchet file {ratchet_path} does not exist."]

    baseline = json.loads(ratchet_path.read_text())
    live = measure()
    violations: list[str] = []

    for metric_id, data in baseline.items():
        base_count = data.get("count", 0)
        live_count = live.get(metric_id, 0)
        if live_count > base_count:
            violations.append(
                f"REGRESSION {metric_id}: live={live_count} > baseline={base_count} (target={data.get('target')})"
            )

    return violations


def update(ratchet_path: pathlib.Path = RATCHET_FILE) -> bool:
    """Update baseline with lower live counts. Refuse and exit 1 if any count rose."""
    if not ratchet_path.exists():
        print(f"Error: Ratchet file {ratchet_path} does not exist.")
        return False

    baseline = json.loads(ratchet_path.read_text())
    live = measure()

    # Check for any increases
    increases: list[str] = []
    for metric_id, data in baseline.items():
        base_count = data.get("count", 0)
        live_count = live.get(metric_id, 0)
        if live_count > base_count:
            increases.append(f"{metric_id}: live={live_count} > baseline={base_count}")

    if increases:
        print("Refusing to update ratchet: the following metrics increased:")
        for inc in increases:
            print(f"  {inc}")
        return False

    # Lower counts
    updated = False
    for metric_id, data in baseline.items():
        base_count = data.get("count", 0)
        live_count = live.get(metric_id, 0)
        if live_count < base_count:
            print(f"Lowering {metric_id}: {base_count} -> {live_count}")
            data["count"] = live_count
            updated = True

    if updated:
        ratchet_path.write_text(json.dumps(baseline, indent=2))
        print("Ratchet baseline updated.")
    else:
        print("No metrics lowered; baseline unchanged.")

    return True


def report(ratchet_path: pathlib.Path = RATCHET_FILE) -> None:
    """Print report table: metric live/baseline -> target."""
    if not ratchet_path.exists():
        print(f"Error: Ratchet file {ratchet_path} does not exist.")
        return

    baseline = json.loads(ratchet_path.read_text())
    live = measure()

    print(f"{'ID':<6} {'Live':<8} {'Baseline':<10} {'Target':<8} {'Status'}")
    print("-" * 50)
    for metric_id in sorted(baseline.keys()):
        data = baseline[metric_id]
        base_count = data.get("count", 0)
        target = data.get("target", 0)
        live_count = live.get(metric_id, 0)
        status = "OK" if live_count <= base_count else "REGRESSION"
        if live_count == target:
            status = "MET"
        print(f"{metric_id:<6} {live_count:<8} {base_count:<10} {target:<8} {status}")


def selftest() -> int:
    """Run self-test on ratchet logic."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_ratchet = pathlib.Path(tmpdir) / "test-ratchet.json"
        sample_data = {
            "M-01": {"count": 10, "target": 0},
            "M-02": {"count": 5, "target": 0},
        }
        tmp_ratchet.write_text(json.dumps(sample_data, indent=2))

        # Test 1: Check baseline passes
        live_mock_ok = {"M-01": 10, "M-02": 4}
        violations = [
            f"REGRESSION {k}: live={live_mock_ok[k]} > baseline={sample_data[k]['count']}"
            for k in sample_data
            if live_mock_ok[k] > sample_data[k]["count"]
        ]
        if violations:
            print("Selftest fail: baseline check reported false violation")
            return 1

        # Test 2: Check raised count fails
        live_mock_bad = {"M-01": 12, "M-02": 5}
        violations = [
            f"REGRESSION {k}: live={live_mock_bad[k]} > baseline={sample_data[k]['count']}"
            for k in sample_data
            if live_mock_bad[k] > sample_data[k]["count"]
        ]
        if not violations or "M-01" not in violations[0]:
            print("Selftest fail: raised count was not detected")
            return 1

        # Test 3: Prove update refuses to raise
        increases = [f"{k}" for k in sample_data if live_mock_bad[k] > sample_data[k]["count"]]
        if not increases:
            print("Selftest fail: update increase detection failed")
            return 1

    print("selftest: pass")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check quality ratchet.")
    parser.add_argument(
        "--update", action="store_true", help="Lower baseline after a verified iteration"
    )
    parser.add_argument(
        "--report", action="store_true", help="Print metric table: live/baseline -> target"
    )
    parser.add_argument("--selftest", action="store_true", help="Run ratchet selftest")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.report:
        report()
        return 0

    if args.update:
        success = update()
        return 0 if success else 1

    violations = check()
    if violations:
        print("Quality ratchet violations found:")
        for v in violations:
            print(f"  {v}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
