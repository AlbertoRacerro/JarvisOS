# BUBU-3 item 1: pointing measurement

Question: does the local model (Gemma 4 12B, llama.cpp, the server the app uses) point at the right first target when
the answer is a multi-step path through the app?

- `scenarios.py` — 12 hand-built page snapshots (`ui_context`, target names only) and questions, with the ids that count
  as correct / plausible as the FIRST `[[point:...]]` directive. `--ids old` renders Components-tree rows as on master
  (ephemeral `el-<n>`), `--ids new` as this change does (`design.tree.component.<slug>`). The spec 231 case includes
  C1-CFD and C1-horizontal as separate component rows; compare the old/new ids on the same twelve questions.
- `pointing_eval.py` — renders each prompt through the backend's own `render_visible_page_block` + `assemble_prompt`
  (plus the Jarvis system envelope). `--emit-prompts` writes only prompt and scoring inputs. The trusted
  `local_eval.run` host operation POSTs those prompts to the loopback server, disables thinking, and scores the first
  directive. The token stays in the host operation.
- `prompts_before.txt` / `prompts_after.txt` — the exact prompts (`--dry-run`) on master vs. this change.

To inspect prompts offline, run from `backend/` with the backend venv:

    PYTHONPATH=. .venv/bin/python ../docs/implementation/evidence/bubu3-pointing/pointing_eval.py \
        --ids new --emit-prompts /tmp/pointing-prompts.json

For `local_eval.run`, the host-only policy registers a prompt export benchmark and the trusted host evaluation.
The paths below are placeholders. The benchmark runs in the sandbox without a credential; the token stays in trusted
host code and is sent only in a loopback HTTP Authorization header. Request `local_eval.run` with `bench`
and the candidate `sha`. The host verifies the commit with GitHub and extracts that commit into a temporary checkout
for the prompt phase. The receipt contains scores and timing, with no prompts or replies.

```json
"bench.run": {"benchmarks": {
  "bubu3-pointing-new": {
    "cwd": "backend",
    "argv": ["/placeholder/backend/.venv/bin/python",
             "../docs/implementation/evidence/bubu3-pointing/pointing_eval.py",
             "--ids", "new", "--emit-prompts", "pointing-prompts.json"],
    "result_file": "pointing-prompts.json",
    "timeout_s": 1800
  }
}},
"local_eval.run": {"evaluations": {
  "bubu3-pointing-new": {
    "ids": "new", "label": "after", "model": "gemma-4-12b-it-qat-q4_0",
    "base_url": "http://127.0.0.1:18081/v1", "max_tokens": 220, "timeout_s": 300,
    "credential_file": "/home/user/.config/jarvisos-hostops/local-model-key"
  }
}}
```

The credential file must be owned by the host user and mode 0600. Register a corresponding `old` entry with `--ids old`
and label `before` for the earlier checkout. The evaluator's `--emit-prompts` mode does not contact the model server.

## Status

Measured 2026-11-05 on the host by the maintainer's session against the live llama-server (model
`gemma-4-12b-it-qat-q4_0`, temperature 0, `max_tokens` 220). The server token stayed in the host process and is not
recorded anywhere.

- BEFORE (`results_before.json`, master `a660a007`, `--ids old`): correct 6, plausible 3, wrong 3, none 0.
- AFTER (`results_after.json`, this branch at `fe11ea61`, `--ids new`): correct 10, plausible 0, wrong 2, none 0.

Both reported cases are correct AFTER: `c1_cfd_vs_horizontal` (wrong before) and the return case
`case_study_back_home`. No scenario that was correct before is worse after.

The two AFTER misses (`dynamic_run_simulation`, `memory_literature`) point at the same wrong targets as BEFORE; they are
not regressions from the stable ids.

The script sends `chat_template_kwargs: {enable_thinking: false}` (as the app does with thinking off); without it every
reply was empty (12/12 none).
