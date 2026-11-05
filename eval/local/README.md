# Local-model re-evaluation

Tools to re-evaluate the local models that fit a 12 GB VRAM machine (one hot model at a time):
classification and intake quality, latency, the llama.cpp build, and retrieval quality of the
"second brain". Run them when a new model, quantization or llama.cpp/Ollama build is on the table.

**Status: all items here are "to improve".** They work against the current code but were written
for earlier model generations: fixtures are small, thresholds are informal, and nothing runs in CI
(they need a live local runtime). Treat results as evidence for a decision, not as a gate.
Outputs go to `local_eval_reports/` at the repository root (gitignored).

All commands run from the repository root unless noted. Ollama must be running with the models
pulled; none of the scripts pulls a model.

| What it measures | How to run | Notes |
|---|---|---|
| Classification quality and token budget of the local classifier | `cd backend && python -m app.modules.local_ai_eval.classification_budget_probe --help` | Calls only a localhost classification endpoint. Library: `backend/app/modules/local_ai_eval/classification_budget_probe.py`, tests in `backend/tests/test_local_ai_classification.py`. |
| Intake signal quality (domain/project/record/sensitivity buckets) | `cd backend && python -m app.modules.local_ai_eval.fast_intake_probe --help` | Compares the model against the deterministic baseline in `deterministic_signals.py`. Tests: `backend/tests/test_local_ai_intake.py`. |
| Auto-routing agreement (classifier + capability matrix vs. a labelled set) | `python eval/local/routing_eval.py` | Fixture: `backend/app/modules/local_ai_eval/routing_eval_set.json`. Needs the classifier model. |
| Latency and sanity per local route class (fast/general/coder/coder_heavy, model swaps) | `python eval/local/route_smoke.py --confirm-live-local` | Goes through `run_ai_task`; reports wall time per call. |
| Local model selection under resource limits (reservation, swap, real execution) | `python eval/local/decision_routing_smoke.py --confirm-live-local --out out.json` | Use a disposable `JARVISOS_DATA_ROOT`, never the operator data root. |
| llama.cpp build + GGUF qualification (load time, VRAM/RAM, prompt/generation speed, tool-use probe) | `python eval/local/llamacpp_qualification.py` | Configured by environment variables (`LLAMACPP_DIST`, `GGUF`, `GGUF_SHA256`, `PORT`, `CTX`, `NGL`). Defaults describe a 27B model on the maintainer's machine; set them for a 12 GB card. Run on AC power. |
| Second-brain retrieval benchmark (recall@10, bundle size, rebuild time) | `python eval/local/second_brain_benchmark.py --embedder e5` | Baseline run: `second_brain_baseline.md`. The registered repository queries target files that may no longer exist: refresh them first. The `hashing` embedder is a fast offline smoke. |
| Engineering semantic judgment (validity, missing data, contradictions, controls) | `python eval/local/semantic_judgment/semantic_eval.py --emit-prompts prompts.json` | Offline cases and deterministic grader; see `semantic_judgment/README.md`. |

## Known gaps (to improve)

- No pass/fail thresholds and no CI wiring; add a small golden set per model role and a comparison
  against the previous report.
- The classification and intake probes carry their own fixtures (several thousand lines); they should
  share one labelled corpus and report latency percentiles alongside accuracy.
- `llamacpp_qualification.py` and the second-brain queries embed machine-specific defaults.
- The deterministic scoring of the rule-based ranker (`local_ai_eval/decision_routing.py`) is covered
  by `backend/tests/test_local_decision_routing.py`; the live-runtime part is `decision_routing_smoke.py`.
