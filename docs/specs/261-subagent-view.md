# 261 — SUBAGENT-VIEW-1: live sub-agents in the chat

Status: planned (draft for maintainer review)
Depends on: 259 (Hermes v0.23.0), 260 (named roles)

## Goal

When Hermes runs a sub-agent under a role, the Jarvis chat shows it live: the role name, a small mascot in the Bubu
style, what it is doing and when it finishes, driven only by real Hermes sub-agent events. "Not everything has to be
useful": the mascot is allowed to be playful, but it never animates without an event behind it.

## Why

Maintainer directives `L20261109220309-dfe1` and `L20261109220757-0951` item 3. Hermes v0.22+ already shows and
steers sub-agents in its own surfaces; JarvisOS should show the same reality in its chat.

## What Hermes v0.23.0 provides (checked in the source)

- `delegate_task` children report through the parent's `tool_progress_callback` with lifecycle events
  `subagent.spawn_requested` (goal preview), `subagent.start`, `subagent.text` (streamed child text) and
  `subagent.complete` (status and a redacted result preview); `tools/delegate_tool_progress.py`,
  `tools/delegation_live_log.py`.
- Delegation backends are pluggable since v0.23.0 (`delegation.backend`; the default strategy is a bundled plugin).

## Decision needed from the maintainer (blocks implementation, not this draft)

JarvisOS disables the Hermes `delegation` toolset today (`worker_shim.DISABLED_TOOLSETS`) because a child run's
actions cannot yet be presented to and authorized by the Jarvis capability broker. Without delegation there are no
sub-agent events to show. Options:

1. **Broker-bounded delegation (recommended).** Enable `delegate_task` only with the default backend, children
   inherit exactly the parent role's admitted tools (no widening, `withhold_tools` applied to every child), a hard cap
   on concurrent children and depth 1, and every child event recorded on the thread. Then build the view.
2. **View only for Jarvis-side work.** Keep Hermes delegation off; show Jarvis-orchestrated role calls (e.g. Isaac
   asking Gregor through a Jarvis tool) as "sub-agents". Smaller authority change, but not Hermes sub-agents.
3. **Defer** until a concrete task needs delegation.

## Scope (after the decision)

In scope:
- The worker shim forwards `subagent.*` progress events as bounded, redacted thread events (no raw child transcripts
  beyond the preview Hermes already redacts).
- A chat strip above the composer listing running and just-finished sub-agents: mascot, role name, goal preview,
  elapsed time, final status. Mascot art per role (Isaac, Sheldon, Gregor, Q, Alfred) in the existing Bubu style.
- Stop for a running child only if Hermes' existing interrupt reaches it; no new steering authority.

Out of scope: new tools or permissions for children; multi-level delegation; a fake or idle animation.

## Acceptance criteria (draft)

1. Every chip in the strip corresponds to a recorded `subagent.*` event on the thread (test with a fake worker that
   emits the four events).
2. No child gets a tool outside its parent role's admitted set (test).
3. AI browser walk from `/` shows a sub-agent chip appearing and completing in a Hermes chat on the fake route before
   the maintainer looks.
