# 252 — Roadmap wakes on a recorded answer

Status: ready
Depends on: none

## Goal

A roadmap paused as `coordinator_blocked` (without a host-operation or job wait) resumes when durable human input is
recorded after the pause: a maintainer directive, a recorded maintainer answer, or a ledger `decision` that names the
id of a recorded `blocker` or `question` entry (for example a host session's "host install done, answering host request
L…"). The same decision also wakes a `no_progress` roadmap.

## Why

On 2026-11-06 a cycle paused as `coordinator_blocked` one minute before the host session recorded the answer to its host
request as a `decision`. Nothing woke the roadmap for about four hours (finding `L20261106160055-f1fd`). Only
`relay_monitor.answer_question` and host-operation/job waits resume a blocked roadmap today; an answer recorded in the
ledger does not.

## Scope

In scope:
- `maintainer_or_hostops_after` in `scripts/relay_continuity.py` also returns true for a ledger `decision` newer than
  the pause whose text names the id (`L` + 14 digits + `-` + 4 hex) of an existing ledger entry of kind `blocker` or
  `question` recorded before that decision.
- `_roadmap_step`: a `coordinator_blocked` pause with no `hostops_wait` and no `job_wait` resumes when the ledger part of
  that check (directive, maintainer answer, answering decision) is true after `paused_at`. A host receipt alone does not
  resume it (receipts already resume through `HOSTOPS-WAIT`). Resuming journals `roadmap_answer_resumed`.

Out of scope (binding non-goals):
- No new service, file, schema, ledger kind or CLI.
- No change to the `hostops_wait` / `job_wait` resume paths, to `answer_question`, or to the pause rules.
- No change to which entries count for `no_progress` other than the new answering decision.

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_continuity.py` or `backend/tests/test_relay_slack_questions.py`

## Design constraints

- Deterministic: string/regex matching over `devctx.read_ledger`; no model call.
- An unreadable ledger (`OSError`) never resumes.
- A decision naming an unknown id, an id of another kind (e.g. a `note` or `claim`), or recorded before the pause never
  wakes.
- A `coordinator_blocked` pause waiting on host operations or jobs keeps exactly its current behaviour.

## Acceptance criteria

1. A `coordinator_blocked` roadmap without waits stays paused across steps until a decision naming a recorded blocker id
   is appended after `paused_at`; the next step leaves the pause and submits the next cycle.
2. The same holds for a maintainer directive and for a `Maintainer answer to question` decision.
3. A decision naming an unknown id or a non-blocker/question id, or dated before the pause, does not resume it.
4. A new host receipt alone does not resume a `coordinator_blocked` roadmap without waits.
5. `maintainer_or_hostops_after` returns true for the answering decision, so a `no_progress` roadmap wakes too.
6. Existing relay continuity and Slack question tests pass unchanged.

## Required tests

- Offline tests in the existing relay continuity test files using the existing roadmap fixtures and a temporary ledger.

## Definition of done

Acceptance criteria pass with the relevant tests and lint. High risk (supervisor): required CI green on the exact head,
a full diff review by a second qualified session, and the real-environment proof: the next real `coordinator_blocked`
pause answered by a recorded decision resumes without manual help (recorded as live activation after merge and the
host install of the supervisor).
