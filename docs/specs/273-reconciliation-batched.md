# 273 — Reconciliation batched into the next registry change

Status: ready
Depends on: —

## Goal

A STATUS.md row that is `in_review` and whose every linked implementation PR is merged counts as done wherever the
supervisor tooling asks whether a spec is merged. The word `merged` is then written by the next PR that touches the
registry anyway, instead of by a PR opened only for that word.

## Why

Supervisor item S9 of the maintainer's current directive order (S3, F1, S6, S7, S10, S8, S9; S5 last). The spec gate
requires the row to be `in_review` and to link the implementation PR while that PR is open
(`check_spec_status.py::implementation`), so `merged` can only be written after the merge, in a second PR. About one
merge in four on this repository is such a reconciliation PR (for example #99, `docs/264-status-merged`). Each costs a
branch, a CI run and a merge, and until it lands the recovery packet lists "reconcile STATUS" as next work and a
mission whose contract is `spec_merged` cannot complete.

## Code facts checked on `origin/master` at `40f6ecc1`

- `scripts/check_spec_status.py::implementation` accepts an implementation PR only when its spec row is `in_review`
  or `merged` and links that PR, and fails with `unmerged dependencies` when a dependency's row is not `merged`. The
  gate reads only the registry text and the PR event; it has no GitHub access.
- `scripts/relay_continuity.py::check_task`, kind `spec_merged`, reads STATUS.md from GitHub master, requires exactly
  one row for the spec, status `merged` and at least one linked `[#N]`, then requires every linked PR to pass the
  `pr_merged` check. `scripts/relay_continuity.py::describe_check` tells the provider that contract in one sentence.
- `scripts/devctx.py::status_state` parses the registry from `origin/master` and sets `deps_merged` on each row from
  the set of rows whose status is literally `merged`.
- `scripts/devctx.py::build_packet` adds the candidate "spec N is in_review but none of its PRs is open: reconcile
  STATUS" for every `in_review` row with no open linked PR, whether its PRs were merged or closed unmerged.
  `scripts/devctx.py::github_state` already lists up to 500 merged PRs (`gh_state["merged"]`).
- `backend/tests/test_devctx.py` asserts the reconcile candidate for spec 003; `backend/tests/test_relay_continuity.py`
  covers `spec_merged` in `test_204_check_task_spec_merged_reads_the_remote_registry`.

## Scope

In scope:

1. **Done means merged or merged-in-review.** One definition: a row is *done* when its status is `merged`, or its
   status is `in_review`, it links at least one PR, and every linked PR is merged on GitHub.
2. **`spec_merged` check.** `check_task` passes for a done row; an `in_review` row with an open or closed-unmerged
   linked PR fails naming that PR, as `pr_merged` does today. `describe_check` says "is `merged` in STATUS.md, or
   `in_review` with every linked PR merged".
3. **Recovery packet.** `status_state` takes the GitHub state and computes `deps_merged` from done rows; each row
   also carries `done`. In `build_packet`, an `in_review` row whose linked PRs are all merged is no longer a
   "reconcile STATUS" candidate; an `in_review` row with a linked PR that is neither open nor merged keeps a candidate
   naming the closed PR. When GitHub state is unavailable, today's behaviour (literal `merged` only) applies.
4. **Process rule.** `docs/PROCESS.md` gains one sentence where the spec lifecycle is described: after an
   implementation PR merges, its row stays `in_review` until the next PR that edits STATUS.md writes `merged`; nobody
   opens a PR only for that word. A PR that sets a dependent spec `in_review` writes `merged` for its done
   dependencies, because the gate still requires it.

Out of scope (binding non-goals):

- No change to `scripts/check_spec_status.py`: the CI gate stays offline and still requires literal `merged` for
  dependencies; the dependent's own registry edit writes it (scope item 4).
- No change to the STATUS.md format, statuses, the ledger, `pr_merged`, mission or task contracts other than the
  `spec_merged` meaning, and no automatic rewriting of STATUS.md by any tool.
- No change to frozen conformance tests (`backend/tests/**/test_*_conformance.py`).

## Files likely touched

- `scripts/relay_continuity.py`
- `scripts/devctx.py`
- `docs/PROCESS.md`
- `backend/tests/test_relay_continuity.py`
- `backend/tests/test_devctx.py`

## Design constraints

- `check_task` keeps one registry read from GitHub master and one `pr_merged` check per linked PR; no new reads.
- `devctx` uses only the merged-PR list it already fetches; no extra `gh` call per row.
- Tests use the existing fake runners and fake `gh` state only.

## Acceptance criteria

1. `spec_merged` passes for a `merged` row with merged PRs and for an `in_review` row whose every linked PR is
   merged; it fails for an `in_review` row with one open linked PR, for an `in_review` row with no linked PR, and for
   `ready`, each with a message naming the reason.
2. With fake GitHub state where spec A is `in_review` with its PR merged and spec B is `ready` and depends on A,
   the packet marks B `deps_merged` and lists no "reconcile STATUS" candidate for A.
3. With A `in_review` and its only PR closed unmerged, the packet keeps a reconcile candidate for A naming the PR,
   and B is not `deps_merged`.
4. With GitHub state unavailable, `deps_merged` and the candidates are as on `40f6ecc1`.
5. `docs/PROCESS.md` states the batching rule; `scripts/check_spec_status.py --self-test`, `check_doc_paths.py` and
   the touched test files pass.
