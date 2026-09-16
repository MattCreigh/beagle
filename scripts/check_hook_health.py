#!/usr/bin/env python3
"""Hook-health gate — make a silent hook failure fail a gate.

2679 hook failures over 13 sessions and 10 hours produced no alarm and no
test failure. goose records the failure at WARN in
~/.local/state/goose/logs/cli/*/*.log; no gate read that log. This script is
that gate.

It performs four checks:

1. Index mode — every hook command inside this repository must be executable
   in the git index (100755). The index, not the disk: a gate that reads the
   disk mode passes on a developer machine and fails in CI.
2. Interpreter — for each such hook, run the resolved interpreter with
   ``-c "import beagle"`` and fail when it exits non-zero.
3. Log — read the newest file under ~/.local/state/goose/logs/cli/*/*.log,
   find every ``"Plugin hook failed"`` line, group the failures by command,
   and for each distinct command re-test its CURRENT health. A command that
   is healthy now produced a historical line and is reported as ignored on
   stdout without affecting the exit code. A command that is still broken,
   or that no longer exists, is a finding. This makes the check
   self-clearing: repairing a hook clears the gate with no new session and
   no bypass.
4. Report freshness — read ~/.beagle/context_report.json and fail when
   ``schema_version`` is absent, or when the file is older than 3600 seconds
   while a goose session updated within that window exists.

Exit code 0 → all checks pass. Exit code 1 → at least one finding.

``--selftest`` writes a temporary log file containing one synthetic
``"Plugin hook failed"`` line, points check 3 at it, and confirms the gate
reports the finding. It prints ``selftest: pass`` and exits 0 when it does.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / ".agents" / "plugins"
GOOSE_LOG_DIR = Path.home() / ".local" / "state" / "goose" / "logs" / "cli"
CONTEXT_REPORT = Path.home() / ".beagle" / "context_report.json"
BOOTSTRAP_RECEIPT = Path.home() / ".beagle" / "bootstrap_run.json"
STALENESS_SECONDS = 3600


def _iter_hook_commands() -> list[tuple[str, str]]:
    """Yield (command, hooks_json_path) for every hook command in the repo.

    Returns:
        A list of (command, hooks_json_path) tuples. Commands that are not
        inside this repository are skipped.
    """
    found: list[tuple[str, str]] = []
    if not PLUGINS_DIR.is_dir():
        return found
    for hooks_json in PLUGINS_DIR.rglob("hooks.json"):
        try:
            data = json.loads(hooks_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for event_hooks in data.get("hooks", {}).values():
            for block in event_hooks:
                for hook in block.get("hooks", []):
                    cmd = hook.get("command", "")
                    if cmd and cmd.startswith(str(REPO_ROOT)):
                        found.append((cmd, str(hooks_json)))
    return found


def _index_mode(path: Path) -> str | None:
    """Return the git index mode for a path, or None when untracked.

    Args:
        path: The file to inspect.

    Returns:
        The mode string (e.g. "100755"), or None when the file is not
        tracked in the index.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        out = subprocess.run(
            [git_bin, "ls-files", "-s", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not out.strip():
        return None
    return out.split()[0]


def _resolve_interpreter(command: str) -> str | None:
    """Resolve the interpreter a hook command runs under.

    A command of the form ``<interpreter> <script>`` yields the interpreter.
    A bare script path yields the interpreter the hook's own bootstrap would
    re-exec into: the ``BEAGLE_HOOK_PYTHON`` env var, then ``[hook].interpreter``
    in the context-management TOML. Returns None when the interpreter cannot
    be resolved.

    Args:
        command: The hook command string.

    Returns:
        The interpreter path, or None.
    """
    parts = command.split()
    if not parts:
        return None
    if not parts[0].endswith(".py"):
        return parts[0]

    # Bare script — resolve the interpreter the hook's bootstrap would use.
    env_value = os.environ.get("BEAGLE_HOOK_PYTHON", "")
    if env_value:
        return env_value

    # Read [hook].interpreter from the context-management TOML, mirroring
    # scripts/hooks/_hook_bootstrap.py.
    import tomllib

    candidates: list[Path] = []
    env_dir = os.environ.get("BEAGLE_CONFIG_DIR", "")
    if env_dir:
        candidates.append(Path(env_dir) / "beagle_core_config" / "context_management.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME", "") or str(Path.home() / ".config")
    candidates.append(Path(xdg) / "beagle" / "beagle_core_config" / "context_management.toml")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        interpreter = str(data.get("hook", {}).get("interpreter", "") or "")
        if interpreter:
            return interpreter
    return None


def _check_index_mode() -> list[str]:
    """Check 1 — every in-repo hook command is executable in the index."""
    findings: list[str] = []
    for command, hooks_json in _iter_hook_commands():
        # The command is an absolute path inside the repo.
        path = Path(command)
        mode = _index_mode(path)
        if mode is None:
            findings.append(
                f"check 1: {path} (declared in {hooks_json}) is not tracked in the git index"
            )
        elif mode != "100755":
            findings.append(
                f"check 1: {path} (declared in {hooks_json}) has index mode {mode}, not 100755"
            )
    return findings


def _check_interpreter() -> list[str]:
    """Check 2 — every in-repo hook's command is healthy.

    Beagle-python hooks must have an interpreter that can ``import beagle``;
    standalone product hooks (``qup hook …``) are healthy when the tool's own
    readiness check passes.
    """
    findings: list[str] = []
    for command, hooks_json in _iter_hook_commands():
        interpreter = _resolve_interpreter(command)
        if interpreter is None:
            findings.append(
                f"check 2: cannot resolve interpreter for {command} (declared in {hooks_json})"
            )
            continue
        script = _extract_script_path(command) or ""
        if _is_standalone_tool(script):
            ok, reason = _tool_selfcheck(script)
            if not ok:
                findings.append(f"check 2: {reason} (declared in {hooks_json})")
            continue
        try:
            result = subprocess.run(
                [interpreter, "-c", "import beagle"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            findings.append(
                f"check 2: interpreter {interpreter} for {command} failed to run: {exc}"
            )
            continue
        if result.returncode != 0:
            findings.append(
                f"check 2: interpreter {interpreter} for {command} cannot import beagle "
                f"(rc={result.returncode})"
            )
    return findings


def _newest_goose_log() -> Path | None:
    """Return the newest goose CLI log file, or None when none exists."""
    if not GOOSE_LOG_DIR.is_dir():
        return None
    candidates = sorted(GOOSE_LOG_DIR.rglob("*.log"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _declared_script_paths() -> set[str]:
    """Return the set of script paths currently declared in hooks.json.

    Reads every ``hooks.json`` under the repository plugins dir and the
    user-level ``~/.agents/plugins`` dir, and normalises each command to its
    script path via ``_extract_script_path``. A script path that is not in
    this set is not part of this system's hook surface — it is a foreign or
    removed command from another project, and a log line naming it is not a
    finding.

    Returns:
        The set of declared script paths.
    """
    declared: set[str] = set()
    roots = [PLUGINS_DIR, Path.home() / ".agents" / "plugins"]
    for root in roots:
        if not root.is_dir():
            continue
        for hooks_json in root.rglob("hooks.json"):
            try:
                data = json.loads(hooks_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for event_hooks in data.get("hooks", {}).values():
                for block in event_hooks:
                    for hook in block.get("hooks", []):
                        cmd = hook.get("command", "")
                        if cmd:
                            script = _extract_script_path(cmd)
                            if script:
                                declared.add(script)
    return declared


def _extract_script_path(command: str) -> str | None:
    """Extract the script path from a hook command string.

    Handles both a bare script path (``/path/to/hook.py``) and an
    ``<interpreter> <script>`` form (``/usr/bin/python3 "$HOME/.../hook.py"``),
    expanding ``$HOME`` / ``${VAR}`` references. Returns None when no script
    path can be identified.

    Args:
        command: The hook command string as recorded in the goose log.

    Returns:
        The expanded script path, or None.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    expanded = [os.path.expandvars(p) for p in parts]
    for p in expanded:
        if p.endswith((".py", ".sh")):
            return p
    # Fall back to the last token that looks like a path.
    for p in reversed(expanded):
        if p.startswith("/") or p.startswith("$"):
            return p
    return None


def _is_standalone_tool(script: str) -> bool:
    """True when the hook command drives a standalone product binary (e.g. qup).

    Standalone tools carry their own venv and must not be judged by beagle's
    import criterion — ``qup hook`` runs from pipx and imports nothing of
    beagle's by design. Health for these is "the tool itself reports ready".
    """
    return script.rstrip("/").endswith(("/bin/qup", "/qup")) or script == "qup"


def _tool_selfcheck(script: str) -> tuple[bool, str]:
    """Run the tool's own readiness command (`<tool> --version`)."""
    try:
        result = subprocess.run(
            [script, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"standalone tool {script} failed to run: {exc}"
    if result.returncode != 0:
        return False, f"standalone tool {script} --version exited {result.returncode}"
    return True, "healthy"


def _command_health(command: str) -> tuple[bool, str]:
    """Re-test a hook command's current health.

    A command is healthy when its script exists on disk, is executable in
    the git index when it is inside the repository, and its resolved
    interpreter can import beagle. Reuses ``_index_mode`` and
    ``_resolve_interpreter`` rather than duplicating checks 1 and 2.

    Args:
        command: The hook command string as recorded in the goose log.

    Returns:
        A (healthy, reason) tuple. ``reason`` names the failing test when
        unhealthy, or "healthy" when healthy.
    """
    script = _extract_script_path(command)
    if script is None:
        return False, f"cannot extract a script path from {command!r}"
    script_path = Path(script)
    if not script_path.is_file():
        return False, f"script {script} does not exist on disk"
    try:
        script_path.relative_to(REPO_ROOT)
        in_repo = True
    except ValueError:
        in_repo = False
    if in_repo:
        mode = _index_mode(script_path)
        if mode is None:
            return False, f"{script} is not tracked in the git index"
        if mode != "100755":
            return False, f"{script} has index mode {mode}, not 100755"
    if _is_standalone_tool(script):
        return _tool_selfcheck(script)
    interpreter = _resolve_interpreter(script)
    if interpreter is None:
        return False, f"cannot resolve interpreter for {script}"
    try:
        result = subprocess.run(
            [interpreter, "-c", "import beagle"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"interpreter {interpreter} for {script} failed to run: {exc}"
    if result.returncode != 0:
        return (
            False,
            f"interpreter {interpreter} for {script} cannot import beagle (rc={result.returncode})",
        )
    return True, "healthy"


def _check_log(log_path: Path | None = None, declared: set[str] | None = None) -> list[str]:
    """Check 3 — no declared hook has a failure whose cause is still present.

    Reads the newest goose log, finds every ``"Plugin hook failed"`` line,
    groups the failures by command, and for each distinct command re-tests
    its CURRENT health. A command that is healthy now produced a historical
    line and is reported as ignored on stdout without affecting the exit
    code. A command that is still broken, or that no longer exists, is a
    finding. This makes the check self-clearing: repairing a hook clears the
    gate with no new session and no bypass.

    Args:
        log_path: Override the log file to inspect (used by --selftest).
        declared: Override the set of declared script paths (used by
            --selftest). Defaults to the live hooks.json set.

    Returns:
        A list of findings (empty when every failed command is healthy now).
    """
    target = log_path or _newest_goose_log()
    if target is None:
        return ["check 3: no goose CLI log found under " + str(GOOSE_LOG_DIR)]
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"check 3: cannot read {target}: {exc}"]

    # Parse each failure line. A line that does not parse is itself a finding.
    failures: list[dict[str, str]] = []
    unparsed = 0
    for line in text.splitlines():
        if '"Plugin hook failed"' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        fields = data.get("fields", {})
        failures.append(
            {
                "command": str(fields.get("command", "")),
                "plugin": str(fields.get("plugin", "")),
                "event": str(fields.get("event", "")),
                "timestamp": str(data.get("timestamp", "")),
            }
        )

    findings: list[str] = []
    if unparsed:
        findings.append(
            f"check 3: {unparsed} 'Plugin hook failed' line(s) in {target} did not parse as JSON"
        )

    # Group by script path, keep the newest per command, and count per
    # command. Only script paths currently declared in a hooks.json are in
    # scope: a log line naming a foreign or removed command from another
    # project is not a finding about this system's hook surface.
    if declared is None:
        declared = _declared_script_paths()
    newest: dict[str, dict[str, str]] = {}
    counts: dict[str, int] = {}
    for f in failures:
        script = _extract_script_path(f["command"])
        if script is None or script not in declared:
            continue
        cmd = f["command"]
        counts[cmd] = counts.get(cmd, 0) + 1
        if cmd not in newest or f["timestamp"] > newest[cmd]["timestamp"]:
            newest[cmd] = f

    for cmd, f in sorted(newest.items()):
        healthy, reason = _command_health(cmd)
        if healthy:
            print(f"check 3: ignoring {counts[cmd]} historical failure(s) for {cmd} — healthy now")
            continue
        findings.append(
            f"check 3: hook {cmd} (plugin={f['plugin']}, event={f['event']}) "
            f"failed at {f['timestamp']} and is still unhealthy: {reason}"
        )
    return findings


def _check_report_freshness() -> list[str]:
    """Check 4 — the context report is fresh and schema-versioned."""
    if not CONTEXT_REPORT.is_file():
        return [f"check 4: context report absent at {CONTEXT_REPORT}"]
    try:
        data = json.loads(CONTEXT_REPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"check 4: context report unreadable: {exc}"]
    if "schema_version" not in data:
        return [f"check 4: context report at {CONTEXT_REPORT} has no schema_version"]

    # wall-clock-ok: compares against a persisted mtime timestamp
    age = time.time() - CONTEXT_REPORT.stat().st_mtime  # nosemgrep: aeca-walltime-for-interval
    if age > STALENESS_SECONDS:
        # The subscriber skips writes BELOW the warning threshold by design, so a
        # light session legitimately leaves an old report behind. Staleness is
        # only a defect when the last recorded state said utilization was at or
        # above the write threshold — i.e. a write was expected and never came.
        try:
            utilization = float(data.get("utilization_pct", 0.0))
        except (TypeError, ValueError):
            utilization = 1.0  # unparseable state → keep failing loud
        log = _newest_goose_log()
        if log is not None:
            log_age = time.time() - log.stat().st_mtime  # nosemgrep: aeca-walltime-for-interval
            if log_age < STALENESS_SECONDS and utilization >= 0.50:
                return [
                    f"check 4: context report at {CONTEXT_REPORT} is {int(age)}s old "
                    f"while a goose session updated within {STALENESS_SECONDS}s exists"
                ]
    return []


def _iter_hook_entrypoints(hooks_dir: Path | None = None) -> list[tuple[Path, str]]:
    """Yield (script_path, claimed_event) for every executable hook entrypoint.

    An entrypoint is an executable ``*.py`` under ``scripts/hooks/`` whose
    module docstring contains the phrase "fires on" followed by a goose event
    name. ``_hook_bootstrap.py`` is a library, not an entrypoint, and is
    exempt (matched on the leading underscore).

    Args:
        hooks_dir: Override the hooks directory (used by --selftest).

    Returns:
        A list of (script_path, claimed_event) tuples.
    """
    target = hooks_dir or (REPO_ROOT / "scripts" / "hooks")
    if not target.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    for script in target.glob("*.py"):
        if script.name.startswith("_"):
            continue
        try:
            text = script.read_text(encoding="utf-8")
        except OSError:
            continue
        # Match "fires on <event>" in the module docstring, where <event> is
        # one of the goose event names. Allow a few intervening words (e.g.
        # "fires on the goose SessionStart event"). "fires on every
        # context-fold" is not a claim about a goose event and must not match.
        import re

        m = re.search(
            r"fires on\s+(?:\w+\s+){0,3}(PreToolUse|PostToolUse|AfterFileEdit|SessionStart|SessionEnd|UserPromptSubmit)",
            text,
        )
        if m:
            found.append((script, m.group(1)))
    return found


def _check_orphaned_entrypoint(
    hooks_dir: Path | None = None, plugins_dir: Path | None = None
) -> list[str]:
    """Check 5 — every entrypoint that claims an event is wired to it.

    For every executable ``*.py`` under ``scripts/hooks/`` whose docstring
    claims it "fires on <event>", assert that some hooks.json under
    ``.agents/plugins/*/hooks/hooks.json`` declares that entrypoint.

    Args:
        hooks_dir: Override the hooks directory (used by --selftest).
        plugins_dir: Override the plugins directory (used by --selftest).

    Returns:
        A list of findings (empty when every entrypoint is wired).
    """
    findings: list[str] = []
    target_plugins = plugins_dir or PLUGINS_DIR
    # Collect the set of declared entrypoint paths.
    declared: set[str] = set()
    if target_plugins.is_dir():
        for hooks_json in target_plugins.rglob("hooks.json"):
            try:
                data = json.loads(hooks_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for event_hooks in data.get("hooks", {}).values():
                for block in event_hooks:
                    for hook in block.get("hooks", []):
                        cmd = hook.get("command", "")
                        if cmd and cmd.startswith(str(REPO_ROOT)):
                            declared.add(str(Path(cmd).resolve()))

    for script, claimed_event in _iter_hook_entrypoints(hooks_dir):
        resolved = str(script.resolve())
        if resolved not in declared:
            findings.append(
                f"check 5: {script} claims it fires on {claimed_event} but no "
                f"hooks.json under {target_plugins} declares it"
            )
    return findings


def _check_bootstrap_receipt(receipt_path: Path | None = None) -> list[str]:
    """Check 6 — the bootstrap receipt exists and is well-formed.

    Reads ``~/.beagle/bootstrap_run.json``. Fails when the file is absent,
    when its ``timestamp`` is not timezone-aware, or when any of the five
    system booleans is absent. Does not fail on a ``false`` value: a system
    that ran and reported failure is a different finding, covered by the
    goose log.

    Args:
        receipt_path: Override the receipt path (used by --selftest).

    Returns:
        A list of findings (empty when the receipt is present and valid).
    """
    target = receipt_path or BOOTSTRAP_RECEIPT
    if not target.is_file():
        return [f"check 6: bootstrap receipt absent at {target}"]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"check 6: bootstrap receipt unreadable: {exc}"]

    findings: list[str] = []
    ts = data.get("timestamp", "")
    if not ts:
        findings.append(f"check 6: bootstrap receipt at {target} has no timestamp")
    elif not (ts.endswith("+00:00") or ts.endswith("Z") or "+" in ts):
        findings.append(f"check 6: bootstrap receipt timestamp {ts!r} is not timezone-aware")

    for key in (
        "render",
        "rag_staleness",
        "auto_hydrate",
        "registry_sync",
        "rehydration_checkpoint",
    ):
        if key not in data:
            findings.append(f"check 6: bootstrap receipt at {target} is missing {key}")
    return findings


def _run_selftest() -> int:
    """Run the self-test: prove checks 3, 5 and 6 catch synthetic failures.

    Returns:
        0 when the self-test passes, 1 otherwise.
    """
    tmp_path: Path | None = None
    try:
        # Check 3a: a synthetic "Plugin hook failed" line naming a command
        # that is currently HEALTHY must produce NO finding (the regression
        # test for this defect — a historical failure is not a finding).
        healthy_cmd = str(REPO_ROOT / "scripts" / "hooks" / "auto_compact.py")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-01T00:00:00Z",
                        "level": "WARN",
                        "fields": {
                            "message": "Plugin hook failed",
                            "plugin": "beagle-context-management",
                            "event": "PostToolUse",
                            "command": healthy_cmd,
                        },
                    }
                )
                + "\n"
            )
            tmp_path = Path(fh.name)
        findings = _check_log(log_path=tmp_path)
        if findings:
            print("selftest: FAIL — check 3 reported a healthy command as a finding")
            return 1

        # Check 3b: a synthetic line naming a command that is DECLARED but
        # MISSING on disk must produce a finding (a vanished hook is a real
        # problem). The declared set is overridden so the test does not
        # depend on the live hooks.json.
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_script = str(Path(tmpdir) / "vanished_hook.py")
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".log", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(
                    json.dumps(
                        {
                            "timestamp": "2026-08-01T00:00:00Z",
                            "level": "WARN",
                            "fields": {
                                "message": "Plugin hook failed",
                                "plugin": "beagle-test",
                                "event": "PostToolUse",
                                "command": missing_script,
                            },
                        }
                    )
                    + "\n"
                )
                tmp_path = Path(fh.name)
            findings = _check_log(log_path=tmp_path, declared={missing_script})
            if not findings:
                print("selftest: FAIL — check 3 did not report the missing command")
                return 1

        # Check 3c: a synthetic line that does not parse as JSON is a finding.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as fh:
            fh.write('not json "Plugin hook failed"\n')
            tmp_path = Path(fh.name)
        findings = _check_log(log_path=tmp_path)
        if not findings:
            print("selftest: FAIL — check 3 did not report the unparseable line")
            return 1

        # Check 5: an entrypoint whose docstring claims an event that no
        # hooks.json declares. Build a temp hooks dir with an orphaned
        # entrypoint and a temp plugins dir with no hooks.json declaring it.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_plugins = Path(tmpdir) / "plugins"
            tmp_plugins.mkdir(parents=True)
            tmp_hooks = Path(tmpdir) / "hooks"
            tmp_hooks.mkdir(parents=True)
            orphan = tmp_hooks / "orphan_hook.py"
            orphan.write_text(
                '"""Orphan hook — fires on SessionStart but is not wired."""\n'
                "def main() -> int:\n    return 0\n",
                encoding="utf-8",
            )
            orphan_findings = _check_orphaned_entrypoint(
                hooks_dir=tmp_hooks, plugins_dir=tmp_plugins
            )
            if not orphan_findings:
                print("selftest: FAIL — check 5 did not report the orphaned entrypoint")
                return 1

        # Check 6: a missing receipt.
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_receipt = Path(tmpdir) / "bootstrap_run.json"
            receipt_findings = _check_bootstrap_receipt(receipt_path=missing_receipt)
            if not receipt_findings:
                print("selftest: FAIL — check 6 did not report the missing receipt")
                return 1

        print("selftest: pass")
        return 0
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Beagle hook-health gate")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the self-test and exit",
    )
    args = parser.parse_args()

    if args.selftest:
        return _run_selftest()

    findings: list[str] = []
    findings.extend(_check_index_mode())
    findings.extend(_check_interpreter())
    findings.extend(_check_log())
    findings.extend(_check_report_freshness())
    findings.extend(_check_orphaned_entrypoint())
    findings.extend(_check_bootstrap_receipt())

    if findings:
        for f in findings:
            print(f, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
