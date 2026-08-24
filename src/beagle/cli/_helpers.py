"""Deprecated: merged into :mod:`beagle.cli.helpers` (SP-8).

This shim re-exports the moved names for one release so any external caller
that imported ``beagle.cli._helpers`` keeps working. Remove this module in the
next release after 1.0.9.
"""

from __future__ import annotations

import warnings

from .helpers import persist_report, resolve_workflow, show_estimate

warnings.warn(
    "beagle.cli._helpers is deprecated; import from beagle.cli.helpers instead. "
    "This shim will be removed in the next release.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["persist_report", "resolve_workflow", "show_estimate"]
