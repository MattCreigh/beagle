"""Tests for Python block implementations."""

from __future__ import annotations

import os

import pytest

from beagle.blocks.context import ExecutionContext
from beagle.blocks.python_blocks.io import glob_files, read_file, write_file
from beagle.blocks.python_blocks.parse import parse_ast, parse_json, parse_toml
from beagle.blocks.python_blocks.state import append_list, set_field
from beagle.blocks.python_blocks.tool import shell_command_sandboxed
from beagle.blocks.python_blocks.transform import extract_sections, merge_dicts
from beagle.blocks.python_blocks.verify import (
    check_file_exists,
    validate_schema,
)


def test_read_file(tmp_path):
    os.environ["BEAGLE_BLOCKS_ROOT"] = str(tmp_path)
    try:
        f = tmp_path / "hello.txt"
        f.write_text("world")
        result = read_file.__raw_func__(None, path=str(f))
        assert result == "world"
    finally:
        del os.environ["BEAGLE_BLOCKS_ROOT"]


def test_write_file(tmp_path):
    os.environ["BEAGLE_BLOCKS_ROOT"] = str(tmp_path)
    try:
        target = tmp_path / "out.txt"
        result = write_file.__raw_func__(None, path=str(target), content="data")
        assert target.read_text() == "data"
        assert result == str(target)
    finally:
        del os.environ["BEAGLE_BLOCKS_ROOT"]


def test_glob_files(tmp_path):
    os.environ["BEAGLE_BLOCKS_ROOT"] = str(tmp_path)
    try:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")
        result = glob_files.__raw_func__(None, pattern="*.py", directory=str(tmp_path))
        assert len(result) == 2
    finally:
        del os.environ["BEAGLE_BLOCKS_ROOT"]


def test_parse_json():
    result = parse_json.__raw_func__(None, text='{"a": 1}')
    assert result["a"] == 1


def test_parse_toml():
    result = parse_toml.__raw_func__(None, text='name = "test"\nvalue = 42')
    assert result["name"] == "test"
    assert result["value"] == 42


def test_parse_ast():
    result = parse_ast.__raw_func__(None, source="def foo(): pass")
    assert result["type"] == "Module"
    assert "foo" in result["functions"]


def test_set_field():
    ctx = ExecutionContext()
    result = set_field.__raw_func__(ctx, key="x", value=10)
    assert result == 10
    assert ctx.get("x") == 10


def test_append_list():
    ctx = ExecutionContext()
    append_list.__raw_func__(ctx, key="items", value="a")
    append_list.__raw_func__(ctx, key="items", value="b")
    assert ctx.get("items") == ["a", "b"]


def test_extract_sections():
    text = "# Title\n\n## Section A\ncontent a\n\n## Section B\ncontent b"
    result = extract_sections.__raw_func__(None, text=text, headers=["Section A", "Section B"])
    assert result["Section A"] == "content a"
    assert result["Section B"] == "content b"


def test_merge_dicts():
    base = {"a": 1, "nested": {"x": 10}}
    overlay = {"b": 2, "nested": {"y": 20}}
    result = merge_dicts.__raw_func__(None, base=base, overlay=overlay)
    assert result["a"] == 1
    assert result["b"] == 2
    assert result["nested"]["x"] == 10
    assert result["nested"]["y"] == 20


def test_check_file_exists(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("x")
    assert check_file_exists.__raw_func__(None, path=str(f)) is True
    assert check_file_exists.__raw_func__(None, path=str(tmp_path / "no.txt")) is False


def test_validate_schema():
    data = {"name": "Alice", "age": 30}
    schema = {"name": "string", "age": "int", "tags": "list"}
    result = validate_schema.__raw_func__(None, data=data, schema=schema)
    assert result["valid"] is False
    assert any("tags" in e for e in result["errors"])


# === shell_command_sandboxed security contract (audit fix S1, v13.17.0) ===
# These tests lock down the security contract introduced when the block was
# changed from ``subprocess.run(..., shell=True)`` to an argv-based invocation
# with a binary allowlist and shell-metacharacter rejection. If any of these
# tests are relaxed, the change must be re-reviewed against the audit report.


def test_shell_command_sandboxed_allows_allowlisted_binary():
    """An allowlisted binary (`echo`) runs to completion with shell=False."""
    result = shell_command_sandboxed.__raw_func__(None, command="echo hello", cwd=".", timeout=5.0)
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]


def test_shell_command_sandboxed_rejects_injection_semicolon():
    """A command containing `;` is rejected without execution."""
    result = shell_command_sandboxed.__raw_func__(
        None, command="echo pwn; rm -rf /tmp/nope", cwd=".", timeout=5.0
    )
    assert result["returncode"] == -1
    assert "metacharacter" in result["stderr"]


def test_shell_command_sandboxed_rejects_pipe():
    """A command containing `|` is rejected without execution."""
    result = shell_command_sandboxed.__raw_func__(
        None, command="echo a | cat", cwd=".", timeout=5.0
    )
    assert result["returncode"] == -1
    assert "metacharacter" in result["stderr"]


def test_shell_command_sandboxed_rejects_command_substitution():
    """Backtick and $() command substitution are rejected."""
    result = shell_command_sandboxed.__raw_func__(None, command="echo `id`", cwd=".", timeout=5.0)
    assert result["returncode"] == -1
    assert "metacharacter" in result["stderr"]


def test_shell_command_sandboxed_rejects_non_allowlisted_binary():
    """A command whose binary is not in the allowlist is rejected."""
    result = shell_command_sandboxed.__raw_func__(
        None, command="sh -c 'echo hi'", cwd=".", timeout=5.0
    )
    assert result["returncode"] == -1
    assert "allowlist" in result["stderr"]


def test_shell_command_sandboxed_rejects_empty_command():
    """An empty command is rejected with a clear error."""
    result = shell_command_sandboxed.__raw_func__(None, command="   ", cwd=".", timeout=5.0)
    assert result["returncode"] == -1
    assert ("empty" in result["stderr"]) or ("metacharacter" in result["stderr"])


def test_shell_command_sandboxed_rejects_unbalanced_quotes():
    """Unbalanced quoting in the input surfaces as a structured error."""
    result = shell_command_sandboxed.__raw_func__(
        None, command="echo 'unterminated", cwd=".", timeout=5.0
    )
    assert result["returncode"] == -1
    assert ("tokenisation" in result["stderr"]) or ("metacharacter" in result["stderr"])


@pytest.mark.parametrize(
    "metachar",
    [";", "&", "|", "`", "$", "<", ">", "\n", "\r", "\0", "!", "(", ")", "{", "}"],
)
def test_shell_command_sandboxed_rejects_all_metachars(metachar):
    """Every shell metacharacter in the deny-list is rejected."""
    # Use a metachar that is not consumed by the preceding metachars above.
    result = shell_command_sandboxed.__raw_func__(
        None, command=f"echo safe{metachar}injected", cwd=".", timeout=5.0
    )
    assert result["returncode"] == -1
    assert "metacharacter" in result["stderr"]
