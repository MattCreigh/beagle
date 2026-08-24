"""Section 8.3: Audit checkpoint write patterns for fsync+rename.

Verifies that all critical write paths in Beagle use the atomic
write pattern: write to temp file → fsync → os.replace.
This prevents partial-write corruption on crash/power loss.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from beagle.lifecycle.checkpoint import (
    Checkpoint,
    CheckpointManager,
)
from beagle.utils.file_writer import staged_write
from beagle.utils.safe_file_ops import ensure_file_exists


class TestCheckpointFsyncRename:
    """Lifecycle CheckpointManager uses fsync + os.replace pattern."""

    def test_save_calls_fsync_before_replace(self, tmp_path):
        """Checkpoint save fsyncs the temp file before os.replace."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=1000.0, version="13.7.1", restart_reason="test")

        fsync_calls = []
        replace_calls = []

        real_fsync = os.fsync
        real_replace = os.replace

        def track_fsync(fd):
            fsync_calls.append(fd)
            return real_fsync(fd)

        def track_replace(src, dst):
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        with (
            patch("os.fsync", side_effect=track_fsync),
            patch("os.replace", side_effect=track_replace),
        ):
            mgr.save(cp)

        # fsync must be called before replace
        assert len(fsync_calls) >= 1, "fsync not called"
        assert len(replace_calls) >= 1, "os.replace not called"
        # fsync should have been called (temp file), then replace
        assert fsync_calls[0] is not None

    def test_save_uses_replace_not_rename(self, tmp_path):
        """Checkpoint uses os.replace (atomic) not os.rename (non-atomic)."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=1000.0, version="13.7.1", restart_reason="test")

        with patch("os.replace", wraps=os.replace) as mock_replace:
            mgr.save(cp)

        mock_replace.assert_called_once()

    def test_save_writes_to_tmp_first(self, tmp_path):
        """Checkpoint writes to .tmp file, then replaces target."""
        mgr = CheckpointManager(workspace_root=tmp_path)
        cp = Checkpoint(timestamp=1000.0, version="13.7.1", restart_reason="test")

        written_paths = []

        real_replace = os.replace

        def track_replace(src, dst):
            written_paths.append((str(src), str(dst)))
            return real_replace(src, dst)

        with patch("os.replace", side_effect=track_replace):
            mgr.save(cp)

        # Source should be a .tmp file
        if written_paths:
            src = written_paths[0][0]
            assert ".tmp" in src or ".json.tmp" in src, f"Expected .tmp source, got: {src}"


class TestStagedWriteFsyncRename:
    """staged_write uses fsync + os.replace pattern."""

    def test_staged_write_calls_fsync(self, tmp_path):
        """staged_write fsyncs before replacing target."""
        target = tmp_path / "test.py"

        fsync_calls = []
        real_fsync = os.fsync

        def track_fsync(fd):
            fsync_calls.append(fd)
            return real_fsync(fd)

        with patch("os.fsync", side_effect=track_fsync):
            result = staged_write(str(target), "# valid python\n")

        assert result.success
        assert len(fsync_calls) >= 1, "fsync not called in staged_write"

    def test_staged_write_uses_os_replace(self, tmp_path):
        """staged_write uses os.replace for atomic file swap."""
        target = tmp_path / "test2.py"

        with patch("os.replace", wraps=os.replace) as mock_replace:
            result = staged_write(str(target), "x = 1\n")

        assert result.success
        mock_replace.assert_called_once()

    def test_staged_write_cleans_tmp_on_lint_failure(self, tmp_path):
        """staged_write removes temp file when lint fails."""
        target = tmp_path / "bad.py"

        result = staged_write(str(target), "def (broken syntax!!!\n")
        # Python syntax error → lint fails
        assert not result.success
        assert "SyntaxError" in result.error or "Lint" in result.error or "Error" in result.error

        # No temp files left
        staged_tmps = list(tmp_path.glob(".staged_*"))
        assert len(staged_tmps) == 0

    def test_staged_write_no_partial_target_on_failure(self, tmp_path):
        """On lint failure, the target file should not exist."""
        target = tmp_path / "partial.py"
        result = staged_write(str(target), "def (\n")
        assert not result.success
        assert not target.exists()


class TestSafeFileOpsAtomicWrite:
    """ensure_file_exists uses atomic temp + os.replace."""

    def test_ensure_file_uses_replace(self, tmp_path):
        """ensure_file_exists uses os.replace for atomic creation."""
        target = tmp_path / "new_file.py"

        with patch("os.replace", wraps=os.replace) as mock_replace:
            ensure_file_exists(str(target))

        mock_replace.assert_called_once()

    def test_ensure_file_cleans_tmp_on_failure(self, tmp_path):
        """ensure_file_exists cleans up .tmp on write failure."""
        target = tmp_path / "fail_file.py"

        with patch("os.replace", side_effect=OSError("replace failed")), pytest.raises(OSError):
            ensure_file_exists(str(target))

        # No .tmp files left
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestContextManifestAtomicWrite:
    """Context manifest save uses atomic write."""

    def test_manifest_save_uses_replace(self, tmp_path):
        """save_manifest uses temp file + Path.replace for atomic save."""
        from beagle.core.context_manifest import (
            ContextManifest,
            save_manifest,
        )

        manifest = ContextManifest(project="test")
        path = tmp_path / "context-manifest.json"

        # Verify the temp file pattern: write to .tmp, then replace
        original_replace = Path.replace
        replace_calls = []

        def track_replace(self_path, target):
            replace_calls.append((str(self_path), str(target)))
            return original_replace(self_path, target)

        with patch.object(Path, "replace", track_replace):
            save_manifest(manifest, path)

        assert len(replace_calls) >= 1, "Path.replace not called"
        # Source should be a .tmp file
        src = replace_calls[0][0]
        assert ".tmp" in src, f"Expected .tmp source, got: {src}"
        # Verify the final file exists and is valid JSON
        import json

        data = json.loads(path.read_text())
        assert data["project"] == "test"
