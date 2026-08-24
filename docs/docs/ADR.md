# Beagle Architecture Decision Records (ADRs)

This document captures the **architecturally significant** design
decisions in Beagle — the choices that, if changed, would ripple
across multiple subsystems.

ADRs are immutable once accepted. To change a decision, add a new
ADR that supersedes the old one (don't edit history).

---

## ADR-001: Single source of truth for package version

**Status:** Accepted
**Date:** v13.21.5
**Deciders:** Core team

### Context

Beagle's package version used to appear in dozens of files: the
`__init__.py`, `pyproject.toml`, `config.toml` header, CLI help
text, and ad-hoc comments like `# v13.7` scattered throughout the
codebase. They drifted apart (v13.7 in CLI, v13.19.5 in `__init__`,
v0.3.0 in some refactor comments). Every release had a "find all
the version strings" chore.

### Decision

The package version lives in exactly **one** place:
`beagle/__init__.py::__version__`. All other
locations import from there.

The top-level `beagle/constants.py` re-exports it
as `PACKAGE_VERSION` for callers that prefer the explicit name.
`pyproject.toml`'s `version` field is verified to match by the
`beagle-version-check.yml` CI workflow.

A pre-commit hook (`scripts/hooks/no_hardcoded_version.py`)
rejects any new short-form version marker (`# v1`, `# v2.5`)
introduced in non-exempt paths. Audit-trail comments of the form
`# v13.21.3: was ...` (with a colon and a justification) remain
allowed in `tests/`, `beagle/security/`, and other exempt paths.

### Consequences

- New contributors can't accidentally introduce a drift.
- The pre-commit hook may need updating when the project's release
  major increments (e.g. when we ship v1.0). The maintenance
  cost is low (one line in `_RECOGNISED_MAJORS`).
- Some legitimate-seeming comments get rejected; the error message
  tells users where to move the file or how to rewrite the comment.

---

## ADR-002: LangGraph for DAG orchestration

**Status:** Accepted
**Date:** v1.0
**Deciders:** Core team

### Context

We needed a DAG execution engine that supports:

- Async execution (Beagle is async-first)
- Checkpointing (for resume after crash)
- Dynamic node spawning (sub-agents)
- Type-hinted state (Pydantic)

### Decision

We use **LangGraph** as the DAG engine, not a homegrown
implementation. The orchestrator (`beagle/core/`)
wraps LangGraph with Beagle-specific concerns: cost tracking,
budget enforcement, context folding, autoDream, and the Goose
subprocess executor (`BeagleDAGNode`).

### Consequences

- We inherit LangGraph's CVE surface (CVE-2025-68664 "LangGrinch
  RCE", CVE-2025-67644 SQLi in metadata filter keys, CVE-2026-34070
  path traversal). The `pyproject.toml` floor pins are
  the defense.
- Subprocess isolation, AST validation, and EVH are layered on top
  of LangGraph — they are not LangGraph features.
- Migrating to a different DAG engine would require rewriting
  `core/orchestrator/`; the rest of the codebase is engine-agnostic.

---

## ADR-003: Path.relative_to() for containment, not str.startswith()

**Status:** Accepted (audit S2/S3, v13.17.0)
**Date:** v13.17.0
**Deciders:** Security team

### Context

The original containment check used `str(path).startswith(root_str)`.
This is bypassable:

- Symlinks inside the root that point outside the root still
    have a string representation starting with the root's string.
- Case-insensitive filesystems (macOS HFS+ default, NTFS) can
    have `Foo` and `foo` be the same path.
- Trailing separators confuse string comparison.

### Decision

All containment checks use `Path.relative_to()`. The Path API
calls `.resolve()` first (resolving symlinks) and then checks
parent-child relationship via the standard library, which is
correct under all the bypass scenarios above.

### Consequences

- The function returns the **resolved** path, not the input. Code
  that relied on the original path's string form must adjust.
- The error message ("resolves to a path which is outside the
  sandbox root") is informative for debugging.
- The contract is locked in by
  `tests/test_security_path_containment.py` and
  `tests/test_security_io_path_containment.py`. Relaxing these
  tests requires a new ADR.

---

## ADR-004: Fail-closed on mandatory dependencies

**Status:** Accepted
**Date:** v13.5.2
**Deciders:** Security team

### Context

Beagle has several security-critical dependencies that must be
present at runtime: `google-re2` for secret scrubbing, the LanceDB
embedder for RAG, the Kùzu graph database. If any of them is
missing, the system has a few options:

- **Fail-closed:** refuse to start, surface a clear error.
- **Fail-open with warning:** start anyway, log a warning, hope
    for the best.
- **Fallback:** switch to a less-secure / less-capable
    alternative.

### Decision

Beagle fails **closed** for `google-re2` (the secret scrubber) and
the RAG stack. If `re2` is missing, the secrets loader refuses to
load any secret; if LanceDB or Kùzu is missing, `rag_search` returns
a structured error response (not a crash).

This is enforced at import time for `re2` and at function-call
time for the RAG dependencies.

### Consequences

- Operators get a clear "this won't work" message instead of a
  silent security degradation.
- `beagle doctor` surfaces dependency status.
- Test environments that don't have the full dependency stack
  (like this one) can still import the package and run unit tests
  that don't touch the RAG layer.

---

## ADR-005: Goose as a subprocess, not in-process

**Status:** Accepted
**Date:** v1.0
**Deciders:** Core team

### Context

Each DAG node is executed by a "Goose" agent. The original
implementation had Goose running in the same Python process as
Beagle. This made several things easier (no IPC, no serialisation)
and several things impossible (true isolation, accurate cost
tracking per subprocess, restart on crash).

### Decision

Beagle spawns each Goose invocation as a **subprocess** (via
`subprocess.run` with `shell=False` and an explicit argv list).
Communication is via JSON-RPC over stdin/stdout (or HTTP for the
newer transport).

### Consequences

- We get process isolation for free — a crashed Goose doesn't
  take down the orchestrator.
- The subprocess boundary is the **security boundary** between
  trusted and untrusted code. All subprocess output passes
  through EVH (Evidence-based output Validation) before being
  used to mutate orchestrator state.
- Restart-on-crash is implemented at the orchestrator level
  (`retries` per node, exponential backoff).
- The subprocess has its own resource limits (memory cap,
  timeout) enforced via `resource.setrlimit` in
  `core/sandbox.py`.

---

## ADR-006: Pydantic v2 for all data models

**Status:** Accepted
**Date:** v13.0
**Deciders:** Core team

### Context

Beagle's state, agent configs, event payloads, and config schema
all need validation. We could use:

- Plain dataclasses (no validation)
- attrs (some validation)
- Pydantic v1 (full validation, but in maintenance mode)
- Pydantic v2 (full validation, Rust core, faster)

### Decision

All data models use **Pydantic v2**. The `pyproject.toml` pins
`pydantic>=2.13.3` to stay current with security fixes.

### Consequences

- Validation is automatic; invalid data is rejected at the
  boundary.
- The Pydantic `.model_dump()` / `.model_validate()` cycle is the
  standard serialization path.
- The `WorkflowConfig` dataclass predates this decision and is
  being migrated to Pydantic in a focused PR.

---

## ADR-007: MCP tool schemas hardened with additionalProperties:false

**Status:** Accepted (F10.3, v13.12.9)
**Date:** v13.12.9
**Deciders:** Security team

### Context

FastMCP's `@mcp.tool()` decorator uses Pydantic's
`model_json_schema()` to generate the input schema. Pydantic's
default schema does **not** emit `additionalProperties: false`,
which means a caller can pass fields the runtime didn't expect.

### Decision

A post-registration hook (`hardening/mcp_schema_hardener.py`)
walks every registered tool's schema and injects
`additionalProperties: false` on every object node, recursively.

The hook is called in every MCP server's startup path, after
all `@mcp.tool()` decorators and before `mcp.run()`.

### Consequences

- Tools reject unknown fields at the JSON-schema level. The
  runtime doesn't have to defensively check for extra fields.
- The `tests/test_mcp_schema_strict.py` regression test locks the
  contract.
- Any new MCP server in the project must call the hardener
  during startup, or it will be flagged in code review.

---

## ADR-008: Helm chart deferred to a separate project

**Status:** Accepted (defer)
**Date:** v13.21.5
**Deciders:** Core team

### Context

A Helm chart for Kubernetes deployment would be a useful artifact
but is a multi-day effort (templates for the orchestrator, MCP
servers, RAG, configmaps, secrets, ingress, RBAC, pod disruption
budgets, etc.).

### Decision

The Helm chart is **deferred** to a separate project
(`beagle-helm-chart`) and tracked as a follow-up. The
`beagle_dockeriser` subproject provides the container images; the
Helm chart is a thin orchestration layer over those images.

### Consequences

- Single-container `docker run` is the supported deployment path
  for now.
- Kubernetes users must deploy the images themselves.
- The Helm chart, when written, will live in a separate repo so
  the Beagle core remains small and Python-only.

---

## Adding a new ADR

Create a new section in this file with the next sequential
number. Use the template from any existing ADR. Open a PR; the
PR review is the discussion. Once merged, the decision is
binding.
