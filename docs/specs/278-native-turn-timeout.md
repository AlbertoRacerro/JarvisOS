# 278 — Native turn timeouts settle without an output hold

Status: ready
Depends on: none

## Goal

A native coordinator turn stopped by the supervisor's wall limit is recorded as a timed-out attempt. Its unfinished effects are reconciled before another attempt, and a missing runner result from that stop does not hold the mission for `output_unparseable`.

## Code facts checked on `origin/master` at `5fac7ad3`

- `scripts/relay_argon.py::run` writes `native-result.json` in a `finally` block, but has no SIGTERM handler. A supervisor SIGTERM exits the process without reaching that block. The native runner allows `TURN_TIMEOUT = 3600` seconds for `agy`, plus 30 seconds for its subprocess timeout.
- `scripts/relay_continuity_common.py::Limits` gives every turn a 3600-second `turn_wall`. `scripts/relay_continuity_adapters.py::RelayAdapter.finish` sends SIGTERM to the process group at that limit and returns `RunResult.timed_out = True`.
- `scripts/relay_continuity_supervisor.py::Supervisor._launch` saves `pending["timed_out"]` before settlement. `Supervisor._settle` passes the timeout diagnostic to `classify_turn`, but does not make the trusted timeout available to `gate`. An adopted process is also stopped on the wall limit in `Supervisor._resolve_pending`.
- `scripts/relay_continuity_turns.py::classify_argon_turn` increments `bad_lines` when events exist without `native-result.json`; it can increment them again when the init model cannot match missing metadata. `scripts/relay_continuity_turns.py::gate` then returns `output_unparseable`. The existing runner `reason == "timeout"` maps to `turn_time_limit` only after metadata is read.
- In a retained native turn cut at the wall limit, `stderr.log` ended with `error: interrupted`, `native-result.json` was absent, and the owner held on `output_unparseable`. Its recorded output contained a terminal error event, not a successful completion.

## Scope

1. Give the native runner an orderly SIGTERM path that writes `native-result.json` with a non-success signal or timeout reason, restores its signal handler, and exits without promoting partial model output to success. Keep the atomic result write.
2. Give native turns enough supervisor wall time for the runner's own `TURN_TIMEOUT` and its 30-second subprocess grace to settle first. Apply the same native limit to a newly launched and an adopted turn; retain the existing non-native wall limit.
3. If the supervisor itself records `pending["timed_out"]`, classify a native turn as `unknown` with harness cause `turn_time_limit`, even when its runner metadata is missing or its output ends mid-event. Treat this as a trusted harness cause, never as model success. Keep the retained tool calls and the existing external-effect reconciliation; an unproven external effect remains parked.
4. A missing or malformed native result without the supervisor's recorded timeout keeps the existing output gate.

## Acceptance criteria

1. An offline test sends SIGTERM to a native runner during a fake in-flight command and checks that the atomic result file exists, records non-success, and leaves no success message.
2. A test based on the retained shape of a timed-out native turn (trimmed of user content) has no runner result and an incomplete or terminal-error stream. With the supervisor's durable timeout flag it settles as `turn_time_limit`, never `output_unparseable`; without that flag it still gates.
3. The timed-out attempt cannot complete a mission task. Its recorded external calls are reconciled or parked before retry, and an unproven call is not repeated automatically.
4. The native supervisor wall limit exceeds the runner's subprocess timeout plus cleanup grace; non-native turns keep their current limit. A restart adopting a native turn uses the same limit.
5. After deployment, a real owner run shows a native timeout, or a controlled temporary owner run with the real runner process proves the signal and settlement path. The owner must not require a maintainer release for this harness timeout.

## Definition of done

Focused tests and lint pass; CI is green on the exact head; a second qualified session reviews the full diff; criterion 5 is proven in a real process.
