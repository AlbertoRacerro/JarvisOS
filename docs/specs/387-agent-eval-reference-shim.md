# 387 — AGENT-EVAL-REFERENCE-SHIM-1: Fix Arm A CLI runner and add proof-only loopback reference shim for Arm B

Status: ready
Depends on: 385

## Goal

Make both reference arms of the three-arm parity program (directive `L20261126095006-552f`) executable offline-from-GPU
and zero-API-cost:
1. Fix Arm A (`agent_eval run --reference haiku`) so `execute_claude_cli` invokes the installed `claude` CLI with valid
   flags (`--model`, `--no-session-persistence`, `--strict-mcp-config`) in an isolated empty working directory.
2. Add a proof-only, stdlib-only loopback OpenAI-compatible HTTP shim (`scripts/benchmarks/agent_eval/reference_shim.py`,
   also reachable via `agent_eval reference-shim`) so a proof backend configured with `JARVISOS_LLAMACPP_HOST` /
   `JARVISOS_LLAMACPP_PORT` can run Arm B (Haiku inside the exact Jarvis `local:` Hermes chat harness, tools, prompts,
   and message budget) without touching production routing or the local GPU.

## Why

Directive `L20261126095006-552f` requires Gate Zero (`B > A`: reference model inside the Jarvis harness must beat the
standalone reference model) before tuning the local model. Spec 385 (`AGENT-EVAL-PARITY-1`) delivered the three-arm
parity comparator and scoreboard, but two gaps block measuring Arm A and Arm B today:
- `scripts/benchmarks/agent_eval/runner.py::execute_claude_cli` passes `-m` (an invalid flag; the CLI requires
  `--model`) and runs in the caller's working directory without `--no-session-persistence` or `--strict-mcp-config`.
- `backend/app/modules/agents/hermes/supervisor.py::_handle_relay` enforces `selected_route.startswith("local:")`, so
  there is no way for `agent_eval` to drive Haiku through the exact same `local:` Hermes chat harness that the local
  model uses without a loopback OpenAI-compatible bridge.

A proof-only loopback shim keeps production Jarvis routing and security boundaries untouched, uses zero GPU resources
(allowing Gate Zero measurement even during a GPU hold), and uses the subscription CLI with no paid API keys.

## Code facts checked at `a653bd3e`

- `scripts/benchmarks/agent_eval/runner.py::execute_claude_cli` (lines 339–355) invokes
  `["claude", "-m", model, "-p", prompt]` with `*_API_KEY` stripped, without `--no-session-persistence`,
  `--strict-mcp-config`, or an isolated empty `cwd`.
- `backend/app/modules/agents/hermes/supervisor.py::_handle_relay` (lines 1216–1219) refuses any non-`local:` route.
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter` calls `GET {origin}/health` in
  `health()` (line 75) and `POST {base_url}/chat/completions` in `complete()` (line 209), sending `model`, `messages`,
  `temperature`, `max_tokens`, `stream: false`, and optional `response_format` (`type: "json_schema"`), and reading
  `choices[0].message.content`, `choices[0].finish_reason`, and `usage.prompt_tokens` / `usage.completion_tokens`.
- `backend/app/modules/local_ai/runtime/llama_cpp.py::llama_cpp_runtime_config` (lines 98–130) reads
  `JARVISOS_LLAMACPP_HOST`, `JARVISOS_LLAMACPP_PORT`, `JARVISOS_LLAMACPP_MODEL_ID`, and `JARVISOS_MANAGE_LLAMACPP`.
- `scripts/vault_guard.py::touches_vault` and `vault_root()` provide fail-closed vault-boundary checks.

## Scope

In scope:
1. **Fix Arm A CLI invocation (`scripts/benchmarks/agent_eval/runner.py::execute_claude_cli`)**:
   - Replace `-m` with `--model`.
   - Pass `--no-session-persistence` and `--strict-mcp-config`.
   - Execute `subprocess.run` with `cwd` set to a fresh empty `tempfile.TemporaryDirectory` and `stdin=subprocess.DEVNULL`
     so standalone reference runs cannot read repository or host files.
   - Keep `*_API_KEY` environment stripping and `_INJECTED_CLAUDE_RUNNER` test injection unchanged.
2. **Add proof-only loopback OpenAI-compatible shim (`scripts/benchmarks/agent_eval/reference_shim.py`)**:
   - Standard-library `http.server.ThreadingHTTPServer` only; no new dependencies.
   - Validate bind address strictly: loopback host only (`127.0.0.1`, `localhost`, `::1`), port `8000` refused.
   - Fail-closed cloud-safety gate: require `--tasks <path>` (a task-set JSON file), refuse any path touching
     `vault_guard.vault_root()` (exit `vault_guard.REFUSED_EXIT`), and refuse any task file where `cloud_ok is not True`.
   - Restrict allowed reference models fail-closed to `"haiku"` or model IDs starting with `"claude-haiku-"`.
   - Serve:
     - `GET /health` -> `200 {"status": "ok"}`
     - `GET /slots` -> `200 [{"id": 0, "is_processing": <bool>}]` (reflecting active in-flight completion)
     - `GET /v1/models` -> `200 {"object": "list", "data": [{"id": <model>, "object": "model"}]}`
     - `POST /v1/chat/completions`:
       - Parse `messages` (extract `system` messages into `--system-prompt`; format remaining turns deterministically
         into the user prompt text; handle both string and list-of-text-parts content).
       - If `response_format.type == "json_schema"` and `response_format.json_schema.schema` is a dict, pass it via
         `--json-schema <compact JSON>`.
       - Invoke `claude -p --model <model> --tools "" --strict-mcp-config --no-session-persistence --output-format json`
         (plus `--system-prompt` and `--json-schema` when present) in a fresh empty temporary directory with `*_API_KEY`
         stripped and `stdin=subprocess.DEVNULL`.
       - Parse the CLI JSON result: use `json.dumps(structured_output)` when `structured_output` is present, otherwise
         `result` text; extract `usage.input_tokens` and `usage.output_tokens` when present.
       - Return a valid OpenAI `chat.completion` JSON payload (`id`, `object`, `created`, `model`, `choices`, `usage`),
         or HTTP `502` / `504` with a bounded error payload (never echoing prompt text or secrets) on CLI failure or
         timeout.
   - Provide a clean injectable completion callable (`runner=...` on server/handler config) so offline tests exercise the
     full HTTP server, schema translation, and error handling without spawning the real `claude` CLI.
   - Add `reference-shim` subcommand to `scripts/benchmarks/agent_eval/runner.py` (`--tasks`, `--host`, `--port`,
     `--model`, `--timeout`) and allow running directly via `python3 -m scripts.benchmarks.agent_eval.reference_shim`.
   - Update `scripts/benchmarks/agent_eval/module.toml` `entrypoints` to include `reference_shim.py` and regenerate
     `docs/MODULES.md`.
3. **Offline tests (`backend/tests/test_agent_eval_reference_shim_387.py`)**:
   - Verify `execute_claude_cli` invokes `--model`, `--no-session-persistence`, `--strict-mcp-config`, `stdin=DEVNULL`,
     empty temporary `cwd`, and strips `*_API_KEY`.
   - Verify fail-closed validation in `reference_shim`: non-loopback host refused, port `8000` refused, missing/false
     `cloud_ok` refused, vault path refused with `vault_guard.REFUSED_EXIT`, non-haiku model refused.
   - Verify live loopback HTTP endpoints (`GET /health`, `GET /slots`, `GET /v1/models`, `POST /v1/chat/completions`)
     with an injected stub runner for plain completions, `response_format.json_schema` structured outputs, multi-turn
     message formatting, and bounded `502`/`504` error handling.
   - Verify `agent_eval reference-shim` CLI parser and vault-guard rejection.

Out of scope (binding non-goals):
- Modifying production backend routing (`supervisor.py`, `local_llamacpp_adapter.py`, or `ai_jobs` router).
- Running any local model on the host GPU or modifying Strata runtime services.
- Adding private domain tasks to the public repository or using paid API keys.

## Files likely touched

- `scripts/benchmarks/agent_eval/runner.py`
- `scripts/benchmarks/agent_eval/reference_shim.py` (new)
- `scripts/benchmarks/agent_eval/module.toml`
- `backend/tests/test_agent_eval_reference_shim_387.py` (new)
- `docs/MODULES.md` (regenerated via `scripts/gen_modules.py`)

## Design constraints

- Standard-library only in `scripts/benchmarks/agent_eval/`; no new dependencies.
- Never leak prompt contents, task data, or vault paths in error responses or logs.
- Vault boundary enforced fail-closed via `scripts/vault_guard.py`.
- All existing `agent_eval` tests (`test_agent_eval_354.py`, `test_agent_eval_373.py`, `test_agent_eval_parity_385.py`)
  remain green.

## Acceptance criteria

1. `execute_claude_cli` passes `--model`, `--no-session-persistence`, and `--strict-mcp-config` in an isolated empty
   working directory with `*_API_KEY` stripped.
2. `reference_shim.py` refuses non-loopback binds, port `8000`, non-`cloud_ok` task files, vault paths, and non-haiku
   models fail-closed.
3. `POST /v1/chat/completions` on the loopback shim translates OpenAI `messages` and optional
   `response_format.json_schema` into a tool-disabled (`--tools ""`) `claude -p` invocation and returns a valid OpenAI
   `chat.completion` JSON payload compatible with `LocalLlamaCppAdapter`.
4. All offline unit tests, `ruff`, `check_architecture_enforcement.py`, `check_doc_paths.py`, `check_spec_status.py`,
   `gen_modules.py`, and CI checks pass.

## Required tests

- `backend/tests/test_agent_eval_reference_shim_387.py`:
  - `execute_claude_cli` flags, empty `cwd`, and `*_API_KEY` stripping;
  - fail-closed host/port/`cloud_ok`/vault/model validation;
  - loopback HTTP `GET /health`, `GET /slots`, `GET /v1/models`, and `POST /v1/chat/completions` (plain + structured
    `json_schema` + error/timeout paths);
  - `agent_eval reference-shim` CLI guardrails.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review and all offline tests passing.
