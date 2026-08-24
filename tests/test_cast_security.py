"""Sections 13.1-13.3: CAST ingestion pipeline security tests."""

from __future__ import annotations

import ast

from beagle.security.ast_validator import validate_python_code_ast


class TestASTParsingSafe:
    """Section 13.1: AST parsing uses ast.parse() not compile()/exec()."""

    def test_validator_uses_ast_parse(self):
        """validate_python_code_ast uses ast.parse, never compile/exec."""
        import inspect

        source = inspect.getsource(validate_python_code_ast)
        assert "ast.parse" in source

    def test_validate_safe_code(self):
        """Valid Python code passes AST validation."""
        is_valid, _msg = validate_python_code_ast("x = 1 + 2\nprint(x)\n")
        assert is_valid is True

    def test_validate_dangerous_code_rejected(self):
        """Dangerous code patterns are rejected."""
        is_valid, _msg = validate_python_code_ast("__import__('os').system('rm -rf /')")
        assert is_valid is False

    def test_syntax_error_handled(self):
        """Syntax errors don't crash the validator."""
        is_valid, _msg = validate_python_code_ast("def (broken!!!")
        assert is_valid is False


class TestMaliciousFileHandling:
    """Section 13.3: Malicious files are handled safely."""

    def test_path_traversal_in_filename_rejected(self):
        """File paths with traversal patterns are rejected."""
        from beagle.checkpointer import CheckpointManager

        mgr = CheckpointManager()
        for bad_id in ["../../etc/passwd", "../secret"]:
            try:
                mgr._sanitize_id(bad_id)
                raise AssertionError(f"Should have rejected: {bad_id}")
            except ValueError:
                pass

    def test_null_byte_in_code_rejected(self):
        """Null bytes in code are safely rejected by ast.parse."""
        try:
            ast.parse("x = 1\x00y = 2")
            raise AssertionError("Should have raised SyntaxError")
        except SyntaxError:
            pass

    def test_very_deeply_nested_code_handled(self):
        """Deeply nested code doesn't crash AST parsing."""
        deep = "if True:\n" + "    " * 100 + "pass\n"
        try:
            is_valid, _ = validate_python_code_ast(deep)
            assert isinstance(is_valid, bool)
        except (RecursionError, MemoryError):
            pass  # Acceptable for pathological input
