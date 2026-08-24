"""Bulk re-render of v13.22.1 XML pointer files across multiple git repos.

Provides a single Python entry point that
``beagle.cli.cli:render_prompts_all`` (the
``beagle render-prompts-all`` CLI subcommand) calls to re-render the
per-repo pointer files (.goosehints, .goose/standards.md, CLAUDE.md)
for every git-managed project under a given root.

The bulk operation enforces the v13.21.13 reversion-path rule
(reversion_safe_delete) for every commit it creates:

  1. The pre-mutation state MUST be in a pushed commit on the remote
     (we push any unpushed commits first as pre-flight).
  2. The bulk-render commit MUST reference the pre-mutation SHA in
     its body.
  3. The pre-mutation state MUST be on a branch (no detached HEADs).

Repos that fail the pre-flight are SKIPPED with an explicit reason
(no destructive default behaviour) and surfaced in the result.

No side effects beyond what the caller passes in: this module does
NOT call ``git push`` itself; that is the responsibility of the
caller (the CLI subcommand or a test). This keeps the unit-testable
surface small and the git surface explicit at the call site.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from beagle.config.paths import resolve_executable

logger = logging.getLogger(__name__)

# v1.0.2: the doctrine SSOT lives in this package (style_guides/guides/*.toml),
# so its location is derivable from this module rather than hardcoded to one
# machine's checkout path. bulk_render walks and commits to OTHER repos, so a
# hardcoded path leaked one developer's layout into every commit message it
# wrote. __file__ is <pkg>/style_guides/bulk_render.py -> parents[1] is <pkg>.
_SSOT_ROOT = Path(__file__).resolve().parents[1]


# Default depth limit for the repo walk. Five levels is enough for the
# the configured workspace tree. Configurable
# per-call so a deeper layout is also supported.
DEFAULT_MAX_DEPTH = 5


@dataclass
class RepoResult:
    """Per-repo outcome from a bulk render.

    Attributes:
        repo: Absolute path to the repo root.
        status: One of "ok", "skipped", "no-changes", "error".
        reason: Human-readable explanation; required for "skipped" / "error".
        pre_mutation_sha: The SHA the post-render commit references in
            its reversion-path. Empty when no commit was created.
        post_mutation_sha: The new SHA, populated when a commit was
            created.
        bytes_written: Total bytes of pointer files written
            (sum of .goosehints + .goose/standards.md + CLAUDE.md).

    """

    repo: Path
    status: str
    reason: str = ""
    pre_mutation_sha: str = ""
    post_mutation_sha: str = ""
    bytes_written: int = 0

    @property
    def short_pre(self) -> str:
        return self.pre_mutation_sha[:9] if self.pre_mutation_sha else ""

    @property
    def short_post(self) -> str:
        return self.post_mutation_sha[:9] if self.post_mutation_sha else ""


@dataclass
class BulkRenderReport:
    """Aggregate report from a bulk render across N repos.

    Attributes:
        root: Root directory walked.
        results: Per-repo results, in walk order.
        total: Convenience: total number of repos considered.

    """

    root: Path
    results: list[RepoResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def no_changes(self) -> int:
        return sum(1 for r in self.results if r.status == "no-changes")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.status == "error")


def _is_git_repo(path: Path) -> bool:
    """True iff ``path`` is a git working tree (``.git`` dir or file)."""
    git = path / ".git"
    return git.is_dir() or git.is_file()


def _has_origin_remote(path: Path) -> bool:
    """True iff the repo at ``path`` has an ``origin`` remote configured."""
    try:
        result = subprocess.run(
            [resolve_executable("git"), "-C", str(path), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return bool(result.stdout.strip())


def _current_branch(path: Path) -> str:
    """Return the current branch name, or empty string on detached HEAD."""
    try:
        result = subprocess.run(
            [resolve_executable("git"), "-C", str(path), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


def _current_sha(path: Path) -> str:
    try:
        result = subprocess.run(
            [resolve_executable("git"), "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


def _upstream_ahead_count(path: Path) -> int:
    """Return the number of commits local is ahead of upstream.

    Returns 0 if the branch has no upstream or on any error (the caller
    surfaces a 'no upstream' / 'detached' skip reason separately).
    """
    try:
        result = subprocess.run(
            [
                resolve_executable("git"),
                "-C",
                str(path),
                "rev-list",
                "--count",
                "@{u}..HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def discover_repos(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Path]:
    """Find every git working tree under ``root`` up to ``max_depth`` levels.

    Walks ``root`` recursively. A directory containing ``.git`` is
    a hit; the walker does NOT descend into it (avoids scanning the
    entire object database). Symlinks to directories are followed
    ONLY if the target exists (broken symlinks are skipped).
    Hidden directories (those starting with ``.``) at the top level
    are scanned normally; once inside a git repo, descent is
    stopped. Caches and other noise (.pytest_cache, .ruff_cache,
    .installer_cache) are excluded at the top level.

    The SSOT (``beagle``) is NEVER included — that is
    the source of truth; the bulk-render CLI re-emits the SSOT via
    ``beagle render-prompts`` (no --target), so bulk-rendering the SSOT
    from the SSOT would be a no-op self-loop.

    Symlinked repos are deduplicated by their resolved real path so a
    symlinked mirror (e.g. ``skylon_plugins/skylon_plugin_fan ->
    ../skylon_plugin_fan``) is not double-counted.
    """
    root = root.resolve()
    if not root.is_dir():
        return []
    # Top-level exclusions: known cache / hidden noise dirs.
    skip_top = {
        ".beagle",
        ".installer_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".git",  # never descend into the root .git if any
    }
    # Repos whose name matches any of these are excluded from the
    # bulk-render. Use the resolved basename of the SSOT package so the
    # discovery works on a host that may have installed the package
    # elsewhere; here we use the well-known project name.
    skip_by_name = {
        "beagle",  # the SSOT
        "legacy_projects_root",  # stale symlink to a ghost copy of the SSOT
    }
    results: list[Path] = []
    seen_resolved: set[str] = set()

    def _walk(p: Path, depth: int) -> None:
        if depth > max_depth:
            return
        # Resolve symlinks for dedup; broken symlinks skipped.
        try:
            resolved = p.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            return
        # On macOS / Linux, resolve() returns the canonical path; if a
        # symlink chain is too long, RuntimeError is raised. We skip.
        if not resolved.is_dir():
            return
        if _is_git_repo(p):
            key = str(resolved)
            if key in seen_resolved:
                return  # dedup
            seen_resolved.add(key)
            if p.name in skip_by_name:
                return  # exclude by name (SSOT)
            results.append(p)
            return  # do not descend into a git repo
        if depth == 0:
            try:
                children = sorted(p.iterdir(), key=lambda x: x.name)
            except (PermissionError, OSError):
                return
            for child in children:
                if child.name in skip_top:
                    continue
                if child.is_dir() or child.is_symlink():
                    _walk(child, depth + 1)
        else:
            try:
                children = sorted(p.iterdir(), key=lambda x: x.name)
            except (PermissionError, OSError):
                return
            for child in children:
                # Skip build / cache dirs at all levels.
                if child.name in {
                    "node_modules",
                    "target",
                    "build",
                    "dist",
                    "__pycache__",
                    ".git",
                }:
                    continue
                if child.is_dir() or child.is_symlink():
                    _walk(child, depth + 1)

    _walk(root, 0)
    return results


def preflight_repo(path: Path) -> tuple[bool, str]:
    """Validate that ``path`` is safe to bulk-render.

    Returns (ok, reason). On (False, reason) the caller MUST skip the
    repo without making any changes. The pre-flight enforces the
    v13.21.13 reversion-path rule:

      1. Must be a git working tree.
      2. Must have an ``origin`` remote (so the post-mutation state can
         be pushed and the reversion path lives on a remote).
      3. Must be on a branch (no detached HEAD; ``git revert`` requires
         a branch ref).
      4. Branch must have an upstream tracking ref.

    Note: a branch with N unpushed commits is still OK — the CLI
    subcommand will push them as a pre-render pre-flight. The unit
    test of this module does NOT push; that is the caller's job.
    """
    if not _is_git_repo(path):
        return False, "not a git working tree"
    if not _has_origin_remote(path):
        return False, "no 'origin' remote (reversion path not satisfiable)"
    branch = _current_branch(path)
    if not branch:
        return False, "HEAD is detached (reversion path via git revert unavailable)"
    # Check upstream exists.
    try:
        subprocess.run(
            [resolve_executable("git"), "-C", str(path), "rev-parse", "--abbrev-ref", "@{u}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return (
            False,
            f"branch {branch!r} has no upstream (cannot satisfy reversion path)",
        )
    return True, ""


def render_one(
    repo: Path,
    *,
    push: bool = False,
) -> RepoResult:
    """Bulk-render the v13.22.1 pointer files for a single repo.

    Returns a RepoResult describing what happened. Does NOT raise on
    per-repo errors — they are encoded in the result so a bulk
    operation can collect everything in one pass.

    When ``push`` is True, any unpushed commits on the current branch
    are pushed first (reversion-path pre-flight), then the pointer
    files are rendered, then the new commit is pushed. When ``push``
    is False (default), the caller is responsible for pushing — this
    keeps the unit-testable surface side-effect-light.
    """
    repo = repo.resolve()
    ok, reason = preflight_repo(repo)
    if not ok:
        return RepoResult(repo=repo, status="skipped", reason=reason)

    pre_sha = _current_sha(repo)
    branch = _current_branch(repo)
    ahead = _upstream_ahead_count(repo)

    if push and ahead > 0:
        try:
            subprocess.run(
                [resolve_executable("git"), "-C", str(repo), "push", "origin", branch],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return RepoResult(
                repo=repo,
                status="error",
                reason=f"failed to push {ahead} unpushed commit(s) first: {exc}",
                pre_mutation_sha=pre_sha,
            )
        # After push, re-resolve SHA so the reversion reference is the
        # post-push HEAD (which is what origin now points at).
        pre_sha = _current_sha(repo)

    # Run the renderer. We import lazily so this module is importable
    # without a fully bootstrapped style_guides/ dependency (the unit
    # tests monkey-patch this symbol).
    try:
        from beagle.style_guides.render import (
            GooseTopOfMindRenderer,
        )
    except ImportError as exc:
        return RepoResult(
            repo=repo,
            status="error",
            reason=f"GooseTopOfMindRenderer not importable: {exc}",
            pre_mutation_sha=pre_sha,
        )

    try:
        renderer = GooseTopOfMindRenderer(target_root=repo)
        _ = renderer.render_all()
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return RepoResult(
            repo=repo,
            status="error",
            reason=f"render failed: {exc}",
            pre_mutation_sha=pre_sha,
        )

    # Determine what changed.
    new_files: list[Path] = []
    modified_files: list[Path] = []
    bytes_written = 0
    for f in (".goosehints", "CLAUDE.md", ".goose/standards.md"):
        fp = repo / f
        if not fp.is_file():
            continue
        with contextlib.suppress(OSError):
            bytes_written += fp.stat().st_size
        try:
            tracked = (
                subprocess.run(
                    [resolve_executable("git"), "-C", str(repo), "ls-files", "--error-unmatch", f],
                    capture_output=True,
                    timeout=10,
                ).returncode
                == 0
            )
        except (subprocess.TimeoutExpired, OSError):
            tracked = False
        if tracked:
            try:
                # tracked and modified
                diff_proc = subprocess.run(
                    [
                        resolve_executable("git"),
                        "-C",
                        str(repo),
                        "diff",
                        "--quiet",
                        "HEAD",
                        "--",
                        f,
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if diff_proc.returncode != 0:
                    modified_files.append(fp)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning(
                    "Cannot diff %s against HEAD (%s); treating it as unmodified, so a "
                    "real change there will not be committed.",
                    f,
                    exc,
                )
        else:
            new_files.append(fp)

    if not modified_files and not new_files:
        return RepoResult(
            repo=repo,
            status="no-changes",
            pre_mutation_sha=pre_sha,
            bytes_written=bytes_written,
        )

    # Build the commit message.
    file_list = ", ".join(str(f.relative_to(repo)) for f in (modified_files + new_files))
    has_hand_maintained = any(f.name == "CLAUDE.md" for f in (modified_files + new_files))
    hand_maintained_block = ""
    if has_hand_maintained:
        hand_maintained_block = (
            "\nNote: CLAUDE.md was a hand-maintained directory context "
            "before this commit. Replaced with the v13.22.1 thin XML "
            "pointer. The pre-mutation state is recoverable via "
            "'git revert HEAD' (pushed to origin). To restore the old "
            "content from a non-Beagle source, re-derive it from the "
            "project's documentation; the v13.22.1 pointer references "
            "the canonical TOML SSOT for all doctrine.\n"
        )
    commit_msg = (
        f"chore(prompt-substrate): re-render v13.22.1 XML pointers via "
        f"beagle render-prompts-all\n\n"
        f"Re-rendered against the v13.22.1 TOML SSOT at:\n"
        # v1.0.2: resolved from this module's own location, not the hardcoded
        # this repo. render_one() bulk-renders OTHER repos,
        # so every commit it wrote claimed an SSOT path that is only correct on
        # one machine and silently wrong on any other checkout.
        f"  {_SSOT_ROOT}\n\n"
        f"Changed: {file_list}\n\n"
        f"This brings the repo's pointer files in line with the SSOT so "
        f"goose's session-start (.goosehints) and code-edit "
        f"(style_guides/injector.py) hooks see the same thin-XML-pointer "
        f"contract used by the rest of the fleet. No source code changes; "
        f"pointer files only.\n\n"
        f"Reversion path: prior commit {pre_sha} (pushed to origin/"
        f"{branch}) captures the pre-mutation state. To revert this "
        f"commit:\n\n"
        f"    git revert HEAD\n"
        f"    git push origin {branch}\n"
        f"{hand_maintained_block}"
    )

    # Stage and commit. We classify failures so the caller gets a
    # useful status:
    #   - .gitignore excludes the pointer files -> status="skipped" with
    #     reason "gitignored". This is a policy decision by the repo
    #     (e.g. skylon_plugin_fan's .gitignore says ".goose/ and
    #     .goosehints are Beagle runtime state, not part of the plugin")
    #     and is NOT an error.
    #   - any other failure -> status="error" with the raw stderr.
    try:
        add_proc = subprocess.run(
            [
                resolve_executable("git"),
                "-C",
                str(repo),
                "add",
                ".goosehints",
                "CLAUDE.md",
                ".goose/standards.md",
            ],
            check=False,  # we want to inspect stderr to classify the error
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return RepoResult(
            repo=repo,
            status="error",
            reason=f"git add timed out / OS error: {exc}",
            pre_mutation_sha=pre_sha,
        )
    if add_proc.returncode != 0:
        add_stderr = (add_proc.stderr or "").strip()
        if "ignored" in add_stderr.lower() or "gitignore" in add_stderr.lower():
            return RepoResult(
                repo=repo,
                status="skipped",
                reason=(
                    "gitignored (.gitignore excludes pointer files; "
                    "this is the repo's policy, not a failure)"
                ),
                pre_mutation_sha=pre_sha,
            )
        return RepoResult(
            repo=repo,
            status="error",
            reason=f"git add failed: {add_stderr or 'exit ' + str(add_proc.returncode)}",
            pre_mutation_sha=pre_sha,
        )
    try:
        subprocess.run(
            [
                resolve_executable("git"),
                "-C",
                str(repo),
                "-c",
                "user.email=goose-agent@beagle.local",
                "-c",
                "user.name=goose (Beagle)",
                "commit",
                "-m",
                commit_msg,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return RepoResult(
            repo=repo,
            status="error",
            reason=f"commit failed: {exc}",
            pre_mutation_sha=pre_sha,
        )

    post_sha = _current_sha(repo)
    result = RepoResult(
        repo=repo,
        status="ok",
        pre_mutation_sha=pre_sha,
        post_mutation_sha=post_sha,
        bytes_written=bytes_written,
    )

    if push:
        try:
            subprocess.run(
                [resolve_executable("git"), "-C", str(repo), "push", "origin", branch],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return RepoResult(
                repo=repo,
                status="error",
                reason=f"commit {post_sha[:9]} created locally but push failed: {exc}",
                pre_mutation_sha=pre_sha,
                post_mutation_sha=post_sha,
            )

    return result


def bulk_render(
    root: Path,
    *,
    push: bool = True,
    max_depth: int = DEFAULT_MAX_DEPTH,
    exclude: list[str] | None = None,
) -> BulkRenderReport:
    """Discover repos under ``root`` and re-render each.

    Returns a BulkRenderReport. The report is built in walk order; the
    caller can serialise it (see cli.py) and decide whether to halt
    on the first error. By default, errors in one repo do NOT abort
    the bulk operation — they are surfaced in the result.

    Args:
        root: Root directory to walk.
        push: If True, push any unpushed commits first and push the
            new render commit. If False, leave commits local-only.
        max_depth: Directory depth limit for the walker.
        exclude: Optional list of repo name patterns to skip. Matched
            against the basename of each discovered repo using
            ``fnmatch.fnmatch``. Excluded repos appear in the report
            with status="skipped" and reason="excluded by --exclude",
            so the operator can verify the skip happened.

    """
    import fnmatch

    root = root.resolve()
    repos = discover_repos(root, max_depth=max_depth)
    report = BulkRenderReport(root=root)
    for r in repos:
        if exclude and any(fnmatch.fnmatch(r.name, pat) for pat in exclude):
            report.results.append(
                RepoResult(
                    repo=r,
                    status="skipped",
                    reason=f"excluded by --exclude (matched one of: {exclude})",
                )
            )
            continue
        report.results.append(render_one(r, push=push))
    return report
