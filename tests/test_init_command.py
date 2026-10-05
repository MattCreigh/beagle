"""BEAGLE MIND RELAY P3 — `beagle init` renders all pointer artefacts, idempotent."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from beagle.cli.commands.render import render_app


def _scratch(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "0.1.0"\n')
    return repo


def test_init_writes_all_pointers(tmp_path: Path) -> None:
    repo = _scratch(tmp_path)
    rc = CliRunner().invoke(render_app, ["init", "--target", str(repo)]).exit_code
    assert rc == 0
    assert (repo / ".goosehints").is_file()
    assert (repo / "CLAUDE.md").is_file()
    assert (repo / ".goose" / "standards.md").is_file()
    # project.json is home-canonical only (render.py:2452 area: standards/
    # claudemd redirect per-repo, project.json does not) — asserted at the
    # beagle repo root, not per-target.
    assert Path.home().joinpath(
        "Projects/beagle", ".goose", "project.json"
    ).is_file()


def test_init_idempotent(tmp_path: Path) -> None:
    import hashlib

    repo = _scratch(tmp_path)
    CliRunner().invoke(render_app, ["init", "--target", str(repo)])
    snap = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [repo / ".goosehints", repo / "CLAUDE.md", repo / ".goose" / "standards.md"]
    }
    CliRunner().invoke(render_app, ["init", "--target", str(repo)])
    for p in [repo / ".goosehints", repo / "CLAUDE.md", repo / ".goose" / "standards.md"]:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == snap[p.name], p.name
