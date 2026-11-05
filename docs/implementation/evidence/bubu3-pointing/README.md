# BUBU-3 item 1: pointing measurement

Question: does the local model (Gemma 4 12B, llama.cpp, the server the app uses) point at the right first target when
the answer is a multi-step path through the app?

- `scenarios.py` — 12 hand-built page snapshots (`ui_context`, target names only) and questions, with the ids that count
  as correct / plausible as the FIRST `[[point:...]]` directive. `--ids old` renders Components-tree rows as on master
  (ephemeral `el-<n>`), `--ids new` as this change does (`design.tree.component.<slug>`). The spec 231 case includes
  C1-CFD and C1-horizontal as separate component rows; compare the old/new ids on the same twelve questions.
- `pointing_eval.py` — renders each prompt through the backend's own `render_visible_page_block` + `assemble_prompt`
  (plus the Jarvis system envelope), POSTs it to the running llama-server (temperature 0, `max_tokens` 220), disables
  thinking via `chat_template_kwargs`, and scores the first directive. Read-only for the app: it only requests
  completions. The server wants the operator's token; pass it in `JARVISOS_LLAMACPP_API_KEY` (it is not stored here).
- `prompts_before.txt` / `prompts_after.txt` — the exact prompts (`--dry-run`) on master vs. this change.

Run (from `backend/`, backend venv; run BEFORE on a master checkout of `thread_ui_context.py`, AFTER on this branch):

    JARVISOS_LLAMACPP_API_KEY=... PYTHONPATH=. .venv/bin/python ../docs/implementation/evidence/bubu3-pointing/pointing_eval.py \
        --label before --ids old --out ../docs/implementation/evidence/bubu3-pointing/results_before.json
    ... --label after --ids new --out .../results_after.json

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
