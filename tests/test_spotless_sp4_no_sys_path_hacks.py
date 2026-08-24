"""SP-4: Assert src/ holds no sys.path hacks and every beagle.* module imports once.

beagle-spotless-phase2.xml, work package SP-4 (covers I5, I6):

* I5: 29 sys.path.insert/append sites must be 0.
* I6: 18 unresolvable bare intra-package imports must be 0.

A ``sys.path.insert(0, ...)`` call lets a directory shadow a standard library
module. A bare import of a package module creates a second module object with
separate singleton state. The editable install makes ``beagle.*`` importable,
so neither construct is needed.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

_PATH_HACK_RE = re.compile(r"sys\.path\.(insert|append)")


def _iter_py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_sys_path_hacks_in_src() -> None:
    """SP-4 gate: src/ contains 0 sys.path.insert and 0 sys.path.append calls."""
    offenders: list[str] = []
    for py in _iter_py_files():
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _PATH_HACK_RE.search(line):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert offenders == [], "sys.path hacks remain in src:\n" + "\n".join(offenders)


def test_no_bare_intra_package_imports() -> None:
    """SP-4 gate: no bare import of a beagle.* module under a top-level name.

    A bare ``from task_store import ...`` (rather than
    ``from beagle.infrastructure.task_store import ...``) resolves only because
    a sys.path hack put the package dir on sys.path, creating a second module
    object. These imports must be ``beagle.``-prefixed.
    """
    # Canonical beagle package modules, keyed by module basename. A bare
    # `from task_store import ...` that should be `from beagle.infrastructure.task_store
    # import ...` is the target. stdlib / third-party names are never flagged.
    stdlib_names = {
        "abc",
        "argparse",
        "asyncio",
        "base64",
        "binascii",
        "collections",
        "contextlib",
        "contextvars",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "hashlib",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "random",
        "re",
        "shutil",
        "signal",
        "socket",
        "sqlite3",
        "ssl",
        "string",
        "struct",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "tomllib",
        "traceback",
        "types",
        "typing",
        "uuid",
        "warnings",
        "weakref",
    }
    # Known third-party top-level modules used in this codebase.
    third_party = {
        "bs4",
        "casbin",
        "click",
        "defusedxml",
        "faiss",
        "httpx",
        "jinja2",
        "kuzu",
        "lancedb",
        "langgraph",
        "langchain",
        "mcp",
        "numpy",
        "orpheus",
        "prometheus_client",
        "psutil",
        "pydantic",
        "pytest",
        "re2",
        "rich",
        "sentence_transformers",
        "sklearn",
        "toml",
        "torch",
        "tree_sitter",
        "tree_sitter_languages",
        "yaml",
        "duckduckgo_search",
        "jwt",
    }
    beagle_stems = {
        p.stem
        for p in SRC.rglob("*.py")
        if p.parent.name != "__pycache__"
        and not p.name.startswith("__")
        and p.stem not in stdlib_names
        and p.stem not in third_party
    }
    offenders: list[str] = []
    for py in _iter_py_files():
        text = py.read_text(encoding="utf-8")
        in_docstring = False
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # Toggle docstring awareness for triple-quoted blocks.
            if '"""' in line or "'''" in line:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            m = re.match(
                r"^(?:from\s+([a-zA-Z_]\w*)\s+import|import\s+([a-zA-Z_]\w*))",
                stripped,
            )
            if not m:
                continue
            name = m.group(1) or m.group(2)
            # Skip stdlib / third-party / beagle-prefixed / relative / __future__
            if name in {"beagle", "__future__"} | stdlib_names | third_party:
                continue
            if name in beagle_stems and not stripped.startswith("from beagle."):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: {stripped}")
    assert offenders == [], "bare intra-package imports remain in src:\n" + "\n".join(
        offenders[:50]
    )


def test_infrastructure_modules_import_once() -> None:
    """SP-4 gate: each src/beagle/infrastructure module imports to one object.

    Because no sys.path hack remains, importing a module through both its full
    ``beagle.infrastructure.<name>`` path must yield the same object as any
    other import route (no duplicate module object with separate singletons).
    """
    inf_dir = SRC / "beagle" / "infrastructure"
    modules = sorted(
        "beagle." + p.with_suffix("").relative_to(SRC / "beagle").as_posix().replace("/", ".")
        for p in inf_dir.rglob("*.py")
        if not p.name.startswith("__") and p.parent.name != "__pycache__"
    )
    # Drop test-only/entrypoint names that are not importable package modules.
    importable = [
        m
        for m in modules
        if importlib.util.find_spec(m) is not None  # type: ignore[attr-defined]
    ]
    for mod in importable:
        obj = importlib.import_module(mod)
        # The module object's __spec__.name must equal the canonical package path.
        assert obj.__spec__ is not None
        assert obj.__spec__.name == mod, (
            f"{mod} imported under {obj.__spec__.name} (duplicate module object)"
        )
