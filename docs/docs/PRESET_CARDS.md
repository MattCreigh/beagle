# Preset Card System

The model-routing config uses a **fleet card** system: each card is a
self-contained TOML file defining an **entire fleet** — all 12 role presets and
all bundles for a single provider. To switch the fleet between providers
(e.g. provider A vs provider B), swap which card loads last.

## File layout

```text
beagle/
  config.toml                 # LIVE overlay — references a bundle by name
  providers.toml              # Provider registry (base_url, env_key, allowed_models)
  presets/                    # Fleet card directory
    _index.toml               # Optional: [meta].load_order controls which fleet wins
    fleet_provider_a.toml     # Complete fleet: all 12 roles + 3 bundles on provider A
    fleet_provider_b.toml     # Complete fleet: all 12 roles + 3 bundles on provider B
```

Each fleet card defines:

- **12 role presets** (`[presets.<role>]`) — primary deployment + fallbacks +
  strategy + temperature for each role (default, cheap, orchestration, coding,
  writing, synthesis, reasoning, fact_checking, deep_analysis, devops, meta, judge).
- **3 named bundles** (`[bundles.<name>]`) — ordered lists of role names +
  overrides (agentic_fleet, provider_b_default, lean_runner).

## How it works

The registry (`src/config/registry.py`) discovers every `*.toml` in `presets/`,
parses each, and merges them into one in-memory store. When two cards define the
same role (which is expected — both fleet cards define all 12 roles), the
**last-loaded card wins** (replace-wholesale, not field-merge). A WARNING is
logged naming the winning card.

### Load order & fleet switching

- If `_index.toml` declares `[meta].load_order`, listed cards load in that order.
  The **last card wins** — so put your active fleet last.
- Without `_index.toml`, cards load **alphabetically** (last alphabetically wins).

**To switch from provider A to provider B:**

1. Edit `presets/_index.toml` and swap the `load_order` so
   `fleet_provider_b.toml` is last.
2. Restart the orchestrator. The registry logs override WARNINGs (expected) and
   the provider B fleet is now active.
3. To revert: swap the order back.

### Fleet selection

The active bundle is selected by plain string name via the model-profile
configuration. Agent profiles continue to render preset placeholders from the
merged card store — unchanged.

### Back-compat

If `presets/` does **not** exist but a legacy `presets.toml` does, the registry
loads the single file as one "card" and behaves exactly as before.

## Adding a new fleet card (developer workflow)

**Scenario:** add a fleet card for a new provider.

1. Ensure the provider is registered in `providers.toml` with its `env_key`,
   `base_url`, and `allowed_models`.
2. Ensure the models are in `config.toml [models.allowed]` (the security
   perimeter — `allowlist.py` validates at boundary).
3. Create `presets/fleet_<provider>.toml` with all 12 role presets and 3
   bundles, all referencing `provider = "<provider>"` and your chosen models.
4. Add `fleet_<provider>.toml` to `presets/_index.toml` `load_order` (put it
   last to make it the active fleet, or before the current fleet to keep it
   available but inactive).
5. Restart the orchestrator. The registry validates all provider/role
   references and fails fast on any unknown provider or missing role.

## Validation

At load time, `registry.validate_cards()` (called from `reload_registry()`)
fails fast if:

- A role preset references a provider **not registered** in `providers.toml`.
- A bundle's `includes` references a role **not defined** by any card.

The model allowlist is enforced separately by `src/config/allowlist.py` against
`config.toml [models.allowed]` — a model must be allowlisted there before it
can be used at runtime.

## CLI

```bash
beagle config cards    # List fleet cards in load order with roles and bundles
```

## See also

- `src/config/_config_path.py` — `find_presets_dir()` / `find_preset_cards()`
- `src/config/registry.py` — `reload_registry()`, `validate_cards()`
- `plans/preset_card_system_v3.md` — the original design plan
