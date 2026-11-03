# 240 — Supervisor UI controls

Status: ready
Depends on: none

## Goal

Give the maintainer supervisor controls and open question answers in Coding → Runtime.

## Why

The supervisor and its questions must be observable and manageable from the workstation.

## Scope

In scope: status, pause, resume, graceful restart, and answer open questions with an option or custom text.
Out of scope: provider execution and arbitrary supervisor commands.

## Files likely touched

- `scripts/relay_observe.py`
- `backend/app/modules/coding/relay_monitor.py`
- `backend/app/modules/coding/runtime_routes.py`
- `frontend/src/components/coding/RelayMonitorPanel.tsx`

## Design constraints

Use existing owner state, roadmap locking, ledger and repository guard. Bound and secret-check input and output. Never treat an answered question as open.

## Acceptance criteria

1. Runtime displays supervisor status, controls and open questions with their options.
2. Controls update owner state and journal the action; answers append maintainer decisions and resume eligible blocked roadmaps.
3. Invalid, secret-bearing and unavailable requests are refused.

## Required tests

Offline tests cover parsing and settlement, control mutations and refusals, answer recording, routes and rendered controls.

## Definition of done

Required checks and risk-appropriate review and execution evidence pass.
