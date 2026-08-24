"""SP-5: tests for blocks/python_blocks/io (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The file I/O blocks (read/write/glob)
run sandboxed to a project root. These exercise the containment guarantee
(Path.relative_to, per doctrine) and the block envelope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.blocks.python_blocks import io


@pytest.fixture
def blocks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the sandbox root at a temp dir."""
    monkeypatch.setenv("BEAGLE_BLOCKS_ROOT", str(tmp_path))
    return tmp_path


def test_write_then_read_round_trip(blocks_root: Path) -> None:
    """write_file writes content that read_file returns (absolute path)."""
    p = blocks_root / "out.txt"
    result = io.write_file(None, path=str(p), content="hello")
    assert result["success"] is True
    read = io.read_file(None, path=str(p))
    assert read["output"] == "hello"


def test_write_creates_parent_dirs(blocks_root: Path) -> None:
    """write_file creates intermediate directories (absolute path)."""
    p = blocks_root / "a" / "b" / "c.txt"
    io.write_file(None, path=str(p), content="x")
    assert p.exists()


def test_read_missing_file_raises_execution_error(blocks_root: Path) -> None:
    """read_file of a missing file surfaces as an ExecutionError envelope."""
    from beagle.blocks.errors import ExecutionError

    with pytest.raises(ExecutionError):
        io.read_file(None, path=str(blocks_root / "missing.txt"))


def test_path_traversal_rejected(blocks_root: Path, tmp_path: Path) -> None:
    """A path escaping the sandbox is rejected (no read/write).

    The @python_block decorator wraps the containment ValueError in an
    ExecutionError envelope, so the block call raises ExecutionError.
    """
    from beagle.blocks.errors import ExecutionError

    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ExecutionError):
        io.read_file(None, path=str(outside))


def test_glob_files(blocks_root: Path) -> None:
    """glob_files finds files under the directory."""
    io.write_file(None, path=str(blocks_root / "src" / "a.py"), content="a")
    io.write_file(None, path=str(blocks_root / "src" / "b.py"), content="b")
    result = io.glob_files(None, pattern="*.py", directory=str(blocks_root / "src"))
    paths = [Path(p).name for p in result["output"]]
    assert "a.py" in paths
    assert "b.py" in paths


def test_contained_resolves_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_contained resolves symlinks before the containment check."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    monkeypatch.setenv("BEAGLE_BLOCKS_ROOT", str(root.resolve()))
    # A symlink pointing outside the root resolves outside → ValueError.
    with pytest.raises(ValueError):
        io._contained(root / "link" / "file.txt")
