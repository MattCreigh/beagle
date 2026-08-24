# Deployment Checklist

Production deployment checklist for the multi-agent orchestration engine.

**Target Hardware:** CPU-only host (no GPU required). LLM inference is remote;
the host performs only orchestration and local CPU-bound embedding work.

---

## Pre-Deployment

### 1. Environment Setup

- [ ] Python >= 3.12 installed
- [ ] `uv` package manager installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [ ] Target venv created (e.g. `uv venv <venv-path> --python 3.12`)
- [ ] Frontend CLI binary installed and accessible (e.g. on `$PATH`)
- [ ] Remote LLM provider API key set as the configured env var

### 2. Hardware Verification

- [ ] Ramdisk mounted at `<ramdisk_mount>` (recommended ~6GB tmpfs)
- [ ] NVMe I/O scheduler set to `none`: `cat /sys/block/<nvme_device>/queue/scheduler`
- [ ] SATA I/O scheduler set to `mq-deadline`: `cat /sys/block/<sata_device>/queue/scheduler`
- [ ] CPU governor set to `performance` during active workflows
- [ ] ZRAM swap configured if memory pressure expected (optional)

See `HARDWARE_TUNING.md` for detailed tuning instructions.

### 3. Configuration

- [ ] `config.toml` reviewed and version matches the release
- [ ] Default model and fallback chain configured
- [ ] Embedding model matches RAG server expectation
- [ ] Rate limit tokens-per-minute configured (default cap)
- [ ] Hardware section: `ramdisk_enabled = true`, paths correct
- [ ] Secrets file at the config root with permissions 0600

### 4. Security

- [ ] MCP transport: stdio (default). HTTP/SSE only if explicitly required
      with Bearer auth
- [ ] Firewall regex timeout configured (ReDoS protection)
- [ ] Token verifier: SHA-256 hashed tokens, constant-time comparison
- [ ] Failed-attempts cap configured in mcp_security (DoS protection)
- [ ] Tool output validation enabled
- [ ] AST validator: `DANGEROUS_AST_NODES` excludes `Attribute`

---

## Build & Install

```bash
cd <repo-root>

# 1. Lint
make lint

# 2. Dead code check
make vulture

# 3. Full test suite
make test

# 4. Build wheel
uv build --wheel

# 5. Install to target venv
WHEEL=$(ls -t dist/*.whl | head -1)
uv pip install --python <venv-python> --force-reinstall "$WHEEL"

# 6. Verify
<venv-python> -c "import beagle; print(beagle.__version__)"
```

---

## Post-Deployment Verification

### 1. Module Import Smoke Test

```bash
<venv-python> -c "
from beagle.core.router import route_query
from beagle.context.auto_hydration import auto_hydrate_sync
from beagle.security import validate_query
from beagle.security.vigil import validate_tool_output
print('All imports OK')
"
```

### 2. Hardware Checks

```bash
<venv-python> -c "
from beagle.infrastructure.hardware_checks import (
    check_ramdisk, check_io_scheduler, get_cpu_governor
)
r = check_ramdisk()
print(f'Ramdisk: available={r.available}, path={r.path}, free={r.free_mb}MB')
s = check_io_scheduler()
for dev, sched in s.items():
    print(f'  {dev}: {sched}')
print(f'CPU governor: {get_cpu_governor()}')
"
```

### 3. RAG Health Check

```bash
<venv-python> -c "
from beagle.context.auto_hydration import auto_hydrate_sync
result = auto_hydrate_sync()
print(f'Status: {result.status}')
print(f'Kuzu nodes: {result.kuzu_nodes}, edges: {result.kuzu_edges}')
"
```

### 4. Workflow Smoke Test

```bash
beagle run "List the files in the current directory" --approve-all
```

---

## Monitoring

- **Telemetry:** OpenTelemetry traces emitted to `<config_root>/otel_events.jsonl`
- **Cost tracking:** Per-workflow cost in `<config_root>/tracking.db` (SQLite WAL)
- **Context monitoring:** ContextMonitor warns at 80% / critical at 95% context usage
- **Event bus:** NDJSON event log at `<config_root>/events.ndjson`
- **Daemon:** Background daemon watches for codebase changes, auto-triggers workflows

---

## Rollback

If the deployment fails:

```bash
# Reinstall previous version
uv pip install --python <venv-python> --force-reinstall dist/<previous-wheel>.whl
```

---

## Known Constraints

| Resource | Limit | Impact |
|---|---|---|
| RAM | Host total, ~10GB for orchestrator | AFM (Adaptive Focus Memory) reduces footprint |
| CPU | All cores via CPU-only orchestration | Inference is delegated to the remote LLM provider |
| GPU | None required | All LLM inference uses the configured remote API |
| Concurrency | 2-6 adaptive workers | Dynamic pool adjusts based on CPU load |
| Rate limit | Configurable (default 50,000 tokens/min) | Configurable in config.toml |
