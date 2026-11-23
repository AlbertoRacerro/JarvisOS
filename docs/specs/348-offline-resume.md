# 348 — OFFLINE-RESUME-1: Offline host wait without provider strikes and plain-Italian resume notice

Status: ready
Depends on: 265

## Goal

When the host loses internet connectivity (for example a disconnected phone hotspot), the continuity supervisor must wait cleanly without counting provider failures, applying provider cooldowns, or tripping a hold; as soon as connectivity returns, it resumes automatically and posts one plain-Italian Slack line reporting the offline window (`"ero offline da <inizio> a <fine>, riprendo"`).

## Why

Directive `L20261123232512-a0c3` (item 1): comparing Windows Wi-Fi events against the owner log showed the 2026-11-22/23 network probe failures were simply the PC being offline (hotspot disconnected 19:05–22:27). No WSL/DNS workaround is needed; instead, the supervisor must treat an offline host as a clean pause rather than provider/harness failures that accumulate cooldown strikes or trip a hold.

## Code facts checked at `20bdbfdc`

- `scripts/relay_continuity_supervisor.py::default_reachable` checks TCP reachability to `github.com:443` with a 5 s timeout.
- `scripts/relay_continuity_supervisor.py::Supervisor._pre_launch_probe` sets `self.state["network_outage"] = True` and returns `"waiting"` when `reachable()` is `False`, and clears `self.state["network_outage"] = False` when `reachable()` returns `True`, without recording the outage start timestamp or posting a resume notice.
- `scripts/relay_continuity_supervisor.py::Supervisor.set_cooldown` increments `transient_<agent>` and `estimated_<agent>` counters on failed turns regardless of whether the host lost network connectivity mid-turn.
- `scripts/relay_continuity_supervisor.py::Supervisor._decide` routes `transient` / `unknown` failures into cooldowns and failure counters that can trip a hold.
- `scripts/relay_continuity_notify.py::Notifier.send_text` posts plain-Italian messages to Slack via bot/webhook with deduplication.

## Scope

In scope:
- **Offline window tracking in `_pre_launch_probe`**:
  - When `reachable()` returns `False`: if `network_outage` was not already `True`, set `self.state["network_outage"] = True` and `self.state["network_outage_since"] = iso(self.host.now())`, record the finding once, save state, and return `"waiting"`. Never increment provider failure counters, never set a cooldown, and never trip a hold.
  - When `reachable()` returns `True` and `self.state.get("network_outage")` is `True`: record `ended_at = self.host.now()`, format the offline window in local/readable `HH:MM` (or `YYYY-MM-DD HH:MM` if spanning dates), clear `network_outage = False` and `network_outage_since = None`, save state, and emit one plain-Italian Slack notification via `Notifier.send_text`:
    `"Ero offline da <inizio> a <fine>, riprendo."` (no spec numbers, PR numbers, SHAs, or internal IDs).
- **Mid-turn disconnect protection in `_decide` / `set_cooldown`**:
  - When a turn finishes with an unsuccessful network-like outcome (`transient`, `unknown`, `timeout`) and a fresh `reachable()` check returns `False`: do NOT call `set_cooldown`, do NOT increment `transient_<agent>` or consecutive-failure / hold counters; instead enter `network_outage = True` (setting `network_outage_since` if unset), set `phase = "waiting"`, save state, and return `"waiting"` so the supervisor resumes automatically once connectivity returns.

Out of scope (binding non-goals):
- No Windows or WSL networking/DNS configuration changes.
- No changes to authentication/quota hold semantics when the host IS online (`reachable() == True`).

## Files likely touched

- `scripts/relay_continuity_supervisor.py`
- `backend/tests/test_relay_continuity.py` (or new `backend/tests/test_offline_resume_348.py`)

## Design constraints

- Standard library only; no external dependencies.
- High risk (supervisor state machine): requires coordinator full-diff review, independent second-session full-diff review, and green CI.
- Plain-Italian Slack notice strictly follows `L20261104105458-8329` (no spec/PR/SHA/internal IDs).

## Acceptance criteria

1. When `reachable()` is `False` at `_pre_launch_probe`, the supervisor sets `network_outage=True` and `network_outage_since`, returns `"waiting"`, increments zero failure/cooldown counters, and never holds across repeated offline probes.
2. When a turn returns `transient` or `unknown` while `reachable()` is `False`, zero provider cooldowns or failure strikes are recorded and no hold is tripped; the supervisor enters `network_outage=True` and waits.
3. When `reachable()` later returns `True`, the supervisor clears `network_outage`, posts exactly one plain-Italian Slack message containing the offline start and end times (`"Ero offline da ... a ..., riprendo."`), and resumes launching without manual intervention.
4. When `reachable()` is `True`, existing `transient` / `quota` / `auth` cooldown and hold behavior is unchanged.

## Required tests

- Offline unit tests covering criteria 1–4 with fake clocks, fake `reachable` toggle, and captured Slack notifications.
- Existing `backend/tests/test_relay_continuity.py` remains green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review + independent second-session full-diff review, spec status updated.
