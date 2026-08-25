"""Vulture whitelist for Beagle — documents why certain patterns are safe."""

# This file documents dead-code analysis decisions (Section 5.1).
# All 6 vulture findings were unused protocol parameters required by
# external frameworks (LangChain callbacks, signal handlers) but unused
# in our implementation. Fixed by prefixing with underscore.
#
# Decisions:
# - callback_handler.py: prompts -> _prompts, input_str -> _input_str,
#   inputs -> _inputs
# - retriever.py: run_manager -> _run_manager (two methods)
# - restart.py: frame -> _frame
#
# NO dead code removed. Every code path represents a competitive
# differentiator or production capability. Golden rule: NOTHING DELETED.
#
# Dynamic code patterns not flagged by vulture (safe):
# - Agent recipes loaded from YAML/metaprompts
# - Model resolver dynamic dispatch
# - Workflow node registration via decorator/registry
# - Subprocess pool dynamic worker creation
# - EventBus subscriber dispatch

# ── v1.0.9 (audit M3): whitelist the verified false positives so `make check`
#    can go green. Each entry names the fixture / re-export / probe it covers.
#    These are NOT dead code — they are pytest fixtures (parameters read as
#    unused locals), intentional package re-exports, and availability probes.
#    Vulture treats a bare name reference in the whitelist as "used".

# pytest fixture parameters (read as unused locals by vulture)
isolated_stores
stub_embedder
mock_subprocess_deps
s1_ok
c_locale

# Intentional package re-exports (consumed by external importers)
resolve_model_for_complex_task
build_hydration_instruction
tree_sitter_languages
_zstd
_ProbeMCP
