# 368 — HERMES-LOCAL-TURN-BUDGET-1: Small local prompts, speed-derived turn deadlines, clean worker recovery and announce-only turn detection

Status: in_review
Depends on: 352, 362, 366

## Goal

A Hermes chat turn on the local default model starts from a prompt of at most about 4k tokens before the
question, gets a deadline derived from the measured speed of the active model, and never leaves a stuck worker
behind for the next turn. A model step that only announces an action ("Let me verify the numbers…") is not
accepted as the final answer. A step cut by the output cap is recorded and retried once when nothing usable came
back. The agent eval gate reports tokens per request and decode speed, and refuses a per-task timeout shorter
than the worker's turn deadline.

## Why

Directive `L20261124193224-8c59` (items 1–4 and 6). In the first live chat-bench run the harness, not the model,
was the bottleneck: requests started at about 15k tokens, so most of the KV cache sat in host RAM and decode fell
to about 10 tok/s. Turns hit the 600 s deadline and the following turns failed within about 45 s. Two turns ended
"completed" with only an announcement, and some steps hit the 2048-token cap. Item 5, a larger context, waits
until this spec is merged and measured. Raising the context is out of scope here.

## Code facts checked at `5b6cac2d`

- `backend/app/modules/ai/thread_service.py::_turn_tools` gives every Hermes turn the same 13 general tools plus surface tools, so 13–17 tool schemas reach the model on every step.
- `backend/app/modules/agents/hermes/supervisor.py::infer_envelope` serialises the full message list and the full tool JSON schemas into the envelope prompt, adds `RELAY_TOOL_PROTOCOL`, and sets `deadline_at` from a fixed `deadline_seconds=120`.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_relay` builds the per-step envelope and calls `run_governed_inference` with `tool_response_schema(tools)`.
- `backend/app/modules/agents/hermes/supervisor.py::run_governed_inference` nudges once only when `is_dead_turn` sees no tool call and an empty answer; non-empty announcement text passes as an answer.
- `backend/app/modules/agents/hermes/supervisor.py::is_dead_turn` and `backend/app/modules/agents/hermes/worker_shim.py::is_dead_turn` are duplicate empty-answer checks.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.turn` waits with a fixed `turn_timeout=600`; on timeout `HermesSupervisor._await` raises and nothing interrupts or restarts the worker.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker.run_turn` holds `turn_lock` until the upstream conversation returns; a second turn meanwhile returns `turn_busy` at once.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` waits a fixed 180 s per relay step and defaults `max_output_tokens` to 2048 for `general` steps.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._build_agent` builds the upstream `AIAgent` with toolsets `mcp-jarvis`, `memory`, `session_search` and the role instructions as `ephemeral_system_prompt`.
- `backend/app/modules/agents/hermes/supervisor.py::_assemble_turn_prompt` appends `ENGINEERING_METHODOLOGY_RULES` to every turn prompt.
- `backend/app/modules/agents/hermes/broker_mcp.py::main` serves the MCP tool definitions and descriptions.
- `backend/app/modules/local_ai/runtime/model_registry.py::get_active_model_entry` returns the active local model entry.
- `scripts/benchmarks/agent_eval/runner.py::run_eval` runs the chat-path eval with a per-task `timeout` that defaults to 120 s.
- `scripts/benchmarks/agent_eval/runner.py::build_parser` defines the eval CLI.

## Scope

In scope:

1. **Prompt budget for local routes.**
   - A deterministic per-turn tool router in `thread_service` (or a helper it calls) replaces the fixed list.
   - Every turn keeps a small core: `jarvis_turn_plan`, `jarvis_retrieval_query`, `jarvis_knowledge_read`.
   - Tool groups are added only when the question or the surface calls for them:
     - research: `paper_search`, `paper_pdf_read`, `browser_page_read`;
     - patent: `patent_read`;
     - code: `repo_read`, `dev_read`;
     - the surface read/act pair;
     - bench, on `bluecad`.
   - Groups are chosen by fixed keyword/surface rules, with no model call. A turn may have at most 7 model-visible tools.
   - When no group matches, the turn gets the core plus research.
   - `memory`, `session_search`, `jarvis_context_preview` and `jarvis_decide` leave the default local set. They stay admitted for explicit use, so `HERMES_TOOL_ALLOWLIST` and `ADMITTED_TOOLS` do not change.
   - For a `local:` route the relay sends compact tool schemas:
     - the first sentence of each description, at most 160 chars;
     - property descriptions dropped;
     - `enum`, `type`, `required` and bounds kept.
   - For a `local:` route, the upstream Hermes system message is replaced by a compact Jarvis system message: the role instructions and one compact form of the methodology rules. The governance wording does not change.
   - A deterministic token estimate (`ceil(chars/3.5)`, labelled estimate) covers base prompt plus tool schemas. It is recorded on each relay step's `ai_jobs` metadata as `prompt_tokens_estimate`.
   - A unit test fixes the budget: a first step with an empty history and a short question estimates ≤ 4,000 tokens for each surface.
2. **Speed-derived deadlines and clean recovery.**
   - Per-step seconds come from the active entry's last 20 successful `ai_jobs` rows (`input_tokens`, `output_tokens`, `latency_ms`):
     - `prefill_s + max_output_tokens / decode_tps`, times 1.5;
     - with fewer than 3 rows: 240 s.
   - The step deadline is clamped to [120, 900] s.
   - The turn deadline is `min(turn_cap × step_deadline + 120, 3600)`, and at least 600 s.
   - The step deadline is used by `infer_envelope`'s `deadline_at`, the worker's relay wait (passed in the turn frame) and the tool wait.
   - The turn deadline is used by `HermesSupervisor.turn`.
   - On a turn timeout the supervisor sends `interrupt`. If no `turn_result` arrives within 30 s, it kills the worker process and marks it for `_recover` before the next turn.
   - The timed-out interaction is marked failed with reason `turn_deadline`.
   - The next turn must not see `turn_busy` or an immediate failure.
3. **Announce-only steps.**
   - One shared helper replaces both `is_dead_turn` copies. It also flags a step as an announcement when all of these hold:
     - there is no tool call;
     - tools were offered and the step is not capped;
     - the answer is under 400 chars;
     - it starts with or consists of a forward-action phrase from a fixed list ("let me", "i'll", "i will", "now i", "next, i", "i am going to", "i'm going to");
     - it contains no digit.
   - An announcement gets one continue nudge.
   - A second announcement in the same step forces a final answer: tools removed, reasoning phase `final`.
   - Each nudge and each forced final writes a `hermes.turn_guard` event: kind and step only, no text.
4. **Output cap.**
   - On `finish_reason == "length"` the relay records `hermes.turn_guard` with kind `length` and whether a tool call or answer could be parsed.
   - When neither could be parsed, the step is retried once with `max_output_tokens` doubled, at most 8192, and the cap is not raised further.
   - The default `general` cap moves from the worker literal to the active entry's output cap when the entry defines one. Otherwise it stays at 2048.
5. **Gate usability.**
   - `run_eval`'s default per-task timeout becomes 3,600 s.
   - The CLI refuses a `--timeout` below 660 s for Hermes routes.
   - The `run --help` text states that the proof backend must run with the operator environment that provides the Hermes venv, or every turn fails at once.
   - Results add per task: mean `prompt_tokens_estimate`, input tokens, output tokens and decode tok/s, read read-only from the proof data root's `ai_jobs`.

Out of scope (binding non-goals):

- Raising the default context size, changing Strata `--vram-experts-gb`, or any host-ops change.
- Changing the egress policy, the tool allowlists, the research budget or the turn cap values.
- New tools, a model-based router, or changes to cloud routes beyond the shared dead-turn helper.
- Editing `backend/tests/**/test_*_conformance.py`.

## Files likely touched

- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/agents/hermes/broker_mcp.py` (only if compact descriptions are produced there)
- `scripts/benchmarks/agent_eval/runner.py`
- `backend/tests/test_hermes_*.py`, `backend/tests/test_agent_eval*.py`

## Design constraints

- `worker_shim.py` stays stdlib-only and free of app imports. Shared helpers it needs are duplicated there only with a test that pins both copies to the same behaviour, or are passed in frames.
- Compact schemas never widen what a tool accepts; the admitted name set and `tool_response_schema` gating are unchanged.
- Deadlines are deterministic functions of recorded rows; no wall-clock benchmarking at runtime.
- Killing a timed-out worker uses the existing process handle and `_recover`. No new supervisor process or daemon.
- No secrets or question text in `hermes.turn_guard` events or `ai_jobs` metadata.

## Acceptance criteria

1. For each surface in `{none, process, bluecad, studio}`, a short English question routed to `local:` has an estimated base prompt plus tool schemas of ≤ 4,000 tokens on the first step, and ≤ 7 tool schemas.
2. A research-worded question gets the research group; a patent-worded question gets `patent_read`; a question with no matching words gets the core plus research.
3. With a fake runner whose recorded rows imply 10 tok/s decode and 20 s prefill, the step deadline is > 225 s and the turn deadline is ≥ the sum of the steps; with no rows it uses the 240 s default.
4. A fake worker that never answers a turn is interrupted, then killed after 30 s grace. The next turn on the same supervisor completes against a responsive fake worker without `turn_busy`.
5. "Let me verify the numbers before reporting." and "I'll make the actual calls now." with tools offered are flagged; "The flow rate is 2.4 L/min." and any capped step are not. A second announcement in the same step yields a forced final request with no tools.
6. A `length` step with an unparseable body is retried once with a doubled cap ≤ 8192; a parseable one is not retried. Both write `hermes.turn_guard`.
7. `agent_eval run --timeout 300` on a Hermes route exits non-zero with a message; the default is 3,600; `--help` mentions the Hermes venv requirement; results include the token and tok/s fields.
8. Lint, mypy ratchet and the focused Hermes and agent-eval tests pass offline.

## Required tests

- Router and prompt-budget unit tests (criteria 1–2), using the real `broker_mcp` tool definitions.
- Deadline function tests over synthetic `ai_jobs` rows (criterion 3).
- Supervisor timeout and recovery test with a fake worker subprocess (criterion 4).
- Announcement and length guard tests in both supervisor and worker-shim paths (criteria 5–6).
- Agent-eval CLI tests (criterion 7).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, one severe diff review,
spec status updated. Then the coordinator reruns the chat gate on a proof backend (hard set and example set), records
tokens per request and tok/s, and hands the answers to the maintainer's chat session for grading.
