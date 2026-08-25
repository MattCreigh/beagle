"""Configuration loading for Goose Agentic Workflow.

Handles TOML-based config file loading, validation, and caching.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import MISSING
from dataclasses import fields as _dc_fields
from pathlib import Path
from typing import Any

# Python 3.11+ has tomllib in stdlib
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-untyped,no-redef]

from ..runtime.goose_cli import default_goose_binary
from .model_resolver import get_preset
from .schema import (
    A2AConfig,
    BehaviorConfig,
    BudgetConfig,
    CacheConfig,
    CircuitBreakerConfig,
    ConnectionsConfig,
    ContextThresholdConfig,
    CoordConfig,
    DecompositionConfig,
    EmbedConfig,
    EnsembleConfig,
    EventBusConfig,
    GooseConfig,
    HardwareConfig,
    HealthConfig,
    LearnedRoutingConfig,
    LifecycleConfig,
    LLMConfig,
    LoggingConfig,
    MCPAuthConfig,
    MCPConfig,
    MCPCORSConfig,
    MemoryConfig,
    MemoryConsolidationConfig,
    NodeTimeoutConfig,
    OllamaCloudConfig,
    OrchestratorConfig,
    OrpheusConfig,
    OutputConfig,
    PathsConfig,
    PoolConfig,
    RAGConfig,
    RateLimitConfig,
    ReproducibilityConfig,
    RuntimeConfig,
    SandboxMicroVMConfig,
    SecurityConfig,
    StateConfig,
    StreamingConfig,
    TimeoutConfig,
    TracingConfig,
    ValidationConfig,
    WorkflowConfig,
    WorkflowDiscoveryConfig,
)

# ── D6 (Fable 5 DD 2026-06-11) — environment variable expansion ───────────
# Schema defaults are the SSOT for section fallbacks (CD-1): .get() calls
# use this map, so a default cannot drift between schema and loader.
_SCHEMA_DEFAULTS: dict[str, dict[str, object]] = {
    cfg.__name__: {f.name: f.default for f in _dc_fields(cfg) if f.default is not MISSING}
    for cfg in (HardwareConfig,)
}

logger = logging.getLogger("Beagle.config.loader")
# config.toml ships placeholder syntax for env-var interpolation
# (${WORKSPACE_ROOT}, {WORKSPACE_ROOT}, ~/, $HOME). tomllib does not
# expand these, so a literal "${WORKSPACE_ROOT}" string was being
# stored as the resolved path — health_check.py would then
# `Path("${WORKSPACE_ROOT}") / "logs"` → relative-resolve that
# under cwd, sometimes `mkdir -p`ing a literal "${WORKSPACE_ROOT}/"
# directory in the repo root. This helper expands both shell-style
# ${VAR} and Python str.format-style {VAR} placeholders, plus ~ and
# $HOME. If a placeholder references an unset env var, the value is
# treated as unset and `default` is returned — never the literal
# placeholder string.
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env_placeholders(value: str) -> str:
    """Expand ${VAR}, {VAR}, ~, and $HOME in a config string.

    Returns the string unchanged if it contains no placeholders. Returns
    an empty string if a ${VAR} placeholder references an unset env var —
    callers should treat empty as "use the default".
    """
    # Expand ~ and $HOME first (os.path.expandvars does NOT handle ~).
    value = os.path.expanduser(value)
    value = os.path.expandvars(value)
    # Handle {VAR} Python str.format-style placeholders (os.path.expandvars
    # only handles $VAR and ${VAR}).

    def _repl(m: re.Match[str]) -> str:
        var = m.group(1) or m.group(2)
        return os.environ.get(var, "")

    return _PLACEHOLDER_RE.sub(_repl, value)


def _resolve_path_value(raw: Any, default: str) -> str:
    """Resolve a path value from TOML, expanding env placeholders.

    If `raw` is None or empty, returns `default`. If `raw` is a string
    that contains ${VAR}/{VAR}/~/$HOME placeholders, expands them. If
    a placeholder references an unset env var, falls back to `default`
    rather than silently storing the literal placeholder.
    """
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        return default
    expanded = _expand_env_placeholders(raw)
    # Detect "still has placeholders" — i.e. a ${VAR} that didn't expand.
    # This happens when the env var is unset; os.path.expandvars leaves
    # the literal ${VAR} in place.
    if expanded and _PLACEHOLDER_RE.search(expanded):
        # Placeholder referenced an unset env var — fall back to default.
        logger.warning(
            "Config path value %r contains unset env-var placeholder; falling back to default %r",
            raw,
            default,
        )
        return default
    return expanded or default


def get_config_path() -> Path:
    """Get the default config file path.

    v13.22.4: delegates to the canonical _config_path resolver.
    The project root config.toml is the one and only SSOT.
    """
    from ._config_path import find_config_toml

    return find_config_toml()


def load_config(path: Path | str | None = None) -> WorkflowConfig:
    """Load configuration from TOML file.

    Args:
        path: Path to config file (uses default if None)

    Returns:
        Loaded WorkflowConfig

    """
    path = get_config_path() if path is None else Path(path)

    config = WorkflowConfig()

    if not path.exists():
        # Return defaults if config doesn't exist
        return config

    with open(path, "rb") as f:
        data = tomllib.load(f)

    # Validate: warn about unknown top-level keys (catches typos like [modelz])
    _validate_config_keys(data, path)

    # Load orchestrator config
    if "orchestrator" in data:
        orch = data["orchestrator"]
        config.orchestrator = OrchestratorConfig(
            timeout_seconds=orch.get("timeout_seconds", 300),
            validation_timeout_seconds=orch.get("validation_timeout_seconds", 60),
            max_retries=orch.get("max_retries", 3),
            retry_delay=orch.get("retry_delay", 5.0),
            max_backoff=orch.get("max_backoff", 60.0),
        )

    # Load sub-agent runtime config (B2)
    if "runtime" in data:
        rt = data["runtime"]
        config.runtime = RuntimeConfig(
            plugin=rt.get("plugin", "goose_cli"),
        )

    # Load goose config
    if "goose" in data:
        g = data["goose"]
        config.goose = GooseConfig(
            # v13.22.3: route through _resolve_path_value so the
            # `${GOOSE_BIN}` template in config.toml (which is the
            # canonical default per the env-override docs in
            # defaults.py) gets expanded; if GOOSE_BIN is unset, the
            # placeholder-expansion returns empty, and we fall back
            # to the runtime's goose-binary resolver (which does the
            # shutil.which chain). This replaces the previous code
            # that returned the literal ``${GOOSE_BIN}`` template
            # when the env var was unset — that broke every
            # workflow's subprocess call and tripped the
            # goose-subprocess circuit breaker.
            binary_path=_resolve_path_value(g.get("binary_path"), default_goose_binary()),
            # Falls back to [model_presets].default rather than a literal,
            # which had drifted to glm-5.1 while agent_config used minimax-m3.
            default_model=g.get("default_model") or get_preset("default"),
            provider=g.get("provider", "ollama_cloud"),
            host=g.get("host", ""),
        )
        # v13.20.1: Read [goose].default_pool_chain (the subprocess pool's
        # happy-path chain) from TOML. Falls back to the schema default if
        # absent. The old hardcoded list in utils/subprocess/pool_config.py
        # used to live here; it is now config-driven.
        if "default_pool_chain" in g:
            config.goose.default_pool_chain = list(g["default_pool_chain"])

    # Load per-recipe model overrides
    if "models" in data:
        config.goose.model_overrides = dict(data["models"])
        # v13.20.1: Read per-model fallback chains from [models.fallback_chains]
        # nested table. Replaces config/models.py:MODEL_FALLBACK_CHAINS as the SSOT.
        # Only the nested [models.fallback_chains] table is consumed here — top-level
        # [models] keys are still treated as agent-to-model overrides.
        fb_chains = data["models"].get("fallback_chains")
        if isinstance(fb_chains, dict):
            config.goose.fallback_chains = {
                str(k): list(v) for k, v in fb_chains.items() if isinstance(v, list)
            }

    # Load budget config
    if "budget" in data:
        b = data["budget"]
        config.budget = BudgetConfig(
            default_usd=b.get("default_usd", 10.0),
            warn_threshold=b.get("warn_threshold", 0.8),
            hard_limit_usd=b.get("hard_limit_usd", 50.0),
        )

    # Load cache config
    if "cache" in data:
        c = data["cache"]
        config.cache = CacheConfig(
            enabled=c.get("enabled", True),
            ttl_hours=c.get("ttl_hours", 24),
            max_size_mb=c.get("max_size_mb", 100),
            memory_max_entries=c.get("memory_max_entries", 100),
        )

    # Load rate limit config
    if "rate_limit" in data:
        r = data["rate_limit"]
        config.rate_limit = RateLimitConfig(
            requests_per_minute=r.get("requests_per_minute", 60),
            tokens_per_minute=r.get("tokens_per_minute", 100000),
            burst_multiplier=r.get("burst_multiplier", 1.5),
            # v13.20.13 (R6.3): backoff knobs
            initial_backoff=r.get("initial_backoff", 1.0),
            max_backoff=r.get("max_backoff", 120.0),
            backoff_multiplier=r.get("backoff_multiplier", 2.0),
            jitter_factor=r.get("jitter_factor", 0.25),
        )

    # Load logging config
    if "logging" in data:
        lg = data["logging"]
        config.logging = LoggingConfig(
            level=lg.get("level", "INFO"),
            json_format=lg.get("json_format", False),
            log_to_file=lg.get("log_to_file", True),
            max_size_mb=lg.get("max_size_mb", 10),
            backup_count=lg.get("backup_count", 5),
        )

    # Load MCP config
    if "mcp" in data:
        m = data["mcp"]
        config.mcp = MCPConfig(
            rag_server_binary=m.get("rag_server_binary", "python3"),
            rag_server_script=m.get("rag_server_script", "infrastructure/mcp_rag_server.py"),
            transport=m.get("transport", "stdio"),
            knowledge_dir=m.get("knowledge_dir", "cache/knowledge_graph"),
            read_only_runtime=m.get("read_only_runtime", True),
            max_vector_results=m.get("max_vector_results", 10),
            max_graph_hops=m.get("max_graph_hops", 3),
            max_graph_results=m.get("max_graph_results", 20),
        )

    # Load LLM global defaults
    if "llm" in data:
        llm = data["llm"]
        config.llm = LLMConfig(
            default_provider=llm.get("default_provider", "ollama_cloud"),
            default_model=llm.get("default_model") or get_preset("default"),
            # Default must name a LIVE, allowlisted model: gemma3:27b was
            # retired by Ollama Cloud on 2026-07-15 and now returns HTTP 410.
            cheap_model=llm.get("cheap_model", "gemma4:31b-cloud"),
            cheap_provider=llm.get("cheap_provider", "ollama_cloud"),
        )

    # Load node timeout config
    if "node_timeout" in data:
        nt = data["node_timeout"]
        config.node_timeout = NodeTimeoutConfig(
            planning_seconds=nt.get("planning_seconds", 120),
            execution_seconds=nt.get("execution_seconds", 90),
            verification_seconds=nt.get("verification_seconds", 90),
            synthesis_seconds=nt.get("synthesis_seconds", 120),
        )

    # Load system-wide timeout config
    if "timeouts" in data:
        to = data["timeouts"]
        config.timeout = TimeoutConfig(
            shell_command_seconds=to.get("shell_command_seconds", 10),
            analysis_seconds=to.get("analysis_seconds", 600),
            iteration_seconds=to.get("iteration_seconds", 120),
            shutdown_grace_seconds=to.get("shutdown_grace_seconds", 30),
            goose_default_seconds=to.get("goose_default_seconds", 300),
            goose_max_seconds=to.get("goose_max_seconds", 1800),
            http_connect_seconds=to.get("http_connect_seconds", 5.0),
            http_read_seconds=to.get("http_read_seconds", 30.0),
        )

    # Load RAG config
    if "rag" in data:
        rag = data["rag"]
        config.rag = RAGConfig(
            turboquant_sidecar=rag.get("turboquant_sidecar", True),
        )

    # Load pool config
    if "pool" in data:
        p = data["pool"]
        config.pool = PoolConfig(
            max_workers=p.get("max_workers", 8),
            default_timeout_seconds=p.get("default_timeout_seconds", 300),
            backoff_base=p.get("backoff_base", 2.0),
            backoff_max=p.get("backoff_max", 60.0),
        )

    # Load context threshold config
    if "context_threshold" in data:
        ct = data["context_threshold"]
        config.context_threshold = ContextThresholdConfig(
            warning=ct.get("warning", 0.50),
            pre_compact=ct.get("pre_compact", 0.58),
            compact=ct.get("compact", 0.70),
            hard_compact=ct.get("hard_compact", 0.78),
            critical=ct.get("critical", 0.85),
            max_tokens=ct.get("max_tokens", 128000),
            tokens_per_iteration=ct.get("tokens_per_iteration", 8000),
            watchdog_seconds=ct.get("watchdog_seconds", 600),
        )

    # Load memory config
    if "memory" in data:
        mem = data["memory"]
        config.memory = MemoryConfig(
            working_memory_ttl=mem.get("working_memory_ttl", 3600),
            episodic_memory_max=mem.get("episodic_memory_max", 100),
            index_token_budget=mem.get("index_token_budget", 2000),
            index_prune_strategy=mem.get("index_prune_strategy", "oldest_first"),
        )

    # Load ensemble config.
    # v1.0.0: this used to repeat the panel and judge defaults inline —
    # ["glm-5.1:cloud", "glm-5:cloud", "minimax-m2.7:cloud", "deepseek-v3.2"]
    # with judge "glm-5:cloud" — a fourth copy of a list that also lived in
    # schema.py, models.py and graph.py. Three of those four models were
    # retired upstream and are absent from [models.allowed]. Passing only the
    # keys actually present lets EnsembleConfig's preset-backed defaults fill
    # the rest, so there is one definition instead of four.
    if "ensemble" in data:
        ens = data["ensemble"]
        config.ensemble = EnsembleConfig(
            **{
                key: ens[key]
                for key in ("panel_models", "judge_model", "timeout_per_model")
                if key in ens
            }
        )

    if "security" in data:
        sec = data["security"]
        config.security = SecurityConfig(
            max_query_length=sec.get("max_query_length", 50000),
        )

    if "output" in data:
        out = data["output"]
        config.output = OutputConfig(
            truncation_threshold=out.get("truncation_threshold", 40000),
        )

    if "circuit_breaker" in data:
        cb = data["circuit_breaker"]
        config.circuit_breaker = CircuitBreakerConfig(
            max_circuits=cb.get("max_circuits", 100),
        )

    if "orpheus" in data:
        orph = data["orpheus"]
        config.orpheus = OrpheusConfig(
            default_max_agent_calls=orph.get("default_max_agent_calls", 5),
            max_cvcp_attempts=orph.get("max_cvcp_attempts", 3),
            grpo_timeout_seconds=orph.get("grpo_timeout_seconds", 300),
            ring_dir=orph.get("ring_dir", "/run/orpheus_ring"),
            transport=orph.get("transport", "unix_socket"),
            create_rings_on_startup=orph.get("create_rings_on_startup", True),
        )

    if "coord" in data:
        co = data["coord"]
        # backend_options is a nested TOML table ([coord.backend_options]),
        # popped into its own dict before the scalar mapping below - passing
        # it through the same co.get(...) pattern as every other field would
        # still work here (this branch builds CoordConfig from explicit
        # keyword args, not **co), but pulling it out first keeps that
        # invariant true even if this branch is later refactored to spread co.
        backend_options = dict(co.get("backend_options", {}))
        config.coord = CoordConfig(
            enabled=co.get("enabled", True),
            backend=co.get("backend", "fakeredis_unix"),
            backend_options=backend_options,
            heartbeat_interval_s=co.get("heartbeat_interval_s", 5),
            agent_ttl_s=co.get("agent_ttl_s", 15),
            grace_ttl_s=co.get("grace_ttl_s", 20),
            lock_ttl_s=co.get("lock_ttl_s", 120),
            event_log_maxlen=co.get("event_log_maxlen", 500),
            connect_timeout_s=co.get("connect_timeout_s", 2.0),
            probe_timeout_s=co.get("probe_timeout_s", 1.0),
            watch_poll_interval_s=co.get("watch_poll_interval_s", 2.0),
            ring_slot_size=co.get("ring_slot_size", 4096),
            ring_slot_count=co.get("ring_slot_count", 64),
            ring_enabled=co.get("ring_enabled", True),
            archive_max_bytes=co.get("archive_max_bytes", 1073741824),
            archive_max_files=co.get("archive_max_files", 30),
            rehydrate_on_spawn=co.get("rehydrate_on_spawn", False),
            socket_mode=co.get("socket_mode", "0600"),
            journal_enabled=co.get("journal_enabled", True),
            journal_fsync_interval_s=co.get("journal_fsync_interval_s", 2.0),
            journal_replay_on_spawn=co.get("journal_replay_on_spawn", True),
            channels_enabled=co.get("channels_enabled", True),
            channel_slot_size=co.get("channel_slot_size", 16384),
            channel_slot_count=co.get("channel_slot_count", 32),
            channel_offer_ttl_s=co.get("channel_offer_ttl_s", 30),
            max_channels_per_agent=co.get("max_channels_per_agent", 16),
        )

        # D-B4/C-B2: an unregistered backend name is a defect, never a
        # silent fallback. Imported lazily so beagle.config gains no
        # import-time dependency on beagle.beacon (most sessions never
        # touch coordination at all).
        if config.coord.enabled:
            from beagle.beacon.backend import UnknownBackendError
            from beagle.beacon.backends import REGISTRY

            if config.coord.backend not in REGISTRY:
                msg = (
                    f"[coord].backend = {config.coord.backend!r} is not a registered "
                    f"backend. Registered: {sorted(REGISTRY)}"
                )
                raise UnknownBackendError(msg)

    # Load behavior config
    if "behavior" in data:
        beh = data["behavior"]
        config.behavior = BehaviorConfig(
            auto_create_missing_files=beh.get("auto_create_missing_files", True),
            safe_file_operations=beh.get("safe_file_operations", True),
        )

    # Load MCP auth config
    if "mcp_auth" in data:
        auth = data["mcp_auth"]
        config.mcp_auth = MCPAuthConfig(
            enabled=auth.get("enabled", True),
            tokens=auth.get("tokens", []),
            require_https=auth.get("require_https", True),
            bind_address=auth.get("bind_address", "127.0.0.1"),
        )

    # Load MCP CORS config
    if "mcp_cors" in data:
        cors = data["mcp_cors"]
        config.mcp_cors = MCPCORSConfig(
            allowed_origins=cors.get("allowed_origins", []),
            allowed_methods=cors.get("allowed_methods", ["GET", "POST"]),
            allowed_headers=cors.get("allowed_headers", ["Authorization", "Content-Type"]),
        )

    # Load paths config
    if "paths" in data:
        p = data["paths"]
        config.paths = PathsConfig(
            workspace_root=_resolve_path_value(
                p.get("workspace_root"), config.paths.workspace_root
            ),
            goose_bin=_resolve_path_value(p.get("goose_bin"), config.paths.goose_bin),
            venv_root=_resolve_path_value(p.get("venv_root"), config.paths.venv_root),
            data_root=_resolve_path_value(p.get("data_root"), config.paths.data_root),
        )

    # Load hardware config
    if "hardware" in data:
        hw = data["hardware"]
        config.hardware = HardwareConfig(
            ramdisk_enabled=hw.get("ramdisk_enabled", True),
            ramdisk_path=hw.get("ramdisk_path", _SCHEMA_DEFAULTS["HardwareConfig"]["ramdisk_path"]),
            ramdisk_size_mb=hw.get("ramdisk_size_mb", 6144),
            ssd_write_saving_log=hw.get("ssd_write_saving_log", True),
            dynamic_concurrency=hw.get("dynamic_concurrency", True),
            concurrency_min=hw.get("concurrency_min", 2),
            concurrency_max=hw.get("concurrency_max", 6),
            cpu_high_threshold=hw.get("cpu_high_threshold", 80.0),
            cpu_low_threshold=hw.get("cpu_low_threshold", 30.0),
            warm_workers_enabled=hw.get("warm_workers_enabled", True),
            warm_worker_count=hw.get("warm_worker_count", 2),
            incremental_ingest=hw.get("incremental_ingest", True),
            zram_enabled=hw.get("zram_enabled", False),
            zram_size_mb=hw.get("zram_size_mb", 8192),
        )

    # Load tracing config
    if "tracing" in data:
        tr = data["tracing"]
        config.tracing = TracingConfig(
            backend=tr.get("backend", "opentelemetry"),
            ebpf_stub=tr.get("ebpf_stub", True),
        )

    # Load embed config
    if "embed" in data:
        em = data["embed"]
        config.embed = EmbedConfig(
            provider=em.get("provider", "nomic"),
            model=em.get("model", "nomic-embed-code"),
            dimension=em.get("dimension", 768),
            hybrid_search=em.get("hybrid_search", True),
            vector_weight=em.get("vector_weight", 0.7),
            bm25_weight=em.get("bm25_weight", 0.3),
        )

    # Load health config
    if "health" in data:
        h = data["health"]
        config.health = HealthConfig(
            check_interval_seconds=h.get("check_interval_seconds", 60),
            rss_warn_mb=h.get("rss_warn_mb", 1024.0),
            rss_critical_mb=h.get("rss_critical_mb", 2048.0),
            fd_warn_pct=h.get("fd_warn_pct", 0.80),
            fd_critical_pct=h.get("fd_critical_pct", 0.95),
            thread_warn=h.get("thread_warn", 100),
            thread_critical=h.get("thread_critical", 200),
            cache_hit_min=h.get("cache_hit_min", 0.30),
            cache_hit_min_lookups=h.get("cache_hit_min_lookups", 100),
            pool_fail_rate_max=h.get("pool_fail_rate_max", 0.20),
            pool_fail_min_runs=h.get("pool_fail_min_runs", 10),
            zombie_warn=h.get("zombie_warn", 1),
            degraded_score=h.get("degraded_score", 0.6),
            critical_score=h.get("critical_score", 0.3),
        )

    # Load lifecycle config
    if "lifecycle" in data:
        lc = data["lifecycle"]
        config.lifecycle = LifecycleConfig(
            consecutive_critical_threshold=lc.get("consecutive_critical_threshold", 3),
            cooldown_seconds=lc.get("cooldown_seconds", 300.0),
            max_restarts=lc.get("max_restarts", 5),
            drain_timeout_seconds=lc.get("drain_timeout_seconds", 10),
            shutdown_timeout_seconds=lc.get("shutdown_timeout_seconds", 30),
            checkpoint_dir=lc.get("checkpoint_dir", ""),
        )

    # Load validation config
    if "validation" in data:
        v = data["validation"]
        config.validation = ValidationConfig(
            enabled=v.get("enabled", True),
            pytest_timeout=v.get("pytest_timeout", 300),
            ruff_timeout=v.get("ruff_timeout", 60),
            mypy_timeout=v.get("mypy_timeout", 120),
            auto_fix=v.get("auto_fix", False),
            max_fix_attempts=v.get("max_fix_attempts", 3),
            run_after_workflow=v.get("run_after_workflow", True),
        )

    # Load reproducibility config
    if "reproducibility" in data:
        r = data["reproducibility"]
        config.reproducibility = ReproducibilityConfig(
            enabled=r.get("enabled", False),
            replay_dir=r.get("replay_dir", ""),
            deterministic_mode=r.get("deterministic_mode", False),
            seed=r.get("seed", ""),
            force_temperature_zero=r.get("force_temperature_zero", False),
        )

    # Load sandbox.microvm config
    if "sandbox" in data:
        sbox = data["sandbox"]
        if "microvm" in sbox:
            vm = sbox["microvm"]
            config.sandbox_microvm = SandboxMicroVMConfig(
                enabled=vm.get("enabled", False),
                firecracker_binary=vm.get("firecracker_binary", "/usr/local/bin/firecracker"),
                kernel_image=vm.get("kernel_image", "/usr/share/beagle/vmlinux"),
                rootfs_image=vm.get("rootfs_image", "/usr/share/beagle/rootfs.ext4"),
                vcpu_count=vm.get("vcpu_count", 1),
                mem_size_mib=vm.get("mem_size_mib", 128),
                timeout_seconds=vm.get("timeout_seconds", 60),
                allow_fallback=vm.get("allow_fallback", False),
            )

    # Load A2A protocol config
    if "a2a" in data:
        a2a_data = data["a2a"]
        config.a2a = A2AConfig(
            enabled=a2a_data.get("enabled", True),
            require_signatures=a2a_data.get("require_signatures", False),
            keypair_path=a2a_data.get("keypair_path", ""),
            auto_generate_keypair=a2a_data.get("auto_generate_keypair", True),
        )

    # v13.19.4: Load the four orphan sections that the test
    # ``test_config_orphan_sections`` exercises. Each one wires up
    # a section-name → dataclass mapping that the loader previously
    # did not perform (the dataclasses were defined but the loader
    # was unaware of them, so loading a config with these sections
    # produced an empty WorkflowConfig and a "Unknown config section"
    # warning for each).
    if "ollama_cloud" in data:
        oc = data["ollama_cloud"]
        # v13.19.4: Expand ${ENV_VAR} placeholders so users can write
        # ``api_key = "${OLLAMA_CLOUD_API_KEY}"`` in config.toml and
        # the value comes from the environment. If the env var is
        # unset, fall back to "" so the secrets loader chain can take
        # over (env / secrets.yaml / prompt).
        api_key_raw = oc.get("api_key", "")
        if isinstance(api_key_raw, str) and api_key_raw.startswith("${"):
            env_name = api_key_raw[2:-1]
            api_key = os.environ.get(env_name, "")
        else:
            api_key = api_key_raw
        config.ollama_cloud = OllamaCloudConfig(
            endpoint=oc.get("endpoint", ""),  # no provider preset — set your own
            timeout_seconds=oc.get("timeout_seconds", 60),
            api_key=api_key,
            max_retries=oc.get("max_retries", 3),
            retry_backoff_seconds=oc.get("retry_backoff_seconds", 1.0),
        )

    if "workflows" in data:
        wf = data["workflows"]
        config.workflow_discovery = WorkflowDiscoveryConfig(
            search_paths=wf.get("search_paths", []),
            validate_on_load=wf.get("validate_on_load", True),
        )

    if "state" in data:
        st = data["state"]
        connection_string = st.get("connection_string", "")
        state_type = st.get("type", "memory")
        if (
            state_type == "postgresql"
            and connection_string
            and not connection_string.startswith(("postgresql://", "postgres://"))
        ):
            raise ValueError(
                f"state.connection_string must start with 'postgresql://' "
                f"or 'postgres://' (got: {connection_string!r})"
            )
        if state_type not in ("memory", "sqlite", "postgresql"):
            raise ValueError(f"state.type must be 'sqlite' or 'postgresql' (got: {state_type!r})")
        config.state = StateConfig(
            type=state_type,
            connection_string=connection_string,
            checkpoint_interval=st.get("checkpoint_interval", 10),
        )

    if "event_bus" in data:
        eb = data["event_bus"]
        ring_size = eb.get("ring_buffer_size", 1024)
        # v13.19.4: Power-of-two warning. The test
        # ``test_event_bus_non_power_of_two_warns`` requires that the
        # value still be the user-supplied non-power-of-two (12345) so
        # the loader surfaces the misconfiguration rather than silently
        # rewriting it. We only emit a warning log.
        if ring_size > 0 and (ring_size & (ring_size - 1)) != 0:
            import logging as _eblog

            _eblog.getLogger("Beagle.config").warning(
                "event_bus.ring_buffer_size=%d is not a power of two; "
                "this may cause reduced ring-buffer performance",
                ring_size,
            )
        config.event_bus = EventBusConfig(
            ring_buffer_size=ring_size,
            retention_seconds=eb.get("retention_seconds", 3600),
            max_buffer_bytes=eb.get("max_buffer_bytes", 10485760),
            callback_timeout_seconds=eb.get("callback_timeout_seconds", 5.0),
        )

    # SP-9 (beagle-spotless-phase2): four WorkflowConfig classes were defined
    # in schema.py and read by active code (hydration_node, subprocess_pool,
    # autodream) but had no loader branch and no config.toml section, so they
    # always ran on dataclass defaults. Add loader branches + TOML sections so
    # an operator can control each subsystem from configuration.

    # RAG query decomposition (read by core/hydration_node.py).
    if "decomposition" in data:
        dc = data["decomposition"]
        config.decomposition = DecompositionConfig(
            enabled=dc.get("enabled", True),
            max_subqueries=dc.get("max_subqueries", 2),
            min_query_length=dc.get("min_query_length", 20),
            merge_max_results=dc.get("merge_max_results", 10),
        )

    # Execution-history-driven model selection (read by utils/subprocess_pool.py
    # and utils/subprocess/pool_config.py).
    if "learned_routing" in data:
        lr = data["learned_routing"]
        config.learned_routing = LearnedRoutingConfig(
            enabled=lr.get("enabled", True),
            min_executions=lr.get("min_executions", 3),
            success_rate_weight=lr.get("success_rate_weight", 0.7),
            latency_weight=lr.get("latency_weight", 0.3),
            stale_after_hours=lr.get("stale_after_hours", 168),
            node_type_routing=lr.get("node_type_routing", True),
        )

    # Intelligent memory consolidation / AutoDream (read by memory/autodream.py).
    if "memory_consolidation" in data:
        mc = data["memory_consolidation"]
        config.memory_consolidation = MemoryConsolidationConfig(
            merge_enabled=mc.get("merge_enabled", True),
            merge_min_group_size=mc.get("merge_min_group_size", 2),
            merge_max_summary_parts=mc.get("merge_max_summary_parts", 5),
            prune_relevance_threshold=mc.get("prune_relevance_threshold", 2.0),
            prune_staleness_days=mc.get("prune_staleness_days", 30),
            prune_dedup_enabled=mc.get("prune_dedup_enabled", True),
        )

    # Token streaming with early termination (read by utils/subprocess_pool.py
    # and utils/subprocess/execution.py).
    if "streaming" in data:
        sc = data["streaming"]
        config.streaming = StreamingConfig(
            enabled=sc.get("enabled", True),
            early_termination=sc.get("early_termination", True),
            termination_pattern=sc.get("termination_pattern", r"</final_answer\s*>"),
            buffer_size=sc.get("buffer_size", 8192),
        )

    # v13.21: [complexity_routing] is read by config/model_resolver.py. We
    # acknowledge it here so the orphan-section guard recognizes a consumer.

    # v14.0: [connections] — outbound connection transport selection. Fixes
    # the pre-existing loader-branch gap (WorkflowConfig.connections had no
    # parser branch, so the orphan-guard test reported it unreachable).
    if "connections" in data:
        conn = data["connections"]
        config.connections = ConnectionsConfig(
            transport=conn.get("transport", config.connections.transport),
        )

    return config


# Every recognised top-level section of config.toml.
# <invariant>
#   This is the ONLY list of valid section names. It was function-local until
#   v1.0.0, so tests/test_config_validation.py kept a hand-copied duplicate
#   that had to be updated in lockstep — and wasn't: the copy was missing
#   [model_presets], so the test failed while the loader was correct.
#   Importers must reference this set rather than re-declare one.
# </invariant>
KNOWN_TOP_LEVEL = frozenset(
    {
        "orchestrator",
        "goose",
        "budget",
        "cache",
        "rate_limit",
        "mcp",
        "router",
        "models",
        "logging",
        "ensemble",  # Panel-of-experts ensemble configuration
        "embed",  # Embedding configuration
        "node_timeout",  # Per-node execution timeouts
        "timeouts",  # System-wide timeout configuration
        "rag",  # RAG subsystem configuration
        "pool",  # Subprocess pool configuration
        "context_threshold",  # Context window management thresholds
        "memory",  # Hierarchical memory configuration
        "security",
        "output",
        "circuit_breaker",
        "orpheus",
        "coord",  # Beacon ephemeral coordination store configuration
        "behavior",  # Agent behavioral configuration
        "mcp_auth",  # MCP authentication configuration
        "mcp_cors",  # MCP CORS configuration
        "paths",  # Dynamic path resolution
        "hardware",  # Hardware-aware performance optimization
        "tracing",  # Embedding model configuration
        "llm",  # LLM model defaults and provider settings
        "overrides",  # plan v2: top-level explicit overlay (N3) — one provider key
        "reflex_arc",  # Reflex arc fast-path configuration
        "langchain_bridges",  # LangChain ecosystem bridge configuration
        "health",  # Self-health monitoring configuration
        "lifecycle",  # Graceful self-restart configuration
        "validation",  # Output validation feedback loop
        "reproducibility",  # Deterministic reproducibility configuration
        "sandbox",  # MicroVM sandbox configuration (Firecracker)
        "a2a",  # Agent-to-Agent cryptographic authentication
        "bridges",  # CrewAI + AutoGen runtime bridge configuration
        "ollama_cloud",  # v13.19.4: Ollama Cloud endpoint + auth config
        "workflows",  # v13.19.4: workflow discovery search paths
        "state",  # v13.19.4: LangGraph state backend (sqlite / postgresql)
        "event_bus",  # v13.19.4: event bus ring-buffer config
        "complexity_routing",  # v13.21: query complexity → model routing
        "model_presets",  # v1.0.0: category → model SSOT, read via model_resolver.get_preset()
        "decomposition",  # SP-9: RAG query decomposition
        "learned_routing",  # SP-9: execution-history model selection
        "memory_consolidation",  # SP-9: AutoDream memory consolidation
        "streaming",  # SP-9: token streaming with early termination
        "runtime",  # B2: sub-agent execution runtime selection ([runtime].plugin)
        "connections",  # v14.0: outbound connection transport selection
        "system",  # v14.0: consolidated system settings
        "context_management",  # v14.0: consolidated context-management policy
        "inference",  # v14.0: consolidated inference-provider policy
        "ipc_and_tools",  # v14.0: consolidated IPC/tool-surface policy
        "security_and_sandbox",  # v14.0: consolidated security/sandbox policy
        "validation_gates",  # v14.0: consolidated validation-gate policy
    }
)


def _validate_config_keys(data: dict[str, Any], path: Path) -> list[str]:
    """Validate that all top-level keys in a TOML config are recognized.

    Logs warnings for unknown keys to catch typos like '[modelz]' vs '[models]'.

    Args:
        data: Parsed TOML data
        path: Path to config file (for error messages)

    Returns:
        List of warning messages (empty if all keys are valid)

    """
    import logging as _log

    logger = _log.getLogger("Beagle.config")
    warnings: list[str] = []

    for key in data:
        if key not in KNOWN_TOP_LEVEL:
            msg = f"Unknown config section '[{key}]' in {path} (check for typos)"
            logger.warning(msg)
            warnings.append(msg)

    return warnings


# Module-level cached config singleton
_cached_config: WorkflowConfig | None = None


def get_config() -> WorkflowConfig:
    """Get the full configuration with env overrides.

    Returns:
        Complete WorkflowConfig

    """
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    # Import here to avoid circular dependency at module level
    from .env_overrides import apply_env_overrides

    config = load_config()
    config = apply_env_overrides(config)
    _cached_config = config
    return config


def reset_config_cache() -> None:
    """Drop the cached :class:`WorkflowConfig` so the next call re-reads.

    :func:`get_config` bakes environment overrides into the cached object, so
    any test that mutates a ``BEAGLE_*`` variable must reset the cache or the
    stale value leaks into every later caller.

    Use this rather than assigning to the module global directly. The cache
    lives here, not in ``config.py`` — that module does ``from .loader import
    *``, which skips underscore-prefixed names, so
    ``beagle.config.config._cached_config = None`` merely creates an unused
    attribute and clears nothing. tests/test_memory_budget.py did exactly that
    and its "clean config cache" fixture was silently a no-op, which made
    TestTokenBudget order-dependent: whichever test ran after
    ``test_env_var_budget`` saw its 8000-token budget.

    Mirrors :func:`beagle.config._config_path.reset_config_path_cache` and
    :func:`beagle.config.agent_config.invalidate_cache`.
    """
    global _cached_config
    _cached_config = None
