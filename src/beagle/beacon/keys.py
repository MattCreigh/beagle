# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Directory-scoped path derivation for a Beacon instance.

See plans/beagle-beacon-coordination.xml WP-2 and the concept spec section 3
("Deciding the socket path"). A Beacon is scoped to a resolved working
directory so that two sessions in the same directory join one instance and
two sessions in different directories never share state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from beagle.config.paths import get_data_root, get_runtime_dir

_DIRHASH_LENGTH = 16


def filehash(path: str) -> str:
    """Return the stable key fragment for a file-lock path.

    Unlike dirhash, this does NOT resolve against the filesystem: the path
    is a logical, repo-relative identifier an agent supplies (e.g.
    "src/beagle/beacon/store.py"), not necessarily one that exists on this
    host. Two spellings of the same logical path must hash identically, so
    callers should pass the same normalised form consistently (repo-relative,
    forward slashes) — normalising an arbitrary caller-supplied string
    against a real filesystem would require the file to exist, which a lock
    target need not.

    Args:
        path: A repo-relative file path string.

    Returns:
        A 16-character lowercase hex digest.

    """
    return hashlib.sha256(path.encode()).hexdigest()[:_DIRHASH_LENGTH]


def dirhash(workdir: Path | str) -> str:
    """Return the stable, content-derived key for a working directory.

    Uses ``Path.resolve()`` before hashing so a symlink or a relative path
    hashes to its real target, never to the string that named it (doctrine:
    path containment by real resolution, never by string prefix).

    Args:
        workdir: The working directory to scope a Beacon instance to.

    Returns:
        A 16-character lowercase hex digest of the resolved absolute path.

    """
    resolved = Path(workdir).resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()
    return digest[:_DIRHASH_LENGTH]


@dataclass(frozen=True)
class BeaconPaths:
    """The full set of filesystem paths for one Beacon instance.

    Every path is derived from ``dirhash`` and lives under a 0700 directory,
    so no other local user can read or write into a running Beacon's state.
    """

    dirhash: str
    base_dir: Path
    socket_path: Path
    rings_dir: Path
    archive_dir: Path

    def agent_ring_path(self, agent_id: str, *, direction: str) -> Path:
        """Return the ring file path for one agent's in or out ring.

        Args:
            agent_id: The full uuid4() agent identifier (never truncated).
            direction: ``"in"`` (agent to Beacon) or ``"out"`` (Beacon to agent).

        Returns:
            The ring file path under ``rings_dir``.

        Raises:
            ValueError: direction is not "in" or "out".

        """
        if direction not in ("in", "out"):
            msg = f"direction must be 'in' or 'out', got {direction!r}"
            raise ValueError(msg)
        return self.rings_dir / f"{agent_id}.{direction}.ring"

    def channel_dir(self, channel_id: str) -> Path:
        """Return the directory holding both rings of one peer channel."""
        return self.rings_dir.parent / "chan" / channel_id


def resolve_paths(workdir: Path | str) -> BeaconPaths:
    """Derive every Beacon filesystem path for a working directory.

    Args:
        workdir: The working directory to scope this Beacon instance to.

    Returns:
        A :class:`BeaconPaths` with every directory already existing at
        mode 0700 (the socket and ring files themselves are created, and
        their own modes set, by the store and connector modules).

    """
    h = dirhash(workdir)
    runtime = get_runtime_dir()
    base_dir = runtime / "coord" / h
    rings_dir = base_dir / "rings"
    archive_dir = get_data_root() / "coord" / h / "archive"

    for d in (base_dir, rings_dir):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    return BeaconPaths(
        dirhash=h,
        base_dir=base_dir,
        socket_path=base_dir / "beacon.sock",
        rings_dir=rings_dir,
        archive_dir=archive_dir,
    )
