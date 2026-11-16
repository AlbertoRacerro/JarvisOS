# 279 — Supervisor waits, outages and question checks settle without loops

Status: ready
Depends on: none

## Goal

A pass of the continuity owner that launches nothing changes no retry counter and restarts no wait. A coordinator
that keeps ending on a wait that cannot be satisfied is held after a few wakes. A wake never launches an agent that is
cooling down. A pending release with a watcher that never settles still reaches its deadline. The question validator
accepts ordinary lines that contain paths or sentence ends after a number.

## Code facts checked on `origin/master` at `4471d779`

- `scripts/relay_continuity_supervisor.py::Supervisor._decide` (non-success branch) adds the same-agent strike
  `counters["transient_<agent>"]` when `retry_same`, and later sleeps the backoff, before returning the launch.
  `Supervisor._launch` runs the reachability probe only afterwards and returns `"waiting"` without saving `pending`
  when it fails, or when another coordinator is active. The next pass decides the same report again and adds another
  strike; past `Limits.same_agent_retries` the agent gets a cooldown through `set_cooldown`.
- `Supervisor._wake_wait` sets `state["wait"] = None` and saves before `_launch`, and increments or resets `resumes`
  there. If `_launch` then returns `"waiting"`, the next pass finds no wait, decides the same successful turn again,
  parses the same `JOB-WAIT`/`CI-WAIT` lines and opens a new wait whose `started_at` is now.
- `Supervisor._settle` counts a task checkpoint only when the turn is not an external wait (`is_external_wait`), and
  `_wake_wait` resets `resumes` to 0 on a fresh-context fallback. A coordinator that ends every turn waiting on a job
  or PR that cannot complete (`unregistered_job`, `head_changed`, `check_failed`, `wait_timeout`) is never limited.
- `_wake_wait` uses `self._choose(failed_agent) or failed_agent`; every other launch path goes through
  `Supervisor._wait_or_hold` when `_choose` returns None.
- `scripts/host_ops.py::Executor.maybe_confirm_release` checks the confirmation deadline only when
  `marker["owner_active"]` is false. With the owner active, the deadline is checked again only in the owner block,
  which follows the watcher block; the watcher block returns False while the watcher unit is not `active` or its
  `NRestarts` grows, so the deadline is never reached and the owner is never started.
- `scripts/devctx.py::_MARKER` matches `\d+\.` followed by a letter or a space after a separator such as `/`.
  `validate_question` therefore refuses the option `1. Aggiorna docs/specs/264.md — conseguenza: x`, the option
  `1. Mergia PR 104. Poi attendi — conseguenza: x`, and the prompt line `Aggiorno docs/specs/264.md o no?`. A prompt
  that mentions `(spec 276)` and `(spec 278)` is also refused, through two `\d+\)` matches.

## Scope

1. In `_decide`, add no same-agent strike and sleep no backoff for a launch that does not happen. Either probe
   reachability (and the coordinator conflict) before the strike, or record the strike only once `_launch` has saved
   `pending`. A pass that launches nothing leaves `counters` unchanged. The retry limit of directive
   L20261109091909-a4bb counts real launches only.
2. In `_wake_wait`, keep the wait, marked as woken with its reason and prompt, until a launch has saved `pending`.
   A pass that returns `"waiting"` re-uses that woken wait on the next pass: same prompt, same `started_at`, and
   `resumes` changed once.
3. Count, per mission task, the wakes whose reason is not `satisfied`, durably across owner restarts. At a small
   limit (3) hold the mission with a named reason instead of launching again. A `satisfied` wake or a turn that
   ends without a wait resets the count.
4. In `_wake_wait`, when `_choose` returns None, go through `_wait_or_hold` with the wake prompt.
5. In `maybe_confirm_release`, check the confirmation deadline before the watcher block in both cases
   (`owner_active` true or false), so a watcher that never settles ends in the existing unhealthy path.
6. In `devctx.py`, count an option marker only at the start of a line or after a real list separator (`;`, `—`,
   `:` followed by a space, or a conjunction), never inside a path or a word, and never after a sentence end
   followed by a capitalised word. `1. a 2. b` inline alternatives stay refused.
7. Optional, only if it fits the same change without new state: an install that failed for a reason that does not
   concern the candidate (for example a deadline or watcher failure) does not mark that sha `refused`
   (`_recover_unsuccessful_install`); the watcher keeps `sent_questions` when its state file is unreadable.

## Acceptance criteria

1. A test reproduces each of points 1 to 6 on `origin/master` first (it fails there) and passes after the change.
2. Point 1: three passes whose launch probe fails leave `counters` and `cooldowns` as they were and switch no agent;
   a reachable pass after them launches the same agent.
3. Point 2: a wake whose launch returns `"waiting"` and a following pass that launches produce one launch with the
   original wake prompt, one change of `resumes`, and the original `started_at`.
4. Point 3: a coordinator that ends four turns in a row on a wait for an unregistered job is held after the third
   wake; a satisfied wake in between resets the count.
5. Point 4: a wake while the chosen agent is cooling down waits or holds and launches nothing.
6. Point 5: with `owner_active` true and a watcher unit that never reaches `active`, the pending release ends as
   unhealthy after its deadline.
7. Point 6: the three lines quoted in the code facts, and the prompt with `(spec 276)` and `(spec 278)`, are accepted;
   the existing refusals in the devctx tests still refuse.
8. Fixtures for points 2 and 3 are copied from a real owner state file and a real wait turn (trimmed of user content).
9. **Real environment.** After the owner next runs a release containing this change, one real wake is observed in
   its journal with no duplicated wait, and the hand-off says so; until then it says the live owner runs older code.

## Definition of done

Focused tests and lint pass; CI is green on the exact head; a second qualified session reviews the full diff;
criterion 9 is recorded after the next owner restart.
