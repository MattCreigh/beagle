# Security Documentation

## Overview

This document describes the security measures implemented across the
Beagle system. It is written for engineers and security reviewers who
need to understand what the code does, not how a particular deployment
is configured.

## Security Features

### 1. Input Validation

All user inputs are validated through `security.validate_query()`,
which checks for:

- Query length limits
- Prompt injection patterns
- Shell metacharacters
- System tag injection attempts
- Optional semantic firewall evaluation (LLM-based)

**Location:** `beagle/security/validation.py`

### 2. Path Traversal Prevention

File paths are validated using `security.validate_file_path()`, which:

- Blocks null bytes
- Prevents `..` traversal
- Validates paths stay within allowed base directories
- Checks for known-sensitive paths
- Resolves symlinks to prevent escape

Containment is implemented with `Path.relative_to()` — never
`str.startswith()`, which is path-prefix-confusable.

**Location:** `beagle/security/validation.py::validate_file_path()`

### 3. Secret Scrubbing

All outputs are scrubbed for sensitive data using
`security.scrub_secrets()`, which detects and redacts:

- API keys and tokens
- Passwords and credentials
- Cloud provider access keys
- Private keys (RSA, OpenSSH)
- Database connection strings with passwords
- Common source-control tokens
- Age encryption keys

**Pattern matching uses:**

- google-re2 for linear-time matching (no ReDoS)
- Fallback to stdlib `re` with timeout protection
- Input length caps

The scrubber is **fail-closed**: if the re2 backend cannot load,
the output is rejected rather than passed through unsanitised.

**Location:** `beagle/security/sanitization.py::scrub_secrets()`

### 4. Rate Limiting

Rate limiting is implemented to prevent abuse:

- Token bucket algorithm
- Configurable per-workflow and per-model
- Burst size support
- Requests-per-second limits
- Authentication-failure throttling (capped entries)

**Location:** `beagle/utils/rate_limiter/`,
`beagle/infrastructure/mcp_security.py`

### 5. Secure Credential Handling

- No hardcoded secrets in code
- Environment variables for sensitive configuration
- Ephemeral secret generation when environment variables are not set
- `secrets.token_hex()` for cryptographic randomness
- Audit-signing secret generates a random session secret if not
  provided

**Locations:**

- `beagle/infrastructure/audit_logger.py`
- `beagle/infrastructure/services/embedding.py`
- `beagle/secrets_loader.py`

### 6. Error Handling (No Information Leakage)

All error messages are:

- Scrubbed for secrets before return
- Truncated to a fixed length
- Logged with full details server-side only
- Replaced with generic messages when returned to clients

**Applied in:**

- `beagle/infrastructure/mcp_utility_server.py`
- `beagle/infrastructure/mcp_rag_server.py`

### 7. Security Logging & Audit Trail

Comprehensive audit logging with:

- Structured JSON logging
- Automatic sensitive data scrubbing
- Cryptographic hash chains for tamper detection
- SQLite persistence
- Event integrity verification

**Location:** `beagle/infrastructure/audit_logger.py`,
`beagle/events/event_bus.py`

### 8. Webhook Security

Webhooks use HMAC-SHA256 signatures:

- Constant-time signature verification
- Configurable secrets per webhook
- Retry logic with exponential backoff

**Location:** `beagle/webhooks.py`

### 9. Transport Security

- MCP servers default to **stdio transport only** — no network
  exposure by default.
- HTTP/SSE transport requires explicit enablement with mandatory
  Bearer token authentication (`TokenVerifier` middleware).
- Strict CORS configuration (no wildcards) enforced when HTTP is
  enabled.
- Rate limiting on authentication failures (capped entries with
  automatic eviction).
- Bearer token is required to start an HTTP-transport server; the
  server refuses to boot without it (fail-closed).

**Location:** `beagle/infrastructure/mcp_security.py`

### 10. Agent Type Whitelisting

Only pre-approved agent types can be spawned:

- Whitelist built from recipe files
- Prevents arbitrary agent execution
- Validated before agent spawning

**Location:** `beagle/security/validation.py::validate_agent_type()`

## Security Commands

Run security checks with the project's standard targets
(`make security`, or the project's documented equivalent). This
typically runs:

1. **Bandit** — Python static security analysis
2. **pip-audit** — Dependency vulnerability scanning

## Security Checklist

- [x] Input validation on all public APIs
- [x] Path traversal prevention
- [x] Rate limiting
- [x] Secret scrubbing
- [x] Secure credential handling
- [x] Error message sanitisation
- [x] Audit logging
- [x] Webhook signatures
- [x] stdio-only transport by default
- [x] Agent whitelisting
- [ ] CSRF protection (N/A — no HTTP endpoints in default
  configuration)
- [x] Dependency vulnerability scanning (automated)

## Vulnerability Remediation Log

The entries below document security defects that have been fixed.
They are kept here for traceability, not as a current-state
description.

### VULN-001: Node-Failure Telemetry Fields

Node execution failures now dispatch a structured failure event via
the event bus from all `except` blocks in `nodes.py`. The event
carries operational context: model identifier, error category,
stderr snippet, duration, and node phase.

**Location**: `events/events.py`, `core/nodes.py`

### VULN-002: Context-Folding Cache String Corruption Guard

The cache `set()` override ensures string values are never passed
to the compressor. Previously, the inherited `set()` method
bypassed the string guard in `put()`, creating a data-corruption
path. Strings are now stored uncompressed via the parent class.

**Location**: `utils/cache.py`
**Verification**: Runtime test confirms string values are stored
and retrieved intact; numeric arrays still compress correctly.

### VULN-003: Configurable Memory-Index Token Budget

The hardcoded token budget has been replaced with a config
cascade: environment variable → `config.memory.index_token_budget`
→ default value (with a documented minimum). A backward-compatible
module-level alias is maintained for existing consumers.

**Location**: `memory/memory_index.py`
**Configuration**: See the `[memory]` section of the deployment
config, or set the corresponding environment variable.

### VULN-004: Embedding Endpoint Routing

Confirmed: the remote embedding endpoint correctly uses the
remote-API path; the local embedding path uses the local-API path.
No mismatch found — this was pre-existing.

**Location**: `infrastructure/services/embedding.py`

### VULN-005: Semantic Firewall Temp File Leak

`NamedTemporaryFile(delete=False)` in `semantic_firewall()` left
sensitive prompt content on disk after crashes. Fixed with
`delete=True` plus explicit `finally` cleanup. Also added
`<user_input>` tag stripping after HTML-escape to prevent prompt
injection (OWASP A03).

**Location**: `beagle/security/firewall.py:semantic_firewall()`

### VULN-006: Cache-Busting DoS via LRU Pattern Check

The cached pattern check accepted raw text as a cache key.
Adversarial queries with trivial whitespace, case, or zero-width
variations could bypass caching and exhaust LRU slots. Fixed by
separating normalisation (Unicode NFKC + zero-width removal +
case-fold) from the cached lookup.

**Location**: `beagle/security/validation.py:_cached_pattern_check()`

### VULN-007: Task-ID Truncation (Reduced Entropy)

`uuid4()[:12]` produced only 48-bit task IDs, creating an IDOR
risk via birthday-paradox collision at scale. Fixed to use the
full `uuid4()` string (122-bit entropy).

**Location**: `infrastructure/task_store.py`

### VULN-008: SIGALRM Regex Timeout Bypass in Async

`_regex_sub_safe()` uses `signal.SIGALRM` for regex timeout, which
only works on the main thread. In async workers, the timeout is
silently skipped. Documented as a known limitation; callers in
async contexts should wrap the call with `asyncio.wait_for`.

**Location**: `beagle/security/validation.py:_regex_sub_safe()`

## Reporting Security Issues

Please report security vulnerabilities privately to the
maintainers via the project's coordinated-disclosure channel.

## Production Deployment

Before deploying to production:

1. Set the audit-signing secret environment variable.
2. Configure webhook secrets.
3. Enable rate limiting with appropriate thresholds.
4. Review and adjust the semantic firewall timeout.
5. Run the project's security gate and address all findings.
6. Enable integrity verification in the audit logger.
