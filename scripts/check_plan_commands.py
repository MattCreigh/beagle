#!/usr/bin/env python3
"""Plan-command gate — no executable plan block runs a stale command.

A plan that corrects a stale command must be able to quote that command in its
own prose, so a naive grep whose pattern lives inside the corpus it scans
cannot express the property this gate enforces. This checker lives OUTSIDE
plans/ and scans only the executable blocks, so a correction may quote a stale
command in <instruction> or <fact> without false-positiving.

Property:  no <verification>, <commands>, <baseline_commands> or
<final_commands> block under plans/ runs a stale command fragment.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Defect name -> literal stale command fragment. Seeded with the two that
# FU-3 corrects.
_STALE: dict[str, str] = {
    "skills-md-listing": "ls src/beagle/skills/*.md",
    "skill-list-index": ".list_index()",
}

# Only these block tags are executable. A correction must be able to quote the
# command it corrects, so instruction/fact/correction/contract are NOT scanned.
_EXEC_TAGS = {"verification", "commands", "baseline_commands", "final_commands"}


def scan(plans_dir: Path) -> list[str]:
    """Report stale command fragments found in executable plan blocks.

    Args:
        plans_dir: Directory tree to scan for ``*.xml`` plan files.

    Returns:
        A list of human-readable findings, one per stale fragment hit. Empty
        when the tree is clean.
    """
    findings: list[str] = []
    for plan in sorted(plans_dir.glob("*.xml")):
        try:
            root = ET.parse(plan).getroot()
        except ET.ParseError as exc:
            findings.append(f"{plan}: unparseable XML: {exc}")
            continue
        for elem in root.iter():
            if elem.tag not in _EXEC_TAGS:
                continue
            text = " ".join(elem.itertext())
            for defect, fragment in _STALE.items():
                if fragment in text:
                    findings.append(
                        f"{plan}: <{elem.tag}> runs stale command for {defect}: {fragment}"
                    )
    return findings


def _selftest() -> int:
    """Prove the gate reports a stale <verification> and ignores a quoted one.

    Returns:
        Exit code 1 on failure, 0 on pass.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # Half 1: a stale fragment in <verification> must be reported.
        d1 = Path(tmp) / "d1"
        d1.mkdir()
        bad = d1 / "bad.xml"
        bad.write_text(
            "<recipe><verification>\nls src/beagle/skills/*.md\n</verification></recipe>",
            encoding="utf-8",
        )
        found = scan(d1)
        if not any("skills-md-listing" in f for f in found):
            print("SELFTEST FAILED: stale <verification> not reported", file=sys.stderr)
            return 1

        # Half 2: the same fragment quoted in <instruction> must NOT be reported.
        # A separate directory so the half-1 finding cannot leak in.
        d2 = Path(tmp) / "d2"
        d2.mkdir()
        ok = d2 / "ok.xml"
        ok.write_text(
            "<recipe><instruction>replace `ls src/beagle/skills/*.md` with "
            "`ls src/beagle/skills/*.xml`</instruction></recipe>",
            encoding="utf-8",
        )
        found_ok = scan(d2)
        if any("skills/*.md" in f for f in found_ok):
            print("SELFTEST FAILED: a quoted correction was reported", file=sys.stderr)
            return 1
    print("selftest: pass")
    return 0


def main() -> int:
    """Entry point.

    Returns:
        Exit code 0 when the tree is clean, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plans-dir",
        default=Path(__file__).resolve().parent.parent / "plans",
        type=Path,
    )
    parser.add_argument("--selftest", action="store_true", help="Run the self-test")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    findings = scan(args.plans_dir)
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
