# Beagle Security Whitepaper

**Audience:** Security reviewers, prospective enterprise users,
compliance teams.
**Status:** Living document — see also `threat-model.md`,
`SECURITY.md`, and `ADR.md`.

---

## 1. Executive summary

Beagle is a multi-agent orchestrator that runs agent subprocesses to
execute workflows. Its security model is **zero-trust at every
trust boundary**, with:

- **Path containment** via `Path.relative_to()` (not
  `str.startswith()`).
- **MCP tool input validation** via a post-registration schema
  hardener that injects `additionalProperties: false` recursively.
- **Input validation** on every user-facing entry point, with
  length caps and Cypher-injection rejection on the RAG layer.
- **AST validation** for any Python code extracted from agent
  output.
- **EVH (Evidence-based output Validation)** on every subprocess
  result before it mutates orchestrator state.
- **HMAC-signed A2A** inter-agent messages with a 1 MiB body cap.
- **Fail-closed** on the mandatory `google-re2` secret scrubber.

A comprehensive threat model (`docs/threat-model.md`) lists the
identified threat categories with mitigations and residual risk
ratings. All mitigations are backed by regression tests under
`tests/test_security_*.py`.

---

## 2. Trust boundaries

Beagle has four primary trust boundaries. Each is enforced by code
in the `beagle.security`, `hardening`, and `cli.cli_graceful_shutdown`
modules.

### 2.1 Operator boundary

The human operator runs the CLI. The operator is **trusted** to
read and write files in their workspace, but is **not trusted**
to:

- Pass arbitrary paths to the orchestrator (containment enforced).
- Inject Python code through the YAML workflow DSL (AST
  validation enforced).
- Modify the secrets loader's output (`google-re2` required;
  fail-closed on tamper).

### 2.2 Subprocess boundary

Each DAG node runs an agent subprocess. The subprocess is the
**untrusted boundary**: it can read project files (within
containment), but its output is treated as adversarial until EVH
validates it.

Enforcement:

- `subprocess.run(argv, shell=False, ...)` — no shell
  interpretation.
- Per-subprocess resource limits via `resource.setrlimit`
  (`core/sandbox.py`).
- Per-subprocess timeout (see
  `constants.DEFAULT_SUBPROCESS_TIMEOUT_S`).
- Output redaction of secrets before they leave the subprocess
  (`beagle/secrets_loader.py`).

### 2.3 MCP client boundary

External MCP clients connect to Beagle's MCP servers. The client is
**partially trusted**: it can call tools, but cannot:

- Inject extra fields into the input schema (rejected by
  `additionalProperties: false`).
- Exceed the configured query cap (truncated by
  `_validate_search_input`).
- Exceed the 1 MiB A2A body cap (rejected).
- Bypass per-server rate limiting.

### 2.4 A2A inter-agent boundary

Other A2A agents connect over HTTP. Messages must carry an HMAC
signature verified against a shared secret resolved from the
configured A2A secret location. Optional OIDC token validation is
supported in zero-trust mode.

---

## 3. Defense in depth

The security model is layered. A single bypass does not
compromise the system; multiple layers must fail.

| Layer | What it defends | What catches it |
|-------|-----------------|-----------------|
| **L1: Schema** | Reject malformed inputs at the boundary. | `mcp_schema_hardener.py`, `_validate_search_input`. |
| **L2: Path** | Reject paths that escape the workspace. | `Path.relative_to()` in `io.py` and other locations. |
| **L3: AST** | Reject dangerous Python constructs. | `validate_python_code_ast` (`beagle/security/ast_validator.py`). |
| **L4: Secret scrub** | Redact API keys / tokens from logs / output. | `scrub_secrets` (`beagle/security/sanitization.py`) — `google-re2` required. |
| **L5: EVH** | Reject subprocess outputs that lack evidence. | `_run_evh_validation` (`core/orchestrator/executor.py`). |
| **L6: Cost cap** | Prevent budget exhaustion. | `cost_tracker.budget_usd` check per node. |
| **L7: Rate limit** | Prevent abuse of MCP / A2A endpoints. | `_check_mcp_rate_limit` (`infrastructure/mcp_*.py`). |
| **L8: Audit log** | Record every security-relevant event. | `events/event_bus.py` → `tracking/database.py` (SQLite WAL). |

Each layer has at least one regression test
(`tests/test_security_*.py`). All layers are exercised in the
integration test suite.

---

## 4. Cryptography

| Purpose | Algorithm | Key source |
|---------|-----------|------------|
| A2A message signing | HMAC-SHA256 | configured per-deployment secret |
| Secret scrubbing | google-re2 regex | patterns in `beagle/security/constants.py` |
| Checkpoint integrity | SHA-256 | computed at write time, verified at load |
| HMAC over CLI tokens | (reserved) | n/a |

Beagle does **not** implement any custom cryptography. All
primitives are delegated to the Python standard library or
audited third-party packages.

---

## 5. Vulnerability management

- **Dependency CVEs**: A scheduled security-audit workflow runs
  daily and on every pull request. It uses Bandit, Safety,
  pip-audit, Semgrep, TruffleHog, OSV-Scanner, and CodeQL. The
  associated dependency-review action blocks moderate-or-higher
  CVEs from being merged.
- **SBOM**: A CycloneDX SBOM (JSON and XML) is generated on every
  push to `main` and every release tag. The SBOM is attached to
  releases.
- **Disclosure**: Private security advisories. See `SECURITY.md`
  for the policy.

---

## 6. Operational security

### 6.1 Secrets

- API keys are read from the configured secrets file (with an
  environment-variable fallback).
- The file is **never** logged or echoed.
- In multi-tenant deployments, secrets are per-tenant and
  scoped via `tenant_id`.

### 6.2 Audit log

Every security-relevant event is published to the event bus:

- `NodeCompleted` / `NodeFailed`
- `SteeringReceived`
- `GuardianApprovalRequired` / `GuardianApprovalGranted` /
  `GuardianApprovalDenied`
- `WorkflowCompleted`

The events are persisted to a SQLite WAL database. The database
is append-only; no UPDATE or DELETE is performed on the audit
table.

### 6.3 Reproducibility

Every workflow run can be replayed from a recorded manifest
(`beagle/replays/<workflow_id>_manifest.json`). The manifest
includes:

- The full input query (after redacting secrets).
- The DAG topology that was executed.
- The LLM calls that were made (with prompts and responses).
- The cost report.

A replayed run is byte-equivalent to the original *except* for
non-deterministic model output, which is logged but not
re-executed.

---

## 7. Compliance

- **CWE coverage**: Beagle's security tests cover the following
  CWEs (verified by `tests/test_security_cwe_*.py`):
  - CWE-22 (Path Traversal)
  - CWE-20 (Improper Input Validation)
  - CWE-79 (XSS — not directly applicable, but the AST
    validator blocks `<script>` in f-strings)
  - CWE-94 (Code Injection)
  - CWE-200 (Information Exposure)
  - CWE-400 (Uncontrolled Resource Consumption)
  - CWE-770 (Allocation of Resources Without Limits)
  - CWE-799 (Improper Control of Interaction Frequency)
- **OWASP Top 10**: covered via the threat model.
- **SOC 2**: Beagle produces an audit log and supports
  per-tenant access controls. Formal SOC 2 certification is
  out of scope for this document.

---

## 8. Reporting a vulnerability

Report a vulnerability to the project's security contact (see
`SECURITY.md`) with:

- A description of the vulnerability.
- Reproduction steps.
- Impact assessment.

The maintainers aim to acknowledge within one business day and
to provide a fix or mitigation within 30 days for high-severity
issues.

---

## 9. Versioning

This whitepaper is updated with every major release. The
canonical version is the latest one in the `main` branch. See
`CHANGELOG.md` for release notes.

---

## 10. Acknowledgements

Beagle's security model is built on the work of many open-source
projects and security researchers. In particular:

- OWASP GenAI Security Project — for the LLM-specific threat
  taxonomy.
- The Hypothesis library — for property-based testing of the
  security validators.
