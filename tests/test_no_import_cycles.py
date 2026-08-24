"""SP-7: assert the module dependency graph holds no import cycles.

beagle-spotless-phase2.xml, work package SP-7 (covers I11: 15 cycles -> 0).

A deferred (function-local) import still creates a cycle — it just delays the
re-entry until runtime. This test parses both module-level and function-local
imports and asserts the directed graph of ``beagle.*`` module dependencies is
acyclic.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "beagle"


def _module_name(rel_parts: tuple[str, ...]) -> str:
    parts = list(rel_parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _collect_edges() -> dict[str, set[str]]:
    """Map each beagle module to the set of beagle modules it imports."""
    edges: dict[str, set[str]] = {}
    for py in SRC.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        mod = _module_name(py.relative_to(SRC).parts)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("beagle."):
                        imports.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("beagle."):
                    imports.add(node.module)
                elif node.level > 0:
                    base = list(py.relative_to(SRC).parts[:-1])
                    target = ".".join([*base, node.module]) if node.module else ".".join(base)
                    imports.add(target)
        # Skip the trivial self-edge (e.g. events/__init__ importing .events)
        imports = {i for i in imports if i != mod}
        edges[mod] = imports
    return edges


def _find_cycles(edges: dict[str, set[str]]) -> list[tuple[str, ...]]:
    nodes = set(edges)
    seen: set[frozenset[str]] = set()
    cycles: list[tuple[str, ...]] = []
    for start in nodes:
        stack = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in edges.get(node, ()):
                if nxt == start:
                    key = frozenset(path)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(tuple(path))
                elif nxt in nodes and nxt not in path:
                    stack.append((nxt, [*path, nxt]))
    return cycles


def test_no_import_cycles_in_beagle_package() -> None:
    """SP-7 gate: the beagle.* module graph holds 0 cycles."""
    edges = _collect_edges()
    cycles = _find_cycles(edges)
    # Allow a single benign package/__init__ basename-collision false positive
    # where a package's __init__ imports its same-named submodule (e.g. events).
    real_cycles = []
    for c in cycles:
        if len(c) == 1:
            # A self-loop only if a module imports itself (not __init__ <-> sub)
            real_cycles.append(c)
            continue
        # Deduplicate paths that are rotations of the same cycle.
        if any(c[i:] + c[:i] == other for other in real_cycles for i in range(len(c))):
            continue
        real_cycles.append(c)
    assert real_cycles == [], "import cycles remain in beagle package:\n" + "\n".join(
        "  " + " -> ".join(c) for c in sorted(real_cycles, key=len)
    )
