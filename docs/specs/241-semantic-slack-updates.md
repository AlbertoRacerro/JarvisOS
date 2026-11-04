# 241 — Semantic Slack updates

Status: merged
Depends on: none

## Goal

The maintainer's Slack digest and alerts say, in plain Italian, what JarvisOS development is doing now, what got done,
what comes next and whether the maintainer has to act. Coordinators write that sentence themselves each turn; nothing
machine-oriented (SHAs, mission/job/turn ids, cycle numbers, paths, commands, risk tiers, model codenames, raw English
log text) reaches Slack. Technical detail stays in the ledger and Coding > Runtime.

## Why

Maintainer directive `L20261104105458-8329`: the digest today repeats ids, boilerplate and the coordinator's raw English
claim text. The maintainer wants to read "Sto aggiungendo la colonna Copilot al pannello quote" and nothing else.

## Scope

In scope:
- A maintainer update recorded by the coordinator each turn as a ledger note in lane `maintainer`
  (`devctx.py note note --lane maintainer "Sto …. Prossimo: …"`), one or two plain Italian sentences, an optional
  `Prossimo:` part. `docs/PROCESS.md` gains one line telling coordinators to write it.
- `scripts/relay_observe.py`: the snapshot picks up the latest maintainer update; `digest_text` renders
  "Sto facendo", "Fatto", "Prossimo", "Serve il tuo intervento" from human sources only; `alert_text` and the incident
  descriptions say in plain Italian what happened and what the maintainer should do ("scrivi a Claude in chat" or the
  existing one-click choice), never CLI commands or ids.
- Fallback when no update was written for the current mission: a plain sentence from the queue item/spec subject,
  never the raw claim/focus note or the turn's English `reason`/`next_step`.
- Merged PRs shown by their human subject (conventional-commit prefix and `(Spec NNN)` suffix removed) with a link;
  the link target may carry the number, the visible text does not.
- Boilerplate (the "perché: roadmap autonoma…" line, per-job status lines) dropped.

Out of scope (binding non-goals):
- No change to the supervisor's mission/roadmap state machine, delivery channel, scheduling or the Runtime panel.
- No new devctx kind, no new storage, no translation model call.
- No blocking guard: the mechanical check is a test, not a runtime filter that suppresses messages.

## Files likely touched

- `scripts/relay_observe.py`
- `backend/tests/test_relay_observe.py`
- `docs/PROCESS.md` (one line)

## Design constraints

- `relay_observe` stays read-only over owner state and the ledger.
- A maintainer update containing a machine marker (hex run of 7+ chars, `m-`/`j-`/`L2026…` ids, a path, a backtick
  command) is not shown; the fallback is used instead.
- Open-question buttons and alert edge-triggering keep their current behaviour.

## Acceptance criteria

1. With a maintainer update in the ledger since the mission was submitted, the digest's "Sto facendo" line is that text
   and "Prossimo" is its `Prossimo:` part.
2. Without one, "Sto facendo" is a plain sentence from the task label; the claim text and turn reason never appear.
3. Digest and alert texts built from a realistic snapshot (mission, jobs, roadmap cycle, claims with SHAs and paths,
   merges, held/paused/owner-down states) contain no hex run of 7+ characters, no `m-`/`j-`/`L2026` id, no path and
   no backtick command (test).
4. Merges render as `• <link|human subject>`.
5. `docs/PROCESS.md` tells the coordinator to write the maintainer update every turn.

## Required tests

- `backend/tests/test_relay_observe.py`: criteria 1–4 offline with fake snapshots and ledger entries; existing digest
  tests updated to the new wording, not deleted.

## Definition of done

Test gate green, criteria met, STATUS row `in_review` then `merged`, the live digest observed once in the real
environment after merge (owed live activation).
