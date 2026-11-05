# 244 — Slack questions with free-text answers

Status: ready
Depends on: none

## Goal

Every open structured maintainer question appears in Slack at once with the same text the maintainer sees in Jarvis:
the question, then the numbered options with their consequences, the recommended one first, in plain Italian, no
buttons. The maintainer answers in Slack with free text ("fammi opzioni 4,1,3,2 e 5 nell'ordine, poi fammi x e y");
the answer is recorded in the ledger exactly like a Jarvis UI answer, Slack confirms receipt in one short line, and a
roadmap paused for lack of progress wakes up on that answer.

## Why

Maintainer directive `L20261105144536-3af0` item 1: question `L20261105130109-901c` reached Slack, at most, as the
generic "rispondi a Claude in chat" alert, and only when the roadmap paused as `coordinator_blocked`. The roadmap had
paused as `no_progress` with `wake_at` five days later, so the answer given in Jarvis woke nothing.

## Scope

In scope:
- **Outbound question.** The watcher's observation step (`relay_observe.observe_step`) sends each open question from
  `parse_open_questions` once (durable record, also across watcher restarts), independent of the roadmap state. Text:
  the prompt, then `N. label — conseguenza: …` lines in ledger order with the recommended option marked
  "(consigliata)", then one line saying the maintainer can answer here in free text. Sent with the bot token
  (`chat.postMessage`) to the designated channel, so its `ts` is kept for thread mapping. Without a bot
  configuration, sent through the existing webhook with no inbound path. The generic "rispondi a Claude in chat"
  text is never sent while a structured open question exists.
- **Inbound answer by polling** (`conversations.history` and `conversations.replies` for threads of sent questions)
  in the host-side watcher loop, at most every 30 s, through the same stdlib opener rules as the webhook (no proxies,
  redirects rejected, host `slack.com` only). Durable cursor so a message is processed once.
  - Accepted only when: channel is the designated channel; `user` equals the configured maintainer user id; no
    `bot_id`, no `subtype`; text 1..1000 characters; `devctx.find_secret` finds nothing. Everything else is ignored
    without a reply.
  - Mapping: a thread reply answers the question posted at that thread's root; a top-level message answers the single
    open question; with more than one open question a top-level message gets one reply asking to answer in the thread
    of the question. With no open question it gets one reply saying there is no open question.
  - Recording: the same ledger decision as the Jarvis UI path, `status=accepted`, `supersedes=[question]`, text
    `Maintainer answer to question <id> (via Slack): <text>`, author `maintainer (slack)`. The record-and-wake logic
    is shared with `backend/app/modules/coding/relay_monitor.answer_question`, not duplicated.
  - Confirmation: one short Italian line in the thread, e.g. "Ricevuto, l'ho registrato come tua risposta."
- **Wake.** Recording an answer (Jarvis or Slack) unpauses a roadmap paused `coordinator_blocked` or `no_progress`.
  A roadmap paused `no_progress` also wakes when a new maintainer directive or maintainer answer is recorded after
  `paused_at`, or a host-ops receipt completes after `paused_at`, regardless of `wake_at`.
- **Configuration.** One host file `~/.config/jarvis/slack_bot.json` (`bot_token`, `channel_id`,
  `maintainer_user_id`); refused unless owner-only (mode 0600) and owned by the current user. Absent file: questions
  go through the webhook, inbound is off.
- Short step-by-step setup instructions in plain Italian (create the Slack app, scopes `chat:write`,
  `channels:history` or `im:history`/`groups:history` for the chosen channel, install, copy bot token, channel id and
  user id, write the file with mode 0600), kept with the code's operator docs and sent to the maintainer as a
  structured question once the code is merged.

Out of scope (binding non-goals):
- No public inbound endpoint, no Events API webhook, no Socket Mode, no new dependency.
- No buttons, interactive components or slash commands.
- No new authority: a Slack answer is a ledger decision with exactly the effect of a Jarvis UI answer. It cannot
  create directives, missions or claims, start or stop roadmap mode, approve host operations, grant credentials,
  `trust_master`, host installs or egress of pack or IP data.
- No change to digest/alert wording rules of spec 241 other than replacing the generic question alert.
- Host install and watcher restart are done by the maintainer's host session, not by this change.

## Files likely touched

- `scripts/relay_continuity.py` (bot config, Slack Web API client, polling in the watch loop, roadmap wake)
- `scripts/relay_observe.py` (question rendering, sent-question record)
- `backend/app/modules/coding/relay_monitor.py` (shared answer recording)
- `backend/tests/test_relay_continuity*.py`, `backend/tests/test_relay_observe.py`, `backend/tests/test_supervisor_ui_240.py`

## Design constraints

- The bot token never appears in the ledger, events, logs, receipts, Slack text, exceptions or the sandbox; errors
  carry an error class or HTTP status only. Slack API responses are not logged.
- The listener runs only in the host-side watcher; nothing in the sandbox or backend reads `slack_bot.json`.
- Recording takes the roadmap lock and re-checks the question is still open, as the UI path does.
- Delivery and polling failures never raise into the supervisor loop.

## Acceptance criteria

1. With one open question in the ledger, one `observe_step` sends exactly one message whose text contains the prompt
   and every option as `N. label — conseguenza: …`, recommended marked; a second step and a restarted watcher send
   nothing more; the generic "rispondi a Claude in chat" text is not sent.
2. A message from the maintainer user id in the designated channel records one decision superseding the question,
   author `maintainer (slack)`, and posts one confirmation; the same message seen again records nothing.
3. Messages from another user, a bot, a message subtype, another channel, an over-long text or a text matching a secret
   pattern record nothing and get no reply.
4. Thread reply maps to its question; a top-level message with two open questions records nothing and gets the
   "rispondi nel thread" reply.
5. A roadmap paused `no_progress` with `wake_at` in the future resumes after a Slack or Jarvis answer, after a new
   directive, and after a completed host-ops receipt; without any of these it stays paused.
6. A config file with mode wider than 0600 is refused and inbound stays off; no test output, log or ledger entry
   contains the token (test with a fake token).
7. The Slack client refuses non-`slack.com` hosts and redirects, with proxies disabled.

## Required tests

- Offline tests with a fake Slack transport and a temporary ledger/control root for criteria 1–7. No network.

## Definition of done

High risk (inbound channel steering the supervisor): test gate green, exact-head CI green, a second-session full diff
review, STATUS `in_review` then `merged`, then after the maintainer's host install the live proof: a real question
reaches Slack and a real Slack reply is recorded and confirmed.
