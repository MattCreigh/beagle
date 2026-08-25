"""Connection transport abstraction — the ONE seam for all outbound connections.

beagle ships with a plain HTTP transport (httpx) as the default. Alternative
transports — notably the proprietary ``beagle-orpheus`` wheel, which speaks
FlatBuffers frames natively over Orpheus ring buffers — implement this same
protocol and are **auto-detected** via the ``beagle.transports`` entry-point
group, but are never activated implicitly: switching away from HTTP is an
explicit, informed operator decision.

Selection order for the ACTIVE transport:

  1. ``$BEAGLE_TRANSPORT`` environment variable
  2. ``[connections] transport = "..."`` in ``~/.config/beagle``
  3. built-in default: ``"http"``

Hot-swap: :func:`activate_transport` switches the active transport at
runtime. In-flight clients created before the swap finish on their own
transport; new clients pick up the new transport immediately.

<invariant>
The open-source distribution contains NO proprietary transport code. The
orpheus transport arrives only as a separately-licensed wheel that
registers itself through the entry-point group below.
</invariant>
<config-change>
<file>src/beagle/config/schema.py</file>
<change>[connections].transport default "http"; never auto-set to a plugin name</change>
<reason>default install must work with zero proprietary components</reason>
</config-change>
<verification-checklist>
1. Fresh configless install reports active transport == "http".
2. Installing beagle-orpheus lists it under available transports but does
   NOT change the active one.
3. BEAGLE_TRANSPORT=orpheus (or config file) activates it; hot-swap back
   works at runtime.
</verification-checklist>
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.core.transports")

_ENTRY_POINT_GROUP = "beagle.transports"
_DEFAULT_TRANSPORT = "http"


# ── Message-level protocol ────────────────────────────────────────────────────


@dataclass(slots=True)
class TransportResponse:
    """Uniform response envelope across every transport."""

    status_code: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Decode the body as JSON.

        Raises:
            ValueError: If the body is not valid JSON.
        """
        import json

        return json.loads(self.body)


class Transport(ABC):
    """Common protocol implemented by every connection backend.

    A Transport is a *connection factory and message exchange*: callers either
    use the message-level API (:meth:`request`) or obtain a client object
    (:meth:`async_client`) matching the httpx AsyncClient surface used across
    the codebase. Implementations must honour explicit timeouts on every call.
    """

    name: str = "abstract"
    description: str = ""

    @abstractmethod
    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> TransportResponse:
        """Perform one round-trip request.

        Args:
            method: HTTP verb or transport-equivalent operation.
            url: Absolute URL (HTTP) or peer-addressed endpoint (others).
            json: Optional JSON-serialisable payload.
            headers: Request headers.
            timeout_s: Hard timeout in seconds — required, never implicit.

        Returns:
            Uniform :class:`TransportResponse`.
        """

    @abstractmethod
    def async_client(self, **kwargs: Any) -> Any:
        """Return a client object compatible with the httpx.AsyncClient surface.

        The default transport returns a real ``httpx.AsyncClient``. Plugin
        transports may return their own adapter; call sites only construct
        clients through this method, never by importing httpx directly.
        """

    @abstractmethod
    def sync_client(self, **kwargs: Any) -> Any:
        """Return a client object compatible with the httpx.Client surface."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release pooled resources."""


# ── Built-in default ─────────────────────────────────────────────────────────


class HTTPTransport(Transport):
    """Plain HTTP(S) over httpx — the shipped default."""

    name = "http"
    description = "Default HTTP/HTTPS transport (httpx)"

    def __init__(self) -> None:
        self._client: Any | None = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> TransportResponse:
        client = self.async_client(timeout=timeout_s)
        resp = await client.request(method, url, json=json, headers=headers)
        await client.aclose()
        return TransportResponse(resp.status_code, resp.content, dict(resp.headers))

    def async_client(self, **kwargs: Any) -> Any:
        if self._client is None or kwargs:
            import httpx

            kwargs.setdefault("timeout", 30.0)
            if kwargs.get("timeout") is not None and not kwargs.pop("pooled", False):
                # Per-call clients honour per-call timeouts; the shared pool
                # keeps the first-seen timeout. Simplest correct policy: one
                # long-lived client is NOT used when an explicit timeout differs.
                return httpx.AsyncClient(**kwargs)
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    def sync_client(self, **kwargs: Any) -> Any:
        import httpx

        kwargs.setdefault("timeout", 30.0)
        return httpx.Client(**kwargs)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ── Registry / discovery / selection ─────────────────────────────────────────


def _discover_plugin_transports() -> dict[str, dict[str, Any]]:
    """Scan installed distributions for registered transports.

    Returns:
        Mapping of transport name → metadata (loaded lazily; factories are
        NOT invoked here — detection never activates).
    """
    found: dict[str, dict[str, Any]] = {}
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 - metadata scan must never crash callers
        logger.debug("transport entry-point scan failed: %s", exc)
        return found
    for ep in eps:
        try:
            meta = ep.load()
            found[ep.name] = {
                "factory": getattr(meta, "factory", meta),
                "description": getattr(meta, "description", ""),
                "entry_point": f"{ep.value}",
            }
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not hide others
            logger.warning("transport plugin %r failed to load: %s", ep.name, exc)
    return found


class TransportRegistry:
    """Holds the available transports and the currently ACTIVE one."""

    def __init__(self) -> None:
        self._builtin: dict[str, Transport] = {"http": HTTPTransport()}
        self._plugin_meta: dict[str, dict[str, Any]] = {}
        self._active_name: str = _DEFAULT_TRANSPORT
        self._rescanned = False

    def rescan(self) -> None:
        """Re-run entry-point discovery (call after installing a wheel)."""
        try:
            self._plugin_meta = _discover_plugin_transports()
        finally:
            self._rescanned = True

    def available(self) -> dict[str, str]:
        """Name → description of every DETECTED transport (built-in + plugins)."""
        if not self._rescanned:
            self.rescan()
        out = {n: t.description for n, t in self._builtin.items()}
        for name, meta in self._plugin_meta.items():
            out[name] = str(meta.get("description") or "third-party transport")
        return out

    def activate(self, name: str) -> Transport:
        """Activate a detected transport by name (hot-swap).

        Args:
            name: Transport name ("http" or an installed plugin's name).

        Returns:
            The newly active transport instance.

        Raises:
            KeyError: If no such transport is detected. Proprietary wheels
                surface a clear message here rather than silently falling back.
        """
        if name in self._builtin:
            self._active_name = name
            return self._builtin[name]
        if not self._rescanned:
            self.rescan()
        meta = self._plugin_meta.get(name)
        if meta is None:
            raise KeyError(f"transport {name!r} not detected; available: {sorted(self.available())}")
        instance = meta["factory"]()
        self._builtin[name] = instance
        self._active_name = name
        logger.info("transport ACTIVATED: %s", name)
        return instance

    def active(self) -> Transport:
        """The currently active transport, resolving env/config on first use."""
        if self._rescanned and self._active_name != _DEFAULT_TRANSPORT:
            return self._builtin[self._active_name]
        wanted = os.environ.get("BEAGLE_TRANSPORT")
        if not wanted:
            wanted = _configured_transport()
        if wanted and wanted != _DEFAULT_TRANSPORT:
            try:
                self.activate(wanted)
            except KeyError as err:
                logger.error("configured transport unavailable: %s — staying on http", err)
        return self._builtin[self._active_name]

    @property
    def active_name(self) -> str:
        return self._active_name


def _configured_transport() -> str | None:
    """Read ``[connections].transport`` from ~/.config/beagle (best effort)."""
    try:
        from ..config.loader import get_config

        cfg = get_config()
        value = getattr(getattr(cfg, "connections", None), "transport", None)
        return str(value) if value else None
    except Exception as exc:  # noqa: BLE001 - config absence must never break selection
        logger.debug("no configured transport (%s)", exc)
        return None


_registry: TransportRegistry | None = None


def get_registry() -> TransportRegistry:
    """Process-wide registry (lazy singleton)."""
    global _registry
    if _registry is None:
        _registry = TransportRegistry()
    return _registry


def reset_registry() -> None:
    """Drop the singleton (tests)."""
    global _registry
    _registry = None


def active() -> Transport:
    """Convenience accessor for the ACTIVE transport."""
    return get_registry().active()


def available_transports() -> dict[str, str]:
    """Detected transports (auto-detected plugins included, none activated)."""
    return get_registry().available()


def activate_transport(name: str) -> Transport:
    """Explicitly hot-swap the active transport."""
    return get_registry().activate(name)
