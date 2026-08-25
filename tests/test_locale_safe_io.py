"""B-30 regression test: reading UTF-8 files under C locale must not crash.

The RAG server and style-guide renderer are started by systemd without LANG
or LC_ALL.  This test proves that config.toml (which contains box-drawing
characters) and style-guide TOML are still readable.
"""

from __future__ import annotations

import contextlib
import locale
import os
from collections.abc import Iterator

import pytest

from beagle.infrastructure import cast_ingestion
from beagle.style_guides import injector


@pytest.fixture
def c_locale(monkeypatch) -> Iterator[None]:
    """Force the C locale for the duration of a test.

    IMPORTANT: The process-global locale (``locale.setlocale(LC_ALL, ...)``)
    must be RESTORED, not just the environment variables. If the C locale
    leaks past the yield, pytest's monkeypatch-only env-var restore leaves
    the Python interpreter in C locale permanently, and every downstream
    test that decodes subprocess output (config.toml, style-guide TOML — both
    contain box-drawing / smart-quote bytes) crashes with
    UnicodeDecodeError: 'ascii' codec can't decode byte 0xe2 ...
    """
    # Save the current PROCESS-LOCAL locale before any mutation. Note: locale
    # can't be set to "" on all platforms, so we guard the probe.
    old_locale: str | None = None
    try:
        old_locale = locale.setlocale(locale.LC_ALL)
    except locale.Error:
        # Platform doesn't have a baseline locale set; pick C.UTF-8 as a safe
        # restore target — it's UTF-8 on Linux and matches the natural state.
        old_locale = "C.UTF-8"

    old_env = (os.environ.get("LC_ALL"), os.environ.get("LANG"))
    monkeypatch.setitem(os.environ, "LC_ALL", "C")
    monkeypatch.setitem(os.environ, "LANG", "C")
    # Update Python's locale state so any locale-sensitive stdlib call sees C.
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "C")
    try:
        yield
    finally:
        # Restore process-local locale FIRST (try/finally is mandatory here —
        # if env restore or pytest-teardown runs first, the process locale
        # still leaks for subsequent tests).
        with contextlib.suppress(locale.Error):
            if old_locale is not None:
                locale.setlocale(locale.LC_ALL, old_locale)
        # Then restore environment variables.
        for key, val in zip(("LC_ALL", "LANG"), old_env, strict=False):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@pytest.mark.usefixtures("c_locale")
def test_config_toml_loads_under_c_locale():
    config_path = cast_ingestion._find_config_toml()
    assert config_path is not None
    with open(config_path, encoding="utf-8") as f:
        contents = f.read()
    assert "incremental_ingest" in contents


@pytest.mark.usefixtures("c_locale")
def test_style_guide_toml_loads_under_c_locale():
    # v14.0: beagle_environment.toml was archived to guides/_archive/ during
    # the renumber; it still carries the box-drawing UTF-8 content this test
    # exercises under the C locale.
    guide = injector.StyleGuideLoader().guides_dir / "_archive" / "beagle_environment.toml"
    assert guide.exists()
    with open(guide, encoding="utf-8") as f:
        contents = f.read()
    # Box-drawing characters are present in the style guide.
    assert any(ord(ch) > 127 for ch in contents)
