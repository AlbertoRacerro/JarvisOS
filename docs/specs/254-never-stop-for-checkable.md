# 254 — The supervisor never stops for something it can check

Status: ready
Depends on: none

## Goal

An interrupted turn whose external effects no probe can settle no longer holds the mission. The mission owner parks
only the uncertain operations (never repeats them), records what it found, sends one plain Slack line, and continues
the queue with a recovery prompt that names the parked operations. A `git commit && git push` interrupted mid-way is
settled by probing the push. Stale durable-job records from ended namespaces stop blocking job admission.

## Why

Maintainer directive `L20261107093027-7f9a` ("the supervisor never stops for something it can check by itself"), after
finding `L20261107092039-c2e5`: an Argon turn timed out during `git commit && git push`, the owner held with
`effects_uncertain` and asked the maintainer to "write to Claude in chat"; about 7.5 hours were lost to a check the
owner could have made. Finding `L20261107110122-bb67`: four `unknown` job records from ended sandbox namespaces, hours
past their deadlines, fill `MAX_RUNNING` so no durable job can start in any later turn.

## Scope

In scope (`scripts/relay_continuity.py`, `scripts/relay_jobs.py`):
- **Compound push probe.** `probe_call` also settles a command whose effectful steps are only local git steps
  (`add`, `commit`, `rm`, `mv`, `tag` without push) followed by exactly one `git push`: the verdict is the push probe's.
  Any other effectful step keeps returning `None`.
- **Park, do not hold.** When `_settle_external` leaves operations unresolved, `_decide` (both the success and the
  failure path) no longer returns `hold("effects_uncertain")`. It journals the reconcile (as today), records one ledger
  `finding` listing each unresolved operation with "parked: may have taken effect, never repeat it", notifies a new
  `effects_parked` event once per turn, and continues with the recovery prompt. In that prompt an unresolved operation
  reads `UNRESOLVED (parked): it may have taken effect; never repeat it, verify its effect from Git, GitHub, host
  receipts or the ledger, and park only the work that depends on it`.
- **Recovery prompt.** The sentence telling the agent to record a blocker and end `blocked` for an unverifiable
  external effect becomes: do not repeat it, record a `devctx.py note blocker` for that one item, and continue other
  authorized work; end `blocked` only when no such work remains. The prompt also tells the agent to commit and push in
  a separate short command, never chained with a long test run, well before the turn deadline.
- **Slack wording.** `effects_parked`: "JarvisOS ha messo da parte un'operazione non verificabile e continua con il
  resto del lavoro." `turn_failed` is notified only when `consecutive_failures` (after this turn) is at least 2; its
  text says what the supervisor does next ("…; JarvisOS riprova con un altro assistente." / "…riprova."). The `hold`,
  `circuit_open`, `mission_held` and `roadmap_paused` texts no longer say "scrivi a Claude in chat"; they say the work
  is paused and waits for a decision in Jarvis.
- **Job admission.** In `relay_jobs.start`, a record whose effective status is `unknown` and whose `deadline` is more
  than 15 minutes in the past does not count toward `MAX_RUNNING` (its wrapper enforces the wall limit, so a live one
  would have ended it). `list`/`show` still report it `unknown`; nothing is rewritten and duplicate detection is
  unchanged.

Out of scope (binding non-goals):
- No new ledger kind, CLI, service, schema or dependency. No probe for host-ops, Slack or ledger effects in this slice
  (they are already local or receipt-backed).
- The other hold reasons (`output_unparseable`, `circuit_failures`, `auth_failure`, …) keep holding; only their Slack
  text changes. A numbered option question for holds is a later slice.
- No change to how local in-flight operations are handled or to probe verdicts for single commands.

## Files likely touched

- `scripts/relay_continuity.py`, `scripts/relay_jobs.py`
- `backend/tests/test_relay_continuity.py`, `backend/tests/test_relay_jobs.py` (or the existing files covering them)

## Design constraints

- Deterministic code only; probes stay read-only.
- A parked operation is never re-run by the supervisor and is never reported as completed.
- `effects_parked` is deduplicated per turn; the ledger finding is written once per turn.
- `GATE_HOLDS`, release and waive paths keep working for holds that still exist (a release of an old
  `effects_uncertain` hold still works).
- The consecutive-failure counter and circuit behaviour are unchanged; only the notification threshold changes.

## Acceptance criteria

1. `probe_call` on `git add -A && git commit -m x && git push origin b` returns the push probe's verdict; with another
   effectful step (e.g. `rm -rf x && git push`) it returns `None`.
2. An interrupted turn whose push no probe can settle yields an (agent, prompt) decision, not a hold; the prompt marks
   the operation `UNRESOLVED (parked)`; one ledger finding and one `effects_parked` notification are recorded. This holds
   for interruptions before the commit, after the commit before the push, after the push, and after a PR comment
   (`gh pr comment`) whose effect cannot be read.
3. The same uncertain operation on a turn reporting success also continues instead of holding.
4. A single failed turn sends no `turn_failed` Slack line; a second consecutive one does.
5. No `relay_continuity.EVENTS` template contains "scrivi a Claude in chat" (`relay_observe` incident lines, which report a stopped owner the maintainer must restart, are unchanged).
6. `relay_jobs start` admits a job when four `unknown` records are past their deadline by more than 15 minutes, and
   still refuses when they are within it or `running`.
7. Existing relay continuity and relay jobs tests pass, updated only where they asserted the old hold or wording.

## Required tests

- Offline tests with the existing fake host, runner and job-record fixtures; one test per interruption point in AC2.

## Definition of done

High risk (supervisor): relevant tests and lint, CI green on the exact head, a full diff review by a second qualified
session, then live activation after merge: the maintainer's chat session reinstalls host-ops and restarts the watcher and
mission owner, and the next interrupted turn with an unverifiable external effect continues without a hold.
