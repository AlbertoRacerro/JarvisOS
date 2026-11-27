# 388 — AGENT-EVAL-EXPLORER-1

Status: ready
Depends on: 387

## Goal

Extend the spec-373/387 agent-eval lab (`scripts/benchmarks/agent_eval/`) with Phase 1 strong-model exploration and deterministic trace-mining tooling: (1) a public/generic request-set generator (`explore-gen`), (2) an explorer runner (`explore`) with deterministic read-only SQLite tool-call trace extraction, work-report parsing, and a bounded simulated maintainer for interactive clarifying turns, (3) an opt-in multi-provider explorer mode (`--mode explorer --provider claude|agy`) on the loopback `reference_shim.py` (leaving default `--mode reference` strictly Haiku-only), and (4) a deterministic offline process miner (`mine`) that extracts transitions, frequent chains, cross-model common vs divergent steps, loops/retries, parallel bursts, and wish-list items with a capability reuse check.

## Why

Implements Steps 1–2 of Maintainer Directive `L20261127191802-5834` (extending `L20261127131857-967e`): strong models explore a proof copy of Jarvis to reveal missing deterministic tools, bridges, and recipes so small local models become reliable engineering colleagues without model distillation.

Prior-work review (LATM, Voyager skill libraries, Agent Workflow Memory / ExpeL, TraceCompiler) sharpens the design:
- Mining raw sequences by bigrams alone captures noisy retries; pairing deterministic DB tool-call events (arguments, timing, status, result digests) with structured explorer work reports (intent per action, workarounds, wish list) surfaces concrete producer-consumer chains and missing deterministic bridges.
- Comparing trajectories across multiple strong models on the same request separates problem-intrinsic steps (common to all models) from model habits or valid alternatives (differing steps).
- Strong models act as tool/workflow discoverers in an isolated proof environment; resulting capabilities are implemented deterministically in code, never via model distillation.

## Code facts checked at `a41f9bde`

- `scripts/benchmarks/agent_eval/runner.py::run_eval` drives chat threads against a loopback proof backend and writes atomic resumable JSON results.
- `scripts/benchmarks/agent_eval/runner.py::_open_proof_db` opens `<data_root>/jarvisos.db` in SQLite read-only mode (`mode=ro`).
- `scripts/benchmarks/agent_eval/runner.py::_bare_tool_name` strips the `mcp__jarvis__` prefix from tool names.
- `scripts/benchmarks/agent_eval/runner.py::_http_request` performs stdlib JSON HTTP requests with deadline enforcement.
- `scripts/benchmarks/agent_eval/runner.py::read_job_metrics` aggregates `ai_jobs` token usage and latency for a repetition time window.
- `scripts/benchmarks/agent_eval/runner.py::validate_base_url` enforces loopback-only hostnames and refuses port `8000`.
- `scripts/benchmarks/agent_eval/runner.py::build_parser` and `scripts/benchmarks/agent_eval/runner.py::main` define and dispatch `agent_eval` CLI subcommands.
- `scripts/benchmarks/agent_eval/reference_shim.py::is_allowed_reference_model` restricts Arm B reference mode to `haiku` / `claude-haiku-*`.
- `scripts/benchmarks/agent_eval/reference_shim.py::build_claude_command` and `scripts/benchmarks/agent_eval/reference_shim.py::default_claude_runner` invoke `claude -p` in a fresh empty temp directory with API-key env vars stripped.
- `scripts/benchmarks/agent_eval/reference_shim.py::ReferenceShimHandler.do_POST` handles `/v1/chat/completions`, enforces model allowlist and union `json_schema` 400 fallback.
- `scripts/benchmarks/agent_eval/reference_shim.py::make_reference_shim_server`, `scripts/benchmarks/agent_eval/reference_shim.py::build_parser`, and `scripts/benchmarks/agent_eval/reference_shim.py::run_shim_cli` construct and run the loopback HTTP shim.
- `backend/app/modules/agents/hermes/supervisor.py::_TOOL_CAPABILITIES` maps registered Hermes MCP tool names to canonical `jarvis.*` capability IDs, and tool execution logs `hermes.tool_started` and `hermes.tool_result` events to `events`.
- `scripts/vault_guard.py::touches_vault` detects whether a path lies inside or contains `JARVISOS_VAULT_PATH`.

## Scope

In scope:
- **`scripts/benchmarks/agent_eval/reference_shim.py` (explorer mode):**
  - Add `--mode {reference,explorer}` (default `reference`) and `--provider {claude,agy}` (default `claude`).
  - When `--mode reference` (default), behaviour is unchanged: only `haiku` / `claude-haiku-*` models are permitted and `--provider agy` is refused fail-closed.
  - When `--mode explorer`, allow strong explorer models via explicit allowlist `is_allowed_explorer_model(model, provider)`:
    - `claude`: `opus`, `claude-opus-*`, `sonnet`, `claude-sonnet-*`, `haiku`, `claude-haiku-*`.
    - `agy`: `gemini-*` (e.g. `gemini-4-neon-medium`, `gemini-4-hydrogen-medium`, `gemini-3.8-flash-high`, `gemini-4-argon-medium`) and `claude-opus-*` / `claude-sonnet-*`.
  - Add `build_agy_command(prompt, model, *, system_prompt=None, json_schema=None)` and `default_agy_runner(cmd, *, timeout)` running `agy -p <full_prompt> --model <model> --output-format json --disable-slash-commands --mode plan` (plus `--json-schema` when present) in a fresh empty temporary directory with `*_API_KEY`, `GH_*`, `GIT_*` stripped, parsing the JSON/text output cleanly into the OpenAI completion payload.
  - Keep loopback-only host check, port-8000 refusal, `vault_guard` check, and `cloud_ok: true` check on `--tasks` (unless `--zone-a` is explicitly passed for a non-vault local proof set, still refusing any vault path fail-closed).
- **`scripts/benchmarks/agent_eval/explorer.py`:**
  - `generate_request_set(*, count=40, heldout_fraction=0.25, seed=42)` (`explore-gen`): deterministic generator producing a public/generic request set (`set_id`, `cloud_ok: true`, `tasks: [...]`) covering all Jarvis families weighted towards design (CAD, CFD, process, FEM, optics, biology) and research (literature, materials, data_vs_model, diagnosis, sensitivity, costs), plus planning/notes, dev diagnostics, machine/GPU control, and settings. Each request includes `id`, `split` (`dev`/`heldout`), `family`, `prompt`, `interactive` (bool), and for interactive items `user_intent` and `preferences` for the simulated maintainer. Zero private project specifics.
  - `extract_thread_tool_trace(data_root, thread_id)`: opens proof `jarvisos.db` read-only via `_open_proof_db` and extracts ordered tool calls for `thread_id` by joining `hermes.tool_started` and `hermes.tool_result` (and `hermes.agent_event`) on `call_id` / `target_id`. Each trace record contains: `seq`, `call_id`, `tool_name` (bare), `capability_id`, `arguments`, `status`, `error_code`, `started_at`, `completed_at`, `duration_ms`, `result_digest`, `evidence_refs`.
  - `wrap_explorer_prompt(prompt)`: appends concise work-report instructions asking the explorer to state a one-line intent before actions and conclude with a structured report (`STEPS_TAKEN`, `WORKAROUNDS`, `WISH_LIST`), explicitly noting that this is a work report (not internal chain-of-thought) and shorter reports are acceptable.
  - `parse_work_report(answer)`: deterministic tolerant parser extracting `steps_taken` (list of strings), `workarounds` (list of strings), and `wish_list` (list of strings) from the explorer's final answer; returns empty lists (never raises) when a model omits or shortens the report.
  - `needs_clarification(answer)` and `SimulatedMaintainer`: detects when an explorer reply asks a clarifying question or requests confirmation of a draft; when `max_clarification_rounds > 0` and a simulated-maintainer callable (or CLI provider) is configured, generates a concise realistic maintainer answer grounded in `user_intent` and `preferences` and posts it as a follow-up interaction on the same thread, recording every turn in the trace.
  - `run_exploration(...)` (`explore`): validates loopback `--base-url` (refusing port 8000) and `vault_guard` on `--requests`, `--data-root`, and `--out`; runs requests sequentially with atomic per-request checkpoints (`exploration_log.json` and `traces.jsonl`) and `--resume` support; records deterministic DB tool trace, `ai_jobs` token/latency metrics, parsed work report, and multi-turn transcript per request.
- **`scripts/benchmarks/agent_eval/miner.py`:**
  - `load_known_capabilities()`: reads registered bare tool names and capability IDs (from `backend/app/modules/agents/hermes/supervisor.py::_TOOL_CAPABILITIES` when importable, or via AST/fallback constant list so stdlib-only usage never fails).
  - `mine_exploration_logs(log_paths, *, min_support=2, known_capabilities=None)` (`mine`): deterministic offline process miner over one or more `exploration_log.json` files:
    - `transitions`: `(tool_a, tool_b)` counts and conditional probabilities `P(tool_b | tool_a)`.
    - `frequent_chains`: contiguous tool chains of length 2..4 appearing at least `min_support` times across traces (recipe candidates).
    - `cross_model`: per-request comparison across models listing `common_tools` (used by every model — problem-intrinsic) and `divergent_tools` (used by only a subset — alternatives / model habits), plus aggregate agreement rate.
    - `loops_and_retries`: consecutive identical tool calls and failed-then-retried tool pairs.
    - `parallel_bursts`: overlapping tool-call time windows within a trace (`next.started_at < prev.completed_at` when timestamps are present).
    - `workarounds` and `wish_list`: normalized, deduplicated frequency-ranked entries with example request IDs and a `reuse_check` field listing any matching existing Jarvis capability IDs/tool names (token-overlap match against `known_capabilities`) so existing capabilities are surfaced before building anything new.
  - `format_mining_report(report)`: concise human-readable Markdown summary written alongside `mining_report.json`.
- **CLI wiring in `scripts/benchmarks/agent_eval/runner.py`:**
  - Add `explore-gen`, `explore`, and `mine` subcommands to `build_parser` and `main`.
- **Tests in `backend/tests/test_agent_eval_explorer.py`:**
  - Offline unit tests covering all new modules and shim explorer mode.

Out of scope (binding non-goals):
- Changes to product backend services, Hermes runtime, SQLite schema, or frontend UI.
- Committing any private request sets or exploration logs to the public repository.
- Any local GPU model invocation, model distillation, or fine-tuning.
- Building Phase 2 deterministic pieces (those follow mining results in separate specs).

## Files likely touched

- `scripts/benchmarks/agent_eval/reference_shim.py`
- `scripts/benchmarks/agent_eval/explorer.py` (new)
- `scripts/benchmarks/agent_eval/miner.py` (new)
- `scripts/benchmarks/agent_eval/runner.py`
- `scripts/benchmarks/agent_eval/module.toml`
- `docs/MODULES.md` (regenerated via `python3 scripts/gen_modules.py`)
- `backend/tests/test_agent_eval_explorer.py` (new)

## Design constraints

- **Stdlib only** in `scripts/benchmarks/agent_eval/` (no new third-party dependencies).
- **Fail-closed safety**:
  - `vault_guard.touches_vault` checked on `--requests`, `--data-root`, `--out`, and `--tasks`; any vault path raises `PermissionError`.
  - Loopback-only host and port `8000` refusal enforced everywhere.
  - Default `reference_shim.py --mode reference` remains strictly Haiku-only (`claude` provider only); all existing `test_agent_eval_reference_shim.py` tests must pass unchanged.
- **Public repository hygiene**: `generate_request_set` uses strictly generic engineering/workbench examples (no private project, organism, or part names).
- **Resilient logging**: A model omitting or refusing part of the work report never fails an exploration item; the deterministic DB tool trace is always retained.
- **Atomic checkpoints**: `exploration_log.json` and `traces.jsonl` are written atomically after each completed request so interrupted runs resume cleanly with `--resume`.

## Acceptance criteria

1. `python3 -m scripts.benchmarks.agent_eval explore-gen --out <path> --count 24` deterministically generates a valid `cloud_ok: true` request set covering design, research, diagnostics, and interactive requests with dev/heldout split and zero private project terms.
2. `python3 -m scripts.benchmarks.agent_eval.reference_shim --mode explorer --provider agy --model gemini-4-neon-medium ...` (and `--provider claude --model opus`) accepts allowlisted explorer models, rejects unknown models, refuses port `8000` and non-loopback hosts, and leaves `--mode reference` strictly Haiku-only.
3. `extract_thread_tool_trace` accurately joins `hermes.tool_started` and `hermes.tool_result` rows from a read-only SQLite fixture for a given `thread_id`, computing `duration_ms` and preserving arguments, status, error code, and `result_digest`.
4. `run_exploration` executes requests against a stubbed loopback proof backend, runs bounded simulated-maintainer follow-up turns when `needs_clarification` triggers, parses full and partial work reports without failing, and supports `--resume` without duplicating completed requests.
5. `mine_exploration_logs` deterministically computes transitions, frequent chains, cross-model common vs divergent tools, loops/retries, parallel bursts, and wish-list items annotated with `reuse_check` matches against registered Jarvis capabilities.
6. All unit tests in `backend/tests/test_agent_eval_explorer.py`, `backend/tests/test_agent_eval_reference_shim.py`, `backend/tests/test_agent_eval_parity.py`, and `backend/tests/test_agent_eval_gate.py` pass offline, and `ruff`, `check_spec_status.py`, `check_code_facts.py`, `gen_modules.py` are clean.

## Required tests

- `backend/tests/test_agent_eval_explorer.py`:
  - Request-set generator determinism, family weighting (design + research >= 60%), dev/heldout split, interactive fields, and absence of private terms.
  - `reference_shim.py` explorer mode: `claude` (`opus`, `sonnet`, `haiku`) and `agy` (`gemini-*`) allowlists, rejection of disallowed models, `reference` mode still rejecting `opus`/`gemini-*`/`agy`, `build_agy_command` flags, and HTTP `/v1/chat/completions` round-trip with stubbed `agy` runner.
  - `extract_thread_tool_trace`: joins `hermes.tool_started` and `hermes.tool_result` for target `thread_id` while ignoring other threads, computes `duration_ms`, strips `mcp__jarvis__`.
  - `parse_work_report`: full structured report, partial report, and plain-text/refusal report (no exception).
  - `run_exploration`: stubbed loopback HTTP proof server + SQLite DB fixture + injectable simulated maintainer verifying multi-turn clarification, vault-path refusal, port-8000 refusal, and `--resume` skipping completed IDs.
  - `mine_exploration_logs`: two-model synthetic logs verifying transition probabilities, frequent chains (`min_support`), cross-model common vs divergent sets, consecutive/retry loop detection, overlapping parallel burst detection, and wish-list `reuse_check` matching registered capabilities (e.g. `jarvis.paper_search`, `jarvis.bench_run`).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
