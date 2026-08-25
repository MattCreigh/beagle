#!/usr/bin/env python3
# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Read-only config drift diagnostic for the live Beagle config root.

Adapted from the golden-master extract tooling ("New Agentic Suite.zip" /
"Supplement.zip", `00_extract.py`). That source was written against a
hypothetical `schema = 3` layout that CONFLICTS with the live registry-based
config; this script is rewritten against the ACTUAL live layout under
`~/.config/beagle` so it can run unmodified against the operator's tree.

It answers the four questions the consolidation audit needs, with evidence:

  1. Model drift  — every model identifier and the file(s) it appears in.
     An identifier in more than one file is a drift signal (the archive's
     "I1" invariant: model identity should live in ONE place, the fleet
     cards, with everything else referencing a capability class).
  2. Allowlist coherence — every model that flows through a ROUTING position
     (default_model, llm_model, fallback_chain, per-agent table,
     complexity_routing) must be present in `[models.allowed]`. This is the
     tree-wide complement to the runtime `validate_against_allowlist` and the
     single-file F6 test.
  3. Agent profile false-specialization — for `coding_agent_config/agents.toml`,
     which profiles differ from the fleet modal value only by prose fields
     (prompt/description/instruction), i.e. are prompt fragments wearing a
     distinct name rather than distinct capabilities.
  4. Credential-shaped strings — a quick scan for `sk-`, `ghp_`, `xoxb-`,
     `AKIA` shaped values that must never be on disk.

Read-only: this script never mutates config. It emits a manifest (JSON + MD)
to `--out` (default `./extract/` under the CWD, NOT under the config root).

Usage:
    python3 scripts/check_config_model_drift.py [--config-root ~/.config/beagle]
    python3 scripts/check_config_model_drift.py --out ./extract
    python3 scripts/check_config_model_drift.py --json   # JSON only, stdout

Exit codes: 0 clean / informational, 1 violations found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# A model identifier: a known bare family with a version/tag token. The
# provider/model (slash) form is deliberately excluded — it only appears in
# fleet cards / provider allowlists, which this scan skips, and the loose
# slash pattern produced path fragments (`301/301`, `bin/python3`).
MODEL_RE = re.compile(
    r"\b(?:gpt|claude|llama|qwen|deepseek|minimax|glm|mistral|gemma|phi|kimi|nemotron)"
    r"[-_.][a-z0-9._:-]*\d[a-z0-9._:-]*\b",
    re.IGNORECASE,
)
# A path is not a model: a model identifier carries a version/size/tag and
# never ends in a file extension.
_URL_RE = re.compile(r"https?://\S+")
_NOT_A_MODEL = re.compile(r"\.(?:toml|ya?ml|json|db|py|xml|md|log)$", re.IGNORECASE)
# Credential-shaped prefixes (memory-only JIT delivery doctrine).
CREDENTIAL_RE = re.compile(r"\b(?:sk-|ghp_|xoxb-|AKIA)[A-Za-z0-9_-]{12,}")

# Files under the config root that legitimately carry model identifiers.
# Fleet cards are the routing SSOT. providers.toml carries the allowlist
# (`[providers.*.allowed_models]`) which is policy, not routing.
FLEET_GLOBS = ("beagle_inference_config/inference/*.toml",)
ALLOWLIST_GLOB = "beagle_inference_config/providers.toml"


def _load_toml(p: Path) -> dict[str, Any]:
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return {"__parse_error__": f"{type(exc).__name__}: {exc}"}


def _scan_models(text: str) -> set[str]:
    """Return the set of model identifiers in *text*."""
    scan = _URL_RE.sub(" ", text)
    return {m for m in MODEL_RE.findall(scan) if not _NOT_A_MODEL.search(m)}


def scan_model_sites(root: Path) -> dict[str, list[str]]:
    """Map every model identifier to the files it appears in (drift signal)."""
    sites: dict[str, list[str]] = defaultdict(list)
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in {".toml", ".yaml", ".yml", ".json"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(root))
        for m in _flatten_model_refs(text, rel):
            sites[m].append(rel)
    return {m: sorted(set(v)) for m, v in sites.items()}


def _flatten_model_refs(text: str, rel: str) -> set[str]:
    """Model refs in a file; only include when the file is not a fleet card.

    Fleet cards (`inference/fleet_*.toml`) and the provider allowlist are the
    sanctioned locations. Everywhere else is a potential drift site.
    """
    if "fleet_" in rel or "providers.toml" in rel:
        return set()
    return _scan_models(text)


def scan_allowlist_coherence(root: Path) -> list[str]:
    """Every routing-position model must be in `[models.allowed]`.

    Routing positions read from `beagle_core_config/config.toml`:
      [goose].default_model, [goose].fallback_chain, [goose].default_pool_chain,
      [llm].default_model, [complexity_routing].<tier>, and the per-agent
      model table under [agents] (when present).
    """
    cfg_path = root / "beagle_core_config" / "config.toml"
    if not cfg_path.is_file():
        return []
    cfg = _load_toml(cfg_path)
    models = cfg.get("models", {})
    raw_allowed = models.get("allowed")
    if isinstance(raw_allowed, dict):
        allowed = {str(k).strip() for k, v in raw_allowed.items() if v}
    elif isinstance(raw_allowed, list):
        allowed = {str(m).strip() for m in raw_allowed if str(m).strip()}
    else:
        return ["no [models.allowed] in config.toml"]

    def norm(m: str) -> str:
        # The allowlist may carry a bare family while routing uses a :tag form.
        # Stripping any `:suffix` matches the /api/tags membership test used in
        # startup health-check (see config.toml <invariant> note).
        return m.split(":")[0] if ":" in m else m

    allowed_bare = {norm(m) for m in allowed}
    routing: set[str] = set()

    goose = cfg.get("goose", {}) or {}
    for key in ("default_model",):
        v = goose.get(key)
        if v:
            routing.add(str(v))
    for key in ("fallback_chain", "default_pool_chain"):
        for m in goose.get(key, []) or []:
            routing.add(str(m))
    llm = cfg.get("llm", {}) or {}
    if llm.get("default_model"):
        routing.add(str(llm["default_model"]))
    for m in (cfg.get("complexity_routing", {}) or {}).values():
        if isinstance(m, str) and m:
            routing.add(m)
    # Per-agent routing table (optional under [agents]).
    for v in (cfg.get("agents", {}) or {}).values():
        if isinstance(v, dict) and v.get("model"):
            routing.add(str(v["model"]))

    violations = []
    for m in sorted(routing):
        if norm(m) not in allowed_bare:
            violations.append(
                f"routing model {m!r} is not in [models.allowed] "
                f"(allowlist has {sorted(allowed)})"
            )
    return violations


def _norm(m: str) -> str:
    return m.split(":")[0] if ":" in m else m


def scan_agent_profiles(root: Path) -> dict[str, Any]:
    """False-specialization verdict over coding_agent_config/agents.toml."""
    p = root / "coding_agent_config" / "agents.toml"
    if not p.is_file():
        return {"missing": str(p)}
    data = _load_toml(p)
    profiles = {k: v for k, v in data.items() if isinstance(v, dict) and "model" in v}
    if not profiles:
        return {"profile_count": 0}

    PROSE = ("prompt", "instruction", "description", "system", "template",
             "backstory", "goal", "role", "contract", "purpose")
    field_vals: dict[str, Counter] = defaultdict(Counter)
    for prof in profiles.values():
        for k, v in prof.items():
            field_vals[k][repr(v)] += 1
    modal = {k: c.most_common(1)[0][0] for k, c in field_vals.items()}

    verdicts = {}
    for name, prof in profiles.items():
        differing = [k for k, v in prof.items() if repr(v) != modal.get(k)]
        prose_only = bool(differing) and all(
            any(tok in str(k).lower() for tok in PROSE) for k in differing
        )
        verdicts[name] = {
            "differing_fields": differing,
            "differs_by_prose_only": prose_only,
            "identical_to_modal": not differing,
        }
    return {
        "profile_count": len(profiles),
        "prose_only_count": sum(1 for v in verdicts.values() if v["differs_by_prose_only"]),
        "identical_count": sum(1 for v in verdicts.values() if v["identical_to_modal"]),
        "profiles": verdicts,
    }


def scan_credentials(root: Path) -> list[str]:
    """Credential-shaped strings in NON-secret files.

    A designated secret store (any ``secrets.yaml`` with restrictive mode)
    is the sanctioned memory-only delivery file and is exempt. A credential
    string anywhere else is a leakage risk.
    """
    hits = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        # Designated secret store: allowed to hold credentials if mode is 0600/0400.
        if p.name == "secrets.yaml":
            try:
                mode = p.stat().st_mode & 0o777
            except OSError:
                mode = 0o644
            if mode in (0o600, 0o400):
                continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CREDENTIAL_RE.search(text):
            hits.append(rel)
    return hits


def digest(d: dict[str, Any]) -> str:
    profiles = d["agent_profiles"]
    lines = [
        "# beagle config — drift diagnostic",
        "",
        "## Model-identifier drift (I1)",
        f"- distinct model identifiers: {len(d['models'])}",
        f"- identifiers in >1 file: {sum(1 for s in d['models'].values() if len(s) > 1)}",
    ]
    for m, sites in sorted(d["models"].items()):
        if len(sites) > 1:
            lines.append(f"  - `{m}` -> {', '.join(sites)}")
    lines += [
        "",
        "## Allowlist coherence",
    ]
    if d["allowlist_violations"]:
        lines += [f"  - {v}" for v in d["allowlist_violations"]]
    else:
        lines.append("  - all routing models are in [models.allowed]")
    lines += [
        "",
        "## Agent-profile statistics (coding_agent_config/agents.toml)",
        f"- profiles: {profiles.get('profile_count', 0)}",
        f"- differing only by prose: {profiles.get('prose_only_count', 0)}",
        f"- identical to modal: {profiles.get('identical_count', 0)}",
        "",
        "## Credentials on disk",
    ]
    if d["credentials"]:
        lines += [f"  - {h}" for h in d["credentials"]]
    else:
        lines.append("  - none (memory-only delivery upheld)")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-root", type=Path, default=Path.home() / ".config/beagle",
                    help="live config root (default ~/.config/beagle)")
    ap.add_argument("--out", type=Path, default=Path("./extract"))
    ap.add_argument("--json", action="store_true", help="emit JSON only, to stdout")
    args = ap.parse_args()

    root = args.config_root.expanduser().resolve()
    if not root.is_dir():
        print(f"FAIL: not a directory: {root}", file=sys.stderr)
        return 2

    report = {
        "config_root": str(root),
        "models": scan_model_sites(root),
        "allowlist_violations": scan_allowlist_coherence(root),
        "agent_profiles": scan_agent_profiles(root),
        "credentials": scan_credentials(root),
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 1 if report["allowlist_violations"] or report["credentials"] else 0

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps(report, indent=2, default=str))
    (args.out / "manifest.md").write_text(digest(report))
    print(digest(report))
    print(f"[written] {args.out / 'manifest.json'}\n[written] {args.out / 'manifest.md'}")
    return 1 if report["allowlist_violations"] or report["credentials"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
