# 277 — The resume cap measures the context a resumed turn starts from

Status: ready
Depends on: none

## Goal

A coordinator session is resumed only while the context it would start from is small enough to leave room for the
next turn, and that size is read from where the agent tools actually write it.

## Code facts checked on `origin/master` at `e4ae9e5a`

- `scripts/relay_continuity_probes.py::turn_context_tokens` and
  `scripts/relay_continuity_turns.py` (the `TurnReport` built from `state.get("context_used_tokens")`) read
  `context_used_tokens` at the top level of a turn's `state.json`. Real turn state files written by Relay keep it under
  `metadata` (`metadata.context_used_tokens`, next to `metadata.context_window_tokens` for claude). The value is never
  found, so the cap never fires. The spec 267 tests write the key at the top level.
- That value is the turn's cumulative usage across all model calls, not the context size: a claude turn on
  2026-11-15 recorded 2 166 425 with a context window of 200 000.
- `scripts/relay_continuity_supervisor.py` compares it with
  `scripts/relay_continuity_common.py::TURN_USAGE_CAP_TOKENS` (2 000 000) in two places: the retry/resume choice and
  the parent choice before launch.
- The context size of the last model call is in the turn's `output.jsonl` for claude: the `usage` of the last
  `assistant` event (`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`). Over 181 real claude
  turns: median 60 400, 90th percentile 125 758, maximum 312 000. Codex output carries only the cumulative
  `turn.completed` usage (no per-call size).

## Scope

1. Replace `turn_context_tokens` with a reader of the last-call context size: for claude turns, the last assistant
   `usage` in `output.jsonl` as above. For any agent whose output has no per-call usage (codex today), return `None`
   (no cap from this measure), never the cumulative total. Keep reading `metadata.context_used_tokens` only as the
   `TurnReport` cumulative usage field, from `metadata`, with the top level accepted for older files.
2. Rename the cap to `RESUME_CONTEXT_CAP_TOKENS = 150_000` (about the 95th percentile of the real distribution) and
   use it in both supervisor sites.
3. Tests use a `state.json` and an `output.jsonl` copied from a real claude turn (trimmed, no private text) and a real
   codex turn shape.

Out of scope: per-call measurement for codex (a later spec, if its session files prove usable).

## Acceptance criteria

1. With the copied real claude turn whose last call used more than the cap, the next turn starts a fresh context; with
   one below the cap it resumes (tests).
2. A codex turn never triggers the cap from cumulative usage (test).
3. `TurnReport.context_used_tokens` is filled from `metadata.context_used_tokens` of the real file (test).
4. **Real run.** After the live owner runs a release with this change, one resume decision in its log shows the
   measured size.

## Definition of done

Test gate green, CI green on the exact head, full diff review by a second qualified session, and criterion 4 seen in
the live owner.
