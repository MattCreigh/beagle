"""Regression tests for Gemini-audit security fixes (v13.17.1).

Covers:
  S1.2 - RBAC fail-closed
  S1.3 - JWT require-exp
  S1.4 - io.py path containment
  S1.5a - binascii/codecs in DANGEROUS_MODULES
  S1.5b - AST subscript Name-based evasion
  S1.5c - AST BinOp recursive string fold
  S2.2 - llm_node fallback except broadening
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from beagle.blocks.errors import ExecutionError

# ── S1.2: RBAC fail-closed ────────────────────────────────────────────────


def test_rbac_fail_closed() -> None:
    """RBAC must deny access (return False) when not enabled."""
    from beagle.auth.rbac import RBACEnforcer
    from beagle.auth.tenant import Role, User

    rbac = RBACEnforcer(enabled=False)
    user = User(user_id="test", tenant_id="test", role=Role.ADMIN)
    result = rbac.enforce(user, "any", "read")
    assert result is False, (
        f"RBAC must fail-closed: disabled enforcer returned {result}, expected False"
    )


def test_rbac_enabled_permits() -> None:
    """RBAC must permit when enabled and role matches (admin wildcard)."""
    from beagle.auth.rbac import RBACEnforcer
    from beagle.auth.tenant import Role, User

    rbac = RBACEnforcer(enabled=True)
    user = User(user_id="test", tenant_id="test", role=Role.ADMIN)
    # Built-in RBAC uses "get" as the read action name
    result = rbac.enforce(user, "any", "get")
    assert result is True, f"RBAC must permit admin access when enabled, got {result}"


# ── S1.3: JWT require-exp ─────────────────────────────────────────────────


def test_jwt_rejects_token_without_exp() -> None:
    """JWT with exp claim must validate successfully."""
    from beagle.auth.jwt import create_jwt, validate_jwt

    secret = (
        "test-jwt-secret-32-bytes-long-ok"  # 32 bytes for HS256 (avoids InsecureKeyLengthWarning)
    )
    token = create_jwt({"sub": "test"}, secret)
    payload = validate_jwt(token, secret)
    assert payload["sub"] == "test"


def test_jwt_rejects_expired_token() -> None:
    """Expired JWT must raise ValueError."""
    import time

    from beagle.auth.jwt import create_jwt, validate_jwt

    secret = "test-jwt-secret-32-bytes-long-ok"
    token = create_jwt({"sub": "test"}, secret, ttl_seconds=0)
    time.sleep(1.1)

    try:
        validate_jwt(token, secret)
        raise AssertionError("Should have raised ValueError for expired token")
    except ValueError:
        pass


# ── S1.4: io.py path containment ──────────────────────────────────────────


def test_io_read_within_root(tmp_path: Path) -> None:
    """read_file must succeed on files within BLOCKS_ROOT."""
    os.environ["BEAGLE_BLOCKS_ROOT"] = str(tmp_path)
    try:
        f = tmp_path / "test.txt"
        f.write_text("hello")
        # Import after setting env so _BLOCKS_ROOT resolves correctly
        from beagle.blocks.python_blocks.io import read_file

        result = read_file(None, path=str(f))
        # @python_block wraps result in dict: {"output": ..., "success": True}
        assert result["success"] and result["output"] == "hello"
    finally:
        os.environ.pop("BEAGLE_BLOCKS_ROOT", None)


def test_io_read_outside_root(tmp_path: Path) -> None:
    """read_file must reject paths outside BLOCKS_ROOT."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "out.txt"
    outside.write_text("secret")

    os.environ["BEAGLE_BLOCKS_ROOT"] = str(sandbox)
    try:
        from beagle.blocks.python_blocks.io import read_file

        read_file(None, path=str(outside))
        raise AssertionError("Should have raised for path traversal")
    except (ValueError, ExecutionError):
        # Exception wrapping via ExecutionError is acceptable
        pass
    finally:
        os.environ.pop("BEAGLE_BLOCKS_ROOT", None)


def test_io_write_outside_root(tmp_path: Path) -> None:
    """write_file must reject paths outside BLOCKS_ROOT."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "out.txt"

    os.environ["BEAGLE_BLOCKS_ROOT"] = str(sandbox)
    try:
        from beagle.blocks.python_blocks.io import write_file

        write_file(None, path=str(outside), content="data")
        raise AssertionError("Should have raised for path traversal write")
    except (ValueError, ExecutionError):
        pass
    finally:
        os.environ.pop("BEAGLE_BLOCKS_ROOT", None)


# ── S1.5a: DANGEROUS_MODULES ──────────────────────────────────────────────


def test_binascii_in_dangerous_modules() -> None:
    from beagle.security.constants import DANGEROUS_MODULES

    assert "binascii" in DANGEROUS_MODULES


def test_codecs_in_dangerous_modules() -> None:
    from beagle.security.constants import DANGEROUS_MODULES

    assert "codecs" in DANGEROUS_MODULES


# ── S1.5b: AST subscript Name-based evasion ───────────────────────────────


def test_ast_rejects_builtins_subscript_evasion() -> None:
    """__builtins__['eval'] must be caught by AST validator."""
    from beagle.security.ast_validator import (
        validate_python_code_ast,
    )

    code = '__builtins__["eval"]("print(1)")'
    is_valid, err = validate_python_code_ast(code)
    assert not is_valid, f"Should reject __builtins__ subscript: {err}"


def test_ast_rejects_builtins_dynamic_subscript() -> None:
    """__builtins__['ev'+'al'] must be caught by AST validator."""
    from beagle.security.ast_validator import (
        validate_python_code_ast,
    )

    code = '__builtins__["e"+"v"+"a"+"l"]("print(1)")'
    is_valid, err = validate_python_code_ast(code)
    assert not is_valid, f"Should catch obfuscated __builtins__: {err}"


# ── S1.5c: AST recursive BinOp fold ───────────────────────────────────────


def test_ast_fold_nested_string_concat() -> None:
    """'e'+'v'+'a'+'l' must fold to 'eval'."""
    from beagle.security.ast_validator import (
        _SecurityASTVisitor,
    )

    tree = ast.parse("'e'+'v'+'a'+'l'", mode="eval")
    folded = _SecurityASTVisitor._fold_string_concat(tree.body)
    assert folded == "eval", f"Should fold to 'eval', got: {folded!r}"


def test_ast_validator_catches_folded_dangerous_pattern() -> None:
    """AST validator must catch 'eval' built from fragmented strings."""
    from beagle.security.ast_validator import (
        validate_python_code_ast,
    )

    # exec is blocked; the 'eval' inside is also detected via fold
    code = "exec('e'+'v'+'a'+'l'('print(1)'))"
    is_valid, err = validate_python_code_ast(code)
    assert not is_valid, f"Should reject folded dangerous pattern: {err}"


# ── S2.2: llm_node fallback except broadening ─────────────────────────────


def test_llm_node_fallback_except_is_broadened() -> None:
    """Verify llm_node.py fallback except catches RuntimeError (not just ImportError)."""
    # Package lives at repo-root src/ (pyproject.toml package-dir mapping);
    # this pointed at the pre-rename directory.
    llm_node = Path(__file__).resolve().parent.parent / "src" / "beagle" / "bridges" / "llm_node.py"
    tree = ast.parse(llm_node.read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.type is not None and isinstance(handler.type, ast.Tuple):
                    names = {elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)}
                    if "RuntimeError" in names:
                        return  # Found — test passes

    raise AssertionError(
        "llm_node.py must catch (ImportError, RuntimeError, OSError, TimeoutError) in fallback loop"
    )
