# 246 — Edit a single requirement in Project Basis

Status: ready
Depends on: none

## Goal

In Project Basis, let the operator open one requirement and correct its statement, rationale or notes. The change goes
through the existing governed Project Knowledge flow: draft, preview impact, then approve.

## Why

Requirements can be listed, expanded and created in Project Basis, but not corrected. The backend already accepts a
Project Knowledge draft operation `update` for an existing requirement (`owner_id` plus `expected_updated_at`, fields
listed in `_ALLOWED_FIELDS`). The create flow in `ProjectKnowledgePanel` already previews impact and approves. Maintainer
choice: option 5 of question `L20261105213009-3f9a`, answered in decision `L20261105214933-cb50`.

## Scope

In scope:
- An "Edit" control on an expanded requirement in Project Basis. It opens an editable form for `statement`, `rationale`
  and `notes`, prefilled from the record.
- Staging the edit as a Project Knowledge draft with one `requirement` `update` operation that carries only the changed
  fields, the requirement id and its `updated_at` as `expected_updated_at`. Then the existing impact preview and the
  existing approve/discard actions.
- After approval the edit is an approved working revision (its change list shows the correction); it becomes the
  canonical requirement only through the existing revalidation and "Final reconcile" controls, unchanged. After
  reconcile, the list reloads and shows the corrected requirement.

Out of scope (binding non-goals):
- The raw `PATCH /requirements/{id}` route, any new backend route, and schema changes.
- Editing status, basis kind, reconciliation gate or criterion fields. Retiring a requirement. Editing other record kinds.
- Changes to impact preview, revalidation, reconcile or applicability semantics.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `frontend/src/components/fusion/FinalOperatorReadSurface.tsx` (Project Basis rows)
- `frontend/src/components/fusion/ProjectKnowledgePanel.tsx` and its API client
- `frontend/tests/` and `frontend/package.json` for a new test
- `e2e/` for a browser walk

## Design constraints

- Reuse the draft/preview/approve path and its parent-revision rules from the create flow. Do not create a second mutation
  path.
- An empty statement cannot be staged. An unchanged form cannot be staged.
- A stale record shows the backend refusal (`owner_stale` or the draft/apply equivalent) as a visible conflict with a way
  to reload. It never silently overwrites.
- In read-only mode, no Edit control is shown. Cancelling the form stages nothing.
- Changing workspace closes the form and drops any unstaged text.

## Acceptance criteria

1. From a clean profile at `/`, an operator can open Project Basis, expand a requirement, edit its statement through
   visible controls, preview the impact, approve it, reconcile it with the existing "Final reconcile" control, and see
   the corrected statement in the list.
2. The staged draft contains exactly one `requirement` `update` operation with the changed fields, the record id and
   `expected_updated_at` equal to the record's `updated_at`.
3. Empty or unchanged input cannot be staged. A stale record gives a visible conflict and nothing is applied. Read-only
   mode has no Edit control.
4. No new route and no use of the raw PATCH. Create, discard and reconcile behaviour is unchanged.

## Required tests

- Offline frontend tests cover the operation payload, changed-fields-only, empty/unchanged refusal, the stale conflict
  message, read-only mode and workspace change.
- A clean-profile browser walk from `/`, on a fresh temporary data root seeded through the API, edits a requirement end to
  end (approve, then the existing Final reconcile) and checks the persisted statement through the canonical
  requirement GET. Keep its evidence directory.

Clarified 2026-11-05 (coordinator, cycle 356): approval never writes canonical requirements
(`test_draft_cas_and_approval_are_noncanonical_until_reconciliation`); the walk reaches the list through the existing
reconcile step instead of changing reconciliation semantics.

## Definition of done

Acceptance criteria pass, along with the relevant tests and lint. Required CI, review and the UI browser walk pass on the
exact head. The registry is updated as the work advances. Accepted under maintainer directive `L20261105144536-3af0` item 3
and decision `L20261105214933-cb50`.
