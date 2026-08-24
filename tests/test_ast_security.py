"""Tests for AST-based code validation (Phase 1B).

Validates that validate_python_code_ast() correctly detects dangerous
Python constructs without false positives, replacing brittle regex patterns
with the Python ast module's guaranteed syntactic analysis.
"""

import pytest

from beagle.security import (
    validate_python_code_ast,
)


class TestASTValidation:
    """Test suite for validate_python_code_ast()."""

    # ── Valid code should pass ──────────────────────────────────────────────

    def test_empty_string_rejected(self):
        """Empty code string should be rejected."""
        valid, msg = validate_python_code_ast("")
        assert not valid
        assert "Empty" in msg

    def test_none_rejected(self):
        """None should be rejected (not a string)."""
        valid, _msg = validate_python_code_ast(None)
        assert not valid

    def test_non_string_rejected(self):
        """Non-string types should be rejected."""
        valid, msg = validate_python_code_ast(42)
        assert not valid
        assert "string" in msg.lower()

    def test_simple_print_passes(self):
        """Simple print statement should pass strict validation."""
        valid, msg = validate_python_code_ast("print('hello world')")
        assert valid
        assert msg == ""

    def test_variable_assignment_passes(self):
        """Simple variable assignment should pass."""
        valid, _msg = validate_python_code_ast("x = 42")
        assert valid

    def test_function_definition_passes(self):
        """Function definition should pass."""
        code = """
def add(a, b):
    return a + b
"""
        valid, _msg = validate_python_code_ast(code)
        assert valid

    def test_class_definition_passes(self):
        """Class definition should pass."""
        code = """
class MyModel:
    def __init__(self, name):
        self.name = name
"""
        valid, _msg = validate_python_code_ast(code)
        assert valid

    def test_list_comprehension_passes(self):
        """List comprehension should pass."""
        valid, _msg = validate_python_code_ast("[x * 2 for x in range(10)]")
        assert valid

    def test_import_json_passes(self):
        """Importing safe modules should pass."""
        valid, _msg = validate_python_code_ast("import json")
        assert valid

    def test_from_import_passes(self):
        """from-importing safe modules should pass."""
        valid, _msg = validate_python_code_ast("from pathlib import Path")
        assert valid

    def test_dict_literal_passes(self):
        """Dict literals should pass."""
        valid, _msg = validate_python_code_ast("{'key': 'value', 'nested': {'deep': 1}}")
        assert valid

    # ── Dangerous imports should be detected ────────────────────────────────

    def test_import_os_rejected(self):
        """Importing os should be rejected in strict mode."""
        valid, msg = validate_python_code_ast("import os")
        assert not valid
        assert "Dangerous import" in msg

    def test_import_subprocess_rejected(self):
        """Importing subprocess should be rejected in strict mode."""
        valid, msg = validate_python_code_ast("import subprocess")
        assert not valid
        assert "subprocess" in msg

    def test_from_os_import_rejected(self):
        """from os import should be rejected in strict mode."""
        valid, msg = validate_python_code_ast("from os import path")
        assert not valid
        assert "Dangerous import" in msg

    def test_import_ctypes_rejected(self):
        """Importing ctypes should be rejected."""
        valid, msg = validate_python_code_ast("import ctypes")
        assert not valid
        assert "ctypes" in msg.lower()

    def test_import_signal_rejected(self):
        """Importing signal should be rejected."""
        valid, msg = validate_python_code_ast("import signal")
        assert not valid
        assert "signal" in msg.lower()

    # ── Dangerous function calls should be detected ─────────────────────────

    def test_eval_rejected(self):
        """eval() should be rejected."""
        valid, msg = validate_python_code_ast("eval('1+1')")
        assert not valid
        assert "Dangerous function call" in msg

    def test_exec_rejected(self):
        """exec() should be rejected."""
        valid, msg = validate_python_code_ast("exec('print(1)')")
        assert not valid
        assert "exec" in msg.lower()

    def test_os_system_rejected(self):
        """os.system() should be rejected."""
        valid, msg = validate_python_code_ast("os.system('ls')")
        assert not valid
        assert "Dangerous" in msg  # Could be module call or module method

    def test_subprocess_run_rejected(self):
        """subprocess.run() should be rejected."""
        valid, msg = validate_python_code_ast("subprocess.run(['ls'])")
        assert not valid
        assert "Dangerous" in msg  # Module call or module method

    def test_shutil_rmtree_rejected(self):
        """shutil.rmtree() should be rejected."""
        valid, msg = validate_python_code_ast("shutil.rmtree('/tmp/test')")
        assert not valid
        assert "Dangerous" in msg  # Module call or module method

    def test_compile_rejected(self):
        """compile() should be rejected."""
        valid, msg = validate_python_code_ast("compile('1+1', '<string>', 'eval')")
        assert not valid
        assert "compile" in msg.lower()

    def test_import_function_rejected(self):
        """__import__() should be rejected."""
        valid, msg = validate_python_code_ast("__import__('os')")
        assert not valid
        assert "Dangerous function call" in msg

    # ── Dangerous attribute access should be detected ────────────────────────

    def test_dunder_import_rejected(self):
        """__import__ attribute access should be rejected."""
        valid, msg = validate_python_code_ast("x.__import__")
        assert not valid
        assert "Dangerous attribute" in msg

    def test_dunder_subclasses_rejected(self):
        """__subclasses__ attribute access should be rejected."""
        valid, msg = validate_python_code_ast("x.__subclasses__()")
        assert not valid
        assert "Dangerous attribute" in msg

    def test_dunder_globals_rejected(self):
        """__globals__ attribute access should be rejected."""
        valid, msg = validate_python_code_ast("func.__globals__")
        assert not valid
        assert "Dangerous attribute" in msg

    # ── File write detection ────────────────────────────────────────────────

    def test_open_write_mode_rejected(self):
        """open() with write mode should be rejected in strict mode."""
        valid, msg = validate_python_code_ast("open('file.txt', 'w')")
        assert not valid
        assert "write mode" in msg.lower() or "Dangerous" in msg

    def test_open_append_mode_rejected(self):
        """open() with append mode should be rejected in strict mode."""
        valid, msg = validate_python_code_ast("open('file.txt', 'a')")
        assert not valid
        assert "write mode" in msg.lower() or "Dangerous" in msg

    def test_open_read_mode_passes(self):
        """open() with read mode should pass."""
        valid, _msg = validate_python_code_ast("open('file.txt', 'r')")
        assert valid

    def test_open_default_mode_passes(self):
        """open() with default mode (no args) should pass."""
        valid, _msg = validate_python_code_ast("open('file.txt')")
        assert valid

    # ── Length guard ─────────────────────────────────────────────────────────

    def test_oversized_code_rejected(self):
        """Code exceeding max length should be rejected."""
        code = "x = 1\n" * 500_000  # ~1MB
        valid, msg = validate_python_code_ast(code)
        assert not valid
        assert "too long" in msg.lower()

    def test_invalid_syntax_rejected(self):
        """Invalid Python syntax should be rejected."""
        valid, msg = validate_python_code_ast("def foo(:\n  pass")
        assert not valid
        assert "Syntax error" in msg

    # ── Non-strict mode ──────────────────────────────────────────────────────

    def test_os_import_allowed_non_strict(self):
        """In non-strict mode, os import should be allowed."""
        valid, _msg = validate_python_code_ast("import os", strict=False)
        assert valid  # Non-strict allows importing os; calls like os.system still blocked

    def test_os_system_rejected_even_non_strict(self):
        """Even in non-strict mode, os.system() should be rejected."""
        valid, _msg = validate_python_code_ast("os.system('ls')", strict=False)
        assert not valid

    def test_eval_rejected_even_non_strict(self):
        """Even in non-strict mode, eval() should be rejected."""
        valid, _msg = validate_python_code_ast("eval('1+1')", strict=False)
        assert not valid

    # ── Complex but safe code ────────────────────────────────────────────────

    def test_async_function_passes(self):
        """Async function definition should pass."""
        code = """
async def fetch_data(url):
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()
"""
        valid, _msg = validate_python_code_ast(code, strict=False)
        assert valid

    def test_dataclass_passes(self):
        """Dataclass definition should pass."""
        code = """
from dataclasses import dataclass

@dataclass
class Result:
    name: str
    score: float
    passed: bool = False
"""
        valid, _msg = validate_python_code_ast(code)
        assert valid

    def test_try_except_passes(self):
        """Try/except blocks should pass."""
        code = """
try:
    result = compute()
except ValueError as e:
    print(f"Error: {e}")
"""
        valid, _msg = validate_python_code_ast(code)
        assert valid

    def test_with_statement_passes(self):
        """Context managers should pass."""
        code = """
with open('data.json', 'r') as f:
    data = json.load(f)
"""
        valid, _msg = validate_python_code_ast(code)
        assert valid


class TestASTSecurityEdgeCases:
    """Edge case tests for AST validation."""

    def test_triple_quoted_string_passes(self):
        """Triple-quoted strings should pass."""
        valid, _msg = validate_python_code_ast('"""This is a docstring"""')
        assert valid

    def test_f_string_passes(self):
        """f-strings should pass."""
        valid, _msg = validate_python_code_ast("f'Hello {name}'")
        assert valid

    def test_walrus_operator_passes(self):
        """Walrus operator should pass (Python 3.8+)."""
        valid, _msg = validate_python_code_ast("if (n := 10) > 5: pass")
        assert valid

    def test_match_statement_passes(self):
        """match/case should pass (Python 3.10+)."""
        code = """
match command:
    case "quit":
        exit()
    case _:
        print("unknown")
"""
        valid, _msg = validate_python_code_ast(code)
        assert valid

    def test_nested_dangerous_calls(self):
        """Nested dangerous calls should be detected."""
        valid, _msg = validate_python_code_ast("eval(os.system('whoami'))")
        assert not valid
        # Should detect at least one dangerous construct

    def test_string_containing_dangerous_code_passes(self):
        """A string literal containing dangerous code should pass (it's just a string)."""
        valid, _msg = validate_python_code_ast("'os.system(\"ls\")'")
        assert valid  # It's a string, not actual code execution


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
