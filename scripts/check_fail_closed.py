#!/usr/bin/env python3
# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Fail-closed manifest gate (adapted from "fail-closed-hardening-003.xml", PHASE-1/2).

A deny-by-default setting looks like a defect to a tidying agent: an empty
fallback looks like an omission, a disabled fallback looks like a missing
feature, a strict mode looks like an avoidable error. Each is deliberate —
it makes the system STOP rather than DEGRADE. This gate makes each fail-closed
value explicit and REJECTS any change that loosens one.

The source brief was written against a hypothetical `schema = 3` config whose
manifest keys do not match the live tree. This script is built against the
REAL live layout under `~/.config/beagle`:

  [sandbox.microvm].allow_fallback       = false   (refuse subprocess degrade)
  [inference].allowlist_strict           = true    (strict model allowlist)
  [security_and_sandbox].fail_closed_firewall = true
  [security_and_sandbox].secret_scrubbing      = true
  [security_and_sandbox].microvm_deny_fallback = true

plus the CORE_CONFIG.toml [security] floor invariants. A value that has been
loosened from the required setting is a HARD failure (exit 1). This is the
PHASE-2 diff-aware check's static half: it catches a loosened value in the
committed config, and it is cheap to run on every commit.

Read-only: never mutates config.

Usage:
    python3 scripts/check_fail_closed.py [--config-root ~/.config/beagle]
    python3 scripts/check_fail_closed.py --json   # machine-readable
    python3 scripts/check_fail_closed.py --selftest

Exit codes: 0 all invariants hold, 1 violation, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / ".config/beagle"

# (toml_path_relative_to_root, [key_path...], required_value, human_risk)
# ``key_path`` is the dotted chain of tables, e.g. ("sandbox", "microvm") then
# the scalar key. The required value is the deny-by-default setting.
FAIL_CLOSED_INVARIANTS: list[tuple[str, tuple[str, ...], str, Any, str]] = [
    (
        "beagle_core_config/config.toml",
        ("sandbox", "microvm"),
        "allow_fallback",
        False,
        "Untrusted code degrades to the host runtime without hardware isolation",
    ),
    (
        "beagle_core_config/config.toml",
        ("inference",),
        "allowlist_strict",
        True,
        "A non-strict allowlist lets an unallowlisted model route silently",
    ),
    (
        "beagle_core_config/config.toml",
        ("security_and_sandbox",),
        "fail_closed_firewall",
        True,
        "The semantic firewall becomes optional (fail-open default)",
    ),
    (
        "beagle_core_config/config.toml",
        ("security_and_sandbox",),
        "secret_scrubbing",
        True,
        "A secret reaches a log or a model provider without scrubbing",
    ),
    (
        "beagle_core_config/config.toml",
        ("security_and_sandbox",),
        "microvm_deny_fallback",
        True,
        "Untrusted code runs on the host without hardware isolation",
    ),
    (
        "CORE_CONFIG.toml",
        ("security",),
        "fail_closed_firewall",
        True,
        "Semantic firewall floor invariant loosened",
    ),
    (
        "CORE_CONFIG.toml",
        ("security",),
        "secret_scrubbing",
        True,
        "Secret scrubbing floor invariant loosened",
    ),
    (
        "CORE_CONFIG.toml",
        ("security",),
        "no_plaintext_secrets_on_disk",
        True,
        "Plaintext secret store permitted on disk",
    ),
    (
        "CORE_CONFIG.toml",
        ("security",),
        "memory_only_secret_delivery",
        True,
        "Secrets may persist outside memory-only delivery",
    ),
    (
        "CORE_CONFIG.toml",
        ("security",),
        "sandbox",
        "deny_by_default",
        "Sandbox policy loosened from deny-by-default",
    ),
]

# A boolean or string setting named with a permissive prefix that, if set to a
# permissive value, is a fail-open risk. PHASE-2 detection-rule 3.
_LOOSE_PREFIX = ("allow_", "permit_", "skip_", "disable_", "bypass_")


def _load(root: Path, rel: str) -> dict:
    p = root / rel
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:  # noqa: BLE001 - report, never guess
        return {}


def _get(data: dict, chain: tuple[str, ...]) -> dict | Any:
    cur: Any = data
    for part in chain:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _setting(data: dict, chain: tuple[str, ...], key: str) -> Any | None:
    section = _get(data, chain)
    if not isinstance(section, dict):
        return None
    return section.get(key)


def _missing(reason: str) -> str:
    return reason


def check_invariants(root: Path) -> tuple[list[str], list[str]]:
    """Return (failures, loose_findings) for the config root.

    ``failures`` is a list of fail-closed invariant violations (empty when all
    hold). ``loose_findings`` is an advisory list of permissive-prefixed
    booleans set to true (informational, not a hard failure).
    """
    fails: list[str] = []
    # Track the loosening scan across both files.
    loose_findings: list[str] = []

    for rel, chain, key, required, reason in FAIL_CLOSED_INVARIANTS:
        data = _load(root, rel)
        val = _setting(data, chain, key)
        if val is None:
            fails.append(f"[{rel}] {key} is MISSING (required {required!r}) — {reason}")
            continue
        if val != required:
            fails.append(
                f"[{rel}] {key} = {val!r}, expected {required!r} — {reason}"
            )

    # PHASE-2 detection-rule 3: warn on any permissive-prefixed bool that is
    # set to a permissive value. We don't hard-fail on these (some are
    # legitimate, e.g. allow_network on a specific tool) — we surface them.
    for rel in ("beagle_core_config/config.toml", "CORE_CONFIG.toml"):
        data = _load(root, rel)
        for section_key, section in data.items():
            if not isinstance(section, dict):
                continue
            for k, v in section.items():
                if k.startswith(_LOOSE_PREFIX) and isinstance(v, bool) and v is True:
                    loose_findings.append(f"[{rel}] {section_key}.{k} = true")
    return fails, loose_findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    root = args.config_root.expanduser().resolve()
    if not root.is_dir():
        print(f"FAIL: not a directory: {root}", file=sys.stderr)
        return 2

    fails, loose = check_invariants(root)

    if args.json:
        print(json.dumps({"failures": fails, "loose_findings": loose}, indent=2))
        return 1 if fails else 0

    if fails:
        print(f"FAIL — {len(fails)} fail-closed invariant(s) violated\n")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("PASS — all fail-closed invariants hold")
    if loose:
        print("\nNote (advisory) — permissive-prefixed settings set to true:")
        for f in loose:
            print(f"  - {f}")
    return 0


def _selftest() -> int:
    """Verify the gate detects a loosened value."""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="fc_selftest_"))
    cfg = root / "beagle_core_config" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "[sandbox.microvm]\nallow_fallback = true\n"
        "[inference]\nallowlist_strict = true\n"
        "[security_and_sandbox]\nfail_closed_firewall = true\n"
        "secret_scrubbing = true\nmicrovm_deny_fallback = true\n"
    )
    core = root / "CORE_CONFIG.toml"
    core.write_text(
        "[security]\nfail_closed_firewall = true\nsecret_scrubbing = true\n"
        "no_plaintext_secrets_on_disk = true\nmemory_only_secret_delivery = true\n"
        'sandbox = "deny_by_default"\n'
    )
    fails, _ = check_invariants(root)
    # The loosened allow_fallback must be caught.
    assert any("allow_fallback" in f for f in fails), "selftest: allow_fallback loosening not caught"
    print("selftest PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
