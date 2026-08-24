"""Tests for beagle.utils.file_writer

Covers: WriteResult, staged_write, apply_patch, apply_full_diff,
preview_diff, _normalize_lines, and _find_contiguous_block.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from beagle.utils.file_writer import (
    WriteResult,
    _find_contiguous_block,
    _normalize_lines,
    apply_full_diff,
    apply_patch,
    preview_diff,
    staged_write,
)

# =====================================================================
# 1. WriteResult dataclass
# =====================================================================


class TestWriteResult:
    """Tests for the WriteResult frozen dataclass."""

    def test_success_creation(self):
        result = WriteResult(success=True, path="/tmp/test.py")
        assert result.success is True
        assert result.path == "/tmp/test.py"
        assert result.error == ""

    def test_failure_creation(self):
        result = WriteResult(success=False, path="/tmp/bad.py", error="SyntaxError: ...")
        assert result.success is False
        assert result.error == "SyntaxError: ..."

    def test_bool_true_for_success(self):
        result = WriteResult(success=True, path="/tmp/x.py")
        assert bool(result) is True
        # Also works in an if-statement context
        passed = bool(result)
        assert passed is True

    def test_bool_false_for_failure(self):
        result = WriteResult(success=False, path="/tmp/x.py", error="fail")
        assert bool(result) is False
        passed = bool(result)
        assert passed is False

    def test_frozen(self):
        result = WriteResult(success=True, path="/tmp/x.py")
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


# =====================================================================
# 2. staged_write() — Python files
# =====================================================================


class TestStagedWritePython:
    """Staged-write tests for .py files (py_compile validation)."""

    def test_valid_python(self, tmp_path):
        target = tmp_path / "good.py"
        content = textwrap.dedent("""\
            def hello():
                return "world"
        """)
        result = staged_write(target, content)
        assert result.success is True
        assert target.read_text() == content

    def test_invalid_python_syntax_error(self, tmp_path):
        target = tmp_path / "bad.py"
        content = "def (\n"  # Syntax error
        result = staged_write(target, content)
        assert result.success is False
        assert "SyntaxError" in result.error

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "dir" / "module.py"
        content = "x = 1\n"
        result = staged_write(target, content)
        assert result.success is True
        assert target.read_text() == content
        assert target.parent.exists()


# =====================================================================
# 3. staged_write() — YAML files
# =====================================================================


class TestStagedWriteYAML:
    """Staged-write tests for .yaml/.yml files."""

    def test_valid_yaml(self, tmp_path):
        target = tmp_path / "config.yaml"
        content = textwrap.dedent("""\
            name: test
            items:
              - a
              - b
        """)
        result = staged_write(target, content)
        assert result.success is True
        assert target.read_text() == content

    def test_valid_yml_extension(self, tmp_path):
        target = tmp_path / "config.yml"
        content = "key: value\n"
        result = staged_write(target, content)
        assert result.success is True

    def test_invalid_yaml(self, tmp_path):
        target = tmp_path / "bad.yaml"
        # Unmatched bracket inside a flow collection trips up yaml.safe_load
        content = "{: [\n"
        result = staged_write(target, content)
        assert result.success is False
        assert "YAML" in result.error


# =====================================================================
# 4. staged_write() — TOML files
# =====================================================================


class TestStagedWriteTOML:
    """Staged-write tests for .toml files."""

    def test_valid_toml(self, tmp_path):
        target = tmp_path / "pyproject.toml"
        content = textwrap.dedent("""\
            [project]
            name = "test"
            version = "0.1.0"
        """)
        result = staged_write(target, content)
        assert result.success is True
        assert target.read_text() == content

    def test_invalid_toml(self, tmp_path):
        target = tmp_path / "bad.toml"
        # Duplicate keys are invalid in TOML
        content = textwrap.dedent("""\
            [section]
            key = "1"
            key = "2"
        """)
        result = staged_write(target, content)
        assert result.success is False
        assert "TOML" in result.error


# =====================================================================
# 5. staged_write() — unknown file types (pass-through)
# =====================================================================


class TestStagedWriteUnknownType:
    """Files with no registered linter should pass through without validation."""

    def test_passes_through_txt(self, tmp_path):
        target = tmp_path / "notes.txt"
        content = "Anything goes here! @#$%^&*()\n"
        result = staged_write(target, content)
        assert result.success is True
        assert target.read_text() == content

    def test_passes_through_md(self, tmp_path):
        target = tmp_path / "README.md"
        content = "# Title\n\nSome body\n"
        result = staged_write(target, content)
        assert result.success is True
        assert target.read_text() == content


# =====================================================================
# 6. apply_patch()
# =====================================================================


class TestApplyPatch:
    """Tests for deterministic in-memory patching."""

    def _write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_successful_patch(self, tmp_path):
        target = tmp_path / "patch_me.py"
        # Ruff 0.16.0 enforces E302 with N=2 blank lines between
        # top-level defs; the original file already complies.
        original = textwrap.dedent("""\
            def old_func():
                pass


            def other():
                return 42
        """)
        self._write_file(target, original)

        old_lines = ["def old_func():\n", "    pass\n"]
        new_lines = ["def new_func():\n", "    return 1\n"]

        result = apply_patch(target, old_lines, new_lines)
        assert result.success is True

        # Patched file must keep E302 compliance (2 blank lines
        # between top-level defs).
        expected = "def new_func():\n    return 1\n\n\ndef other():\n    return 42\n"
        assert target.read_text() == expected

    def test_patch_rejected_when_old_lines_not_found(self, tmp_path):
        target = tmp_path / "patch_me.py"
        self._write_file(target, "x = 1\n")

        old_lines = ["def nonexistent():\n"]
        new_lines = ["def replacement():\n"]

        result = apply_patch(target, old_lines, new_lines)
        assert result.success is False
        assert "REJECTED" in result.error
        # Original file must be untouched
        assert target.read_text() == "x = 1\n"

    def test_patch_nonexistent_file(self, tmp_path):
        target = tmp_path / "missing.py"
        result = apply_patch(target, ["old\n"], ["new\n"])
        assert result.success is False
        assert "does not exist" in result.error

    def test_patch_with_stripped_match(self, tmp_path):
        """apply_patch falls back to stripped comparison if exact match fails."""
        target = tmp_path / "flex_patch.py"
        # Write file with trailing spaces on the function definition line
        original = "def greet():   \n    print('hi')\n"
        self._write_file(target, original)

        # old_lines without trailing spaces => exact match fails, stripped match succeeds
        old_lines = ["def greet():\n", "    print('hi')\n"]
        new_lines = ["def greet():\n", "    print('hello')\n"]

        result = apply_patch(target, old_lines, new_lines)
        assert result.success is True
        assert "print('hello')" in target.read_text()


# =====================================================================
# 7. apply_full_diff()
# =====================================================================


class TestApplyFullDiff:
    """Tests for full-file replacement."""

    def test_full_replacement(self, tmp_path):
        target = tmp_path / "replace.py"
        staged_write(target, "old = True\n")
        old_content = target.read_text()

        result = apply_full_diff(target, "new = False\n")
        assert result.success is True
        assert target.read_text() == "new = False\n"
        assert target.read_text() != old_content

    def test_backup_creation(self, tmp_path):
        target = tmp_path / "backup_test.py"
        original = "version = 1\n"
        staged_write(target, original)

        result = apply_full_diff(target, "version = 2\n", create_backup=True)
        assert result.success is True
        assert target.read_text() == "version = 2\n"

        backup = target.with_suffix(target.suffix + ".bak")
        assert backup.exists()
        assert backup.read_text() == original

    def test_full_diff_on_new_file(self, tmp_path):
        """apply_full_diff on a nonexistent file should still succeed (mkdir + write)."""
        target = tmp_path / "brand_new.py"
        result = apply_full_diff(target, "x = 42\n")
        assert result.success is True
        assert target.read_text() == "x = 42\n"


# =====================================================================
# 8. preview_diff()
# =====================================================================


class TestPreviewDiff:
    """Tests for unified-diff preview that must NOT modify the file."""

    def test_generates_diff_without_modifying_file(self, tmp_path):
        target = tmp_path / "preview.py"
        original = "def foo():\n    pass\n"
        staged_write(target, original)

        new_content = "def foo():\n    return 1\n"
        diff = preview_diff(target, new_content)

        # Diff should contain marker lines
        assert "--- a/preview.py" in diff or "-a/preview.py" in diff or "preview.py" in diff
        assert "def foo" in diff

        # Original file must be untouched
        assert target.read_text() == original

    def test_preview_of_nonexistent_file(self, tmp_path):
        target = tmp_path / "ghost.py"
        new_content = "x = 1\n"
        diff = preview_diff(target, new_content)
        # Should produce a diff against empty content (all additions)
        assert "+" in diff or "x = 1" in diff


# =====================================================================
# 9. _normalize_lines()
# =====================================================================


class TestNormalizeLines:
    """Tests for the trailing-newline normalization helper."""

    def test_adds_trailing_newline(self):
        lines = ["abc", "def"]
        result = _normalize_lines(lines)
        assert result == ["abc\n", "def\n"]

    def test_preserves_existing_trailing_newline(self):
        lines = ["abc\n", "def\n"]
        result = _normalize_lines(lines)
        assert result == ["abc\n", "def\n"]

    def test_mixed_lines(self):
        lines = ["has newline\n", "missing newline"]
        result = _normalize_lines(lines)
        assert result == ["has newline\n", "missing newline\n"]

    def test_empty_input(self):
        result = _normalize_lines([])
        assert result == []

    def test_empty_string_line(self):
        """Even an empty string should get a trailing newline."""
        result = _normalize_lines([""])
        assert result == ["\n"]


# =====================================================================
# 10. _find_contiguous_block()
# =====================================================================


class TestFindContiguousBlock:
    """Tests for contiguous-block search in a line list."""

    def _make_haystack(self):
        return [
            "line_one\n",
            "line_two\n",
            "line_three\n",
            "line_four\n",
            "line_five\n",
        ]

    def test_exact_match(self):
        haystack = self._make_haystack()
        needle = ["line_two\n", "line_three\n"]
        assert _find_contiguous_block(haystack, needle) == 1

    def test_exact_match_single_line(self):
        haystack = self._make_haystack()
        needle = ["line_four\n"]
        assert _find_contiguous_block(haystack, needle) == 3

    def test_stripped_match(self):
        """Lines differing only in trailing whitespace should still match."""
        haystack = ["line_one   \n", "line_two  \n", "line_three\n"]
        needle = ["line_one\n", "line_two\n"]
        assert _find_contiguous_block(haystack, needle) == 0

    def test_not_found(self):
        haystack = self._make_haystack()
        needle = ["not_in_haystack\n"]
        assert _find_contiguous_block(haystack, needle) is None

    def test_empty_needle_returns_none(self):
        haystack = self._make_haystack()
        assert _find_contiguous_block(haystack, []) is None

    def test_needle_longer_than_haystack_returns_none(self):
        haystack = ["short\n"]
        needle = ["a\n", "b\n", "c\n"]
        assert _find_contiguous_block(haystack, needle) is None
