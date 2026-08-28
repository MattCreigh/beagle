"""Atomic file writes.

This module is the single implementation of write-temp-then-rename for the
project.  A reader of the destination sees either the complete previous
document or the complete new one, never a partial write.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _fsync_parent(path: Path) -> None:
    """Fsync the parent directory so a rename is durable across power loss.

    ``os.replace`` alone guarantees atomicity of the *visible* state but not
    durability: the directory entry change can be lost on a crash before the
    journal flushes. Opening the parent dir read-only and fsyncing it pushes
    the rename to stable storage.

    D-12 (release-readiness audit 2026-08-28): this is the missing half of
    the write-temp-fsync-rename protocol. The file's data is fsynced before
    rename, but the rename itself was not — so a power failure could restore
    the pre-rename state even though the file's fsync succeeded. This matters
    for Ed25519 signing seeds written via :func:`atomic_write_bytes`.

    Guarded for platforms that do not permit directory fsync (some filesystems
    / OSes raise ``EINVAL`` or ``EISDIR`` on ``os.fsync(fd)`` for a dir FD).
    """
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        # Directory not openable for reading — nothing we can do; the write
        # itself still succeeded.
        return
    try:
        os.fsync(fd)
    except OSError:
        # Directory fsync unsupported on this platform/filesystem. The file
        # data fsync already ran; rename durability is best-effort.
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> Path:
    """Write ``text`` to ``path`` atomically.

    The content goes to a temporary file in the destination's own directory,
    so the final ``os.replace`` is a same-filesystem rename and therefore
    atomic.  The mode is applied to the temporary file BEFORE the rename, so
    the destination is never visible with the wrong permissions.

    Args:
        path: Destination path.  Its parent directory is created if absent.
        text: Complete content to write.
        mode: Permission bits applied before the rename.

    Returns:
        The destination path.

    Raises:
        OSError: The write, the fsync, or the rename failed.

    <invariant>
      A concurrent reader of ``path`` never observes a partial document and
      never observes the file with permissions wider than ``mode``.
    </invariant>

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        # D-12: durability of the rename itself.
        _fsync_parent(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> Path:
    """Write ``data`` to ``path`` atomically (binary variant).

    Same write-temp-fsync-rename protocol as :func:`atomic_write_text`, for
    raw binary payloads (e.g. Ed25519 signing seeds) whose on-disk format
    must stay byte-exact across versions.

    Args:
        path: Destination path.  Its parent directory is created if absent.
        data: Complete binary content to write.
        mode: Permission bits applied before the rename.

    Returns:
        The destination path.

    Raises:
        OSError: The write, the fsync, or the rename failed.

    <invariant>
      A concurrent reader of ``path`` never observes a partial document and
      never observes the file with permissions wider than ``mode``.
    </invariant>

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        # D-12: durability of the rename itself (critical for Ed25519 seeds).
        _fsync_parent(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path
