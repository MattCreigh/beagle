"""Tests for beagle.permission_context — D-15 (release-readiness audit 2026-08-28).

``READ_ONLY_PERMISSION_CONTEXT`` was denylist-based (fail-open): any tool not
on the deny list passed through. Rebuilt on ``allow_names`` so it FAILS
CLOSED — only an explicit allowlist of read-only tools passes, and any future
mutating tool added to the surface is blocked by construction.
"""

from __future__ import annotations

from beagle.permission_context import READ_ONLY_PERMISSION_CONTEXT, ToolPermissionContext


def test_read_only_blocks_all_mutating_tools() -> None:
    """write/patch/git/npm/pip/docker-style tools must all be blocked."""
    mutating = [
        "write_file",
        "replace",
        "patch",
        "rm",
        "mkdir",
        "chmod",
        "chown",
        "git_push",
        "git_commit",
        "npm_install",
        "pip_install",
        "docker_run",
        "run_command",
        "execute",
        "deploy",
        "create_task",
        "cancel_task",
    ]
    for tool in mutating:
        assert READ_ONLY_PERMISSION_CONTEXT.blocks(tool), f"{tool} must be blocked (fail-open!)"


def test_read_only_allows_known_read_tools() -> None:
    """read/list/query-style tools must pass."""
    reads = ["read", "list", "query", "search", "get", "status", "validate", "log"]
    for tool in reads:
        assert not READ_ONLY_PERMISSION_CONTEXT.blocks(tool), f"{tool} should be allowed"


def test_unknown_tool_fails_closed() -> None:
    """A tool not in the allowlist (even a benign-sounding one) is blocked —
    this is the D-15 fail-closed guarantee."""
    assert READ_ONLY_PERMISSION_CONTEXT.blocks("list_workflows")
    assert READ_ONLY_PERMISSION_CONTEXT.blocks("get_metrics")
    assert READ_ONLY_PERMISSION_CONTEXT.blocks("beagle_system_status")


def test_allow_names_is_explicit_not_deny_based() -> None:
    """The context must carry an allow_names allowlist (fail-closed)."""
    assert READ_ONLY_PERMISSION_CONTEXT.allow_names is not None


def test_from_iterables_allow_names_fails_closed() -> None:
    """ToolPermissionContext.from_iterables(allow_names=...) blocks unknown tools."""
    ctx = ToolPermissionContext.from_iterables(allow_names={"read", "list"})
    assert ctx.blocks("write_file")
    assert ctx.blocks("list_workflows")
    assert not ctx.blocks("read")
