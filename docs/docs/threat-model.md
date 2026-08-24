# Beagle Threat Model

This document is the threat model for the Beagle agentic workflow
system. It formalises the trusted-host assumption stated in
`SECURITY.md` and enumerates the threats Beagle defends against,
the assets at risk, and the controls that mitigate each threat.

## 1. Trust boundary

Beagle assumes a **trusted host**. The security model protects
against malicious *model output* and *untrusted code executed inside
the sandbox* — prompt injection, path traversal, secret
exfiltration, and dangerous subprocess calls. It does **not** defend
against a genuinely malicious host or kernel: if an attacker
already has code execution on the host, no application-layer
sandbox can contain them.

```text
+------------------------- TRUSTED -------------------------+
|  Host OS + kernel + operator (assumed trusted)            |
|                                                            |
|  +--------------------- UNTRUSTED -----------------------+ |
|  |  Model output (LLM)       Untrusted code (payload)    | |
|  |  Remote A2A peers         External web content         | |
|  +-------------------------------------------------------+ |
+------------------------------------------------------------+
```

## 2. Assets at risk

| Asset | Description |
|-------|-------------|
| Secrets | API keys, A2A signing keys, SOPS-encrypted config, the orchestrator's secret store |
| Config | Deployment configuration, provider settings, presets, auth policy, style guides |
| RAG index | Vector store plus graph store over the indexed codebase |
| Orchestrator state | Progress files, tracking database, workflow runs |
| Host filesystem | Anything the sandbox or a subprocess can reach |

## 3. Threats and controls

### T1 — Prompt injection (model output)

- **Threat:** A model output or retrieved RAG chunk contains
  instructions that override the system prompt or exfiltrate
  secrets.
- **Controls:** input validation, AST checking, secret scrubbing,
  the semantic firewall, and prompt-boundary tag stripping
  *before* HTML-escaping (OWASP A03).

### T2 — Path traversal

- **Threat:** A crafted path escapes the intended directory and
  reads or writes an arbitrary file.
- **Controls:** `Path.relative_to()` containment checks (never
  `str.startswith()`), and a deny-by-default sandbox.

### T3 — Secret exfiltration

- **Threat:** Logs or model output leak API keys or signing keys.
- **Controls:** `secrets_loader.py` (environment variables first,
  then the configured secrets file), restrictive file permissions,
  and the secret-pattern scrubber with a documented minimum-length
  threshold.

### T4 — Dangerous subprocess / dynamic code execution

- **Threat:** Untrusted code calls `eval`, `exec`, `os.system`,
  or imports a dangerous module.
- **Controls:** `validate_python_code_ast()` (AST-based; blocks
  `eval`/`exec` and dangerous imports), the `SandboxedExecutor`
  (timeouts + rlimits), and the `MicroVMSandbox` (KVM isolation
  when Firecracker and `/dev/kvm` are present).

### T5 — Unauthenticated MCP / A2A access

- **Threat:** A remote client reaches the MCP or A2A surface
  without authentication.
- **Controls:** bearer-token authentication on every
  streamable-HTTP MCP transport (fail-closed `RuntimeError` if the
  configured bearer token is missing), HMAC signing for A2A
  requests, and an RBAC policy engine.

### T6 — Supply-chain / dependency compromise

- **Threat:** A third-party dependency is compromised.
- **Controls:** pinned versions in `pyproject.toml`, CVE floors on
  high-risk packages (langchain, mcp, click), and the
  import-linter contract that forbids importing
  `langchain_community` from `beagle`.

## 4. Out of scope (assumed trusted)

- The host OS and kernel.
- The model providers (the configured LLM provider).
- Third-party dependencies (report those to their respective
  projects).

## 5. Reporting

Report a vulnerability through the repository's coordinated
disclosure flow (private advisory). See `SECURITY.md` for the
policy.
