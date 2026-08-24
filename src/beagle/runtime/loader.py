"""Resolve the configured sub-agent execution runtime.

Axis 2 of the replaceability model: the ``[runtime].plugin`` config value
selects which :class:`beagle.runtime.base.AgentRuntime` Beagle uses to
spawn sub-agents. Plugins register themselves under the
``beagle.runtimes`` entry-point group in ``pyproject.toml``; this loader
dispatches on that group so a new runtime can be added without editing
code here.
"""

from __future__ import annotations

import logging
from importlib import metadata

from beagle.config.schema import WorkflowConfig
from beagle.runtime.base import AgentRuntime
from beagle.runtime.goose_cli import GooseCliRuntime

logger = logging.getLogger("Beagle.runtime.loader")

# Hardcoded default that does not require the config layer to be loaded
# first (breaks a potential import cycle at cold start).
_DEFAULT_PLUGIN = "goose_cli"


def _goose_cli_factory() -> GooseCliRuntime:
    """Entry-point factory for the ``goose_cli`` runtime plugin.

    Returns:
        A configured :class:`GooseCliRuntime` instance.

    """
    return GooseCliRuntime()


def _discover_plugins() -> dict[str, AgentRuntime]:
    """Build a {plugin_name: instance} map from entry points.

    Falls back to the built-in ``goose_cli`` when the entry-point group is
    absent (e.g. running from a source tree without an installed package).

    Returns:
        Mapping of runtime plugin name to an instance.

    """
    plugins: dict[str, AgentRuntime] = {"goose_cli": GooseCliRuntime()}
    try:
        eps = metadata.entry_points(group="beagle.runtimes")
    except (AttributeError, TypeError):
        # No metadata / not installed as a package yet.
        return plugins
    for ep in eps:
        try:
            factory = ep.load()
            plugins[ep.name] = factory()
        except (ImportError, AttributeError, TypeError) as exc:
            # A broken plugin must not take the whole runtime loader down;
            # the built-in goose_cli default remains available. But it must
            # not be silent either — the operator needs a diagnostic naming
            # the entry point and the exception.
            logger.warning(
                "runtime plugin %r failed to load (%s: %s); falling back to the "
                "built-in goose_cli runtime",
                ep.name,
                type(exc).__name__,
                exc,
            )
            continue
    return plugins


def get_runtime(config: WorkflowConfig | None = None) -> AgentRuntime:
    """Return the runtime instance selected by configuration.

    Args:
        config: The loaded workflow config. When ``None``, loads the
            default config to read ``[runtime].plugin``.

    Returns:
        An :class:`AgentRuntime` instance.

    Raises:
        KeyError: When the configured plugin is not registered.

    """
    plugin_name = _DEFAULT_PLUGIN
    if config is not None:
        plugin_name = config.runtime.plugin or _DEFAULT_PLUGIN

    plugins = _discover_plugins()
    if plugin_name not in plugins:
        raise KeyError(
            f"runtime plugin {plugin_name!r} is not registered under "
            "the 'beagle.runtimes' entry-point group"
        )
    return plugins[plugin_name]


def runtime_plugin_name(config: WorkflowConfig | None = None) -> str:
    """Return the configured runtime plugin name.

    Args:
        config: The loaded workflow config, or ``None`` to use the default.

    Returns:
        The configured plugin name (e.g. ``goose_cli``).

    """
    if config is None:
        return _DEFAULT_PLUGIN
    return config.runtime.plugin or _DEFAULT_PLUGIN
