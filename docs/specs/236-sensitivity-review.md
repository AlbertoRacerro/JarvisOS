# 236 — Selected source sensitivity review

Status: ready
Depends on: none

## Goal

In Review, let the operator inspect the latest sensitivity label for the selected MemoryStore record in the active workspace, including whether that label is still current. This is a read-only first W4 slice.

## Why

The backend can resolve a label for a specific source, while Review already provides workspace-scoped selection of assumptions, parameters and decisions. Connecting those existing surfaces makes the label reviewable in context without inventing an inventory or granting new authority.

## Scope

In scope:
- A visible sensitivity inspection for the selected Review record, using its record kind and canonical id.
- Clear loading, no-label, current, stale and error states, with a way to retry or refresh.

Out of scope (binding non-goals):
- Creating or changing labels, derivatives, policy, provider access or egress; context previews; automatic promotion or any new backend route.
- A label or derivative inventory, review history, and selection of requirement or evidence records in this slice.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `frontend/src/stages/ReviewStage.tsx`
- `frontend/src/api/memory.ts` or a focused frontend API module
- `frontend/tests/` and `frontend/package.json` if a new test is added
- `e2e/` for a browser walk of the changed surface

## Design constraints

- Review's existing workspace and selectable `assumption`, `parameter` and `decision` records provide the lookup: GET `/ai/sensitivity/labels/current` with `workspace_id` and `subject_ref` formed as `<record_kind>:<id>`. The record's separate `source_ref` field is not this identity.
- The response is a `SensitivityLabelRead` or null. A returned latest label can have `current: false` and a `stale_reason`; never present it as current. Null means no label returned, not a sensitivity level or permission decision.
- Show the returned level, current/stale state and stale reason when present, and enough provenance to identify the label (classification source, actor, policy version and creation time). Do not infer a policy outcome from the label.
- Changing workspace, filter or selected record must clear or replace the displayed result; a late response must not be shown for another selection. Errors, including a source no longer found, must not look like an unlabeled source.
- Existing Review acceptance and rejection remain separate actions. Inspecting sensitivity performs no mutation.

## Acceptance criteria

1. From a clean profile at `/`, an operator can choose a workspace, open Review, select a visible supported record and inspect its sensitivity using visible controls.
2. The lookup uses the active workspace and selected canonical kind/id. A null response is explicitly shown as no label; a returned label shows its level and provenance, and distinguishes current from stale with the returned reason.
3. Loading, failure and retry are visible. Workspace, filter and selection changes cannot display an obsolete or misattributed response, including when requests finish out of order.
4. Inspection sends no mutation and does not alter existing promotion, provider, policy or egress behavior.

## Required tests

- Offline frontend tests cover request identity, null/current/stale/error states, retry and out-of-order responses across workspace and selection changes. Use fixtures, not paid providers.
- A clean-profile browser walk from `/` reaches Review through visible controls, selects a record and exercises the inspection with representative returned states; retain its evidence directory.

## Definition of done

Acceptance criteria and relevant tests and lint pass; required CI and review evidence for the implementation and the UI browser walk pass; update the canonical registry as work advances. Accepted under maintainer directive `L20261105071756-367c` item 3b.
