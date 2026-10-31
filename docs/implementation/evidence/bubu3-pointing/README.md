# BUBU-3 item 1: pointing measurement

Question: does the local model (Gemma 4 12B, llama.cpp, the server the app uses) point at the right first target when
the answer is a multi-step path through the app?

- `scenarios.py` — 12 hand-built page snapshots (`ui_context`, target names only) and questions, with the ids that count
  as correct / plausible as the FIRST `[[point:...]]` directive. `--ids old` renders Components-tree rows as on master
  (ephemeral `el-<n>`), `--ids new` as this change does (`design.tree.component.<slug>`). The spec 231 case includes
  C1-CFD and C1-horizontal as separate component rows; compare the old/new ids on the same twelve questions.
- `pointing_eval.py` — renders each prompt through the backend's own `render_visible_page_block` + `assemble_prompt`
  (plus the Jarvis system envelope), POSTs it to the running llama-server (temperature 0, `max_tokens` 220), scores the
  first directive. Read-only for the app: it only requests completions. The server wants the operator's token; pass it
  in `JARVISOS_LLAMACPP_API_KEY` (it is not stored here).
- `prompts_before.txt` / `prompts_after.txt` — the exact prompts (`--dry-run`) on master vs. this change.

Run (from `backend/`, backend venv; run BEFORE on a master checkout of `thread_ui_context.py`, AFTER on this branch):

    JARVISOS_LLAMACPP_API_KEY=... PYTHONPATH=. .venv/bin/python ../docs/implementation/evidence/bubu3-pointing/pointing_eval.py \
        --label before --ids old --out ../docs/implementation/evidence/bubu3-pointing/results_before.json
    ... --label after --ids new --out .../results_after.json

## Status

The live server answered `/health` but returned HTTP 401 for completions: its per-launch token lives only in the
running app/server process, and it was not given to this task, so no model call was made and there are no
`results_*.json` yet. The prompts above are what the model will see; the scores are still to be measured.
