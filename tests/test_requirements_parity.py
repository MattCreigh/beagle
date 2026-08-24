"""H3 regression — requirements.txt must match pyproject.toml dependencies.

The 2026-08-15 enterprise audit found `requirements.txt` omitted 11 runtime
dependencies declared in `pyproject.toml`, including two CVE floors
(`langchain-core`, `langgraph-checkpoint-sqlite`) and `torch` (which, absent
the CPU index directive, resolved the CUDA stack on a CPU-only host). This
test asserts the two dependency sets are equal so the drift cannot silently
return.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_REQUIREMENTS = _ROOT / "requirements.txt"


def _pyproject_deps() -> set[str]:
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    return set(data["project"]["dependencies"])


def _requirements_deps() -> set[str]:
    """Parse requirements.txt, skipping index directives and comments."""
    deps: set[str] = set()
    for line in _REQUIREMENTS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        # Normalise: strip inline comments and whitespace.
        line = re.split(r"\s+#", line)[0].strip()
        deps.add(line)
    return deps


def test_requirements_matches_pyproject() -> None:
    """Every pyproject dependency must appear in requirements.txt and vice versa."""
    pyproject = _pyproject_deps()
    requirements = _requirements_deps()

    missing = pyproject - requirements
    extra = requirements - pyproject

    assert not missing, (
        f"requirements.txt omits {len(missing)} deps declared in pyproject.toml: {sorted(missing)}"
    )
    assert not extra, (
        f"requirements.txt has {len(extra)} deps not in pyproject.toml: {sorted(extra)}"
    )


def test_requirements_has_cve_floors() -> None:
    """The two CVE-floor specifiers must be present verbatim."""
    text = _REQUIREMENTS.read_text()
    assert "langchain-core>=1.2.22,<2" in text, "langchain-core CVE floor missing"
    assert "langgraph-checkpoint-sqlite>=3.0.1" in text, (
        "langgraph-checkpoint-sqlite CVE floor missing"
    )


def test_requirements_has_cpu_torch_index() -> None:
    """The CPU-only PyTorch index directive must be present."""
    text = _REQUIREMENTS.read_text()
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in text, (
        "CPU-only PyTorch index directive missing — pip-only install would resolve the CUDA stack"
    )
    assert "torch==2.11.0" in text, "torch pin missing"
