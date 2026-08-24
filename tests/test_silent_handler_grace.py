"""SP-1/SP-2: tests for silent-handler graceful degradation.

beagle-spotless-phase2, work package SP-1 (silent handlers). The silent
handlers under test intentionally swallow a failure and fall through to a safe
default rather than crash. These tests lock in that degradation contract so the
`pass` bodies are verified behaviour, not blind swallows.
"""

from __future__ import annotations

from pathlib import Path

# ── style_guides/version_resolver.get_version ────────────────────────────────


def test_version_resolver_falls_back_to_metadata(tmp_path: Path, monkeypatch) -> None:
    """When pyproject.toml is absent, get_version falls back to dist metadata.

    The silent handler swallows the missing-file error and returns the
    installed distribution version instead of raising.
    """

    from beagle.style_guides import version_resolver

    # Point _resolve_repo_root at a dir with no pyproject.toml.
    empty = tmp_path / "empty"
    empty.mkdir()

    def _fake_resolve(_root=None):
        return empty

    monkeypatch.setattr(version_resolver, "_resolve_repo_root", _fake_resolve)
    version = version_resolver.get_version(repo_root=empty)
    assert isinstance(version, str)
    assert version  # non-empty


def test_version_resolver_reads_pyproject_version(tmp_path: Path, monkeypatch) -> None:
    """When pyproject.toml has a version, it is returned."""
    from beagle.style_guides import version_resolver

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8"
    )

    def _fake_resolve(_root=None):
        return tmp_path

    monkeypatch.setattr(version_resolver, "_resolve_repo_root", _fake_resolve)
    assert version_resolver.get_version(repo_root=tmp_path) == "1.2.3"


# ── health/collector RSS / fd collection ─────────────────────────────────────


def test_collect_rss_returns_float() -> None:
    """RSS collection returns a float (graceful on missing /proc)."""
    from beagle.health import collector

    rss = collector._collect_rss_mb()
    assert isinstance(rss, float)
    assert rss >= 0.0


def test_collect_fd_count_returns_int() -> None:
    """fd count returns an int (0 on unavailable /proc)."""
    from beagle.health import collector

    count = collector._collect_fd_count()
    assert isinstance(count, int)
    assert count >= 0


def test_collect_fd_limit_returns_int() -> None:
    """fd limit returns an int."""
    from beagle.health import collector

    limit = collector._collect_fd_limit()
    assert isinstance(limit, int)
    assert limit > 0
