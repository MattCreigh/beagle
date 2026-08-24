# Configurable Defaults

Every tunable default in Beagle lives in config, typed by the schema in
`src/beagle/config/schema.py`, valued in
`src/beagle/default_config/beagle_core_config/config.toml`, and read at
runtime — never embedded at a call site. This is enforced by CI; see the
"Regression gates" section below.

Governing plan: `plans/beagle-config-defaults-abstraction.xml`.

## What counts as a tunable

```text
is_tunable(v) = is_numeric_or_string_literal(v)
                AND affects_duration_or_capacity_or_selection(v)
                AND NOT is_security_floor(v)
                AND NOT is_protocol_contract(v)
                AND NOT is_secret_or_crypto_parameter(v)

where:
  is_security_floor             permission modes, secret patterns, sandbox limits
  is_protocol_contract          frozen return types / op sets
  is_secret_or_crypto_parameter key lengths, hash widths, algorithm ids
```

Security floors and protocol contracts stay as code on purpose: they are
invariants, not preferences. The classification registry records the reason
for every such constant.

## The coordination defaults (`[coord]`)

| Key | Default | Read by |
|-----|---------|---------|
| `probe_timeout_s` | `1.0` | `beagle coord status/watch` roster probes |
| `watch_poll_interval_s` | `2.0` | `beagle coord watch` refresh loop |
| `connect_timeout_s` | `2.0` | store attach path |
| `archive_max_bytes` | `1073741824` | journal rotation threshold |
| `archive_max_files` | `30` | journal rotation count |
| `journal_fsync_interval_s` | `2.0` | write-behind fsync timer |

The journal constructor takes its durability values as REQUIRED keywords;
whoever wires a `Journal` passes them from `[coord]`. A default there would
create a second source of truth.

## Tranche-2 refinement (2026-08-22)

Every former file-level `pending ['*']` row is now SYMBOL-level: 446
findings across 133 files classified (195 `invariant`, 251 `pending`
tunables awaiting a `[defaults.*]` move tranche). New symbols in any
classified file now fail the gate — the allowlist is tight.

## Regression gates

- `scripts/check_hardcoded_defaults.py` — AST scan over `src/beagle`;
  every numeric/string literal default must be covered by a row in
  `src/beagle/config/defaults_registry.toml` (statuses: `moved`,
  `invariant`, `derived`, `pending`). Unlisted findings fail with exit 1.
  Run `--selftest` to verify detection, `--json` for machine output.
- `tests/test_no_new_magic_values.py` — runs the gate inside pytest.
- `tests/test_config_defaults_parity.py` — shipped-TOML values must equal
  schema defaults for `[coord]`; extend section by section as later tranches
  migrate.

## Adding or moving a default

Follow the four-edit checklist (one per edit, all required):

1. Typed field on the owning dataclass in `config/schema.py`.
2. Aggregate wiring where the loader builds it (`config/loader.py`).
3. Shipped value in `default_config/beagle_core_config/config.toml`,
   equal to the previous hardcoded value.
4. Call site reads config instead of the literal.

Then reclassify the literal in `defaults_registry.toml`
(`pending` -> `moved`) so the gate stays green.
