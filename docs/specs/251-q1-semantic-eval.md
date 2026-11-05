# 251 — Q1 execution: host-run semantic-judgment evaluation of local models

Status: ready
Depends on: 249

## Goal

One typed host operation runs the Spec 249 semantic-judgment cases against one registered local model in one
registered setting (thinking on or off, token budget, attempts), grades every reply with the Spec 249 grader, and
returns only labels, scores, tokens, timings and memory samples. It stops the default responder (Gemma) before a
candidate that needs the GPU, and always restores it afterwards. The credential and the model text never leave the
host.

## Why

Maintainer directive `L20261105222120-78af` item 4: run Q1 with Q2 ("studio notturno") measured inside it, when the app
is idle or at night, stopping Gemma during runs and restoring it after. The current `local_eval.run` (Spec 243) cannot
do this. It needs `--ids old|new`, caps `max_tokens` at 4096 and time at 300 s, forces thinking off, and scores a
pointing directive instead of the Spec 249 grader.

## Scope

In scope:
- A new evaluation kind in `scripts/host_ops.py`: the `local_eval.run` policy entry gains `"kind": "semantic_judgment"`.
  The existing `pointing` behaviour stays the default and is unchanged. Each registered entry (one cell) names:
  - `model`, `base_url` (loopback only), `credential_file`;
  - `thinking` (`on` or `off`) and an optional `verification` (bool, sent only as `chat_template_kwargs.verification`);
  - `max_tokens` from 1 to 8192, `per_request_timeout_s` up to 1800 and `timeout_s` up to 28800;
  - `attempts` from 1 to 8, with `temperature` (0 when `attempts` is 1) and a fixed seed per attempt;
  - an optional `server_unit` (a `service.control` unit that serves the candidate) and `stop_default` (bool).
- **Prompt phase:** unchanged sandboxed checkout at the candidate SHA. It runs the registered benchmark that calls
  `semantic_eval.py --emit-prompts <result_file>`. Rows are `{name, prompt, category, answer}`; only `prompt` is sent to
  the model.
- **Model phase:** if `stop_default` is set, stop the default llama.cpp model through the local backend route
  `POST /local-ai/runtime/llama-cpp/stop` on loopback. Then start `server_unit` if one is named, wait for readiness
  within the timeout, and send each prompt `attempts` times. A `finally` path always stops `server_unit` and, if it
  stopped the default, starts it again and reports whether the restore succeeded. A failed restore is a `Failed`
  outcome that names it, and never a silent success.
- **Grading phase:** a second sandboxed run of `semantic_eval.py --grade <replies.json>` in the same checkout. Host_ops
  writes the replies file into the checkout and keeps only the grader's numeric and label output.
- **Q2 consolidation:** when `attempts > 1`, the grader output also gives, per case, the majority answer, its score,
  whether the attempts disagree, and how many attempts scored 1, next to the single-attempt score. These are
  deterministic: majority over the parsed `(verdict, options, missing)` tuples, ties counted as disagreement.
- **Returned detail:** per case and attempt, the parsed labels (`verdict`, `options`, `missing`), `score`,
  `parse_error`, completion tokens, whether reasoning content was present, and seconds. Per run: per-category totals,
  tokens/s, peak VRAM (from `nvidia-smi --query-gpu=memory.used` sampled during the run), peak host RAM used (from
  `/proc/meminfo`), errors and restarts. No reply text, no prompt text, no credential.
- `eval/local/semantic_judgment/semantic_eval.py` gains the consolidation output in `--grade`. The policy example goes in
  `eval/local/semantic_judgment/README.md`.

Out of scope (binding non-goals):
- Installing Strata or models, writing the host policy, raising the WSL memory limit. These are host steps for the
  maintainer's chat session and need the maintainer's OK for downloads.
- Making any candidate the default responder, or changing provider routing (directive: Gemma remains the default).
- LLM-as-judge, cloud models, any egress beyond loopback.
- UI. Results are presented to the maintainer from the receipts.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `scripts/host_ops.py` (`op_local_eval_run` and helpers)
- `backend/tests/test_host_ops.py`
- `eval/local/semantic_judgment/semantic_eval.py`, `README.md`, `backend/tests/test_semantic_judgment_cases.py`

## Design constraints

- Candidate code runs only inside the existing sandboxed checkout phases. Host_ops never imports it.
- Every value comes from the host policy, except the candidate SHA. The request names only the registered cell and the
  SHA, exactly as `local_eval.run` does today.
- Loopback only for `base_url` and the backend route, with no proxies and no redirects, as today.
- One hot model: the op refuses to start `server_unit` while the default model is running unless `stop_default` is set.
  A second evaluation while one is running is refused.
- The restore path runs on success, failure, timeout and cancellation. Tests cover each one.
- The credential check that already guards the returned detail stays.

## Acceptance criteria

1. With a fake loopback model server and a fake backend in tests, a `semantic_judgment` cell returns per-case labels,
   scores and the per-category totals equal to `semantic_eval.py --grade` on the same replies.
2. Thinking on/off and `verification` are sent as `chat_template_kwargs` exactly as registered. `max_tokens` up to 8192
   is accepted, and above it is refused.
3. With `stop_default`, the default model is stopped before the candidate starts and restored afterwards on success,
   model error, timeout and cancellation. A failed restore is reported as `Failed`.
4. `attempts: 3` returns the majority answer, the disagreement flag and the count of attempts that scored 1 per case.
5. The returned detail contains no prompt text, reply text or credential. Unregistered cells, non-loopback URLs and
   out-of-range settings are refused.
6. The existing `pointing` evaluations behave exactly as before. Their tests pass unchanged.

## Required tests

- Offline `backend/tests/test_host_ops.py` cases for every acceptance criterion, with fake HTTP, fake `systemctl`,
  fake `nvidia-smi` and a fake clock.
- `backend/tests/test_semantic_judgment_cases.py` for the consolidation output.
- High risk: a full diff review by a second qualified session, and the operation proven in the real environment. The
  real proof is one Gemma cell (thinking off, 4k, one attempt) run while the app is idle, with Gemma restored and
  checked afterwards. It is recorded as live activation after merge and the host install.

## Definition of done

Acceptance criteria pass, along with the relevant tests and lint. Required CI, the second-session review and the live
proof pass on the exact head. The registry is updated as the work advances. Accepted under maintainer directive
`L20261105222120-78af` item 4 (high risk).
