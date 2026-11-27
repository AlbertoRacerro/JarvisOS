# 386 — MAINTAINER-STOPPED-GUARD-1: Refuse automated restart of maintainer-stopped local model services

Status: ready
Depends on: —

## Goal

Enforce a fail-closed host-ops guard so that whenever a local model service (Strata or llama.cpp systemd unit) is
stopped by the maintainer or the maintainer's chat session, automated supervisor, benchmark, or background `service.control`
`start`/`restart` requests for that unit are refused until the maintainer explicitly starts the unit or clears the hold.

## Why

Directive `L20261127072944-28df` establishes a permanent rule extending spec 375 (maintainer-first): if a local model
service was stopped by the maintainer or by the maintainer's chat session (for example to free the GPU for local work),
the supervisor must not restart it on its own — it may only ask. Today `scripts/host_ops.py::op_service_control` executes
any allowlisted `start` or `restart` request without checking whether the maintainer stopped the unit.

## Code facts checked at `bc40e416`

- `scripts/host_ops.py::op_service_control` executes `start`, `stop`, or `restart` via `_systemctl` for allowlisted
  units, enforcing per-unit `OWNER_ACTIONS` restrictions, without tracking who stopped a local model unit.
- `backend/app/modules/local_ai/runtime/host_start.py::write_service_control_request` writes
  `jarvisos.hostops.request.v1` JSON requests into `spool/requests/` with a `requester` string field.
- `backend/app/modules/local_ai/runtime/host_start.py::StrataRuntimeController.start` writes a `service.control` `start`
  request and polls `check_receipt` in `get_state`.

## Scope

In scope:
1. **Maintainer-stopped unit state in `scripts/host_ops.py`**:
   - Maintain a small atomic JSON state file alongside host-ops state (e.g. `maintainer_stopped_units.json` in the
     host-ops state/spool parent directory, configurable via context/test fixture) mapping unit names to metadata
     (`stopped_at`, `requester`, `reason`).
   - Identify maintainer-initiated requests by `requester` (e.g. `"maintainer"` or `"maintainer-chat"`, or explicit
     `maintainer: true` argument in `service.control`).
   - When `op_service_control` stops a local model unit (`jarvisos-strata-*.service` or `jarvisos-llama-*.service`, or any
     unit configured under `local_model_units`) on behalf of the maintainer, record the unit in
     `maintainer_stopped_units.json`.
   - When `op_service_control` receives `start` or `restart` for a unit currently recorded as maintainer-stopped:
     - If the requester is NOT the maintainer (`maintainer` / `maintainer-chat`), raise `Refused` with a clear message
       (`"unit stopped by maintainer; ask before restarting"`) without invoking `systemctl`.
     - If the requester IS the maintainer, proceed with `systemctl` and remove the unit from
       `maintainer_stopped_units.json` upon success.
   - Add a small CLI helper subcommand in `scripts/host_ops.py` (e.g. `hold-unit` / `release-unit` / `held-units`) so the
     maintainer or chat session can also mark or clear a unit stopped outside the spool (e.g. direct `systemctl --user stop`).
2. **Clean refusal propagation in `backend/app/modules/local_ai/runtime/host_start.py`**:
   - Ensure `StrataRuntimeController.get_state` surfaces a `"refused"` receipt with its error detail cleanly as
     `state="stopped"` / `last_error` without looping or retrying `start`.
3. **Offline tests (`backend/tests/test_maintainer_stopped_guard_386.py`)**:
   - Cover maintainer stop -> automated start refused (no `systemctl` call), automated stop -> automated start allowed,
     maintainer start clears the hold, `hold-unit` / `release-unit` CLI helpers, and `StrataRuntimeController` handling
     of a refusal receipt.

Out of scope (binding non-goals):
- Starting, stopping, or probing any live local model service on the host GPU.
- Changing non-local-model systemd units (e.g. SearXNG, backup timers, mission owner).
- Modifying the Strata binary or UI screens.

## Files likely touched

- `scripts/host_ops.py`
- `backend/app/modules/local_ai/runtime/host_start.py`
- `backend/tests/test_maintainer_stopped_guard_386.py` (new)
- `docs/MODULES.md` (regenerated via `scripts/gen_modules.py` if line counts shift)

## Design constraints

- Fail-closed: if `maintainer_stopped_units.json` exists and is unreadable/corrupt, refuse automated `start`/`restart`
  of local model units rather than silently allowing it.
- High risk (host-ops service control): requires coordinator full-diff review and independent second-session review.
- Offline tests only (fake `systemctl` runner and temp spool/state directory); never touch live systemd units or port
  8000.

## Acceptance criteria

1. `op_service_control` records a local model unit when stopped by `maintainer` / `maintainer-chat`, refuses automated
   `start`/`restart` with `Refused` (zero `systemctl` invocations), and clears the record when the maintainer starts
   or releases it.
2. Automated `stop` followed by automated `start` (normal model swap when not maintainer-stopped) continues to work
   unchanged.
3. Corrupt/unreadable hold state fails closed for automated `start`/`restart` of local model units.
4. `StrataRuntimeController` surfaces refusal receipts cleanly without retrying.
5. Offline tests, ruff, repo gates, CI on exact head, and independent second-session review pass.

## Required tests

- `backend/tests/test_maintainer_stopped_guard_386.py`:
  - maintainer `stop` -> automated `start` and `restart` refused without calling `systemctl`;
  - automated `stop` -> automated `start` succeeds;
  - maintainer `start` clears the hold and allows subsequent automated operations;
  - corrupt hold file fails closed on automated `start`;
  - `hold-unit` / `release-unit` CLI helpers;
  - `StrataRuntimeController.get_state` surfaces refusal receipt without retrying.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review and independent second-session review.
