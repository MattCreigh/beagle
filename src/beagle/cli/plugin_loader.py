"""Frontend plugin discovery via the ``beagle.frontends`` entry-point group.

WP-3 of the 1.4.1 release remediation. Modelled on
:mod:`beagle.mcp_plugins`: detection is automatic, activation is the plugin's
own ``app`` object being mounted under its entry-point name.

The loader's one invariant is that it cannot break the host: a plugin that
fails to import, exports no ``app``, or hangs in its ``__init__`` is skipped
with the error recorded — never raised. Discovery runs *after* the built-in
commands are registered, so a plugin can never shadow a built-in command.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any
from unittest.mock import patch

import typer

logger = logging.getLogger("Beagle.cli.plugin_loader")

_ENTRY_POINT_GROUP = "beagle.frontends"

# Per-plugin import timeout (plugin-pattern contract section 5): a hung
# plugin ``__init__`` must not stall ``beagle --help``. Five seconds is
# generous for a module import; anything longer is a defect in the plugin.
_PLUGIN_IMPORT_TIMEOUT_S = 5.0


@dataclass(slots=True)
class FrontendPluginInfo:
    """One discovered frontend plugin and its mountable app."""

    name: str
    app: Any = None
    importable: bool = True
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _import_entry_point(
    ep_name: str, ep_value: str
) -> tuple[Any | None, str, dict[str, Any]]:
    """Import one entry point and return ``(app, error, meta)``.

    Runs in a worker thread so a hung module ``__init__`` can be abandoned at
    the timeout rather than blocking the CLI. Returns the Typer ``app``
    exported by the target module, or an error string when the module cannot
    be imported or does not export ``app``.
    """
    try:
        module_name, _, attr = ep_value.partition(":")
        if not module_name:
            return None, f"malformed entry point {ep_value!r}", {}
        module = importlib.import_module(module_name)
        if attr:
            app = getattr(module, attr, None)
        else:
            app = getattr(module, "app", None)
        if app is None:
            return None, f"module {module_name!r} exports no 'app' object", {}
        return app, "", {}
    except BaseException as exc:  # noqa: BLE001 - WP-3: one broken plugin must never break the host CLI
        # Deliberately broad: the contract is that ANY failure to import a
        # third-party plugin — ImportError, SyntaxError, SystemExit raised at
        # module scope, a plugin bug of any kind — is recorded and skipped,
        # never propagated to the CLI. CancelledError does not occur here
        # because this runs on a worker thread, not the loop.
        return None, f"{type(exc).__name__}: {exc}", {}


def discover_frontend_plugins() -> list[FrontendPluginInfo]:
    """Detect installed frontend plugins via entry points.

    Returns:
        One :class:`FrontendPluginInfo` per detected plugin. Failures are
        reported per-plugin and never raised: the CLI must start with a
        broken plugin installed.
    """
    try:
        eps = list(entry_points(group=_ENTRY_POINT_GROUP))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.debug("frontend-plugin scan failed: %s", exc)
        return []

    discovered: list[FrontendPluginInfo] = []
    # Workers are daemonised by patching Thread.__init__: the initializer
    # callback runs after the thread has started, and CPython forbids setting
    # daemon on a live thread. An abandoned hung import must not keep the CLI
    # process alive at interpreter shutdown.
    _thread_init = threading.Thread.__init__

    def _daemonised_init(
        self: threading.Thread,
        group: None = None,
        target: Callable[..., object] | None = None,
        name: str | None = None,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        *,
        daemon: bool | None = None,
    ) -> None:
        # Mirrors threading.Thread.__init__'s real signature rather than a
        # (*a, **kw) passthrough: mypy checks a passthrough against every
        # overload of the target, which produced eleven arg-type errors. The
        # body is unchanged — the daemon flag is still forced True.
        _thread_init(self, group, target, name, args, kwargs, daemon=daemon)
        self.daemon = True

    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="beagle-plugin-scan")
    # Deliberately NOT used as a context manager: ``with`` calls
    # shutdown(wait=True) on exit, which would wait out the full hung import
    # and turn the per-plugin timeout into a process-wide stall.
    patch_ctx = patch.object(threading.Thread, "__init__", _daemonised_init)
    patch_ctx.__enter__()
    try:
        futures: dict[Any, FrontendPluginInfo] = {}
        for ep in eps:
            info = FrontendPluginInfo(name=ep.name)
            futures[pool.submit(_import_entry_point, ep.name, ep.value)] = info
            discovered.append(info)

        for future, info in futures.items():
            try:
                app, error, meta = future.result(timeout=_PLUGIN_IMPORT_TIMEOUT_S)
            except FuturesTimeout:
                info.importable = False
                info.error = (
                    f"import exceeded {_PLUGIN_IMPORT_TIMEOUT_S:.0f}s; plugin skipped"
                )
                logger.warning("[PluginLoader] %s: %s", info.name, info.error)
                continue
            except BaseException as exc:  # noqa: BLE001 - same contract as _import_entry_point
                info.importable = False
                info.error = f"{type(exc).__name__}: {exc}"
                logger.warning("[PluginLoader] %s: %s", info.name, info.error)
                continue
            info.app, info.error, info.meta = app, error, meta
            if error:
                info.importable = False
                logger.warning("[PluginLoader] %s: %s", info.name, error)
    finally:
        patch_ctx.__exit__(None, None, None)
        # cancel_futures=True and wait=False: a hung plugin's worker would
        # otherwise keep teardown waiting out the full import. Workers are
        # daemonised via the Thread.__init__ patch above, so an abandoned
        # import cannot hold the CLI process open at shutdown.
        pool.shutdown(wait=False, cancel_futures=True)

    return discovered


def mount_frontend_plugins(app: typer.Typer, register: Callable[[typer.Typer, str], None]) -> int:
    """Mount every discovered plugin's ``app`` under its entry-point name.

    Args:
        app: The host Typer application.
        register: The host's registration callable — ``app.add_typer`` for the
            built-ins. Called with ``(plugin_app, name)`` so a plugin can never
            shadow a built-in command group.

    Returns:
        The number of plugins mounted.
    """
    mounted = 0
    for plugin in discover_frontend_plugins():
        if not plugin.importable or plugin.app is None:
            continue
        with contextlib.suppress(Exception):
            register(plugin.app, plugin.name)
            mounted += 1
    if mounted:
        logger.info("[PluginLoader] mounted %d frontend plugin(s)", mounted)
    return mounted


# Entry-point group a plugin may use to name its process-launching entry point
# explicitly, as ``<name> = "<module>:<callable>"``. Optional: when absent, the
# convention in :func:`_resolve_frontend_launcher` applies.
_LAUNCHER_ENTRY_POINT_GROUP = "beagle.frontends.launcher"

# Candidate attribute names on the launcher module, in priority order. ``main``
# is the console-script convention; ``run`` is the exec-replacing variant; a
# plugin may also export an explicit ``launch``.
_LAUNCHER_ATTRS = ("launch", "main", "run")


def _entry_points_for(group: str) -> list[Any]:
    """Return the entry points in ``group``, or ``[]`` when the scan fails."""
    try:
        return list(entry_points(group=group))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.debug("frontend launcher scan failed for %s: %s", group, exc)
        return []


def _resolve_frontend_launcher(name: str) -> Callable[[], int] | None:
    """Resolve the callable that launches frontend ``name``, if any is installed.

    Resolution order:

    1. An explicit ``beagle.frontends.launcher`` entry point of that name —
       ``module:attr``. This is the preferred, unambiguous form.
    2. Convention over the ``beagle.frontends`` entry point of that name: the
       module's own ``launch``/``main``/``run``, else those names on a sibling
       ``<package>.launcher`` module.

    Every failure is swallowed and reported as ``None``: the host must start
    even when a frontend plugin is broken, exactly as for command mounting.
    """
    explicit = _entry_points_for(_LAUNCHER_ENTRY_POINT_GROUP)
    conventional = _entry_points_for(_ENTRY_POINT_GROUP)

    explicit_matches = [ep for ep in explicit if ep.name == name]
    conventional_matches = [ep for ep in conventional if ep.name == name]

    for ep in explicit_matches:
        callable_fn = _callable_from_entry_point(ep, attr_fallback=None)
        if callable_fn is not None:
            return callable_fn

    for ep in conventional_matches:
        callable_fn = _callable_from_entry_point(ep, attr_fallback=None)
        if callable_fn is not None:
            return callable_fn
        # Convention: the entry-point module carries the Typer ``app`` for the
        # subcommand surface; the process-launching entry point lives beside it
        # in ``<package>.launcher``.
        parent = ep.module.rpartition(".")[0]
        if not parent:
            continue
        callable_fn = _callable_from_module(f"{parent}.launcher")
        if callable_fn is not None:
            return callable_fn

    return None


def _callable_from_entry_point(
    ep: EntryPoint, attr_fallback: str | None
) -> Callable[[], int] | None:
    """Import ``ep``'s module and return a launcher callable, or ``None``."""
    module_name, _, attr = ep.value.partition(":")
    if not module_name:
        return None
    return _callable_from_module(module_name, attr or attr_fallback)


def _callable_from_module(
    module_name: str, attr: str | None = None
) -> Callable[[], int] | None:
    """Import ``module_name`` and return its launcher callable, or ``None``.

    When ``attr`` is given only that attribute is considered (it must be
    callable). Otherwise the module is probed for ``_LAUNCHER_ATTRS`` in order.
    """
    try:
        module = importlib.import_module(module_name)
    except BaseException as exc:  # noqa: BLE001 - a broken plugin must not break the host
        logger.warning("[PluginLoader] %s import failed: %s", module_name, exc)
        return None

    if attr:
        candidate = getattr(module, attr, None)
        return candidate if callable(candidate) else None

    for candidate_attr in _LAUNCHER_ATTRS:
        candidate = getattr(module, candidate_attr, None)
        if callable(candidate):
            return candidate
    return None


def launch_frontend(name: str) -> int | None:
    """Launch frontend ``name`` as the default command, or report its absence.

    The host never names a frontend *module* — only the entry-point name — so a
    frontend can be extracted to its own distribution without editing the host
    CLI. This is what keeps the D-18 extraction from re-introducing a
    host-to-plugin import edge.

    Args:
        name: The frontend's entry-point name (e.g. ``"pi"``).

    Returns:
        The process exit code, or ``None`` when no such frontend is installed.
        A ``None`` means the caller should fall back to its normal surface — it
        is not an error.
    """
    launcher = _resolve_frontend_launcher(name)
    if launcher is None:
        logger.debug("[PluginLoader] no launcher for frontend %r", name)
        return None
    try:
        return int(launcher())
    except SystemExit as exc:
        # A launcher may terminate the process itself (``raise SystemExit(0)``
        # on success, which is the console-script convention). Normalise it to
        # an exit code so the caller owns the actual exit.
        return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)


__all__ = [
    "FrontendPluginInfo",
    "discover_frontend_plugins",
    "launch_frontend",
    "mount_frontend_plugins",
]
