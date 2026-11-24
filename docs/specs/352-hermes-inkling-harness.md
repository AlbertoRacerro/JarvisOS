# 352 — HERMES-INKLING-HARNESS-1: Deterministic agent-loop guardrails and literature paper search

Status: ready
Depends on: 351

## Goal

Build the benchmark-proven deterministic harness rules into the Jarvis Hermes agent loop — methodology prompt, bounded research budget, identical-call blocking, compact/shrunk tool outputs, dead-turn nudge, forced final answer at turn cap, and small per-request tool sets — and add a read-only `paper_search` tool (Europe PMC and Crossref public APIs: title, DOI, abstract) so local models can retrieve scientific metadata reliably even when publisher web pages block automated readers.

## Why

Directive `L20261124012919-7d88` (items 2 and 3): agent-loop evaluations showed that without these deterministic guardrails, a compact local model loops on repeated web searches and fails to answer; with them, it scored 15.5/16 on engineering, research, and fitting tasks. Publisher pages frequently block bots, so structured metadata lookup via Europe PMC / Crossref is required for reliable literature research.

## Code facts checked at `2bac0b80`

- `backend/app/modules/agents/hermes/supervisor.py::_assemble_turn_prompt` constructs the per-turn system/user prompt envelope sent to Hermes.
- `backend/app/modules/agents/hermes/supervisor.py::_admitted_tools` computes the set of broker tools exposed to a Hermes turn.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_tool` dispatches broker tool calls and returns their results to the worker.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_relay` mediates inference calls from the Hermes worker through `run_governed_inference`.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` executes the tool-calling loop inside the Hermes worker shim.
- `backend/app/modules/agents/hermes/worker_shim.py::withhold_tools` filters model-visible tool schemas per turn.
- `backend/app/modules/agents/hermes/broker_mcp.py::main` serves the bounded MCP tool surface to Hermes.

## Scope

In scope:
- **Methodology prompt (`_assemble_turn_prompt`)**:
  - Inject concise, deterministic engineering methodology rules: report numbers only from tool outputs; compute physics with deterministic tools rather than searching for answers; express uncertain inputs as an explicit range; read a source before citing it; never invent a DOI.
- **Deterministic loop guardrails (`supervisor.py` / `worker_shim.py`)**:
  - **Research budget**: cap search/read research calls per turn (default 8); once reached, return a concise budget-reached notice instructing the model to synthesize its final answer from gathered evidence.
  - **Identical repeated-call blocking**: hash `(tool_name, canonical_args)` per turn; block exact duplicate calls and return a deterministic short message telling the model the result was already provided.
  - **Compact tool outputs & old-output shrinking**: bound individual tool payloads to compact KPIs/summaries, and shrink earlier tool-result messages in the turn transcript as new steps arrive to preserve KV-cache headroom.
  - **Dead-turn nudge**: when a model step returns neither a tool call nor a final answer (empty/stalled turn), inject one deterministic nudge prompt to continue or conclude (bounded retries).
  - **Forced final answer at turn cap**: on the final allowed step of a turn, withhold tools (`withhold_tools`) and request a direct final answer with `reasoning_phase="final"` so the model cannot stall out on a tool call.
  - **Small per-request tool list**: keep the tool schemas exposed per turn tightly scoped to the active surface/intent rather than dumping the entire catalog.
- **Literature metadata tool (`paper_search`)**:
  - Add a bounded, read-only `paper_search` tool querying Europe PMC (`https://www.ebi.ac.uk/europepmc/webservices/rest/search`) and Crossref (`https://api.crossref.org/works`) public REST APIs, returning compact structured records (`title`, `authors`, `year`, `journal`, `doi`, `abstract` truncated to a safe bound, `source`).
  - Fail closed on invalid queries, network errors, redirects, or oversized responses; never send private workspace/pack content in search queries (subject to existing egress/sanitization boundaries).

Out of scope (binding non-goals):
- No host systemd service installation for SearXNG (tracked separately as item 4 of `L20261124012919-7d88`).
- No regression-eval harness migration (tracked separately as item 6 of `L20261124012919-7d88`).
- No fine-tuning, LoRA, secondary verifier model, or translation layer.

## Files likely touched

- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/paper_search_tool.py` (new)
- `backend/tests/test_hermes_inkling_harness_352.py` (new)
- `backend/tests/test_hermes_runtime.py` (compatibility alignment if needed)

## Design constraints

- All unit tests run completely offline using fake HTTP openers and fake inference stubs; zero live network calls in tests.
- No secrets, credentials, or private pack content ever enter `paper_search` queries or logs.
- High risk (agent broker/egress and loop control): requires coordinator full-diff review, independent second-session full-diff review, and green CI.

## Acceptance criteria

1. Turn prompts include the methodology rules (tool-sourced numbers, computed physics, stated ranges, read-before-citing, no invented DOIs).
2. Identical repeated tool calls within a turn are blocked deterministically without re-executing the tool; research calls beyond the per-turn budget (default 8) return a budget-reached notice.
3. Large tool outputs are compacted and older tool results in the turn history are shrunk deterministically; empty dead turns trigger a bounded nudge; reaching the turn cap withholds tools and forces a final answer with `reasoning_phase="final"`.
4. `paper_search` parses Europe PMC and Crossref JSON responses into bounded `{title, doi, abstract, ...}` records, refuses redirects/oversized bodies, and handles upstream errors cleanly.
5. Existing Hermes runtime and security tests remain green.

## Required tests

- Offline unit tests in `backend/tests/test_hermes_inkling_harness_352.py` covering criteria 1–4.
- Existing `backend/tests/test_hermes_runtime.py` and Hermes read-tool tests remain green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review + independent second-session full-diff review, spec status updated.
