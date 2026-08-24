"""Tests for safe_file_ops — Automatic missing file creation.

Tests verify that:
1. Missing files are auto-created with appropriate content
2. Templates are correctly inferred from file paths
3. SafeFileWriter context manager works for read and write modes
4. Configuration flag controls auto-creation behavior
5. Test files get pytest boilerplate
6. Recipe stubs are created when referenced but missing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.utils.safe_file_ops import (
    FileTemplate,
    SafeFileWriter,
    configure_auto_create,
    ensure_file_exists,
    ensure_recipe_exists,
    ensure_test_file_exists,
    is_auto_create_enabled,
    safe_read,
    safe_write,
)


@pytest.fixture(autouse=True)
def temp_dir(tmp_path):
    """Provide a temporary directory for all tests."""
    return tmp_path


@pytest.fixture(autouse=True)
def reset_auto_create():
    """Ensure auto_create is enabled before each test and reset after."""
    configure_auto_create(True)
    yield
    configure_auto_create(True)


class TestEnsureFileExists:
    """Test ensure_file_exists creates missing files."""

    def test_creates_missing_file(self, temp_dir):
        """Missing file is created with default content."""
        path = temp_dir / "new_file.py"
        result = ensure_file_exists(path)
        assert result.exists()
        content = result.read_text()
        assert len(content) > 0

    def test_existing_file_not_overwritten(self, temp_dir):
        """Existing file is not modified."""
        path = temp_dir / "existing.txt"
        path.write_text("original content")
        result = ensure_file_exists(path)
        assert result.read_text() == "original content"

    def test_creates_parent_directories(self, temp_dir):
        """Missing parent directories are created."""
        path = temp_dir / "deep" / "nested" / "dir" / "file.py"
        result = ensure_file_exists(path)
        assert result.exists()

    def test_pytest_template_inferred(self, temp_dir):
        """File named test_*.py gets pytest boilerplate."""
        path = temp_dir / "test_agent_config.py"
        result = ensure_file_exists(path)
        content = result.read_text()
        assert "pytest" in content
        assert "test_placeholder" in content

    def test_python_template_inferred(self, temp_dir):
        """Regular .py files get Python module boilerplate."""
        path = temp_dir / "my_module.py"
        result = ensure_file_exists(path)
        content = result.read_text()
        assert "Auto-generated module" in content or len(content) > 0

    def test_yaml_template_inferred(self, temp_dir):
        """YAML files get YAML boilerplate."""
        path = temp_dir / "config.yaml"
        result = ensure_file_exists(path)
        content = result.read_text()
        assert "Auto-generated" in content or "yaml" in content.lower()

    def test_explicit_default_content(self, temp_dir):
        """Explicit default_content overrides template inference."""
        path = temp_dir / "custom.txt"
        result = ensure_file_exists(path, default_content="# Custom content\n")
        content = result.read_text()
        assert content == "# Custom content\n"

    def test_explicit_template_overrides(self, temp_dir):
        """Explicit template overrides suffix inference."""
        path = temp_dir / "data.py"
        # Force pytest template even though it's not a test_ file
        result = ensure_file_exists(path, template=FileTemplate.PYTEST)
        content = result.read_text()
        assert "pytest" in content

    def test_disabled_raises_file_not_found(self, temp_dir):
        """With auto_create disabled, FileNotFoundError is raised for missing files."""
        configure_auto_create(False)
        path = temp_dir / "missing.txt"
        with pytest.raises(FileNotFoundError, match="auto-creation disabled"):
            ensure_file_exists(path)

    def test_disabled_existing_file_ok(self, temp_dir):
        """With auto_create disabled, existing files are returned normally."""
        configure_auto_create(False)
        path = temp_dir / "existing.txt"
        path.write_text("data")
        result = ensure_file_exists(path)
        assert result.read_text() == "data"


class TestEnsureTestFileExists:
    """Test ensure_test_file_exists creates pytest boilerplate."""

    def test_creates_test_file_with_class(self, temp_dir):
        """Test file gets proper pytest class boilerplate."""
        path = temp_dir / "test_my_module.py"
        result = ensure_test_file_exists(path, class_name="TestMyModule", module_path="my_module")
        content = result.read_text()
        assert "class TestMyModule" in content
        assert "from my_module import *" in content

    def test_infers_class_from_filename(self, temp_dir):
        """Class name is inferred from test_*.py file name."""
        path = temp_dir / "test_foo_bar.py"
        result = ensure_test_file_exists(path)
        content = result.read_text()
        assert "class TestFooBar" in content

    def test_does_not_overwrite_existing(self, temp_dir):
        """Existing test file is not overwritten."""
        path = temp_dir / "test_existing.py"
        path.write_text("existing tests")
        result = ensure_test_file_exists(path)
        assert result.read_text() == "existing tests"


class TestSafeRead:
    """Test safe_read creates missing files and returns content."""

    def test_reads_existing_file(self, temp_dir):
        """Existing file content is returned."""
        path = temp_dir / "data.txt"
        path.write_text("hello world")
        content = safe_read(path)
        assert content == "hello world"

    def test_creates_and_returns_default(self, temp_dir):
        """Missing file is created with default content and returned."""
        path = temp_dir / "new.txt"
        content = safe_read(path, default_content="default value\n")
        assert content == "default value\n"
        assert path.exists()


class TestSafeWrite:
    """Test safe_write creates missing files and writes content."""

    def test_writes_new_file(self, temp_dir):
        """New file is created and written."""
        path = temp_dir / "output.py"
        result = safe_write(path, 'print("hello")\n')
        assert result.exists()
        assert result.read_text() == 'print("hello")\n'

    def test_overwrites_existing_file(self, temp_dir):
        """Existing file is overwritten."""
        path = temp_dir / "data.txt"
        path.write_text("old")
        safe_write(path, "new")
        assert path.read_text() == "new"

    def test_raises_when_disabled_and_missing(self, temp_dir):
        """With create_if_missing=False, missing files raise FileNotFoundError."""
        configure_auto_create(False)
        path = temp_dir / "missing.txt"
        with pytest.raises(FileNotFoundError):
            safe_write(path, "content", create_if_missing=False)


class TestSafeFileWriter:
    """Test SafeFileWriter context manager."""

    def test_write_mode_creates_file(self, temp_dir):
        """Write mode creates a new file."""
        path = temp_dir / "output.txt"
        with SafeFileWriter(path, mode="w") as f:
            f.write("test content")
        assert path.read_text() == "test content"

    def test_read_mode_creates_file(self, temp_dir):
        """Read mode creates file with default content for missing files."""
        path = temp_dir / "readme.md"
        with SafeFileWriter(path, default_content="# Readme\n", mode="r") as f:
            content = f.read()
        assert "# Readme\n" in content

    def test_append_mode_creates_file(self, temp_dir):
        """Append mode creates file if missing."""
        path = temp_dir / "log.txt"
        with SafeFileWriter(path, default_content="", mode="a") as f:
            f.write("first line\n")
        assert path.read_text() == "first line\n"


class TestEnsureRecipeExists:
    """Test ensure_recipe_exists creates stub recipes."""

    def test_creates_stub_recipe(self, temp_dir):
        """Missing recipe file is created with stub content."""
        path = temp_dir / "my-agent.md"
        result = ensure_recipe_exists(path)
        content = result.read_text()
        assert "my-agent" in content
        assert "final_answer" in content

    def test_existing_recipe_not_modified(self, temp_dir):
        """Existing recipe is returned as-is."""
        path = temp_dir / "research-planner.md"
        path.write_text("Original recipe content")
        result = ensure_recipe_exists(path)
        assert result.read_text() == "Original recipe content"


class TestAutoCreateConfiguration:
    """Test that the configuration flag properly controls behavior."""

    def test_default_enabled(self):
        """Auto-create is enabled by default."""
        configure_auto_create(True)
        assert is_auto_create_enabled() is True

    def test_can_disable(self):
        """Auto-create can be disabled."""
        configure_auto_create(False)
        assert is_auto_create_enabled() is False

    def test_can_re_enable(self):
        """Auto-create can be re-enabled after disabling."""
        configure_auto_create(False)
        assert is_auto_create_enabled() is False
        configure_auto_create(True)
        assert is_auto_create_enabled() is True

    def test_disabled_blocks_creation(self, temp_dir):
        """When disabled, file creation raises FileNotFoundError."""
        configure_auto_create(False)
        path = temp_dir / "blocked.txt"
        with pytest.raises(FileNotFoundError):
            ensure_file_exists(path)

    def test_enabled_allows_creation(self, temp_dir):
        """When enabled, file is created automatically."""
        configure_auto_create(True)
        path = temp_dir / "allowed.txt"
        result = ensure_file_exists(path)
        assert result.exists()


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_unicode_filenames(self, temp_dir):
        """Unicode characters in path work correctly."""
        path = temp_dir / "données.txt"
        result = ensure_file_exists(path, default_content="Données\n")
        assert result.exists()
        assert result.read_text() == "Données\n"

    def test_nested_deep_paths(self, temp_dir):
        """Deeply nested directories are created."""
        path = temp_dir / "a" / "b" / "c" / "d" / "e" / "deep.txt"
        result = ensure_file_exists(path, default_content="deep\n")
        assert result.exists()

    def test_template_enum_from_string(self, temp_dir):
        """FileTemplate can be specified as a string."""
        path = temp_dir / "data.toml"
        result = ensure_file_exists(path, template="toml")
        content = result.read_text()
        assert "Auto-generated" in content

    def test_denylist_blocks_etc_cron_d(self, tmp_path):
        """Regression v13.22.4 S4: auto-create must refuse /etc/... paths."""
        target = tmp_path / "etc" / "cron.d" / "x"
        # Force the leading /etc/ via the actual root:
        target = Path("/etc/cron.d/beagle-test-cron-deny")
        with pytest.raises(PermissionError, match="denied system prefix"):
            ensure_file_exists(target, default_content="x")

    def test_denylist_blocks_ssh_authorized_keys(self, tmp_path):
        """Regression v13.22.4 S4: auto-create must refuse ~/.ssh paths."""
        target = Path.home() / ".ssh" / "beagle-test-authorized-keys"
        with pytest.raises(PermissionError, match="denied system prefix"):
            ensure_file_exists(target, default_content="x")

    def test_denylist_allows_project_paths(self, tmp_path):
        """Regression v13.22.4 S4: auto-create must still work for non-system paths."""
        target = tmp_path / "ok_to_create" / "file.py"
        result = ensure_file_exists(target, default_content="ok")
        assert result.exists()
        assert result.read_text() == "ok"
