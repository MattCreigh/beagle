# Front-End Contract — Top-of-Mind directive (BEAGLE MIND RELAY)

Any front end (goose, pi, Claude Code, OpenClaw-over-MCP, future) can carry the
Beagle doctrine with exactly three integration points. Everything else is
Beagle-internal and front-end-agnostic.

## SSOT
Style guides are TOMLs. Global: `<config_root>/style_guides/guides/`
(`~/.config/beagle/...`, override with `$BEAGLE_CONFIG_ROOT`).
Project: `<repo>/<DIRNAME_UPPER>_STYLE_GUIDE.toml` (discovered walking up from
the scope dir; nearest overrides central — `beagle.style_guides.loader.discover_local`).
Rendered artefacts are never hand-edited; re-render from TOML.

## The one MCP surface (Beagle unified server, stdio)
- Tool `beagle_context_directive(scope_dir, phase, file_path)`
  - `phase="session_start"` → tiered per-turn doctrine (load-bearing tier),
    domain resolved from `scope_dir` (cwd_globs in `domain_map.toml`) plus any
    project-local guides.
  - `phase="fold"` / `"compact"` → compact slice (≤ 2048 B, COMPACT_MAX_BYTES).
  - `phase="file"` + `file_path` → extension-matched + local guides for that
    file (byte-identical to `ContextInjector.inject_for_file`).
  - Returns JSON: `{status, phase, scope_dir, domain, directive, layers,
    bytes, cap_ok}`. Unknown inputs → `status="error"`, never raises.
- Resource `beagle://top-of-mind` — same as session_start for the server cwd.

## The three integration points
1. **Session start** — call the tool with `scope_dir=<init dir>`,
   `phase="session_start"`; inject `["directive"]` into the system/top context.
2. **Context fold / compaction** — call with `phase="fold"`, inject the slice
   into the rehydration block. (Beagle's own fold path already appends the
   same slice to `~/.beagle/post_compaction_rehydration.txt` automatically.)
3. **File edit** (optional) — call with `phase="file"` + `file_path` before
   editing, for the file-scoped rules.

## goose specifics (already wired, no action needed by other fronts)
- Per-turn: tom extension reads `~/.config/goose/beagle_top_of_mind.xml`
  (`GOOSE_MOIM_MESSAGE_FILE`); freshness is driven every PostToolUse by
  `scripts/hooks/auto_compact.py` calling the mtime-guarded `render_canonical()`.
- Session start: `beagle-bootstrap` hook force-renders (SessionStart event).
- Fold/stop: `auto_compact.py` (PostToolUse) + `post_final_fold.py` (Stop).

## First-time setup of a repo
`beagle init --target <repo>` → writes `.goosehints`, `CLAUDE.md`,
`.goose/standards.md` (+ canonical home artefacts). Idempotent (byte-identical
on re-run).
