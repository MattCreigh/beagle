"""TC4 degradation tests — orpheus optional transport (performance-remediation-002).

Source brief: "Supplement 3.zip" / `performance-remediation-002.xml`, test class TC4
"Degradation test":

    The absence of pyrsistent must fall back to a deep copy. The absence of
    /dev/kvm must refuse to execute. The absence of orpheus must raise an import
    error. A fallback that is silent where the specification requires a loud
    warning is a defect.

The pyrsistent-deepcopy and /dev/kvm-degrade directions are already pinned by
`test_v13_4_features.py::test_deep_fork_state` and
`test_microvm_sandbox.py::test_deny_by_default_...` respectively. This module
closes the third gap — the **orpheus optional** path.

Orpheus is a separately-licensed proprietary transport (open-source wheel
`beagle_orpheus` NOT installed). The contract in
`src/beagle/infrastructure/_orpheus_optional.py`:

  - The module MUST import successfully (a default install works on the built-in
    HTTP transport). It must NOT raise at import time.
  - Every stub symbol MUST raise a `RuntimeError` with install guidance at first
    USE. A silent no-op would be the exact defect the brief forbids.

These tests pin that fail-closed behaviour. They run with the wheel absent —
the normal state on this host.
"""

from __future__ import annotations

import pytest


def _has_orpheus_wheel() -> bool:
    """True when the proprietary beagle_orpheus wheel is actually installed."""
    try:
        import beagle_orpheus  # type: ignore[import-not-found]  # noqa: F401
        return True
    except ImportError:
        return False


def test_module_imports_cleanly_without_wheel():
    """The optional module must import successfully (no import-time raise)."""
    import beagle.infrastructure._orpheus_optional as opt

    assert callable(opt.get_orpheus_client)
    assert callable(opt.get_ipc)
    assert callable(opt.create_rings)
    assert callable(opt.cleanup_rings)
    assert "OrpheusClient" in opt.__all__


@pytest.mark.skipif(
    _has_orpheus_wheel(),
    reason="beagle_orpheus wheel installed — native transport present, stubs inactive",
)
@pytest.mark.parametrize(
    "symbol",
    [
        "get_orpheus_client",
        "get_ipc",
        "create_rings",
        "cleanup_rings",
    ],
)
def test_stub_raises_at_use_not_at_import(symbol: str):
    """Each stub symbol raises at first use, naming the symbol and install hint."""
    import beagle.infrastructure._orpheus_optional as opt

    with pytest.raises(RuntimeError) as excinfo:
        getattr(opt, symbol)()
    msg = str(excinfo.value)
    assert symbol in msg
    assert "not installed" in msg.lower()
    assert "install" in msg.lower()


@pytest.mark.skipif(
    _has_orpheus_wheel(),
    reason="beagle_orpheus wheel installed — stubs replaced by native client",
)
def test_client_class_raises_at_instantiation():
    """OrpheusClient instantiation raises (never a silent stub object)."""
    from beagle.infrastructure._orpheus_optional import OrpheusClient

    with pytest.raises(RuntimeError) as excinfo:
        OrpheusClient()
    assert "not installed" in str(excinfo.value).lower()
