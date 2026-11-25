# 376 — VAULT-OVERLAY-1: Optional private vault overlay for protected dimensions and local-only items

Status: ready
Depends on: none

## Goal

The backend can load an optional private overlay named by `JARVISOS_VAULT_PATH`. It holds the operator's protected
dimensions, `local_only` items and protected propositions, kept outside both the repository and the pack. Protected
dimensions from the overlay are enforced by the search guard in the same way as the pack's own. `local_only` items and
protected propositions are loaded, validated and exposed through accessors for the later consult gate (spec to come).
The rest of the code can ask whether the overlay is loaded. When the variable is unset, everything behaves exactly as
today.

## Why

Directive `L20261125114331-7ab2` (storage split), item 1: specific material moves out of the pack into a vault that cloud
sessions never read. The guards must still see it, and they must fail closed when it is broken. The companion consult
design (`L20261125114331-2bd1` §3.2, §3.4, and the vault rule "Missing vault … consults that need `local_only` go to
`off`") needs these lists and this status as inputs. This slice only loads the lists and enforces the dimensions. It
adds no consult code.

## Code facts checked at `2aed215d`

- `backend/app/core/packs.py::read_pack` validates `[search_guard].protected_dimensions` entries (`value` > 0 and
  finite, `unit` in `KNOWN_SEARCH_GUARD_UNITS`, `tolerance_rel` in [0, 1)). It returns them as
  `backend/app/core/packs.py::ProtectedDimension` inside `backend/app/core/packs.py::SearchGuardConfig`.
- `backend/app/core/packs.py::_load` is an `lru_cache` keyed on the env value, and
  `backend/app/core/packs.py::load_packs` reads `JARVISOS_PACK_PATH`.
- `backend/app/core/packs.py::search_guard` merges the guard rules of every pack, taking the strictest thresholds.
- `backend/app/main.py::create_app` calls `load_packs()` at startup, so an invalid pack stops the app.
- `scripts/search_guard.py::evaluate_query` blocks when `scripts/search_guard.py::pack_is_ready` is false. It then
  checks pack terms, and for each pack runs `scripts/search_guard.py::_matches_exact_combination` and
  `scripts/search_guard.py::_matches_protected_dimension`. The dimension check requires one context-group term in the
  query when the config has groups.
- The search guard runs as a separate host unit. Its environment comes from
  `~/.config/jarvisos-hostops/search-guard.env` (`scripts/host_ops.py`, unit template `jarvisos-search-guard`).
- `backend/app/api/system.py` reports the loaded packs (`id`, `ui_labels`) in the system status.

## Scope

1. **Loader.** A new `backend/app/core/vault.py` defines `VAULT_PATH_ENV = "JARVISOS_VAULT_PATH"`, a frozen `Vault`
   dataclass and `load_vault() -> Vault | None`. The result is cached per env value, as `_load` does for packs. The
   variable names one directory, which must contain `jarvis_vault.toml` with these optional keys only:
   - `protected_dimensions`: a list of tables with the pack schema and validation, reusing `ProtectedDimension` and
     the same unit set;
   - `local_only`: a list of tables `{id, text}`;
   - `protected_propositions`: a list of tables `{id, text}`.

   Each `id` matches `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` and is unique within its list. Each `text` is a non-empty
   string of at most 2,000 characters. Any other key, or any malformed entry, raises `VaultError`. An unset or blank
   variable returns `None`.
2. **Accessors.** `vault_status() -> "absent" | "loaded"`, `vault_protected_dimensions()`, `local_only_items()` and
   `protected_propositions()`. The last two return tuples of `(id, text)`. All return empty tuples when the vault is
   absent.
3. **Startup.** `create_app` calls `load_vault()` after the packs. An invalid vault raises at startup. The log line is
   only `Vault overlay loaded (N dimensions, M local-only items, P propositions)`, with no path and no content.
4. **Search guard.** In `evaluate_query`, a set but unloadable vault blocks every query with a new reason
   `vault_load_error`, as `pack_load_error` does. That reason gets an Italian message in
   `backend/app/modules/ai/search_guard_summary.py::REASON_MESSAGES_IT`. After the per-pack checks, the vault's
   protected dimensions are checked with the merged config from `packs.search_guard()`: the union of the pack groups as
   context terms, and `allow_generic_ranges` from that merge. A hit blocks with reason `protected_dimension`, like a
   pack hit, and the log records no value.
5. **System status.** `GET` system status adds `"vault": "absent" | "loaded"` and nothing else about the vault.

Out of scope (binding non-goals):

- Any consult tool, judge, number-taint check or use of `local_only` and propositions beyond loading them (consult
  design step 1 and step 2 specs).
- Moving the data root, backups, sandbox mount guards and supervisor tooling (storage-split items 2–5, separate specs).
- Removing `protected_dimensions` from the pack schema: the pack keeps accepting them, and the operator moves the
  real values out by hand.
- Writing the real vault file. Operator content stays out of the repository.

## Files likely touched

- `backend/app/core/vault.py` (new)
- `backend/app/main.py`
- `backend/app/api/system.py`
- `scripts/search_guard.py`
- `backend/app/modules/ai/search_guard_summary.py`
- `backend/tests/fixtures/vaults/zephyr/jarvis_vault.toml` (new, fake values only)
- `backend/tests/test_vault_overlay_376.py` (new)
- `frontend/src/api/generated` only if the system status contract is generated (regenerate, never hand-edit)

## Design constraints

- **No vault content outside the process.** Values, texts and the vault path never appear in logs, events,
  exceptions shown to clients, API responses or the search-guard log. `VaultError` messages name the file and the key
  or entry index, never a value or text.
- **Fail closed.** If the variable is set and the vault cannot be read, startup fails and the search guard blocks.
  Neither ever falls back to "absent".
- The vault is read-only to the app: no write path and no API that returns its content.
- Pack behaviour, and every test that runs without the variable, is unchanged. CI runs with no vault.
- Standard library only (`tomllib`).

## Acceptance criteria

1. With the variable unset, `vault_status()` is `"absent"`, the accessors return empty tuples, startup succeeds and
   the existing search-guard tests pass unchanged.
2. With the fake vault, the accessors return its entries. A query naming a vault dimension within tolerance, together
   with a pack context term, is blocked as `protected_dimension`. The same value outside the tolerance, or a generic
   range that spans it, passes.
3. Each malformed vault raises `VaultError`, and the message contains no value or text from the file: unknown key,
   duplicate id, bad id, empty text, text over the limit, bad unit, non-positive value, or a missing file in a set
   directory.
4. With a set but broken vault, `evaluate_query` returns `(False, "vault_load_error", None)`, and `create_app`
   raises.
5. The system status contains `"vault"` with `"absent"` or `"loaded"`, and no path or content.
6. Lint, the focused tests and CI pass.

## Required tests

- `backend/tests/test_vault_overlay_376.py`: offline tests with the fake `zephyr` pack and a fake vault in
  `tmp_path` or the fixture. They cover loading, validation errors and their messages, cache isolation between env
  values, search-guard blocking and passing, `vault_load_error`, the startup failure, and the system status field.

## Definition of done

Merged with green CI on the exact head, after one severe coordinator review and an independent second review: the
slice gates outbound search, so it is high risk under `AGENTS.md`. Owed live activation, by the coordinator through
host ops: add `JARVISOS_VAULT_PATH` to the app's operator environment and to `search-guard.env` once the maintainer
has created the vault file. Until then the live status reports `"absent"`, which is correct.
