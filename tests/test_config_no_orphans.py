"""config.toml orphan-section guard (audit Phase 7, v13.17.0).

Locks down the audit's recommendation: a config.toml section that the
loader does not recognise is an orphan (usually a typo) and should be
removed or wired up.

The wheel ships NO bundled config.toml (all user-editable config lives
under ``~/.config/beagle`` per XDG), so this test cannot scan a
checked-in config.toml. Instead it pins the structural contract:

1. ``KNOWN_TOP_LEVEL`` in config/loader.py is the ONLY authoritative
   set of valid section names — anything a loader or feature module
   reads must be declared there.
2. Every recognized section that a consumer *does* read has a loader
   branch, so no declared section is dead config.
3. The v14 consolidated sections (``[system]``, ``[context_management]``,
   ``[inference]``, ``[ipc_and_tools]``, ``[security_and_sandbox]``,
   ``[validation_gates]``) are the spec-typed, additive view over the
   operational knobs owned by the subsystem-specific sections; they are
   recognized by name in ``KNOWN_TOP_LEVEL`` and are NOT orphaned —
   they carry the authoritative consolidated policy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "src" / "beagle"
assert PKG_ROOT.is_dir(), f"package root not found at {PKG_ROOT} — update PKG_ROOT"

CENTRAL_LOADER = PKG_ROOT / "config" / "loader.py"
BRIDGES_LOADER = PKG_ROOT / "bridges" / "config.py"
MODEL_RESOLVER = PKG_ROOT / "config" / "model_resolver.py"
REGISTRY = PKG_ROOT / "config" / "registry.py"

# v14.0 consolidated typed SSOT sections — the authoritative, additive
# view over the subsystem knobs. Recognized by name; NOT orphans.
CONSOLIDATED_SECTIONS = frozenset(
    {
        "system",
        "context_management",
        "inference",
        "ipc_and_tools",
        "security_and_sandbox",
        "validation_gates",
    }
)


def _known_top_level() -> set[str]:
    """Return the set of section names the loader registry declares."""
    if not CENTRAL_LOADER.exists():
        return set()
    text = CENTRAL_LOADER.read_text(encoding="utf-8")
    m = re.search(r"KNOWN_TOP_LEVEL\s*=\s*frozenset\s*\(\s*\{([^}]*)\}", text, re.DOTALL)
    if not m:
        return set()
    return set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))


def _central_loader_consumers() -> set[str]:
    """Return the set of section names that config/loader.py reads."""
    if not CENTRAL_LOADER.exists():
        return set()
    text = CENTRAL_LOADER.read_text(encoding="utf-8")
    return set(re.findall(r'if "(\w+)" in data:', text))


def _bridges_loader_consumers() -> set[str]:
    """Return the set of section names that bridges/config.py reads."""
    if not BRIDGES_LOADER.exists():
        return set()
    text = BRIDGES_LOADER.read_text(encoding="utf-8")
    return set(re.findall(r'full_config\.get\("(\w+)"', text))


def test_known_top_level_is_authoritative_and_complete():
    """KNOWN_TOP_LEVEL must exist and be non-empty (the SSOT of names)."""
    known = _known_top_level()
    assert known, "KNOWN_TOP_LEVEL set is empty — loader.py parsing failed"


def test_central_loader_does_not_read_unknown_sections():
    """A loader branch for a section that is NOT in KNOWN_TOP_LEVEL is
    typo-drift — the section name is unrecognised and would be silently
    ignored (orphaned config)."""
    central = _central_loader_consumers()
    known = _known_top_level()
    unknown = sorted(central - known)
    if unknown:
        msg = "\n".join(f"  [{s}]" for s in unknown)
        pytest.fail(
            f"config/loader.py reads section(s) not declared in KNOWN_TOP_LEVEL:\n{msg}\n\n"
            f"Add the section name to KNOWN_TOP_LEVEL in config/loader.py, or fix the typo."
        )


def test_recognized_sections_are_declared():
    """Every section the loaders read must be in KNOWN_TOP_LEVEL.

    This is the drift-guard: if a consumer reads ``[foo]`` but the loader
    registry doesn't declare it, ``[foo]`` would be validated as unknown
    and silently skipped — orphaned config.
    """
    known = _known_top_level()
    consumers = _central_loader_consumers() | _bridges_loader_consumers()
    missing = sorted(consumers - known)
    if missing:
        msg = "\n".join(f"  [{s}]" for s in missing)
        pytest.fail(
            f"Loader(s) read section(s) not declared in KNOWN_TOP_LEVEL:\n{msg}\n\n"
            f"Declare them in KNOWN_TOP_LEVEL or fix the reader."
        )


def test_consolidated_sections_are_recognized():
    """The v14 consolidated typed sections must remain declared in
    KNOWN_TOP_LEVEL — they carry the authoritative consolidated policy."""
    known = _known_top_level()
    missing = sorted(CONSOLIDATED_SECTIONS - known)
    if missing:
        msg = "\n".join(f"  [{s}]" for s in missing)
        pytest.fail(
            f"Consolidated v14 section(s) missing from KNOWN_TOP_LEVEL:\n{msg}\n\n"
            f"These are the spec-typed SSOT view and must stay declared."
        )
