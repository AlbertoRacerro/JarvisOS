# 374 — HERMES-LOCAL-TURN-FIXES-1: Keep tool grants alive for the whole turn, stop the format echo, and keep patent fields visible

Status: in_review
Depends on: 371, 372

## Goal

Three harness defects found in the spec 372 chat gate no longer cost a local Hermes turn its answer: the per-interaction
tool grants stay valid for as long as the turn itself may run, so `jarvis_python` is not refused after ten minutes; the
answer format is described in words and an echoed empty format block is stripped from the final answer; and a
`jarvis_patent_read` result reaches the model with its bibliographic fields, claim 1 and an explicit register status.
Two methodology rules from the same grading are added to the local prompt.

## Why

Directive L20261125154544-9ec2 graded the 372 gate by hand and named these defects as costing points independent of
the model, to be fixed before the hard set is re-run twice. The reliability-techniques work after it is only measurable
once the harness stops losing answers.

## Code facts checked at `1d1dd9a9`

- `backend/app/modules/ai/thread_service.py::_install_hermes_retrieval_grant` issues the interaction's read grants
  (retrieval, decide, dev_read, repo_read, knowledge_read, paper_search, patent_read, paper_pdf, browser_page) with
  `expires_at=now + timedelta(minutes=10)`, after `worker.live_grants.clear()`.
- `backend/app/modules/ai/thread_service.py::_install_surface_grants` issues the `jarvis.calc_run` grant and the
  surface read/act grants with the same fixed ten minutes.
- `backend/app/modules/agents/hermes/supervisor.py::turn_deadline` gives a turn `max(600, min(turn_cap * step_seconds
  + 120, 3600))` seconds; `HermesSupervisor.turn` uses it as the turn timeout, so a local turn may run up to an hour.
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` returns `capability_denied` with no text when the
  grant is no longer active (`CapabilityGrantRef.is_active`), the same code as an unknown grant.
- In the 372 gate (fresh root, task H5) eight `jarvis_python` calls succeeded and the ninth, about eleven minutes after
  the session was bound, was refused with `capability_denied`; the model then wrote that Python was "refused" and gave
  no numbers. Checked in the gate's proof database: `hermes.session_bound` at 15:07:18, the refused
  `hermes.tool_result` at 15:18:47. The research budget and the repeated-call rule
  (`backend/app/modules/agents/hermes/worker_shim.py::Worker.claim_tool_call`) do not apply to `jarvis_python`.
- `backend/app/modules/agents/hermes/worker_shim.py::COMPACT_METHODOLOGY` ends with the literal block
  `Answer: 1-3 sentences\nNumbers:\nSources:\nCaveats:`; `STEP_CAP_MESSAGE` and
  `backend/app/modules/agents/hermes/supervisor.py::CONTINUATION_REQUEST` refer to "the format". Answers H3 and H5
  began with that literal block followed by the real answer.
- `backend/app/modules/agents/hermes/supervisor.py::missing_final_answer` treats any `Answer:` line
  (`_ANSWER_LINE`) as a final answer, so the echoed template alone satisfies it.
- `backend/app/modules/agents/hermes/patent_read_tool.py::execute_patent_read` returns title, applicants, priority,
  filing and publication dates, the advisory Google status, claim 1, and `official_status` /
  `official_status_error` from the EPO Register. Probed live once on 2026-11-25 for the H6 publication: every Google
  field was filled; the register answered `http_403_challenge`, so `official_status` was `unverified`.
  `backend/app/modules/agents/hermes/worker_shim.py::compact_tool_payload` on that result kept every field.
- Not verified: what the model actually received in H6 (the gate stores only result digests). The answer said the tool
  returned "status/family metadata only" and "status not confirmed"; the likely causes are the later 700-character
  trim of older results (spec 372) cutting claim 1, and the bare `unverified` status read as "nothing confirmed".

## Scope

In scope:
1. **Grant lifetime = turn lifetime.** Every grant issued by `_install_hermes_retrieval_grant` and
   `_install_surface_grants` expires at issue time plus the worker's own `turn_deadline(...)` plus a 60 s margin,
   computed from the worker's `step_deadline_seconds` and `turn_cap` (read with defaults where a fake worker lacks
   them). Grants stay per interaction: `live_grants.clear()` at the next interaction is unchanged.
2. **Refusals carry a reason.** When `dispatch_tool` refuses because the grant is unknown, expired or of another
   capability, the tool result carries a short `message` the model can act on (for example "grant expired: answer with
   what you have" or "unknown grant_id"); an expired grant uses the error code `grant_expired`. A `jarvis_python` call
   with bad arguments says which argument is wrong (`code` missing, not a string, or over the size limit).
3. **Format as a description.** `COMPACT_METHODOLOGY`, `STEP_CAP_MESSAGE` and `CONTINUATION_REQUEST` describe the
   answer format in words (a final message without tool calls whose first line starts with `Answer:` and has one to
   three sentences, followed by `Numbers:`, `Sources:` and `Caveats:` sections) and never contain the bare template
   lines.
4. **Strip an echoed empty template.** One deterministic function removes a leading block made only of the four
   format labels with no content of their own (`Answer: 1-3 sentences`, `Numbers:`, `Sources:`, `Caveats:`, in that
   order, optionally run together with the following text) from a local-route final answer before it is returned to
   the thread. `missing_final_answer` ignores such a block, so a step that is only the empty template still gets the
   continuation request.
5. **Patent result readable.** `execute_patent_read` puts the bibliographic fields and claim 1 first in its result
   and, when the register is not read, sets an explicit sentence such as "official legal status not verified: the EPO
   Register refused automated access (HTTP 403)" next to the advisory Google status. The 700-character trim of older
   tool results keeps claim 1's first 300 characters for a patent result (or the trim is shown not to reach it by a
   test through the real history path).
6. **Methodology rules.** The local methodology adds: "Use every given input; if you leave one out, say why." and
   "If your own check shows an inconsistency, fix it before answering."
7. **Gate evidence.** The paired chat gate is re-run twice on a fresh root for the hard set (and once for the example
   set) at the implementation head; results report per task score, completion, tool calls and decode speed (H6 decode
   speed is reported, not tuned).

Out of scope (binding non-goals):
- Extending the patent tool to the EPO OPS API or any new egress host (needs a maintainer credential; PARK).
- The verification pass, k-sample voting or other reliability techniques (next step of the program).
- The maintainer-first scheduling and effort-by-task-type work of directive L20261125155527-273f (its own spec).
- Cloud-route prompts, grants or behaviour beyond the shared refusal message.

## Files likely touched

- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/agents/hermes/calc_run_tool.py`
- `backend/app/modules/agents/hermes/patent_read_tool.py`
- `backend/tests/` (new or extended Hermes tests)

## Design constraints

- Grants keep their scope, capability and per-interaction clearing; only `expires_at` changes. No grant outlives one
  hour plus the margin.
- Refusal messages never include grant ids, paths, secrets or argument values.
- The template strip removes only the exact empty-label block at the start of the answer; any line with its own
  content is kept unchanged. It is deterministic code, no model call.
- `jarvis_python` keeps its offline sandbox path, limits and fail-closed behaviour from spec 371.
- No new dependency, setting or schema change.

## Acceptance criteria

1. A test with a fake clock shows a `jarvis_python` call 15 minutes after the interaction started succeeds on a worker
   whose `turn_deadline` exceeds 15 minutes, and is refused with `grant_expired` and a message after the deadline plus
   margin.
2. Every grant installed by `_install_hermes_retrieval_grant` and `_install_surface_grants` has
   `expires_at - issued_at` equal to the worker's turn deadline plus 60 s.
3. A refused call for an unknown grant returns `capability_denied` with a message; a `jarvis_python` call without
   `code` returns `invalid_arguments` with a message naming `code`.
4. `COMPACT_METHODOLOGY`, `STEP_CAP_MESSAGE` and `CONTINUATION_REQUEST` contain no line equal to `Numbers:`,
   `Sources:` or `Caveats:` and no `Answer: 1-3 sentences`; the methodology contains the two rules of scope item 6.
5. The strip function turns the H5 answer shape (`Answer: 1-3 sentences\nNumbers:\nSources:\nCaveats:Answer: I could
   ...`) into the answer starting at the second `Answer:`, leaves a normal answer byte-identical, and is applied to the
   local final answer; `missing_final_answer` returns the text for a reply that is only the empty template.
6. A test through the local tool-history path with a fake patent opener shows claim 1's opening text and the register
   sentence still present in the model messages after four later tool results.
7. Two hard-set gate runs and one example-set run at the implementation head are recorded with their result paths;
   no run shows `jarvis_python` refused for an expired grant.

## Required tests

- Offline unit tests for grant lifetime, refusal messages, the template strip and `missing_final_answer`, the prompt
  constants, and the patent result order and register sentence with a fake opener; no network.
- Existing Hermes, calc-run and patent-read tests stay green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, gate evidence recorded, spec status updated. Risk:
medium (feature code with tests; grants change lifetime only, not scope).
