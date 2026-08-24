# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""JIT spawn and stale-socket detection for a Beacon instance.

See plans/beagle-beacon-coordination.xml WP-4, decision D-01, and the
architecture block's logic block ("When does Beacon spawn, stay, and tear
down?"). Beacon is a standalone subprocess — the first agent to attach spawns
it, and it outlives that agent's own session (concept spec section 4.2).

"Detect a dead process, not a vanished one": a socket file that exists but
does not answer PING is a crashed Beacon. It is unlinked and a fresh server
is spawned in its place, rather than either reusing a dead file or refusing
to start because a file happens to be present.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from beagle.beacon.backend import BackendDriver, BackendUnavailableError
from beagle.beacon.backends import get_driver
from beagle.beacon.keys import BeaconPaths, resolve_paths
from beagle.config.config import get_config

logger = logging.getLogger("Beagle.beacon.spawn")

_SPAWN_POLL_INTERVAL_S = 0.05


def _current_driver() -> BackendDriver:
    return get_driver(get_config().coord.backend)


def is_live(paths: BeaconPaths, *, connect_timeout_s: float) -> bool:
    """Return True if a Beacon is reachable at paths.socket_path right now."""
    return _current_driver().is_live(paths, connect_timeout_s=connect_timeout_s)


def _clear_stale_socket(paths: BeaconPaths) -> None:
    """Clear the on-disk remains of a store that no longer answers."""
    _current_driver().clear_stale(paths)


def _spawn_subprocess(workdir: Path) -> None:
    """Launch `python -m beagle.beacon.server <workdir>` as a detached process.

    Args:
        workdir: The working directory the new Beacon instance is scoped to.

    """
    subprocess.Popen(
        [sys.executable, "-m", "beagle.beacon.server", str(workdir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from this process's session/controlling tty
    )


def ensure_running(
    workdir: Path | str,
    *,
    connect_timeout_s: float = 2.0,
    spawn_wait_s: float = 5.0,
) -> BeaconPaths:
    """Ensure a Beacon instance is live for workdir, spawning one if needed.

    Implements the spawn logic block from the concept spec:
        spawn(p) = attach_ok(p) OR (missing(p) AND spawn_server(p))

    Args:
        workdir: The working directory to scope the Beacon instance to.
        connect_timeout_s: Timeout for the liveness PING probe.
        spawn_wait_s: Maximum time to wait for a newly spawned server to
            start answering PING.

    Returns:
        The resolved BeaconPaths, guaranteed to have a live server behind
        socket_path when this returns without raising.

    Raises:
        TimeoutError: A backend that requires a server process did not
            answer PING within spawn_wait_s of being spawned.
        BackendUnavailableError: a backend that does NOT require a server
            process (driver.capabilities.requires_server is False) never
            became live within spawn_wait_s. There is no subprocess to
            wait on spawning in this case — the backend attaches directly
            — so a TimeoutError naming a spawn that never happened would
            be misleading.

    """
    paths = resolve_paths(workdir)
    driver = _current_driver()

    if driver.is_live(paths, connect_timeout_s=connect_timeout_s):
        return paths

    driver.clear_stale(paths)
    if driver.capabilities.requires_server:
        _spawn_subprocess(Path(workdir).resolve())

    deadline = time.monotonic() + spawn_wait_s
    while time.monotonic() < deadline:
        if driver.is_live(paths, connect_timeout_s=connect_timeout_s):
            return paths
        time.sleep(_SPAWN_POLL_INTERVAL_S)

    if driver.capabilities.requires_server:
        msg = f"Beacon did not answer PING within {spawn_wait_s}s of being spawned for {workdir}"
        raise TimeoutError(msg)
    msg = (
        f"backend {driver.capabilities.name!r} did not become live within "
        f"{spawn_wait_s}s for {workdir}"
    )
    raise BackendUnavailableError(msg)
