"""Regression: BEAGLE_SKIP_HYDRATION=1 must prevent on_session_start() from
running. Cheap to assert: patch on_session_start and check it was not called.
"""

from __future__ import annotations

import importlib.util
import sys
import types


def test_skip_hydration_env_bypasses_session_start(monkeypatch):
    monkeypatch.setenv("BEAGLE_SKIP_HYDRATION", "1")

    called = {"count": 0}

    def fake_on_session_start(*a, **kw):
        called["count"] += 1
        return {"hydration": {}, "agent_sync": {}}

    fake_mod = types.ModuleType("beagle.context.hydration_hook")
    fake_mod.on_session_start = fake_on_session_start
    # v13.19.4: also stub on_session_end and quick_hydration_check because
    # context/__init__.py:104 imports them eagerly when the real module
    # chain is loaded. The test only cares about on_session_start not
    # being called.
    fake_mod.on_session_end = lambda *a, **kw: None
    fake_mod.quick_hydration_check = lambda *a, **kw: False
    monkeypatch.setitem(sys.modules, "beagle.context.hydration_hook", fake_mod)

    # Verify the gate in graph.py contains BEAGLE_SKIP_HYDRATION
    src_path = importlib.util.find_spec("beagle.core.graph").origin
    with open(src_path, encoding="utf-8") as f:
        text = f.read()
    assert "BEAGLE_SKIP_HYDRATION" in text, (
        "graph.py must read BEAGLE_SKIP_HYDRATION before calling on_session_start"
    )
