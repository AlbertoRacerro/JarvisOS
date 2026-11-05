# 245 — Freshness invalidation detail in Review

Status: ready
Depends on: none

## Goal

When the operator accepts a replacement parameter in Review, show which flowsheet records became stale and why, not only
how many. Read-only.

## Why

Accepting a replacement already records a freshness invalidation with one mark per affected record, each carrying the
dependency path from the superseded parameter. Review shows only `affected_count`. The backend already serves the full
invalidation at `GET /workspaces/{workspace_id}/flowsheet/invalidations/{invalidation_id}`; no screen uses it. Maintainer
choice: option 1 of question `L20261105213009-3f9a`, answered in decision `L20261105214933-cb50`.

## Scope

In scope:
- After a successful replacement acceptance in Review, a visible detail of that invalidation: source and replacement
  parameter, affected count, and the list of affected records with record kind, record id, reason and the dependency path
  from the superseded parameter to that record.
- Loading, error and retry states for the detail. Keep it readable when many records are affected: show a bounded first
  part with a visible "show all" control, without changing the count. The backend caps marks at 1000.

Out of scope (binding non-goals):
- New backend routes, schema changes, or any change to promotion, invalidation, provider, policy or egress behaviour.
- Finding the invalidation again after a reload, a history of invalidations, navigation to the affected records, and
  clearing stale marks.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `frontend/src/stages/ReviewStage.tsx`
- a focused frontend API function, typed from `frontend/src/api/generated` when the contract is there
- `frontend/tests/` and `frontend/package.json` for a new test
- `e2e/` for a browser walk of the changed surface

## Design constraints

- The lookup uses the active workspace and the `invalidation.id` returned by the accept call. Nothing else names it.
- The reason is shown in plain words. `upstream_parameter_superseded` reads as the upstream parameter being replaced. An
  unknown code is shown as the raw code, never hidden. The path shows each ref in order, from the superseded parameter to
  the affected record.
- Zero affected records is stated explicitly, as "no record became stale", not as an empty list.
- Changing workspace or accepting another replacement replaces the detail. A late response must not show for another
  invalidation. An error must not look like zero affected records.
- Loading the detail performs no mutation.

## Acceptance criteria

1. From a clean profile at `/`, an operator can choose a workspace, open Review, accept a replacement parameter with
   visible controls, and see the affected records with kind, id, reason and path.
2. The detail is fetched with the active workspace and the returned invalidation id. The shown count matches
   `affected_count`. Zero affected records is stated explicitly.
3. Loading, failure and retry are visible. Out-of-order responses across workspace changes or a second acceptance cannot
   show a detail for the wrong invalidation.
4. No new route and no mutation. Existing accept and reject behaviour is unchanged.

## Required tests

- Offline frontend tests cover the request identity, the many/zero/error states, retry, the bounded list with "show all",
  reason wording including an unknown code, and out-of-order responses.
- A clean-profile browser walk from `/` reaches Review through visible controls, accepts a replacement on a temporary
  data-root copy or seeded fixture, and checks the detail. Keep its evidence directory.

## Definition of done

Acceptance criteria pass, along with the relevant tests and lint. Required CI, review and the UI browser walk pass on the
exact head. The registry is updated as the work advances. Accepted under maintainer directive `L20261105144536-3af0` item 3
and decision `L20261105214933-cb50`.
