# 267 — Session continuity across external waits

Status: ready
Depends on: 264, 266

## Goal

When a coordinator turn ends with `continue` solely to await a background worker job or the required CI checks on its own pull request, the supervisor suspends launches, waits and polls without invoking a model, and resumes that work in the same supported session. Concrete numeric bounds on reported turn usage (`TURN_USAGE_CAP_TOKENS`), elapsed wait time (`WAIT_TIMEOUT_SECONDS`), consecutive resumes (`MAX_SESSION_RESUMES`), and checkpoint spacing (`MIN_CHECKPOINT_INTERVAL_SECONDS`) prevent unbounded or unaffordable execution loops.

## Why

Maintainer directives `L20261113011123-36ed` (ordered supervisor item S6) and `L20261113100248-6ede` (F4 goes with S6):
The current supervisor repeatedly rebuilds full mission prompts after a successful turn with a pending task and starts a new mission cycle for each turn, while a pending mission task already supports continuation. Repeated cold restarts incur unnecessary prompt ingestion costs, discard in-flight reasoning context, and degrade performance. Conversely, unconstrained session resumption risks memory explosion (e.g. earlier Relay threads growing to 1.5–2.7M input tokens), endless polling loops, or silent stalls on unreadable external checks. A bounded wait with precise numeric thresholds, durable restart persistence, strict polling semantics, and deterministic fail-safe wake transitions allows efficient continuity without compromising supervisor authority or safety.

## Code facts (verified at `7b47e97b`; rechecked at `13907343`)

- `scripts/relay_continuity.py` `Supervisor._decide`: rebuilds the full mission prompt (`f"[relay-continuity attempt {nonce}]\n{task}"`) after a successful turn with a pending task and does not parse wait directives from the turn's final message.
- `scripts/relay_continuity.py` `Supervisor._advance`: increments `counts[str(index)]` on every `continue` status toward `MAX_TASK_CHECKPOINTS = 12`, holding with `task_checkpoint_limit` when exceeded, without distinguishing bare checkpoints from external waits.
- `scripts/relay_continuity.py` `Supervisor._launch`: sets `parent = None` if `agent in NATIVE_AGENTS` or `self.state.get("agent") in NATIVE_AGENTS` or provider transitions (e.g. `agent == "codex" and previous != "codex"`), otherwise reusing `self.state["head_session"]`.
- `scripts/relay_continuity.py` `_roadmap_step`: pauses roadmap cycles on `coordinator_blocked` and parses `JOB-WAIT: (j-\d{8}T\d{6}Z-[0-9a-f]{6})`, but only for roadmap cycle settling after a completed turn reports `blocked`.
- `scripts/relay_continuity.py` `roadmap_ci_fingerprint`: computes a hash across the 20 most recent CI runs repository-wide, rather than tracking required status checks for a specific PR head commit.
- `scripts/relay_jobs.py` `TERMINAL`: defines the terminal states for registered jobs: `("succeeded", "failed", "cancelled", "timeout", "interrupted", "lost")`.
- `scripts/relay_continuity.py` `classify_turn`: extracts turn status, events, and tool calls; reads events from `output.jsonl` and status from `state.json`.

## Scope

In scope:

1. **Wait syntax and parsing**:
   - Parse wait directives from the coordinator's durable final reply (`final_text(turn_dir)`):
     - `JOB-WAIT: <job id>` where `<job id>` matches `j-\d{8}T\d{6}Z-[0-9a-f]{6}`.
     - `CI-WAIT: <pr number>@<head sha>` where `<pr number>` matches `\d+` and `<head sha>` matches `[0-9a-f]{40}`.
   - Strict parsing: no arbitrary URLs, paths, or shell commands permitted in wait directives. Multiple wait lines are parsed, stripped of leading/trailing whitespace, and deduplicated.
2. **Numeric bounds, sources, and units**:
   - `WAIT_TIMEOUT_SECONDS = 7200` (2 hours): elapsed wall-clock seconds from `wait_started_at` (`host.now() - wait_started_at`).
   - `CI_POLL_INTERVAL_SECONDS = 60` (60 seconds): minimum elapsed wall-clock seconds between GitHub CI check queries, tracking `last_ci_poll`.
   - `JOB_POLL_INTERVAL_SECONDS = 15` (15 seconds): minimum elapsed wall-clock seconds between local job status queries, tracking `last_job_poll`.
   - `MAX_SESSION_RESUMES = 4`: consecutive resumes of the same session; the next wait after that starts a fresh context.
   - `TURN_USAGE_CAP_TOKENS = 2_000_000`: Relay reports only cumulative usage per turn (`state.json` `context_used_tokens`, input plus output over every request of the turn; a normal turn reads about 1.4M), not the size of the context. A last turn above this value starts a fresh context; a missing or unreadable value counts as no evidence and only `MAX_SESSION_RESUMES` applies.
   - `MIN_CHECKPOINT_INTERVAL_SECONDS = 60` (60 seconds): minimum elapsed wall-clock seconds between bare `continue` checkpoints.
   - `MAX_TASK_CHECKPOINTS = 12`: existing limit on bare checkpoints before triggering `task_checkpoint_limit` hold.
3. **Multiple wait semantics and transition rules**:
   - Normal completion requires conjunction: all named jobs must reach a terminal state AND all named PR required CI checks must reach a completed state (`success` or `failure`).
   - Immediate wake with uncertainty on head change or PR closure:
     - If any awaited PR head advances (`observed_head != expected_head`) or the PR is closed or merged (`pr_state in ("closed", "merged")`), the supervisor terminates the wait immediately without waiting for remaining jobs/checks. It wakes the session reporting the head change or closure, marking uncompleted waits as uncertain (`uncertain`).
   - Immediate wake on check failure:
     - If any required check on an awaited PR fails (`failure`, `timed_out`, `action_required`, `cancelled`), the wait ends immediately, waking the session to report the check failure.
   - Immediate wake on unregistered job:
     - If any job ID in a `JOB-WAIT` line is not registered in `~/jarvis-control/work/jobs/<id>.json`, the wait terminates immediately, waking the session with `unregistered_job` and uncertainty.
   - Immediate wake on wait timeout:
     - When `host.now() - wait_started_at >= WAIT_TIMEOUT_SECONDS` (7,200s), the wait terminates, waking the session with `wait_timeout` and uncertainty for unfinished waits. A timeout never infers success.
4. **Required CI check semantics**:
   - Evaluates branch protection / ruleset required status checks (or check runs) for the exact commit `<head sha>`.
   - `success`: all required checks report status `completed` and conclusion in `("success", "neutral", "skipped")`.
   - `failure`: one or more required checks report conclusion in `("failure", "timed_out", "action_required", "cancelled")`.
   - `no_checks_reported`: if zero checks are registered 120 seconds after the wait started, this is treated as an uncertain failure, never success.
   - `remote_unreadable`: network or API errors during polling do not trigger immediate failure if under `WAIT_TIMEOUT_SECONDS`. If unreadable status persists through the full 7,200s timeout, the wait ends with `remote_unreadable` and uncertainty. Under no circumstances is CI success inferred from unreadable evidence.
5. **Session continuity vs fresh context**:
   - If the parent session `_launch` would choose today is not `None` (same non-native agent; codex only after codex), fewer than `MAX_SESSION_RESUMES` consecutive resumes have run, and the last turn's `context_used_tokens` is not above `TURN_USAGE_CAP_TOKENS`:
     - Relaunch with `parent = head_session` and a concise result prompt summarizing wait outcomes:
       `[relay-continuity resume {nonce}]\nExternal wait satisfied:\n- JOB <id>: <status>\n- CI PR #<pr>@<sha>: <status>\nResume work on mission {mission_id} task {n}/{total}.`
   - Otherwise (usage over the cap, resume limit reached, provider changed, or a native agent, which keeps its existing agy-conversation rule):
     - Relaunch with `parent = None` (fresh context), full `MISSION_PROMPT`, and durable handoff summary.
6. **Checkpoint vs wait discipline**:
   - Satisfied waits do NOT increment `counts[str(index)]` toward `MAX_TASK_CHECKPOINTS = 12`.
   - Bare `continue` turns increment `counts[str(index)]`. If `host.now() - last_checkpoint_at < 60s`, sleep the difference before relaunching.
7. **Cycle text and hand-off (F4 of the directive)**:
   - In `ROADMAP_TASK`, replace "A running registered job never idles the cycle: while it runs, take job-independent authorized work." with: "While a registered job runs, take the next item in the order the standing directives give; only when none is left, other authorized work. To wait for your own job or PR checks, end with `RELAY_STATUS continue` and a `JOB-WAIT:` or `CI-WAIT:` line; the supervisor resumes you when it completes."
   - Replace the sentence that forbids keeping a turn open to wait for a job or CI with the same instruction: end the turn with a wait line, never a sleeping command.
   - The cycle text asks every hand-off note to name the next directive item.
8. **Durability, restart recovery, and side-effect holds**:
   - Persist active wait state in supervisor state under `state["wait"]`. An owner restart while waiting resumes polling without launching a turn or losing wait parameters.
   - Honoring stop requests (`stop_requested`, SIGINT, SIGTERM): supervisor halts cleanly in `waiting` phase without launching.
   - Side-effect holds: `_settle_external` and `gate(report)` execute before recording any wait. Uncertain side-effects, unverified commands, or parked calls trigger standard supervisor holds (`gate` / `effects_uncertain`) without waiting.
   - Existing supervisor holds (`permissions_changed`, `attempt_unresolved`, `circuit_failures`, `task_checkpoint_limit`, `task_check_failed`, `task_blocked`) retain absolute authority before launching.

Out of scope (binding non-goals):

- No owner-side automated git merge, PR approval, or commit pushing.
- No automated credential refresh or secret modification.
- No changes to how a completed mission task's independent completion check is evaluated (`_verify`).
- No new external network infrastructure, daemon processes, or database tables.
- No changes to frozen conformance tests (`backend/tests/**/test_*_conformance.py`).

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_continuity.py`

## Design constraints

- Wait state must be derived strictly from the durable final message (`final_text(turn_dir)`), never memory alone.
- Wait directive parsing must be strict; reject any line containing backticks, shell operators, or non-matching formats.
- Wait timeouts and unreadable evidence must always report uncertainty; never report CI success or job success without deterministic evidence.
- Safe defaults stay safe: paid AI disabled, budget zero, fake providers in tests.
- Offline tests must use fake host, clock, and PR check data.

## Acceptance criteria

1. In offline tests with a fake host and clock, a `continue` turn naming valid `JOB-WAIT:` and `CI-WAIT:` directives transitions supervisor to `waiting` without launching a model.
2. The wait polls registered jobs at `>= 15s` intervals and PR CI checks at `>= 60s` intervals until all conditions reach terminal/complete states, waking with a short result prompt containing the exact statuses.
3. If an awaited PR head advances or the PR is closed during a wait, the supervisor terminates the wait immediately, waking the session with reason `head_changed` or `pr_closed` and stating uncertainty for uncompleted waits.
4. If an awaited job ID is unregistered, the supervisor wakes immediately with `unregistered_job` and uncertainty.
5. If elapsed wait time reaches `7200` seconds (`WAIT_TIMEOUT_SECONDS`), the wait terminates and wakes with `wait_timeout` and uncertainty; neither job nor CI success is inferred.
6. If zero checks are reported on an awaited PR head after 120 seconds, or if remote evidence is unreadable through the timeout, the wait wakes with uncertainty (`no_checks_reported` or `remote_unreadable`), never success.
7. An unrelated CI run on another PR or branch does not wake a waiting PR.
8. A failed required check on an awaited PR wakes the session immediately and is reported as failed.
9. Satisfied waits do not increment the task's checkpoint counter toward `MAX_TASK_CHECKPOINTS = 12`. Twelve consecutive satisfied waits do not trigger `task_checkpoint_limit`.
10. Twelve bare `continue` turns trigger the `task_checkpoint_limit` hold. Bare `continue` turns enforce a minimum 60-second interval before relaunch.
11. A session whose parent `_launch` would keep (same non-native agent) resumes with `parent = head_session` while fewer than `MAX_SESSION_RESUMES = 4` consecutive resumes have run and the last turn's `context_used_tokens` is at most `2_000_000`.
12. A last turn above `2_000_000` reported tokens, a fifth consecutive resume, a provider change or a native agent launches a fresh context (`parent = None`) with the full mission prompt and durable handoff; a missing usage value alone never forces or prevents a resume.
13. Restarting the supervisor during an active wait restores wait state and resumes polling without launching a model turn.
14. A stop signal or stop request during a wait transitions the supervisor to `stopped` without launching.
15. Existing side-effect holds (`gate`, `effects_uncertain`) and supervisor holds take precedence over waiting or resuming.
16. The roadmap cycle text contains the replacement sentences of scope item 7 and no longer says that a running job never idles the cycle.

## Required tests

Focused offline tests in `backend/tests/test_relay_continuity.py`:

- `test_wait_parsing_strict_and_deduplicated`: verifies regex matching of `JOB-WAIT` and `CI-WAIT`, rejecting invalid syntax and deduplicating multiple lines.
- `test_job_wait_satisfaction_and_resume_prompt`: verifies wait until all jobs reach terminal states (`TERMINAL`) and asserts short resume prompt formatting.
- `test_ci_wait_satisfaction_and_unrelated_ci_ignored`: verifies wait for exact `<head sha>` required checks, confirming unrelated repository CI runs do not wake the PR.
- `test_pr_head_change_or_closure_wakes_immediately_with_uncertainty`: verifies immediate wake when head sha changes or PR closes, reporting uncertainty.
- `test_unregistered_job_wakes_immediately_with_uncertainty`: verifies immediate wake when job ID is not in jobs registry.
- `test_wait_timeout_wakes_with_uncertainty`: verifies wake after 7,200s wall clock with `wait_timeout` and uncertainty without inferring success.
- `test_no_checks_or_unreadable_ci_fails_closed`: verifies that 0 checks reported or unreadable remote CI evidence never infers passing.
- `test_satisfied_waits_do_not_consume_checkpoint_limit`: verifies 12 satisfied waits do not hold, whereas 12 bare checkpoints do.
- `test_bare_checkpoint_enforces_minimum_interval`: verifies supervisor throttles bare checkpoints to at least 60s spacing.
- `test_context_cap_and_resume_limits_force_fresh_context`: verifies session continuation under the cap, and fresh context launch above 2M reported tokens or after 4 consecutive resumes.
- `test_provider_change_and_native_agents_force_fresh_context`: verifies provider transitions set `parent = None` and native agents keep their existing conversation rule.
- `test_wait_persists_across_restart_and_honors_stop`: verifies owner restart retains wait polling without launching, and stop requests halt cleanly.
- `test_side_effect_holds_preempt_external_waits`: verifies `_settle_external` and `gate` holds trigger before any wait can be registered.

## Definition of done

The acceptance criteria hold and the required evidence is recorded on the implementation PR. The registry reflects the implementation state.
