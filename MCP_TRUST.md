# MCP Trust Policy

**Version:** 1.0  
**Last updated:** 2026-04-26

## Overview

Beagle uses Model Context Protocol (MCP) servers for tool integration (RAG,
utility, OpenClaw). Each MCP server has a **trust label** that controls which
transport it may use and what security requirements apply.

## Trust Labels

| Label | Meaning | Allowed Transport | Auth Required |
|-------|---------|-------------------|---------------|
| `trusted` | Server is maintained by the Beagle project or explicitly vetted by the operator | `stdio`, `http`, `sse` | Only for HTTP |
| `untrusted` | Third-party or unvetted server | `http`, `sse` only | **Yes — always** |

## Default Policy

All MCP servers default to `trust_label = "untrusted"`. This is a
deny-by-default posture: a new MCP server that has not been explicitly
configured is **refused on stdio transport** because stdio has no
authentication layer.

To mark a server as trusted (allowing stdio transport), add to `config.toml`:

```toml
[mcp]
trust_label = "trusted"
```

## Transport Security Matrix

| Transport | Trusted | Untrusted |
|-----------|---------|-----------|
| `stdio` | ✅ Allowed | ❌ Refused |
| `http` | ✅ Auth required | ✅ Auth required |
| `sse` | ✅ Auth required | ✅ Auth required |
| `streamable-http` | ✅ Auth required | ✅ Auth required |

HTTP/SSE transports **always** require authentication regardless of trust
label (Bearer token in `[mcp_auth].tokens` or `BEAGLE_MCP_TOKEN` env var,
loopback binding only).

## Rationale

**Why refuse untrusted stdio?**

`stdio` transport has no authentication layer — any process that can write to
the MCP server's stdin can invoke tools. For a trusted server (e.g., the Beagle
RAG server we ship), this is acceptable because the calling process is the
Beagle orchestrator itself. For a third-party MCP server, this creates an
escalation path: if the external server is compromised, it can invoke any
Beagle tool without authentication.

**Why not just trust everything?**

The supply-chain risk of MCP servers is real. A compromised or malicious MCP
server on stdio can:

- Execute arbitrary code via `run_workflow` or `run_beagle_workflow`
- Read secrets via RAG search
- Modify files via code tools

Marking a server as `trusted` is an explicit operator decision to accept this
risk.

## Configuration Reference

```toml
# config.toml

[mcp]
transport = "stdio"           # Transport: stdio | http | sse
trust_label = "untrusted"     # Trust: trusted | untrusted

[mcp_auth]
enabled = true                 # Enable auth for HTTP transport
tokens = ["beagle-..."]          # Bearer tokens
require_https = true           # HTTPS required for HTTP transport
bind_address = "127.0.0.1"     # Loopback only
```

## Enforcement

The `enforce_transport_security()` function in
`infrastructure/mcp_security.py` enforces this policy at server startup.
Violations raise `RuntimeError` and prevent the server from starting — there is
no silent fallback.

## Changing Trust Labels

1. Edit `config.toml` `[mcp].trust_label`
2. Restart the Beagle orchestrator (or the specific MCP server process)
3. Verify: `beagle health-check` should show the MCP server as healthy

## Audit

All trust label decisions are logged at `INFO` level when the MCP server
starts. Security rejections are logged at `WARNING` level. These entries are
captured by the Beagle audit trail.
