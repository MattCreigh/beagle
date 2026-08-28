"""Regression tests for beagle.utils.atomic — D-12 (release-readiness audit 2026-08-28).

The write-temp-fsync-rename protocol fsynced the file's data but never the
parent directory, so the rename itself could be lost on power failure. This
asserts the parent dir is fsynced AFTER the replace, in the correct order.
"""

from __future__ import annotations

import os
from pathlib import Path

from beagle.utils import atomic


def test_atomic_write_text_fsyncs_parent_dir_after_replace(tmp_path: Path) -> None:
    """os.fsync must be called on the parent directory fd after os.replace."""
    target = tmp_path / "doc.txt"
    events: list[str] = []
    real_open = os.open
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_open(path, flags, *args, **kwargs):  # noqa: ANN001
        fd = real_open(path, flags, *args, **kwargs)
        p = os.fsdecode(path)
        events.append(f"open:{os.path.basename(p)}")
        return fd

    def spy_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def spy_replace(src, dst, *args, **kwargs):  # noqa: ANN001
        events.append(f"replace:{os.path.basename(os.fsdecode(dst))}")
        return real_replace(src, dst, *args, **kwargs)

    os.open = spy_open  # type: ignore[assignment]
    os.fsync = spy_fsync  # type: ignore[assignment]
    os.replace = spy_replace  # type: ignore[assignment]
    try:
        atomic.atomic_write_text(target, "hello", mode=0o600)
    finally:
        os.open = real_open  # type: ignore[assignment]
        os.fsync = real_fsync  # type: ignore[assignment]
        os.replace = real_replace  # type: ignore[assignment]

    assert target.read_text(encoding="utf-8") == "hello"
    # Order: temp open → file-data fsync → replace → parent-dir open → parent fsync.
    assert events[0].startswith("open:.doc.txt")  # temp file created in parent
    assert events.count("fsync") == 2  # file data + parent dir
    replace_idx = events.index("replace:doc.txt")
    assert events[replace_idx + 1].startswith("open:test")  # parent opened right after replace
    assert events[-1] == "fsync"  # final fsync is the parent dir


def test_atomic_write_bytes_fsyncs_parent_dir(tmp_path: Path) -> None:
    """Binary variant (Ed25519 seed path) must also fsync the parent dir."""
    target = tmp_path / "seed.bin"
    fsync_calls: list[os.fsencode] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    os.fsync = spy_fsync  # type: ignore[assignment]
    try:
        atomic.atomic_write_bytes(target, b"\x01\x02\x03", mode=0o600)
    finally:
        os.fsync = real_fsync  # type: ignore[assignment]

    assert target.read_bytes() == b"\x01\x02\x03"
    # At least one fsync on the file data + one on the parent dir.
    assert len(fsync_calls) >= 2


def test_parent_fsync_graceful_when_unsupported(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """A platform that cannot fsync a directory must not fail the write."""
    target = tmp_path / "doc.txt"

    real_open = os.open
    opened_for_fsync: list[int] = []

    def spy_open(path, flags, *args, **kwargs):  # noqa: ANN001
        fd = real_open(path, flags, *args, **kwargs)
        if os.path.basename(os.fsdecode(path)) == str(tmp_path.name):
            opened_for_fsync.append(fd)
        return fd

    monkeypatch.setattr(os, "open", spy_open)

    def spy_fsync(fd: int) -> None:
        if fd in opened_for_fsync:
            raise OSError(22, "Invalid argument")  # EINVAL: dir fsync unsupported
        real_fsync(fd)

    import os as _os

    real_fsync = _os.fsync
    monkeypatch.setattr(_os, "fsync", spy_fsync)

    atomic.atomic_write_text(target, "still works", mode=0o600)
    assert target.read_text(encoding="utf-8") == "still works"
