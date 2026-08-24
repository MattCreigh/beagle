"""SP-10/SP-11: contract for config.paths.resolve_executable.

beagle-spotless-phase2, work packages SP-10 (B607) and SP-11 (S607). Eighteen
subprocess call sites passed a bare executable name as argv[0], deferring the
lookup to the OS at exec time. Which binary ran therefore depended on the PATH
the process happened to inherit, and a writable PATH entry ahead of the real
tool is an execution primitive.

These tests pin the resolver contract the call sites now depend on:
an absolute path, an operator override, a named failure, and a stable result
across calls.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from beagle.config.paths import reset_executable_cache, resolve_executable


@pytest.fixture(autouse=True)
def _clear_cache():
    """The resolver caches for the process lifetime; each test starts clean."""
    reset_executable_cache()
    yield
    reset_executable_cache()


def test_resolves_to_an_absolute_path() -> None:
    resolved = resolve_executable("sh")
    assert Path(resolved).is_absolute()
    assert Path(resolved).exists()


def test_missing_executable_raises_a_named_error() -> None:
    """A missing tool must name itself and the override variable, not fail obscurely."""
    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_executable("beagle-no-such-executable")

    message = str(excinfo.value)
    assert "beagle-no-such-executable" in message
    assert "BEAGLE-NO-SUCH-EXECUTABLE_BIN" in message or "_BIN" in message


def test_missing_executable_raises_filenotfounderror_subclass() -> None:
    """Call sites rely on the type: subprocess raises the same one for a missing binary.

    Several call sites (file_writer's optional ruff/yamllint, goose_launcher's
    `ss` fallback) already had `except FileNotFoundError` handlers that treated
    a missing tool as a soft degradation. Resolution must not change which
    handler fires.
    """
    with pytest.raises(OSError):
        resolve_executable("beagle-no-such-executable")


def test_env_override_wins_over_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "fake-git"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    monkeypatch.setenv("GIT_BIN", str(fake))
    reset_executable_cache()

    assert resolve_executable("git") == str(fake)


def test_result_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution happens once; a later PATH change must not move the binary."""
    first = resolve_executable("sh")

    monkeypatch.setenv("PATH", "")
    second = resolve_executable("sh")

    assert second == first


def test_reset_clears_the_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resolve_executable("git")

    fake = tmp_path / "other-git"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("GIT_BIN", str(fake))

    reset_executable_cache()

    assert resolve_executable("git") == str(fake)


def test_hyphenated_name_maps_to_underscored_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "tool"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    monkeypatch.setenv("SOME_TOOL_BIN", str(fake))
    reset_executable_cache()

    assert resolve_executable("some-tool") == str(fake)


def test_no_call_site_passes_a_bare_executable_name() -> None:
    """Regression guard for the 18 S607 sites this work package cleared.

    The doctrine ruff profile enforces this too, but that profile is a separate
    config; this keeps the invariant inside the suite that every contributor runs.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "beagle"
    offenders: list[str] = []

    for path in src.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            for tool in ('"git",', '"ruff",', '"yamllint",', '"ss",', '"python3",'):
                if stripped.startswith(f"[{tool}") or stripped == tool:
                    offenders.append(f"{path.relative_to(src)}:{lineno}: {stripped}")

    assert not offenders, "bare executable name used as argv[0]:\n" + "\n".join(offenders)


def test_path_env_is_honoured_when_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an override the resolver reads the current PATH, not a hardcoded dir."""
    monkeypatch.delenv("SH_BIN", raising=False)
    real = resolve_executable("sh")
    reset_executable_cache()

    monkeypatch.setenv("PATH", os.path.dirname(real))
    assert resolve_executable("sh") == real
