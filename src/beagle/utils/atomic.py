"""Atomic file writes.

This module is the single implementation of write-temp-then-rename for the
project.  A reader of the destination sees either the complete previous
document or the complete new one, never a partial write.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


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
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path
