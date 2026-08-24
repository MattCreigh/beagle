# Security Policy

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Report security issues privately to the maintainers:

- **Private vulnerability reporting:** use the repository's **Security →
  Report a vulnerability** flow on GitHub (private advisory). This is the
  supported, non-public channel and does not require a personal email
  address.

We will acknowledge receipt within 48 hours and aim to ship a fix within
7 days for Critical/High findings. Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (or a minimal PoC)
- Affected versions
- Any suggested fix, if known

We operate a coordinated-disclosure policy: please give us a reasonable
window to fix and release before public disclosure.

## Trusted-Host Assumption

**Beagle assumes a trusted host.** The security model protects against
malicious *model output* and *untrusted code executed inside the sandbox* —
prompt injection, path traversal, secret exfiltration, and dangerous
subprocess calls — via input validation, AST checking, secret scrubbing, and
the semantic firewall.

It does **not** defend against a genuinely malicious host or kernel. If an
attacker already has code execution on the host, no application-layer
sandbox can contain them. Deploy Beagle only on hosts you control and trust.

### Sandbox isolation

- `SandboxedExecutor` enforces configurable timeouts and resource limits
  (rlimits) on untrusted code.
- `MicroVMSandbox` adds KVM hardware isolation **only when Firecracker +
  `/dev/kvm` are present**.
- **Deny-by-default:** if the MicroVM path is unavailable and
  `allow_fallback` is `False` (the default), Beagle **refuses** to run the
  payload at reduced isolation (exit 126) rather than silently degrading.
  A permitted degrade requires explicit `allow_fallback=true` and emits a
  loud WARNING.

### Model allowlist

Every model that flows into the LLM bridge, subprocess pool, or orchestrator
MUST be on `[models.allowed]` in `config.toml`. The semantic firewall's
`FIREWALL_MODEL` is validated against this allowlist at startup — a
misconfigured firewall model fails early rather than silently blocking every
query.

## Scope

In scope:

- The Beagle orchestrator, RAG pipeline, MCP servers, and CLI
- The sandbox (SandboxedExecutor / MicroVMSandbox)
- The semantic firewall and input validation
- The A2A protocol and RBAC

Out of scope (assumed trusted):

- The host OS and kernel
- The model providers (Ollama Cloud, OpenRouter)
- Third-party dependencies (report those to their respective projects)

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |
