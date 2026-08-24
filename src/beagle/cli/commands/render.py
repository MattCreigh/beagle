"""Prompt-substrate rendering commands (render-prompts/-all, render-hints).

Extracted from cli.py in the v1.0.0 F2 split. Registered flat on the root
app via ``app.add_typer(render_app)`` (no name), so the CLI surface is
byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console(stderr=True)

render_app = typer.Typer()

# <invariant>
# Typer declares a parameter's CLI metadata by evaluating typer.Option() in the
# argument default. Evaluating a call in a default is the B008 pattern, so each
# non-scalar option is built once here and referenced by name below. Typer reads
# an OptionInfo as an immutable descriptor and never mutates it, so one module
# level instance per parameter is safe to share across invocations.
# </invariant>

_TARGET_OPTION = typer.Option(
    None,
    "--target",
    "-t",
    exists=True,
    file_okay=False,
    dir_okay=True,
    readable=True,
    resolve_path=True,
    help="Per-repo render target. When set, .goosehints, "
    ".goose/standards.md, and CLAUDE.md are written under this directory "
    "instead of the beagle package root. The "
    "home-canonical artefacts (Top-of-Mind, system instruction, "
    "compaction prompt, doctrine report, project.json) are unaffected — "
    "they always go to ~/.config/goose/ and the beagle "
    "package root because the goose runtime reads them from those "
    "canonical paths. Use this when a sibling repo (e.g. skylon, "
    "server_1) needs the v13.22.1 XML-pointer style on its own "
    "pointer files.",
)

_ROOT_OPTION = typer.Option(
    None,
    "--root",
    "-r",
    exists=True,
    file_okay=False,
    dir_okay=True,
    readable=True,
    resolve_path=True,
    help="Root directory to walk for git repos. Defaults to the current "
    "working directory. The walker scans up to --max-depth levels deep.",
)

_EXCLUDE_OPTION = typer.Option(
    [],
    "--exclude",
    "-x",
    help="Repo name (or glob) to skip. Repeatable. Matched against the "
    "basename of each discovered repo. Use this to bulk-render a "
    "fleet of repos while skipping the ones not yet valid on the "
    "current machine (e.g. ``--exclude skylon_plugin_fan``).",
)


@render_app.command("render-prompts")
def render_prompts(
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Also render the compact Top-of-Mind variant for mid-task "
        "injection when context pressure is high (>45%).",
    ),
    target: Path | None = _TARGET_OPTION,
) -> None:
    """Render ALL Beagle prompt-substrate files from the style-guide TOML SSOT.

    Writes the following artefacts:

    - ~/.config/goose/beagle_top_of_mind.xml        (Top-of-Mind, per-turn XML)
    - .goosehints                                  (session-start XML pointer)
    - ~/.config/goose/beagle_system_instruction.xml (system-prompt instruction)
    - ~/.config/goose/prompts/compaction.xml       (post-compaction template)
    - docs/DOCTRINE.md                             (human-readable doctrine report)
    - .goose/project.json                          (project metadata)
    - .goose/standards.md                          (thin XML pointer to the SSOT)
    - CLAUDE.md, src/CLAUDE.md  (thin XML pointers to the SSOT)

    The doctrine SSOT is the TOML; rendered prompt-substrate is XML/YAML.
    CLAUDE.md and standards.md are thin pointers (not doctrine copies) — they
    keep a .md name only for their consumers (Claude Code, recipes,
    rehydration). docs/DOCTRINE.md is the one human-readable .md report. Run
    this after editing any style guide TOML in
    src/style_guides/guides/.

    With ``--target <dir>``, the per-repo pointers (.goosehints,
    .goose/standards.md, CLAUDE.md) are written under ``<dir>`` instead of
    the beagle root. The canonical home artefacts are
    always emitted regardless. This lets a sibling repo (skylon, server_1,
    orpheus) carry the same pointer style without disturbing the
    beagle-canonical artefacts.
    """
    from ...style_guides.render import (
        GooseTopOfMindRenderer,
        render_compact,
    )

    renderer = GooseTopOfMindRenderer(target_root=target)
    results = renderer.render_all()

    if compact:
        # Also write a compact variant for mid-task injection.
        compact_xml = render_compact()
        results["hints_compact"] = Path(compact_xml) if compact_xml else Path()

    table = Table(title="Beagle prompt-substrate render results")
    table.add_column("Artefact", style="bold")
    table.add_column("Path", style="cyan")
    table.add_column("Bytes", justify="right")

    # render_all() uses a bare `Path()` as the "not rendered" sentinel.
    # `Path()` is PosixPath('.') and Path defines no __bool__, so it is
    # ALWAYS truthy — a `if not path` guard silently never fires. That made
    # every skipped artefact show up as a real row pointing at "." with the
    # containing directory's size (4096 bytes), and inflated the summary
    # count. Compare against the sentinel explicitly.
    rendered = {name: p for name, p in results.items() if p != Path()}

    for name, path in rendered.items():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        table.add_row(name, str(path), str(size))

    console.print(table)
    skipped = len(results) - len(rendered)
    skipped_note = f" ({skipped} skipped)" if skipped else ""
    console.print(
        f"\n[green]Rendered {len(rendered)} artefact(s){skipped_note} from the "
        f"TOML SSOT (XML/YAML substrate; CLAUDE.md + standards.md are XML "
        f"pointers).[/green]"
    )


@render_app.command("render-prompts-all")
def render_prompts_all(
    root: Path | None = _ROOT_OPTION,
    max_depth: int = typer.Option(
        5,
        "--max-depth",
        "-d",
        min=1,
        max=20,
        help="Maximum directory depth to walk. 5 (default) is enough for the "
        "shallow default suits large workspace trees; raise it for a deeper one.",
    ),
    no_push: bool = typer.Option(
        False,
        "--no-push",
        help="Create the per-repo commits locally but do NOT push them to "
        "origin. The reversion-path rule (v13.21.13) requires the "
        "post-mutation state to be on a remote; use --no-push only when "
        "you intend to push manually later.",
    ),
    # Removed 2026-07-28: a `--skip-existing/--no-skip-existing` option was
    # declared here but never read — it was not forwarded to bulk_render(),
    # which has no such parameter. Its own help text described the same
    # outcome ("no commit when unchanged") for BOTH branches, so the flag
    # advertised a distinction that did not exist and could not be honoured.
    # Skipping byte-identical repos is unconditional in bulk_render and is
    # reported as the "no-changes" status. Re-add only alongside a real
    # parameter on bulk_render.
    exclude: list[str] = _EXCLUDE_OPTION,
) -> None:
    """Bulk-render v13.22.1 XML pointer files across multiple git repos.

    Discovers every git working tree under ``--root`` (default: cwd) up to
    ``--max-depth`` levels deep, and re-runs the per-repo pointer write
    for each. For every repo a single commit is created (and pushed by
    default) referencing the pre-mutation SHA in the body, satisfying
    the v13.21.13 reversion-path rule.

    Repos that fail the pre-flight (no ``origin`` remote, detached HEAD,
    branch with no upstream) are SKIPPED with a reason — never changed.
    The SSOT (``beagle``) is excluded by name since it
    is the source of truth.

    Exit code: 0 if every rendered repo succeeded OR had no changes;
    1 if any repo had a hard error (commit failed, push failed).
    """
    from ...style_guides.bulk_render import bulk_render

    target_root = root or Path.cwd()
    report = bulk_render(
        target_root,
        push=not no_push,
        max_depth=max_depth,
        exclude=exclude,
    )

    table = Table(title=f"beagle render-prompts-all: {target_root}")
    table.add_column("Repo", style="bold")
    table.add_column("Status", style="cyan")
    table.add_column("Pre", style="dim")
    table.add_column("→", style="dim")
    table.add_column("Post", style="dim")
    table.add_column("Bytes", justify="right")
    table.add_column("Reason")

    for r in report.results:
        status_style = {
            "ok": "[green]ok[/green]",
            "no-changes": "[blue]no-ch.[/blue]",
            "skipped": "[yellow]skip[/yellow]",
            "error": "[red]ERROR[/red]",
        }.get(r.status, r.status)
        repo_short = str(r.repo).replace(str(target_root), ".") or str(r.repo)
        table.add_row(
            repo_short,
            status_style,
            r.short_pre,
            "→",
            r.short_post,
            str(r.bytes_written) if r.bytes_written else "",
            r.reason or "",
        )

    console.print(table)
    console.print(
        f"\n[green]Discovered {report.total} repo(s); "
        f"ok={report.ok} no-changes={report.no_changes} "
        f"skipped={report.skipped} errors={report.errors}[/green]"
    )

    if report.errors > 0:
        raise typer.Exit(code=1)


_TARGET_NAME_OPTION = typer.Option(
    "top_of_mind_xml",
    "--target",
    help="Emission target for the rendered directive. One of: "
    "goosehints, claude_md, top_of_mind_xml, mcp_resource. "
    "`top_of_mind_xml` writes the canonical ~/.config/goose/"
    "beagle_top_of_mind.xml (the per-turn default); the others emit "
    "through the axis-1 render-target interface.",
)
_DIR_OPTION = typer.Option(
    None,
    "--dir",
    "-d",
    help="Directory to scope the directive to. File targets write under "
    "this directory; the mcp_resource target records it in its payload. "
    "Defaults to the current working directory.",
)


@render_app.command("render-hints")
def render_hints(
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress success output. Use this when the per-turn Goose "
        "'tom extension' calls render-hints every turn to refresh the "
        "Top-of-Mind (GOOSE_MOIM_MESSAGE_FILE) — keeps the turn quiet.",
    ),
    target: str = _TARGET_NAME_OPTION,
    directory: Path | None = _DIR_OPTION,
) -> None:
    """Refresh the Top-of-Mind artefact only (~/.config/goose/beagle_top_of_mind.xml).

    This is the **per-turn live-propagation entry point**. The external
    Goose "tom extension" (which injects ``GOOSE_MOIM_MESSAGE_FILE`` every
    turn) should call ``beagle render-hints --quiet`` immediately before
    reading the file, so a mid-session edit to any style-guide TOML in
    ``src/style_guides/guides/`` reaches the running
    model on the very next turn. The call is cheap: ``render_canonical`` is
    mtime-guarded and no-ops when the source TOMLs are unchanged (the
    contract is pinned by ``tests/test_render_canonical_staleness.py``).

    Failure-tolerant by design: a transient render error must never break
    the per-turn loop, so this logs, reports, and exits 0, leaving the
    previous (still-valid) Top-of-Mind in place. For the full set of
    prompt-substrate files use ``beagle render-prompts``.
    """
    from ...style_guides.render import GooseTopOfMindRenderer, render_canonical

    # C1: route through the axis-1 render-target interface. The canonical
    # per-turn default (top_of_mind_xml) keeps the mtime-guarded
    # render_canonical() path; any other target emits through the interface.
    status: str | None = None
    try:
        if target == "top_of_mind_xml" and directory is None:
            path = render_canonical()
        else:
            renderer = GooseTopOfMindRenderer()
            status = renderer.emit(
                target,
                scope=directory or Path.cwd(),
                target_dir=directory,
            )
            path = None
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional — a per-turn render must never break the loop; log + keep the existing artefact
        logger.warning("render-hints: Top-of-Mind refresh failed: %s", exc)
        if not quiet:
            console.print(
                f"[yellow]Top-of-Mind refresh skipped (transient error: "
                f"{exc}); existing artefact left in place.[/yellow]"
            )
        return

    if not quiet:
        if status is not None:
            console.print(f"[green]{status}[/green]")
        elif path is not None:
            size = path.stat().st_size
            console.print(f"[green]Top-of-Mind rendered → {path} ({size} bytes)[/green]")
