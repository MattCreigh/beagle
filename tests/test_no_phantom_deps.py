"""v13.12.5: Assert pyrsistent is NOT importable from any beagle module.

Prevents the phantom dependency from being re-asserted in future PRs.
"""

import ast
import importlib
import re
from pathlib import Path

import beagle


def _source_paths():
    """Yield Path objects for every .py file under beagle/."""
    src = Path(beagle.__path__[0])
    yield from src.rglob("*.py")


def test_no_pyrsistent_importable_from_any_module():
    """AST-inspect all source files for pyrsistent imports."""
    try:
        pyrsistent_spec = importlib.util.find_spec("pyrsistent")
    except (ModuleNotFoundError, ImportError):
        pyrsistent_spec = None

    if pyrsistent_spec is not None:
        # pyrsistent is installed system-wide; verify no module imports it
        violating_modules = []
        for py_file in _source_paths():
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "pyrsistent":
                            violating_modules.append(str(py_file.relative_to(py_file.parents[2])))
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == "pyrsistent"
                ):
                    violating_modules.append(str(py_file.relative_to(py_file.parents[2])))

        assert not violating_modules, (
            f"These modules import pyrsistent: {violating_modules}\n"
            "pyrsistent is a phantom dependency — remove the import."
        )


def test_pyrsistent_not_in_pyproject_deps():
    """Verify pyrsistent is NOT listed in pyproject.toml dependencies."""
    # This is a compile-time check — pyproject.toml is at project root
    try:
        from tomllib import loads
    except ImportError:
        from tomli import loads  # type: ignore[import-not-found,unused-ignore]

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject_path.exists():
        with open(pyproject_path, encoding="utf-8") as fh:
            data = loads(fh.read())
        deps = data.get("project", {}).get("dependencies", [])
        for dep in deps:
            dep_name = re.split(r"[<>=!~]", dep, maxsplit=1)[0].strip()
            assert dep_name != "pyrsistent", (
                f"pyrsistent found in pyproject.toml dependencies: {dep!r}\n"
                "Remove it — pyrsistent is not used in the codebase."
            )
