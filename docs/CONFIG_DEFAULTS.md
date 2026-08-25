# Configurable Defaults

Every tunable default in Beagle lives in code — typed by the schema in
`src/beagle/config/schema.py`, valued in `src/beagle/config/defaults.py`,
and read at runtime — never embedded at a call site. Nothing configurable
ships in the package: all user-editable configuration lives under
`~/.config/beagle` (XDG), seeded by `beagle config init` from programmatic
defaults.

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
invariants, not preferences.

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

## Regression gates

- `scripts/check_hardcoded_defaults.py` — AST scan over `src/beagle`
  that reports every numeric/string literal default (kwarg defaults and
  SCREAMING_CASE module constants). Report-only since the classification
  registry was retired; pass an explicit `--registry FILE` to gate against
  an allowlist. Run `--selftest` to verify detection, `--json` for machine
  output.
- `tests/test_no_new_magic_values.py` — runs the scanner's selftest and a
  report-only scan inside pytest.
- `tests/test_config_defaults_parity.py` — pins the no-bundled-config
  contract: a fresh (empty) XDG config dir loads neutral, provider-neutral
  defaults, and `generate_default_config()` emits TOML that parses back to
  values matching the schema defaults (no drift between generator and
  schema).

## Adding or moving a default

Follow the four-edit checklist (one per edit, all required):

1. Typed field on the owning dataclass in `config/schema.py`.
2. Aggregate wiring where the loader builds it (`config/loader.py`).
3. Default value in `config/defaults.py`, equal to the previous hardcoded
   value (this is what `generate_default_config()` emits).
4. Call site reads config instead of the literal.
