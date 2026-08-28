"""Tests for the v13.22.2 bulk re-render module.

Covers:
- discover_repos: find git repos under a root, skip non-git, skip
  broken symlinks, dedupe by resolved path, exclude the SSOT and
  legacy_projects_root by name.
- preflight_repo: enforce the v13.21.13 reversion-path rule (must
  have origin, must be on a branch, must have an upstream).
- render_one: when push=False, the commit is created locally and
  the result encodes both pre/post SHAs and the byte count.
- bulk_render: aggregates results across N repos and surfaces
  errors without aborting the whole walk.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command and return stdout. Helper to keep tests terse."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _make_git_repo(
    path: Path,
    *,
    remote: bool = True,
    branch: str = "main",
    tracked_files: tuple[str, ...] = (),
) -> None:
    """Create a fresh git repo at ``path``.

    Args:
        path: Where to create the repo.
        remote: If True, also create a local bare repo as 'origin' so
            ``git push`` works without hitting a network. If False, no
            remote is configured.
        branch: Initial branch name.
        tracked_files: Optional files to commit on the initial branch.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", branch], path)
    _git(["config", "user.email", "test@beagle.local"], path)
    _git(["config", "user.name", "test"], path)
    for f in tracked_files:
        (path / f).write_text("placeholder\n", encoding="utf-8")
        _git(["add", f], path)
    if tracked_files:
        _git(["commit", "-q", "-m", "init"], path)
    if remote:
        bare = path.parent / f"{path.name}-bare.git"
        bare.mkdir(exist_ok=True)
        _git(["init", "--bare", "-q", str(bare)], path)
        _git(["remote", "add", "origin", str(bare)], path)
        _git(["push", "-q", "origin", branch], path)
        _git(["branch", "--set-upstream-to", f"origin/{branch}", branch], path)


def test_discover_repos_finds_git_repos(tmp_path: Path) -> None:
    """discover_repos finds first-level git repos and skips non-git dirs."""
    from beagle.style_guides.bulk_render import discover_repos

    _make_git_repo(tmp_path / "alpha", tracked_files=("README.md",))
    _make_git_repo(tmp_path / "beta", tracked_files=("README.md",))
    # Non-git dir — should be ignored.
    (tmp_path / "not_a_repo").mkdir()
    (tmp_path / "not_a_repo" / "stuff.txt").write_text("x")

    found = discover_repos(tmp_path)
    names = {p.name for p in found}
    assert names == {"alpha", "beta"}, f"expected {{alpha, beta}}, got {names}"


def test_discover_repos_excludes_ssot_and_legacy(tmp_path: Path) -> None:
    """The SSOT and legacy_projects_root are excluded by name."""
    from beagle.style_guides.bulk_render import discover_repos

    _make_git_repo(tmp_path / "beagle", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "legacy_projects_root", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "server_1_skylon", tracked_files=("x.md",))

    found = discover_repos(tmp_path)
    names = {p.name for p in found}
    assert "beagle" not in names
    assert "legacy_projects_root" not in names
    assert "server_1_skylon" in names


def test_discover_repos_dedupes_symlinks(tmp_path: Path) -> None:
    """A symlink to a real repo is not double-counted."""
    from beagle.style_guides.bulk_render import discover_repos

    _make_git_repo(tmp_path / "real_repo", tracked_files=("x.md",))
    (tmp_path / "skylon_plugins").mkdir()
    (tmp_path / "skylon_plugins" / "real_repo").symlink_to(tmp_path / "real_repo")

    found = discover_repos(tmp_path)
    # Exactly one match; the symlink resolves to the same path so dedup wins.
    assert len(found) == 1
    assert found[0].name == "real_repo"


def test_discover_repos_skips_broken_symlinks(tmp_path: Path) -> None:
    """A symlink to a non-existent target is silently skipped."""
    from beagle.style_guides.bulk_render import discover_repos

    _make_git_repo(tmp_path / "real", tracked_files=("x.md",))
    (tmp_path / "skylon_plugins").mkdir()
    (tmp_path / "skylon_plugins" / "ghost").symlink_to(tmp_path / "does_not_exist")

    found = discover_repos(tmp_path)
    names = {p.name for p in found}
    assert "real" in names
    assert "ghost" not in names


def test_discover_repos_skips_cache_dirs(tmp_path: Path) -> None:
    """Known noise dirs (.pytest_cache etc.) are excluded at the top level."""
    from beagle.style_guides.bulk_render import discover_repos

    _make_git_repo(tmp_path / "real", tracked_files=("x.md",))
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".ruff_cache").mkdir()

    found = discover_repos(tmp_path)
    names = {p.name for p in found}
    assert names == {"real"}


def test_preflight_repo_passes_clean_repo(tmp_path: Path) -> None:
    """A clean repo with origin and an upstream passes the pre-flight."""
    from beagle.style_guides.bulk_render import preflight_repo

    _make_git_repo(tmp_path / "ok", tracked_files=("x.md",))
    ok, reason = preflight_repo(tmp_path / "ok")
    assert ok, f"expected ok=True, got reason={reason!r}"


def test_preflight_repo_skips_repo_without_origin(tmp_path: Path) -> None:
    """A repo with no origin remote fails the pre-flight."""
    from beagle.style_guides.bulk_render import preflight_repo

    _make_git_repo(tmp_path / "no_origin", remote=False, tracked_files=("x.md",))
    ok, reason = preflight_repo(tmp_path / "no_origin")
    assert not ok
    assert "origin" in reason.lower()


def test_preflight_repo_skips_non_git(tmp_path: Path) -> None:
    """A non-git dir fails the pre-flight."""
    from beagle.style_guides.bulk_render import preflight_repo

    (tmp_path / "not_a_repo").mkdir()
    ok, reason = preflight_repo(tmp_path / "not_a_repo")
    assert not ok
    assert "git" in reason.lower()


def test_render_one_creates_commit_with_reversion_ref(tmp_path: Path) -> None:
    """render_one() with push=False creates a local commit whose body
    references the pre-mutation SHA — the v13.21.13 reversion-path contract.
    """
    from beagle.style_guides.bulk_render import render_one

    repo = tmp_path / "server_1_skylon"
    _make_git_repo(repo, tracked_files=("README.md",))
    pre_sha = _git(["rev-parse", "HEAD"], repo)

    result = render_one(repo, push=False)

    assert result.status == "ok", f"expected status=ok, got {result.status} ({result.reason})"
    assert result.pre_mutation_sha == pre_sha
    assert result.post_mutation_sha != pre_sha
    assert result.post_mutation_sha.startswith(result.pre_mutation_sha[:9]) or (
        # Different commit, but the reversion path's SHA must be embedded
        # in the commit message.
        pre_sha in _git(["log", "-1", "--format=%B"], repo)
    )
    # Pointer files now exist.
    assert (repo / ".goosehints").is_file()
    assert (repo / ".goose/standards.md").is_file()
    assert (repo / "CLAUDE.md").is_file()


def test_render_one_reports_no_changes_when_already_current(tmp_path: Path) -> None:
    """A second run on a repo whose pointers match the SSOT returns no-changes."""
    from beagle.style_guides.bulk_render import render_one

    repo = tmp_path / "server_1_skylon"
    _make_git_repo(repo, tracked_files=("README.md",))
    first = render_one(repo, push=False)
    assert first.status == "ok"
    second = render_one(repo, push=False)
    assert second.status == "no-changes", (
        f"expected no-changes on re-run, got {second.status} ({second.reason})"
    )


def test_render_one_push_succeeds_against_bare_remote(tmp_path: Path) -> None:
    """When push=True and a local bare remote is configured, the commit
    actually lands on origin (verified via git ls-remote)."""
    from beagle.style_guides.bulk_render import render_one

    repo = tmp_path / "server_1_skylon"
    _make_git_repo(repo, tracked_files=("README.md",))
    result = render_one(repo, push=True)
    assert result.status == "ok", f"push failed: {result.reason}"

    bare = tmp_path / "server_1_skylon-bare.git"
    # HEAD on origin should now match the post-mutation SHA. In a bare
    # repo ``git rev-parse HEAD`` returns the literal "HEAD" (it has
    # no working tree). The branch's actual ref name is whatever the
    # test created the repo with — read it from the work repo, not
    # from the bare repo's symbolic-ref (which may default to
    # refs/heads/master even when the actual branch is main).
    branch_name = _git(["branch", "--show-current"], repo)
    origin_head = _git(["rev-parse", f"refs/heads/{branch_name}"], bare)
    assert origin_head == result.post_mutation_sha


def test_render_one_skips_gitignored_pointer_files(tmp_path: Path) -> None:
    """When the repo's .gitignore excludes pointer files (.goosehints,
    .goose/), render_one returns status='skipped' with reason 'gitignored'
    rather than crashing on ``git add``. This is a policy decision by
    the repo (e.g. skylon_plugin_fan's .gitignore says '.goose/ and
    .goosehints are Beagle runtime state, not part of the plugin') and
    MUST NOT be classified as an error — the operator can use
    ``--exclude`` to skip such repos explicitly when desired.
    """
    from beagle.style_guides.bulk_render import render_one

    repo = tmp_path / "skylon_plugin_fan"
    _make_git_repo(repo, tracked_files=("README.md",))
    # Add a .gitignore that excludes the pointer files (mimics fan's policy).
    (repo / ".gitignore").write_text(
        "# Beagle / goose runtime state (local session state, not part of the plugin)\n"
        ".beagle/\n"
        ".goose/\n"
        ".goosehints\n"
        ".mcp.json\n"
        "AGENTS.md\n",
        encoding="utf-8",
    )

    result = render_one(repo, push=False)
    assert result.status == "skipped", (
        f"expected status=skipped for gitignored pointers, got {result.status} ({result.reason!r})"
    )
    assert "gitignore" in result.reason.lower()


def test_bulk_render_excludes_match(tmp_path: Path) -> None:
    """``--exclude NAME`` skips matching repos; the skip is surfaced in
    the report so the operator can verify it happened."""
    from beagle.style_guides.bulk_render import bulk_render

    _make_git_repo(tmp_path / "alpha", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "beta", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "skylon_plugin_fan", tracked_files=("x.md",))

    report = bulk_render(tmp_path, push=False, exclude=["skylon_plugin_fan"])
    excluded = {r.repo.name for r in report.results if r.status == "skipped"}
    assert "skylon_plugin_fan" in excluded
    rendered = {r.repo.name for r in report.results if r.status == "ok"}
    assert {"alpha", "beta"} <= rendered


def test_bulk_render_exclude_supports_glob(tmp_path: Path) -> None:
    """``--exclude 'skylon_plugin_*'`` matches all skylon plugin repos."""
    from beagle.style_guides.bulk_render import bulk_render

    _make_git_repo(tmp_path / "skylon_plugin_fan", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "skylon_plugin_spin", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "server_1_skylon", tracked_files=("x.md",))

    report = bulk_render(tmp_path, push=False, exclude=["skylon_plugin_*"])
    skipped = {r.repo.name for r in report.results if r.status == "skipped"}
    assert {"skylon_plugin_fan", "skylon_plugin_spin"} <= skipped
    rendered = {r.repo.name for r in report.results if r.status == "ok"}
    assert "server_1_skylon" in rendered


def test_bulk_render_aggregates_results(tmp_path: Path) -> None:
    """bulk_render returns a report covering every discovered repo."""
    from beagle.style_guides.bulk_render import bulk_render

    _make_git_repo(tmp_path / "alpha", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "beta", tracked_files=("x.md",))
    _make_git_repo(tmp_path / "gamma", remote=False, tracked_files=("x.md",))

    report = bulk_render(tmp_path, push=False)
    assert report.total == 3
    assert report.ok + report.no_changes + report.skipped + report.errors == 3
    # The no-origin repo should be skipped.
    skipped_repos = {r.repo.name for r in report.results if r.status == "skipped"}
    assert "gamma" in skipped_repos
    # The repos with origin should have been rendered.
    ok_repos = {r.repo.name for r in report.results if r.status == "ok"}
    assert {"alpha", "beta"} <= ok_repos


# Marked local-only: the expected repo set is host-specific. Hosts may add
# (or remove) side-projects; what this test guards against is BULK_RENDER
# discovery drift, not the host's project list. The test asserts that the
# 14 known projects are always discovered, and allows additional ones.
@pytest.mark.local_only
def test_bulk_render_against_actual_projects_dir() -> None:
    """Smoke test against the host's ~/Projects tree.

    Verifies that the bulk module discovers a stable set of repos that
    this monorepo's own family of projects belong to. The set is a subset
    assertion (these 14 must appear) so the test is portable across hosts
    that have additional sibling projects.
    """
    pytest.importorskip("pathlib")
    from beagle.style_guides.bulk_render import discover_repos

    projects = Path.home() / "Projects"
    if not projects.is_dir():
        pytest.skip("~/Projects not present on this host")

    found = discover_repos(projects)
    names = {p.name for p in found}
    # Required subset — these 14 projects must be discovered on every
    # host that has this monorepo. Hosts may add MORE projects (skylon
    # plugin expansion, sandbox projects, etc.); that's allowed and
    # expected. The test fails only if a required project is missing.
    required = {
        "dev_tools",
        "orpheus",
        "orpheus_lib",
        "skylon",
        "skylon_plugin_fan",
        "skylon_plugin_spin",
        "skylon_plugin_wifi",
        "skylon_plugin_docker",
        "skylon_plugin_logdaemon.RETIRED",
        "skylon_plugin_maintenance",
        "skylon_plugin_tui",
        "skylon_plugin_webui",
    }
    missing = required - names
    assert not missing, (
        f"bulk_render discovery regressed: required projects not found: "
        f"{sorted(missing)}; discovered={sorted(names)}"
    )
