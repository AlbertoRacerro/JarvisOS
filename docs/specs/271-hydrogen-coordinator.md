# 271 — Gemini 4 Hydrogen as a fallback coordinator

Status: ready
Depends on: 268, 269

## Goal

When every other coordinator is cooling down, out of quota or unavailable, the persistent owner can still run the
mission on Gemini 4 Hydrogen through the agy CLI, which draws from the normal Gemini buckets. Hydrogen is never the
sticky coordinator and never displaces an available one, except when Gemini weekly quota would otherwise expire unused.

## Why

Point 3 of maintainer directive `L20261113192444-38eb` ("coordinator with S10"): at 15:36 UTC on 2026-11-13 every
coordinator was cooling down while the Gemini pool had 68 % of its five-hour window free. Measured the same day (agy
1.5.8, in the sandbox): `agy models` lists `gemini-4-hydrogen-low|medium|high|xhigh`; a Hydrogen prompt moved
`gemini-5h` and `gemini-weekly` and not `gemini-argon-wtu-weekly`. The Flash and Hydrogen workers spend the same pool,
so the directive limits the coordinator to "when the other coordinators are out, or when Gemini quota would otherwise
expire unused". Its quality on coordinator work is unknown; this spec admits it as a last resort, not by rank.

## Code facts checked on `origin/master` at `af697e31`

- `scripts/relay_argon.py::model_for` accepts only `argon` and `agy_opus`, each with its own default model
  (`scripts/relay_argon.py::MODEL`, `scripts/relay_argon.py::OPUS_MODEL`), environment override and allowed set
  (`scripts/relay_argon.py::MODELS`, `scripts/relay_argon.py::OPUS_MODELS`).
- `scripts/relay_argon.py::read_quota` requires `scripts/relay_argon.py::OPUS_BUCKETS` for `agy_opus` and
  `scripts/relay_argon.py::REQUIRED_BUCKETS` (both Gemini buckets plus the argon bucket) otherwise; the module-level
  CLI parser limits `--agent` to `argon` and `agy_opus`.
- `scripts/relay_continuity.py::AGENTS`, `scripts/relay_continuity.py::PREFERRED_AGENTS` and
  `scripts/relay_continuity.py::NATIVE_AGENTS` list the agents; `scripts/relay_continuity.py::FIELD_RULES` validates
  `agent` and `from_agent` against `AGENTS`.
- `scripts/relay_continuity.py::classify_argon_turn` checks the stream's model against `OPUS_MODELS` for `agy_opus`
  and `MODELS` otherwise.
- `scripts/relay_continuity.py::Supervisor.low_headroom` reads native agents through
  `scripts/relay_continuity.py::RelayAdapter.argon_limits`, whose windows carry `used_percent`, `resets_at` and
  `disabled` but not the bucket id.
- `scripts/relay_continuity.py::Supervisor._choose` orders candidates `current`, then `PREFERRED_AGENTS`, then `codex`
  (spec 269 reorders the replacements by measured room and makes the seed sticky).
- `scripts/worker_routing.py::SPEND_DOWN` (24 h) is the existing use-it-or-lose-it horizon for worker quota.

## Scope

In scope:

1. **Native agent `hydrogen`.** `relay_argon.py`: `HYDROGEN_MODEL = "gemini-4-hydrogen-medium"` (override
   `JARVISOS_AGY_HYDROGEN_MODEL`), `HYDROGEN_MODELS` = `gemini-4-hydrogen-{low,medium,high}`, `HYDROGEN_BUCKETS` =
   `gemini-5h` and `gemini-weekly` only; `model_for`, `read_quota` and the `--agent` choices accept `hydrogen` with
   these values. A `hydrogen` turn with an argon or Opus model, or an argon turn with a Hydrogen model, is refused.
2. **Supervisor membership.** `relay_continuity.py`: `hydrogen` joins `AGENTS` and `NATIVE_AGENTS` but **not**
   `PREFERRED_AGENTS`, so it never seeds a mission (269 sticky seed) and a mission after a Hydrogen-completed one
   is seeded with `PREFERRED`. `classify_argon_turn` checks Hydrogen turns against `HYDROGEN_MODELS`.
3. **Last resort.** `_choose` considers `hydrogen` after `codex`: replacements are the preferred agents by room
   (spec 269), then `codex`, then `hydrogen`; the "first available when all are low" fallback uses the same order.
4. **Spend-down exception.** `argon_limits` adds the bucket `id` to each window. When the `gemini-weekly` window of a
   usable Hydrogen reading resets within `SPEND_DOWN` (24 h, the worker router's horizon, imported or mirrored with a
   comment) and has at least `Limits.min_free_percent` free, `hydrogen` moves to the front of the replacements
   (still after a `current` agent that is available with room). It is recorded in the switch decision reason as
   `gemini_spend_down`.

Out of scope (binding non-goals):

- No change to the worker router or `agy_worker.py` (Hydrogen workers are admitted separately by the same directive),
  to cooldowns, `available`, the wait/hold path or mission contracts.
- No `xhigh` model, no new quota source, setting or dependency; no change to argon or Opus behaviour.
- No change to frozen conformance tests.

## Files likely touched

- `scripts/relay_argon.py`, `scripts/relay_continuity.py`
- `backend/tests/test_relay_argon_223.py`, `backend/tests/test_relay_continuity.py`

## Design constraints

- Implement on top of the merged 268 and 269 (same file); keep their behaviour intact.
- The native runner's fail-closed quota preflight applies to `hydrogen` unchanged: a turn without a fresh reading
  with room in both Gemini buckets does not run.
- Readings are the existing injectable `host.relay.argon_limits`; tests use fakes only and never call `agy`.

## Acceptance criteria

1. `model_for("hydrogen", env)` returns `gemini-4-hydrogen-medium` by default, accepts the three allowed overrides
   and refuses any other model; `read_quota(..., agent="hydrogen")` requires exactly the two Gemini buckets.
2. With `claude`, `argon` and `agy_opus` available with room, a new mission never runs on `hydrogen`, including after
   a mission that `hydrogen` completed (seeded with `PREFERRED`).
3. With every preferred agent and `codex` unavailable and `hydrogen` available with room, `_choose` returns
   `hydrogen`, records a switch and sends one `provider_switched` notification.
4. With a fake Hydrogen reading whose `gemini-weekly` window resets in 10 h with 40 % free, and the current agent
   unavailable, `hydrogen` is chosen ahead of an available `argon`; with the reset 3 days away, or 10 % free, it is not.
5. A Hydrogen turn whose stream reports an argon model is classified as a model mismatch, as for the other natives.
6. Existing relay suites stay green.

## Required tests

Focused offline tests with fake readings and clocks in `test_relay_argon_223.py` (criteria 1, 5) and
`test_relay_continuity.py` (criteria 2 to 4).

## Definition of done

The acceptance criteria hold; exact-head CI is green; a severe review and an independent second-session full-diff
review (supervisor code, high risk) pass on the implementation PR; the first live Hydrogen coordinator turn is
observed in the real environment (owner restart owed after merge); the registry row is set accordingly.
