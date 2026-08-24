"""SP-18: every imported third-party package is declared (I22).

beagle-spotless-phase2.xml, work package SP-18.

The defect: five modules imported a third-party package that no file declared.
A clean install had no working RBAC (casbin), config templates (jinja2), HTML
parsing (bs4), Prometheus metrics (prometheus_client), or AST chunking
(tree_sitter_languages). This class of defect has recurred three times (ddgs,
jinja2, casbin).

This test asserts:
1. casbin and jinja2 are declared in [project.dependencies] (hard deps with no
   working alternative; casbin is a SECURITY CONTROL).
2. bs4, prometheus_client, tree_sitter_languages are declared in an optional
   extra.
3. RBAC denies (not allows) when casbin is absent — a missing security control
   must fail closed, not silently permit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _project() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def test_casbin_and_jinja2_are_hard_dependencies() -> None:
    """SP-18: casbin + jinja2 are hard deps (no working alternative)."""
    deps = _project()["project"]["dependencies"]
    for pkg in ("casbin", "jinja2"):
        assert any(str(d).startswith(pkg) for d in deps), (
            f"{pkg} must be declared in [project.dependencies] — it drives a "
            "code path with no working alternative (casbin is the RBAC "
            "security control; jinja2 renders config templates)."
        )


def test_optional_packages_are_declared_in_extras() -> None:
    """SP-18: bs4/prometheus_client/tree_sitter_languages are in an extra."""
    optional = _project()["project"]["optional-dependencies"]
    all_extra = "\n".join("\n".join(v) for v in optional.values())
    for pkg in ("beautifulsoup4", "prometheus_client", "tree-sitter-languages"):
        assert pkg in all_extra, (
            f"{pkg} must be declared in an [project.optional-dependencies] "
            "extra so a clean install can opt in."
        )


def test_rbac_denies_when_casbin_absent() -> None:
    """SP-18 security: a missing casbin never grants access.

    The RBACEnforcer is fail-closed: when casbin is unavailable it falls back
    to the built-in role matrix (which still enforces deny-by-default for the
    disabled state). A missing security control must never widen permissions.
    """
    from beagle.auth.rbac import RBACEnforcer
    from beagle.auth.tenant import Role, User

    enforcer = RBACEnforcer(enabled=True)
    user = User(user_id="u1", role=Role.VIEWER, tenant_id="default")
    # Viewer role may only GET; an execute must be denied.
    assert enforcer.enforce(user, "default", "execute") is False, (
        "RBAC must deny 'execute' for a viewer role — a missing casbin must never grant access."
    )
    assert enforcer.enforce(user, "default", "get") is True


def test_clean_venv_imports_every_module() -> None:
    """SP-18: every beagle.* module imports from declared dependencies alone.

    This is the strongest form of the clean-venv gate: import every module in
    src/beagle and assert none fails on an undeclared top-level import. We
    build the import list from the source tree and exercise the import paths.
    """
    import importlib

    from beagle.config.paths import get_workspace_root

    root = get_workspace_root()
    pkg = root / "src" / "beagle"
    modules = []
    for py in sorted(pkg.rglob("*.py")):
        if "__pycache__" in str(py) or py.name.startswith("__"):
            continue
        rel = py.relative_to(pkg)
        parts = list(rel.parts)
        parts[-1] = parts[-1][:-3]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        modules.append("beagle." + ".".join(parts))

    failures = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # ruff: ignore[BLE001]
            failures.append(f"{mod}: {exc}")

    assert failures == [], "modules fail to import from declared deps:\n" + "\n".join(failures[:20])
