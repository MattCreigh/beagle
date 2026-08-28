# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""The backend registry — how a TOML value becomes a store client.

See plans/beagle-coord-backend-slot.xml WP-B4, decision D-B3, and hard
constraint C-B1/C-B2.

``REGISTRY`` maps a backend name to its driver CLASS. ``get_driver``
resolves a name to a driver INSTANCE and is the only place a caller
should ever construct one — this makes swapping backends purely a
``[coord].backend`` config edit (C-B1) and makes an unknown or
misconfigured name a loud, immediate failure rather than a silent
fallback (C-B2, D-B4).
"""

from __future__ import annotations

from beagle.beacon.backend import BackendDriver, UnknownBackendError
from beagle.beacon.backends.fakeredis_unix import FakeredisUnixDriver

REGISTRY: dict[str, type[BackendDriver]] = {
    "fakeredis_unix": FakeredisUnixDriver,
}


def register(name: str, driver_cls: type[BackendDriver]) -> None:
    """Add a backend to the registry.

    Exists so a conformance test can register a backend without editing
    ``src/`` (C-B1, WP-B5).

    Args:
        name: The registry key — the value `[coord].backend` selects.
        driver_cls: A class implementing :class:`BackendDriver`.

    Raises:
        ValueError: `name` is already registered. A silent overwrite
            would let one test change the backend every later test
            resolves.

    """
    if name in REGISTRY:
        msg = f"backend {name!r} is already registered to {REGISTRY[name]!r}"
        raise ValueError(msg)
    REGISTRY[name] = driver_cls


def get_driver(name: str) -> BackendDriver:
    """Resolve a registered backend name to a driver instance.

    Args:
        name: A key of `REGISTRY` — normally `[coord].backend`.

    Returns:
        A fresh instance of the registered driver class.

    Raises:
        UnknownBackendError: `name` is not registered. The message lists
            every registered name (C-B2). There is no default and no
            fallback.

    """
    driver_cls = REGISTRY.get(name)
    if driver_cls is None:
        msg = f"[coord].backend = {name!r} is not a registered backend. Registered: {sorted(REGISTRY)}"
        raise UnknownBackendError(msg)
    return driver_cls()
