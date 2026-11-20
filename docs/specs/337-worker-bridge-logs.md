# 337 — WORKER-BRIDGE-LOGS-1: the OpenCode worker bridge records why requests stall

Status: ready
Depends on: 334

## Goal

A temporary-pool worker run leaves enough evidence to tell a provider stall from a harness fault, and its result
names the changed files correctly. No behaviour of the worker changes otherwise.

## Why

Directive `L20261120084344-afc2` item 4 (bridge first: when a well-scored free model fails, suspect the connection).
Three consecutive implementation runs on the same free pool finished useful work but hit the wall-clock timeout.
Their JSON transcripts show about half of the model steps start, produce no `step_finish`, and are restarted 130–170 s
later, with no error event in the stream and an empty `stderr.txt`. The cause cannot be isolated from that evidence:
OpenCode's own logs, which name retries and their reasons, are not kept. Separately, `result.json` drops the first
character of the first changed path (`ackend/...`).

## Code facts checked at `9e0185f6`

- `scripts/opencode_worker.py::run` (around the `argv = sandbox + [binary, "run", "--standalone", ...]` line) starts
  `opencode run --standalone --model ... --format json --auto <task>` and writes stdout to `transcript.json` and
  stderr to `stderr.txt`, both with the key replaced by `[REDACTED]`.
- OpenCode 2.0.12 accepts `--print-logs` (logs to stderr; server logs need `--standalone`, already passed) and
  `--log-level <debug|info|warn|error|...>`.
- `scripts/opencode_worker.py::changed_files` calls `.strip()` on the whole `git status --porcelain` output, removing
  the leading space of the first line's two-character status, so `line[3:]` cuts one character of that path.
- Tests: `backend/tests/test_opencode_worker.py` (fake `opencode` binary).

## Scope

In scope:
- Pass `--print-logs --log-level info` to `opencode run`, so `stderr.txt` holds OpenCode's logs (key still redacted).
- `changed_files`: parse the porcelain output without stripping leading whitespace; the first path is exact.
- `result.json` gains `steps: {"started": n, "finished": n, "max_gap_seconds": x}` computed from the transcript's
  `step_start`/`step_finish` events (unparseable lines are skipped; all zero when there is no transcript).

Out of scope (binding non-goals):
- Changing timeouts, retries or provider options before the logs show the cause (a follow-up decides from evidence).
- Any change to routing, admission or the pool file.

## Files likely touched

- `scripts/opencode_worker.py`
- `backend/tests/test_opencode_worker.py`

## Design constraints

- The key never appears in any evidence file (existing redaction covers stderr; keep it).
- Log level `info`, not `debug`, to keep evidence small; no prompt bodies are added to the logs by this change.

## Acceptance criteria

1. The fake-binary test sees `--print-logs` and `--log-level info` in the argv of `opencode run`.
2. With a worktree whose first porcelain line is ` M backend/x.py`, `changed_files` returns `backend/x.py`.
3. A transcript with 3 `step_start`, 2 `step_finish` events and a 140 s gap gives
   `steps == {"started": 3, "finished": 2, "max_gap_seconds": 140.0}` (within rounding); an empty transcript gives zeros.
4. One real run on the free pool keeps OpenCode logs in `stderr.txt` with the key absent; the coordinator records what
   the logs say about the stalled steps.

## Required tests

- Unit tests for criteria 1–3 in `backend/tests/test_opencode_worker.py`.
- Criterion 4 is a live run with its evidence directory.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
