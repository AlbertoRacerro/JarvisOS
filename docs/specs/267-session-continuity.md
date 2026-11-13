# 267 — Session continuity across external waits

Status: draft
Depends on: 264, 266

## Goal

When a coordinator turn ends only to wait for a worker job or the required checks on its own PR, the supervisor waits without using a model and resumes that work in the same supported session. A bounded context size, wait time, and checkpoint policy prevent an endless or unaffordable resume chain.

## Why

The current supervisor starts a new roadmap mission for each cycle, while a pending mission task already supports continuation. Consecutive cold sessions repeat the same reading. A waiting task should retain its context when the provider supports it and keep a durable handoff for recovery.

## Code facts checked on `origin/master` at `46a90afb`

- `Supervisor._decide` rebuilds the full mission prompt after a successful turn with a pending task and does not inspect wait lines.
- `Supervisor._advance` counts every `continue` toward `MAX_TASK_CHECKPOINTS`, currently 12.
- `Supervisor._launch` selects the parent session and already forces a new context for native providers and provider changes.
- `_roadmap_step` waits for an incomplete mission; it handles `JOB-WAIT` only after a completed cycle reports `blocked`.
- `roadmap_ci_fingerprint` hashes the 20 most recent CI runs, independent of the PR being awaited.

## Scope

In scope:

- A `continue` final message may name `JOB-WAIT: <job id>` or `CI-WAIT: <pr number>@<head sha>`. Wait only for registered jobs and the required checks on the named, unchanged PR head. Several waits complete together. A closed PR or changed head ends its wait with that reason stated.
- Persist the wait through an owner restart, poll it without a model, honor stop requests, and resume after a bounded time with the observed reason. Unreadable remote evidence never means the checks passed.
- Resume with a short result prompt and a parent session where supported. Preserve the existing fresh-context behavior on provider changes. Above a context cap, or after a bounded number of resumes when usage is unavailable, start a fresh context with the full mission prompt and the durable handoff.
- Satisfied waits do not consume the checkpoint limit. Bare `continue` checkpoints retain the limit and have at least a 60-second interval.
- Wake on the named PR's required checks, instead of unrelated recent CI runs. Tell coordinators to report a wait when no other ordered, authorized work remains.

Out of scope:

- No owner-side merge, work selection, summarization, new mission type, or provider permission.
- No change to how a completed mission task is independently verified.

## Design constraints

- Derive the wait from the finished turn's durable final text, not memory alone.
- Keep wait parsing strict and bounded; no arbitrary URLs or commands in wait lines.
- Existing supervisor holds, side-effect probes, and restart recovery retain authority over launching.
- A wait timeout reports uncertainty; it never reports CI success or job success without evidence.

## Acceptance criteria

1. With a fake host and clock, a `continue` with either wait type causes no launch until completion, a head change, closure, or the bound. The next prompt states the observed result.
2. Restart during a wait still waits. Stop during a wait stops without launching.
3. Twelve satisfied waits do not trigger the checkpoint hold. Twelve bare checkpoints still do, with a minimum 60-second interval.
4. A supported session resumes with its parent while below the cap; an oversized or unsupported session starts fresh with the full task and handoff.
5. An unrelated CI run cannot wake the waiting PR; failed required checks do wake it and are reported as failed.

## Required tests

- Focused offline supervisor tests using a fake host and clock for all acceptance paths, including unreadable check evidence and owner restart.
- Exact-head CI, a severe diff review, and an independent full diff review because this changes supervisor control flow.

## Definition of done

The acceptance criteria hold and the required evidence is recorded on the implementation PR. The registry reflects the implementation state.
