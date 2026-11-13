# 266 — Deliver maintainer questions even when their format is incomplete

Status: ready
Depends on: none

## Goal

Every unsettled ledger question reaches the maintainer once. New questions use the structured format in `docs/PROCESS.md`; older questions remain visible and answerable even if their options cannot be parsed.

## Code facts

- `scripts/devctx.py` `command_note` currently records `question` text without checking option lines.
- `scripts/relay_observe.py` `parse_open_questions` skips a question when no option line matches its parser. `snapshot` and `observe_step` then cannot expose or send it.
- `scripts/relay_observe.py` `record_answer` uses `parse_open_questions` to decide whether a question can be answered. The observer records sent question IDs in `sent_questions`.

## Scope

1. Reject a new `devctx.py note question` with fewer than two separate, sequential numbered option lines. Explain the required format in the command error. Reject inline alternatives; do not write a ledger entry on rejection.
2. Return every unsettled legacy question from `parse_open_questions`. If no options parse, expose the bounded, cleaned original text as its prompt with an empty option list. `question_text` sends that prompt once without made-up choices. The existing free-text answer path works for it.
3. Preserve the existing once-only send behavior and do not resend already recorded question IDs. Previously unsent, still-open questions become eligible for the next observer pass.

## Non-goals

- Rewriting historical ledger entries, generating options for them, changing answer authority or delivery transport.
- Resending settled or already sent questions.

## Acceptance criteria

1. Structured new questions with two to four numbered option lines are recorded; malformed ones fail with an actionable error and no ledger mutation.
2. An unsettled unstructured legacy question appears in the observer snapshot and is delivered once with free-text answering available. Structured questions keep their existing options.
3. A settled question is absent. A previously sent question is not sent again.

## Evidence

Focused offline tests cover validation, legacy delivery and settlement, with the observer's fake transport. Run targeted Ruff and exact-head CI. This is supervisor code: review the full diff in a second qualified session before merge.
