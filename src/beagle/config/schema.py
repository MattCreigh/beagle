"""Configuration dataclass schema for Goose Agentic Workflow.

All @dataclass class definitions for configuration objects.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..runtime.goose_cli import default_goose_binary
from .paths import get_workspace_root


def _default_model_from_presets() -> str:
    """Resolve the default model from config.toml ``[model_presets]``.

    Used as a ``default_factory`` so the value is read at instantiation
    rather than at class-definition time.

    v1.0.0: these fields were the literal ``"glm-5.1:cloud"`` while
    ``agent_config`` had moved to ``minimax-m3:cloud``. The two disagreed, so
    the effective default depended on which module answered first. Both now
    route through the single accessor.

    Imported lazily to keep ``schema`` free of a module-level dependency on
    ``model_resolver``.

    Returns:
        The configured default model name.

    """
    from .model_resolver import get_preset

    return get_preset("default")


# Preset categories composing the default panel-of-experts, in order. Names
# only — the concrete models come from config.toml [model_presets], so the
# panel is retuned by editing TOML, never this list.
_PANEL_PRESET_CATEGORIES = ("default", "orchestration", "coding", "deep_analysis", "writing")


def _default_panel_from_presets() -> list[str]:
    """Default ensemble panel, resolved from config.toml ``[model_presets]``.

    Only used when config.toml declares no ``[ensemble]`` table; when it does,
    ``loader.py`` overwrites the field outright.

    Returns:
        One model name per category in :data:`_PANEL_PRESET_CATEGORIES`,
        de-duplicated while preserving order (two categories may legitimately
        resolve to the same model).

    """
    from .model_resolver import get_preset

    seen: dict[str, None] = {}
    for category in _PANEL_PRESET_CATEGORIES:
        seen.setdefault(get_preset(category), None)
    return list(seen)


def _default_judge_from_presets() -> str:
    """Default ensemble judge, resolved from config.toml ``[model_presets]``.

    Returns:
        The configured judge model name.

    """
    from .model_resolver import get_preset

    return get_preset("judge")


@dataclass
class RuntimeConfig:
    """Configuration for the sub-agent execution runtime (axis 2).

    Selects which :class:`beagle.runtime.base.AgentRuntime` plugin Beagle
    uses to spawn sub-agents. The default is ``goose_cli`` (a local
    ``goose`` subprocess). ``http_agent`` selects the A2A remote runtime.
    """

    plugin: str = "goose_cli"


@dataclass
class OrchestratorConfig:
    """Configuration for the DAG orchestrator."""

    timeout_seconds: int = 300
    validation_timeout_seconds: int = 60
    max_retries: int = 3
    retry_delay: float = 5.0
    max_backoff: float = 60.0


@dataclass
class GooseConfig:
    """Configuration for Goose CLI."""

    # v13.22.3: the goose-binary resolver handles the env override, the
    # shutil.which("goose") lookup, and the .orig-symlink fallback.
    # The previous default was the literal
    # ``Path.home() / ".local/bin/goose.orig"`` which never resolved
    # on a clean install where the binary is named ``goose`` (no
    # suffix), so every workflow's subprocess call failed silently
    # and tripped the goose-subprocess circuit breaker.
    # v1.1.1 (B1a): resolve at instance time, not class-definition time,
    # so importing the schema on a machine without goose does not run the
    # resolver as a side effect of importing the module.
    binary_path: str = field(default_factory=default_goose_binary)
    default_model: str = field(default_factory=_default_model_from_presets)
    provider: str = "ollama_cloud"
    host: str = ""
    model_overrides: dict[str, str] = field(default_factory=dict)
    # v13.20.1: Per-model fallback chains — SSOT in config.toml under
    # [models.fallback_chains]. Keyed by primary model name; value is the
    # ordered list of fallbacks. Replaces the old config/models.py:MODEL_FALLBACK_CHAINS
    # dict; readers must go through config.goose.fallback_chains, not Python.
    fallback_chains: dict[str, list[str]] = field(default_factory=dict)
    # v13.20.1: Subprocess-pool default chain — tried when caller supplies
    # no model_override. SSOT in config.toml under [goose].default_pool_chain.
    # No provider presets ship with beagle — it works with most any
    # OpenAI-compatible API. Set your own chain here or in config.toml,
    # e.g.: ["your-primary-model", "your-fallback-model"]  (see README).
    default_pool_chain: list[str] = field(default_factory=list)
    # v13.19.4: Toggle for the Top-of-Mind doctrine injection into
    # Beagle-spawned subagents. Default True so doctrine delivery works
    # out of the box; can be disabled per-deployment (e.g., air-gapped
    # environments or strict-bypass test rigs).
    doctrine_inject_into_subagents: bool = True


@dataclass
class BudgetConfig:
    """Configuration for cost/budget management."""

    default_usd: float = 10.0
    warn_threshold: float = 0.8
    hard_limit_usd: float = 50.0


@dataclass
class CacheConfig:
    """Configuration for result caching."""

    enabled: bool = True
    ttl_hours: int = 24
    max_size_mb: int = 100
    memory_max_entries: int = 100


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    requests_per_minute: int = 60
    tokens_per_minute: int = 100000
    burst_multiplier: float = 1.5
    # v13.20.13 (R6.3): backoff knobs for the WorkflowRateLimiter
    # exponential-backoff-with-jitter pattern. Exposed as flat
    # attributes on the root config (not nested under rate_limit
    # because the loader already supports flat keys with
    # `rate_limiter_<name>` mapping via getattr). Defaults match
    # the historical in-code values so behaviour is preserved
    # for deployments without an explicit [rate_limiter] block.
    initial_backoff: float = 1.0  # seconds
    max_backoff: float = 120.0  # seconds (2 minutes)
    backoff_multiplier: float = 2.0
    jitter_factor: float = 0.25  # ±25% jitter


@dataclass
class LoggingConfig:
    """Configuration for logging."""

    level: str = "INFO"
    json_format: bool = False
    log_to_file: bool = True
    max_size_mb: int = 10
    backup_count: int = 5


@dataclass
class TimeoutConfig:
    """Configuration for operation timeouts across the system."""

    # Subprocess timeouts
    shell_command_seconds: int = 10
    analysis_seconds: int = 600  # 10 minutes for large codebases
    iteration_seconds: int = 120  # Per-iteration timeout
    shutdown_grace_seconds: int = 30

    # Goose subprocess timeouts
    goose_default_seconds: int = 300
    goose_max_seconds: int = 1800  # 30 min max

    # HTTP client timeouts
    http_connect_seconds: float = 5.0
    http_read_seconds: float = 30.0


@dataclass
class NodeTimeoutConfig:
    """Per-node execution timeouts (seconds).

    These defaults are tuned for the slowest model tier in the routing chain
    (Kimi writing-tier for synthesis, large models for verification). Synthesis
    in particular must scale with the breadth of the upstream DAG — a fan-out
    workflow like self-improvement feeds synthesis 13-17K tokens of accumulated
    context, which requires ~3-5 minutes of wall-clock time to consolidate
    into a fully-cited report. The previous 120s default was the root cause
    of v13.x synthesis timeouts (see audit: synthesis-writer 120s).
    """

    planning_seconds: int = 180
    execution_seconds: int = 180
    # v1.0.2 (P-fix3): 150 → 300. See config/defaults.py [node_timeout]
    # for the full rationale — the 150s verification budget was the root
    # cause of 'fact-checker: Timeout after 150s' surfacing as
    # completed_with_errors even when synthesis produced a valid artifact.
    verification_seconds: int = 300
    synthesis_seconds: int = 300


@dataclass
class PoolConfig:
    """Subprocess pool configuration."""

    max_workers: int = 8
    default_timeout_seconds: int = 300
    backoff_base: float = 2.0
    backoff_max: float = 60.0


@dataclass
class ContextThresholdConfig:
    """Context window management thresholds.

    Phase 4.4: hard_compact is a secondary fallback threshold that fires
    regardless of the primary trigger mechanism.  If should_compact (0.70)
    was missed — measurement error, skipped check, race — this catch-all
    forces compaction before the sovereignty threshold (0.80) is reached.

    hard_compact defaults to 0.78: 2% below HARD_SOVEREIGN (0.80) to leave
    headroom for the checkpoint-save-before-fold dance.
    """

    warning: float = 0.50
    pre_compact: float = 0.58  # v13.15.6: Beagle-fold-first zone
    compact: float = 0.70
    hard_compact: float = 0.78  # Phase 4.4: secondary fallback, fires regardless
    critical: float = 0.85
    max_tokens: int = 128000
    tokens_per_iteration: int = 8000
    # Phase 4.4: time-based watchdog — if no checkpoint occurs within this
    # many seconds and context is above the warning threshold, force compaction.
    watchdog_seconds: int = 600  # 10 minutes

    @property
    def effective_compact(self) -> float:
        """Return the effective compaction threshold.

        Honors GOOSE_AUTO_COMPACT_THRESHOLD env var override if set,
        otherwise returns the TOML-configured compact value.
        This is the single source of truth for compaction decisions.
        """
        import os

        with contextlib.suppress(ValueError):
            val = float(os.environ.get("GOOSE_AUTO_COMPACT_THRESHOLD", ""))
            if 0.0 < val < 1.0:
                return val
        return self.compact


@dataclass
class MemoryConfig:
    """Hierarchical memory configuration."""

    working_memory_ttl: int = 3600
    episodic_memory_max: int = 100
    index_token_budget: int = 2000
    index_prune_strategy: str = "oldest_first"  # oldest_first | relevance_weighted | hybrid


@dataclass
class MemoryConsolidationConfig:
    """Configuration for intelligent memory consolidation (AutoDream).

    Controls how AutoDream merges, prunes, and refreshes episodic
    memory during background consolidation cycles.
    """

    merge_enabled: bool = True  # Enable semantic merge of similar entries
    merge_min_group_size: int = 2  # Minimum entries to form a merge group
    merge_max_summary_parts: int = 5  # Max sentences in merged summary
    prune_relevance_threshold: float = 2.0  # Entries below this score are pruned
    prune_staleness_days: int = 30  # Prune entries older than this
    prune_dedup_enabled: bool = True  # Deduplicate near-identical entries


@dataclass
class EnsembleConfig:
    """Multi-model ensemble configuration."""

    # config.toml [ensemble] is the SSOT and loader.py overwrites all three
    # fields whenever that table is present. These defaults apply only when
    # it is absent, and they resolve from [model_presets] so even the
    # no-[ensemble] path stays TOML-driven.
    # v1.0.0: the previous literals were ["glm-5.1:cloud", "glm-5:cloud",
    # "minimax-m2.7:cloud", "deepseek-v3.2"] with judge "glm-5:cloud" — three
    # of those four were retired upstream and none of the three are in
    # [models.allowed], so this fallback could only ever raise
    # ModelNotAllowedError.
    panel_models: list[str] = field(default_factory=_default_panel_from_presets)
    judge_model: str = field(default_factory=_default_judge_from_presets)
    timeout_per_model: int = 120


@dataclass
class PathsConfig:
    """Configuration for dynamic path resolution.

    Replaces hardcoded paths throughout the codebase with configurable values.
    Environment variables take precedence.
    """

    workspace_root: str = str(get_workspace_root())
    # v13.22.3: route through the centralised goose-binary resolver
    # helper so the env override, shutil.which("goose"), and the
    # .orig-symlink fallback all work the same way here as in the
    # GooseConfig.binary_path above.
    goose_bin: str = field(default_factory=default_goose_binary)
    venv_root: str = os.environ.get("BEAGLE_VENV_ROOT", "")
    data_root: str = os.environ.get("BEAGLE_DATA_ROOT", str(Path.home() / ".beagle"))
    # v13.22.3: also delegates to the goose-binary resolver for the
    # ``PathsConfig.goose_bin`` mirror, so the schema-level and
    # paths-level views agree. (Shutil.which handles the same chain
    # inline here; the centralised helper is the canonical
    # implementation; the duplicate is kept for clarity and to
    # avoid a circular import from a future config layer.)


@dataclass
class MCPAuthConfig:
    """Configuration for MCP server authentication.

    SECURITY: HTTP transport requires explicit opt-in AND valid tokens.
    No default fallback to insecure mode.
    """

    enabled: bool = True
    tokens: list[str] = field(default_factory=list)
    require_https: bool = True
    bind_address: str = "127.0.0.1"  # Loopback only


@dataclass
class MCPCORSConfig:
    """CORS configuration for MCP HTTP transport.

    SECURITY: No wildcard origins allowed. Must explicitly list allowed origins.
    """

    allowed_origins: list[str] = field(default_factory=list)  # EMPTY = deny all
    allowed_methods: list[str] = field(default_factory=lambda: ["GET", "POST"])
    allowed_headers: list[str] = field(default_factory=lambda: ["Authorization", "Content-Type"])


@dataclass
class BehaviorConfig:
    """Behavioral configuration for agent file operations.

    Controls whether missing files are auto-created during workflow execution,
    preventing FileNotFoundError crashes when expected files don't exist.
    """

    auto_create_missing_files: bool = True
    safe_file_operations: bool = True


@dataclass
class HardwareConfig:
    """Hardware-aware performance optimization configuration.

    MSI MPG Z390I GAMING EDGE AC (Intel i7-9700K 8c/8t, 16GB RAM, 931GB NVMe + 5.5TB HDD + 3.6TB SSD).
    """

    ramdisk_enabled: bool = True
    ramdisk_path: str = "/mnt/beagle_rag_staging"
    ramdisk_size_mb: int = 6144
    ssd_write_saving_log: bool = True
    dynamic_concurrency: bool = True
    concurrency_min: int = 2
    concurrency_max: int = 6
    cpu_high_threshold: float = 80.0
    cpu_low_threshold: float = 30.0
    warm_workers_enabled: bool = True
    warm_worker_count: int = 2
    incremental_ingest: bool = True
    zram_enabled: bool = False
    zram_size_mb: int = 8192


@dataclass
class TracingConfig:
    """Tracing backend configuration."""

    backend: str = "opentelemetry"  # "opentelemetry" or "ebpf" (experimental)
    ebpf_stub: bool = True


@dataclass
class SecurityConfig:
    """Security configuration."""

    max_query_length: int = 50000


@dataclass
class OutputConfig:
    """Output configuration."""

    truncation_threshold: int = 40000


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""

    max_circuits: int = 100


@dataclass
class EventBusConfig:
    """Event bus ring-buffer configuration.

    v13.19.4: Added to make event_bus config section schema-validated.
    Ring buffer size MUST be a power of two (load_config enforces this
    at validation time, emitting a warning and falling back to 1024
    if not).
    """

    ring_buffer_size: int = 1024
    retention_seconds: int = 3600
    max_buffer_bytes: int = 10485760  # 10 MiB
    callback_timeout_seconds: float = 5.0


@dataclass
class OllamaCloudConfig:
    """OpenAI-compatible provider endpoint configuration.

    Despite the historical section name, this works with MOST ANY API that
    exposes an OpenAI-compatible chat/completions surface (Ollama, vLLM,
    LiteLLM, OpenAI, llama.cpp server, ...). No endpoint is preset — you
    choose the provider explicitly in ~/.config/beagle config.toml.
    """

    endpoint: str = ""  # e.g. https://your-provider.example/v1  (see README)
    timeout_seconds: int = 60
    api_key: str = ""
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0


@dataclass
class WorkflowDiscoveryConfig:
    """Workflow file discovery configuration.

    v13.19.4: Added to make the [workflows] section schema-validated.
    """

    search_paths: list[str] = field(default_factory=list)
    validate_on_load: bool = True


@dataclass
class StateConfig:
    """LangGraph state backend configuration.

    v13.19.4: Added to make the [state] section schema-validated.
    """

    type: str = "memory"  # "memory" | "sqlite" | "postgresql"
    connection_string: str = ""
    checkpoint_interval: int = 10


@dataclass
class OrpheusConfig:
    """Orpheus and agent interactions configuration."""

    default_max_agent_calls: int = 5
    max_cvcp_attempts: int = 3
    grpo_timeout_seconds: int = 300
    ring_dir: str = "/run/orpheus_ring"  # Ring buffer directory for IPC
    transport: str = "unix_socket"  # "unix_socket" | "http_sse"
    create_rings_on_startup: bool = True  # Auto-create rings at startup


@dataclass
class CoordConfig:
    """Beacon ephemeral coordination store configuration.

    See plans/beagle-beacon-coordination.xml WP-1. Beacon is a per-working-
    directory, JIT-spawned fakeredis store behind a unix socket, with an
    orpheus-ring fast path for fire-and-forget writes.
    """

    enabled: bool = True
    heartbeat_interval_s: int = 5
    agent_ttl_s: int = 15
    grace_ttl_s: int = 20
    lock_ttl_s: int = 120
    event_log_maxlen: int = 500
    connect_timeout_s: float = 2.0
    probe_timeout_s: float = 1.0  # read-only roster/liveness probes fail fast
    watch_poll_interval_s: float = 2.0  # `coord watch` refresh period
    ring_slot_size: int = 4096  # measured at this size (M-1)
    ring_slot_count: int = 64  # 266 KB per agent (M-6)
    ring_enabled: bool = True  # False forces every write onto the socket
    archive_max_bytes: int = 1073741824  # audit_reader contract (D-07)
    archive_max_files: int = 30  # audit_reader contract (D-07)
    rehydrate_on_spawn: bool = False  # never resurrect dead agents by default
    socket_mode: str = "0600"
    journal_enabled: bool = True  # D-12 write-behind durability
    journal_fsync_interval_s: float = 2.0  # NEVER fsync per mutation (D-12)
    journal_replay_on_spawn: bool = True  # rebuild the board; never the roster (I-6)
    channels_enabled: bool = True  # D-09 peer rendezvous
    channel_slot_size: int = 16384  # peer messages carry payloads, not telemetry
    channel_slot_count: int = 32  # 512 KB per ring (M-14)
    channel_offer_ttl_s: int = 30  # an unaccepted offer is GC'd (D-11)
    max_channels_per_agent: int = 16  # bounds tmpfs at 16 MB/agent (M-14)
    backend: str = "fakeredis_unix"  # D-B3; key of beacon.backends.REGISTRY
    backend_options: dict[str, str] = field(default_factory=dict)  # passed to the driver verbatim


@dataclass
class RAGConfig:
    """Configuration for the RAG subsystem.

    WP-5 M3: ``turboquant_sidecar`` gates the TurboQuant sidecar writer/loader.
    Previously the sidecar was gated by a non-existent ``get_config_value``
    that always defaulted to True, so an operator could not switch it off.
    """

    turboquant_sidecar: bool = True


@dataclass
class MCPConfig:
    """Configuration for the MCP RAG subsystem."""

    rag_server_binary: str = "python3"
    rag_server_script: str = "infrastructure/mcp_rag_server.py"
    transport: str = "stdio"
    knowledge_dir: str = "cache/knowledge_graph"
    read_only_runtime: bool = True
    max_vector_results: int = 10
    max_graph_hops: int = 3
    max_graph_results: int = 20


@dataclass
class LLMConfig:
    """Global LLM defaults — fallback when agents.toml is missing or incomplete.

    All models run via Ollama Cloud (OpenAI-compatible API).
    Per-agent overrides belong in beagle/config/agents.toml.
    """

    default_provider: str = "ollama_cloud"
    default_model: str = field(default_factory=_default_model_from_presets)
    # Retired upstream 2026-07-15 (HTTP 410); default must stay a live,
    # allowlisted model. config.toml [llm].cheap_model is the SSOT.
    cheap_model: str = "gemma4:31b-cloud"
    cheap_provider: str = "ollama_cloud"


@dataclass
class EmbedConfig:
    """Embedding model configuration."""

    provider: str = "nomic"
    model: str = "nomic-embed-code"
    dimension: int = 768
    hybrid_search: bool = True
    vector_weight: float = 0.7
    bm25_weight: float = 0.3
    # Batch/pacing knobs for the local Ollama embedding runner (SSOT — the
    # no-new-magic-values gate forbids literals at call sites). batch_size
    # caps texts per /api/embed call; inter_batch_pause_s sleeps between
    # batches so a large ingest cannot saturate a shared CPU-only host.
    batch_size: int = 32
    inter_batch_pause_s: float = 0.0


@dataclass
class HealthConfig:
    """Configuration for self-health monitoring.

    Controls thresholds for health score calculation and the
    interval between periodic checks in the dark factory model.
    """

    check_interval_seconds: int = 60
    rss_warn_mb: float = 1024.0
    rss_critical_mb: float = 2048.0
    fd_warn_pct: float = 0.80
    fd_critical_pct: float = 0.95
    thread_warn: int = 100
    thread_critical: int = 200
    cache_hit_min: float = 0.30
    cache_hit_min_lookups: int = 100
    pool_fail_rate_max: float = 0.20
    pool_fail_min_runs: int = 10
    zombie_warn: int = 1
    degraded_score: float = 0.6
    critical_score: float = 0.3


@dataclass
class LifecycleConfig:
    """Configuration for graceful self-restart lifecycle management.

    Controls when Beagle should checkpoint its state, shutdown
    subsystems, and re-exec the process to recover from degraded health.
    """

    consecutive_critical_threshold: int = 3  # Health criticals before restart
    cooldown_seconds: float = 300.0  # Min seconds between restarts
    max_restarts: int = 5  # Max restarts before giving up
    drain_timeout_seconds: int = 10  # Max wait for subprocess drain
    shutdown_timeout_seconds: int = 30  # Total shutdown time budget
    # v1.0.2: empty = "resolve via checkpointer.get_checkpoint_dir()"
    # (BEAGLE_CHECKPOINT_DIR env, else state.connection_string anchored on
    # data_root, else <data_root>/checkpoints). The old ".beagle/checkpoints"
    # claimed to be "relative to workspace root" — i.e. inside the package
    # install tree — and no runtime caller ever honoured it anyway.
    checkpoint_dir: str = ""


@dataclass
class ValidationConfig:
    """Configuration for output validation feedback loop.

    Controls automated testing, linting, and type-checking after
    workflows produce code, feeding results back as structured findings.
    """

    enabled: bool = True
    pytest_timeout: int = 300
    ruff_timeout: int = 60
    mypy_timeout: int = 120
    auto_fix: bool = False
    max_fix_attempts: int = 3
    run_after_workflow: bool = True  # Auto-run after every write-mode workflow


@dataclass
class ReproducibilityConfig:
    """Configuration for deterministic reproducibility of workflow executions.

    Controls whether workflow inputs are recorded for replay, where
    manifests are stored, and whether deterministic mode is forced.
    """

    enabled: bool = False  # Record inputs for replay by default
    # v1.0.2: empty = "resolve via reproducibility.recorder.DEFAULT_REPLAY_DIR"
    # (BEAGLE_REPLAY_DIR env override, else <data_root>/replays). The old
    # default ".beagle/replays" was CWD-relative, so manifests landed wherever
    # the process happened to start, and startup/health_check.py resolved it
    # against workspace_root — i.e. mkdir'd it inside the package install tree.
    # An absolute path here still wins; a relative one is resolved against
    # data_root, never the package dir.
    replay_dir: str = ""  # Where manifests are stored
    deterministic_mode: bool = False  # Force deterministic mode globally
    seed: str = ""  # Fixed seed (empty = auto-generate)
    force_temperature_zero: bool = False  # Override all model temperatures to 0


@dataclass
class SandboxMicroVMConfig:
    """Configuration for MicroVM sandbox (Firecracker).

    Requires Firecracker binary + KVM kernel/rootfs images.
    Disabled by default — enable after running scripts/setup_firecracker.py.
    """

    enabled: bool = False
    firecracker_binary: str = "/usr/local/bin/firecracker"
    kernel_image: str = "/usr/share/beagle/vmlinux"
    rootfs_image: str = "/usr/share/beagle/rootfs.ext4"
    vcpu_count: int = 1
    mem_size_mib: int = 128
    timeout_seconds: int = 60
    allow_fallback: bool = False  # Deny-by-default: refuse subprocess degrade


@dataclass
class DecompositionConfig:
    """Configuration for RAG query decomposition.

    When enabled, complex multi-part queries are split into focused
    sub-queries for better RAG recall, then results are merged and
    deduplicated.
    """

    enabled: bool = True  # Master switch — disable to use single query only
    max_subqueries: int = 2  # Maximum sub-queries per decomposition
    min_query_length: int = 20  # Only decompose queries longer than this
    merge_max_results: int = 10  # Max results after merge & dedup


@dataclass
class LearnedRoutingConfig:
    """Configuration for execution-history-driven model selection.

    When enabled, the subprocess pool reorders its fallback chain
    based on accumulated success/latency data in model_performance.
    """

    enabled: bool = True  # Master switch — disable to use static chains
    min_executions: int = 3  # Minimum runs before a model is re-ranked
    success_rate_weight: float = 0.7  # Weight for success_rate vs latency
    latency_weight: float = 0.3  # Weight for latency component
    stale_after_hours: int = 168  # Discard rankings older than 7 days
    node_type_routing: bool = True  # Route per node type vs global


@dataclass
class StreamingConfig:
    """Configuration for token streaming with early termination.

    When enabled, subprocess stdout is read line-by-line instead of
    via process.communicate().  Once </final_answer> is detected, the
    subprocess is terminated early — saving ~10-30% wall-clock time.
    """

    enabled: bool = True  # Enable streaming output reading
    early_termination: bool = True  # Stop reading after </final_answer>
    termination_pattern: str = r"</final_answer\s*>"  # Regex for detection
    buffer_size: int = 8192  # Read buffer size in bytes


@dataclass
class A2AConfig:
    """Configuration for Agent-to-Agent cryptographic authentication.

    Ed25519/HMAC signing for inter-agent request verification.
    """

    enabled: bool = True
    require_signatures: bool = False  # Strict mode: reject unsigned messages
    keypair_path: str = ""  # Path to HMAC secret (empty = auto-generate)
    auto_generate_keypair: bool = True


@dataclass
class ConnectionsConfig:
    """Outbound connection transport selection.

    The default is the built-in HTTP transport. Proprietary alternatives
    (e.g. ``orpheus`` — FlatBuffers over ring buffers, from the separately
    licensed beagle-orpheus wheel) are auto-DETECTED once installed but are
    only used when explicitly named here (or via ``$BEAGLE_TRANSPORT``).
    """

    transport: str = "http"  # "http" | installed plugin name


# ---------------------------------------------------------------------------
# v14.0: Six typed SSOT sections (Beagle Configuration Ecosystem Rebuild).
# These dataclasses back the spec's six consolidated sections. They are
# ADDITIVE — the oracle-critical sections ([goose], [models], [llm], [budget],
# [mcp], [paths], [health], [sandbox.microvm], …) remain the runtime SSOT for
# their respective subsystems. These new sections provide the typed,
# consolidated spec view over the same operational knobs.
# ---------------------------------------------------------------------------


@dataclass
class SystemConfig:
    """Consolidated system-level settings (v14.0 [system])."""

    workspace_root: str = ""
    data_root: str = "~/.beagle"
    log_level: str = "INFO"
    max_query_length: int = 50000
    budget_usd_default: float = 10.0
    budget_usd_hard_cap: float = 50.0


@dataclass
class ContextManagementConfig:
    """Consolidated context-management policy (v14.0 [context_management])."""

    pre_compact_threshold: float = 0.58
    compaction_engine: str = "turboquant_3bit"
    auto_dream_enabled: bool = True
    skip_tools_regex: str = (
        "beagle_session_bootstrap|report_context_usage|check_and_fold_context"
    )


@dataclass
class InferenceConfig:
    """Consolidated inference-provider policy (v14.0 [inference])."""

    provider_registry: str = "~/.config/beagle/beagle_inference_config/providers.toml"
    active_fleet_card: str = "fleet_ollama_cloud"
    allowlist_strict: bool = True
    fallback_budget_hops: int = 3


@dataclass
class IpcAndToolsConfig:
    """Consolidated IPC/tool-surface policy (v14.0 [ipc_and_tools])."""

    orpheus_ring_path: str = "/run/orpheus/nexus"
    ghost_vault_socket: str = "/run/server_1/orpheus/ghost.sock"
    mcp_transport: str = "stdio"
    tool_registry_path: str = "~/.config/beagle/style_guides/guides/03_tool_registry.toml"


@dataclass
class SecurityAndSandboxConfig:
    """Consolidated security/sandbox policy (v14.0 [security_and_sandbox])."""

    fail_closed_firewall: bool = True
    secret_scrubbing: bool = True
    microvm_enabled: bool = False
    microvm_deny_fallback: bool = True


@dataclass
class ValidationGatesConfig:
    """Consolidated validation-gate policy (v14.0 [validation_gates])."""

    qa_gate_binary: str = "/opt/Projects/bin/qup"
    test_runner_binary: str = "/opt/Projects/bin/tup"
    max_cvcp_revisions: int = 3


@dataclass
class WorkflowConfig:
    """Combined configuration for workflow execution."""

    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    goose: GooseConfig = field(default_factory=GooseConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    node_timeout: NodeTimeoutConfig = field(default_factory=NodeTimeoutConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    context_threshold: ContextThresholdConfig = field(default_factory=ContextThresholdConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    memory_consolidation: MemoryConsolidationConfig = field(
        default_factory=MemoryConsolidationConfig
    )
    ensemble: EnsembleConfig = field(default_factory=EnsembleConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    orpheus: OrpheusConfig = field(default_factory=OrpheusConfig)
    coord: CoordConfig = field(default_factory=CoordConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    mcp_auth: MCPAuthConfig = field(default_factory=MCPAuthConfig)
    connections: ConnectionsConfig = field(default_factory=ConnectionsConfig)
    mcp_cors: MCPCORSConfig = field(default_factory=MCPCORSConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    reproducibility: ReproducibilityConfig = field(default_factory=ReproducibilityConfig)
    sandbox_microvm: SandboxMicroVMConfig = field(default_factory=SandboxMicroVMConfig)
    a2a: A2AConfig = field(default_factory=A2AConfig)
    learned_routing: LearnedRoutingConfig = field(default_factory=LearnedRoutingConfig)
    decomposition: DecompositionConfig = field(default_factory=DecompositionConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    # v13.19.4: Defaults for the four sections the orphan-sections test
    # exercises. Without these defaults, `cfg.ollama_cloud` is missing
    # when the section is absent from config.toml — the test asserts
    # that a default-constructed WorkflowConfig has these attributes.
    ollama_cloud: OllamaCloudConfig = field(default_factory=OllamaCloudConfig)
    workflow_discovery: WorkflowDiscoveryConfig = field(default_factory=WorkflowDiscoveryConfig)
    state: StateConfig = field(default_factory=StateConfig)
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)
    # v14.0: Six typed SSOT sections (Beagle Configuration Ecosystem Rebuild).
    system: SystemConfig = field(default_factory=SystemConfig)
    context_management: ContextManagementConfig = field(default_factory=ContextManagementConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    ipc_and_tools: IpcAndToolsConfig = field(default_factory=IpcAndToolsConfig)
    security_and_sandbox: SecurityAndSandboxConfig = field(
        default_factory=SecurityAndSandboxConfig
    )
    validation_gates: ValidationGatesConfig = field(default_factory=ValidationGatesConfig)


# ---------------------------------------------------------------------------
# Registry data model (plan v2, B5: dataclasses to match this dataclass module;
# N1: `model` is the BARE model string — fqid is derived, never stored).
#
# SP-7: the Provider / ModelDeployment / ModelPreset / PresetBundle types live
# in config/model_types.py (a leaf module). They are imported at the top of this
# file and re-exported via the module namespace for backward compatibility.
# ---------------------------------------------------------------------------
