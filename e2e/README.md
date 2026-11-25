# End-to-end browser walks

Seed of the end-to-end suite. Each script drives the real built frontend in Chromium (Playwright)
against a private backend running on a temporary copy of a data root, and writes screenshots plus a
JSON evidence file to the evidence directory you pass in.

**Intended use**

- Nightly e2e: run every walk against the example project and compare with the previous night.
- Required walk before human review for any pull request that changes the UI: run the walks that touch
  the changed surfaces and attach the evidence directory (screenshots included) to the PR.

**Status: seed, to develop.** The walks were written one spec at a time and are not wired to CI yet.
Known gaps: a shared runner/launcher, a single evidence format, an app path map generated from the walks
(so that Bubu can learn the app's paths), and pass/fail reporting. The walks that start from a populated
data root (`<source-data-root>`) need an example project; the example content is provided by the
optional pack, not by this repository.

| Walk | What it covers | Run |
|---|---|---|
| `named_roles_walk.mjs` | Spec 260 named role chooser, unavailable models, Sheldon mode and Settings routes (fresh temporary data root; no provider calls) | `node e2e/named_roles_walk.mjs <evidence-dir>` |
| `design_discovery_walk.mjs` | A fresh profile reaches every Design capability through visible controls only (role + accessible name) | `node e2e/design_discovery_walk.mjs <evidence-dir> <source-data-root>` |
| `design_visual_walk.mjs` | Visual proof: Workshop, code and math rendering, CFD field inspection, charts, status language, Light/Dark at two viewports | `node e2e/design_visual_walk.mjs <evidence-dir> <source-data-root>` |
| `cfd_pipe_walk.mjs` | Smooth-tube CFD path with a real OpenFOAM toolchain (`cfd_operator_registry.py` builds the operator registry) | `node e2e/cfd_pipe_walk.mjs [evidence-dir]` |
| `cfd_field_3d_walk.mjs` | Clean-profile 3D field, computed and mirrored probes on a retained finished run | `node e2e/cfd_field_3d_walk.mjs <evidence-dir> <source-data-root>` |
| `grounding_walk.mjs` | Bubu grounding: pointer targets resolved against the real Design screens (uses `frontend/src/app/designTargets.ts`) | `node e2e/grounding_walk.mjs <evidence-dir> <source-data-root>` |
| `owner_predicate_walk.mjs` | Measures the Process owner surface after operator actions (uses `frontend/src/app/ownerPredicates.ts`) | `node e2e/owner_predicate_walk.mjs <evidence-dir> <source-data-root>` |
| `requirement_edit_walk.mjs` | Edit, approve and reconcile a requirement from a fresh temporary data root | `node e2e/requirement_edit_walk.mjs <evidence-dir>` |
| `literature_datum_walk.mjs` | Literature: check datums against markdown lines and generated PDF pages, including matching and wrong locations, and, when `JARVISOS_DOCLING_PYTHON`, `JARVISOS_DOCLING_ARTIFACTS` and `JARVISOS_WALK_TABLE_PDF` are set, local table extraction and cell selection (fresh temporary data root) | `node e2e/literature_datum_walk.mjs <evidence-dir>` |
| `freshness_invalidation_walk.mjs` | Replacement freshness detail and read-only inspection (fresh temporary data root) | `node e2e/freshness_invalidation_walk.mjs <evidence-dir>` |
| `sensitivity_review_walk.mjs` | Selected source sensitivity: current, stale and no label, with read-only inspection (fresh temporary data root) | `node e2e/sensitivity_review_walk.mjs <evidence-dir>` |
| `design_feature_inspector_walk.mjs` | Spec 235 feature inspector: add a tube, rebuild, 3D preview, change length, revision lineage, client refusal, undo (fresh temporary data root) | `node e2e/design_feature_inspector_walk.mjs <evidence-dir>` |
| `bubu_guided_tour_walk.mjs` | Spec 242 Bubu guided tour: Next through every stop (route, pointed target or "Not available here"), Back, Exit, Escape, and no POST/PUT/DELETE during the tour (fresh temporary data root, or a copy with the C1 case study) | `node e2e/bubu_guided_tour_walk.mjs <evidence-dir> [<source-data-root>]` |
| `llama_runtime_panel_walk.mjs` | Coding → Runtime local model status, confirmed stop and failure using intercepted controls (fresh temporary data root) | `node e2e/llama_runtime_panel_walk.mjs <evidence-dir>` |
| `coding_supervisor_walk.mjs` | Coding → Runtime supervisor controls (pause, resume, graceful restart) and open maintainer question answers | `node e2e/coding_supervisor_walk.mjs <evidence-dir>` |
| `provider_quota_walk.mjs` | Coding → Runtime provider usage columns, quota rings, Claude windows, and failed-refresh retention | `node e2e/provider_quota_walk.mjs <evidence-dir>` |
| `sim_results_requirement_verification_walk.mjs` | Spec 253 simulation result ingestion, red-dot exceptions, single batch notification, resolution (correct + confirm), requirement verdicts and unvalidated markers | `node e2e/sim_results_requirement_verification_walk.mjs <evidence-dir>` |
| `cad_verdict_lights_walk.mjs` | Spec 301 CAD verdict lights: solid, mesh and static FEM evidence with shared verdict lights, Italian labels and check table (fresh temporary data root) | `node e2e/cad_verdict_lights_walk.mjs <evidence-dir>` |
| `freecad_bridge_walk.mjs` | Spec 326 FreeCAD document listing, typed numeric preview/apply and rolled-back failure against a fake peer | `node e2e/freecad_bridge_walk.mjs <evidence-dir> [<data-root-copy>]` |
| `readiness_process_compare_walk.mjs` | Spec 320 Prontezza: a converged Process-editor run turns Calcolo and Confronto green, the comparison names the flowsheet, and a verdict is recorded on it (fresh temporary data root, temporary pack copy) | `node e2e/readiness_process_compare_walk.mjs <evidence-dir>` |
| `process_dynamic_physics_walk.mjs` | Spec 325 Dynamic physics UI: thermal, carbonate/pH and optics/fouling controls save and persist on a photobioreactor draft (fresh temporary data root) | `node e2e/process_dynamic_physics_walk.mjs <evidence-dir> [<data-root-copy>]` |
| `case_walk.mjs` | Spec 328 CASE-WALK-1: one readiness case from seven red cells to seven green through visible controls only (workspace, source with paper file, located datum, accept, parameter, card run, verdict, decision, retrieval check). After the backend starts the script only GETs the board; it never writes through the API. A step with no visible control fails the walk and `summary.json` names the step and control. Evidence: `board.jsonl`, one screenshot per step, `summary.json` | `node e2e/case_walk.mjs <evidence-dir> [--pack <pack-dir>] [--case <case-id>] [--data-root-copy <dir>]` |

**Private vault (spec 377).** Walks and agent-eval runs use fresh roots or copies of non-vault roots only. When `JARVISOS_VAULT_PATH` is set, every walk that reads a source data root first runs `scripts/vault_guard.py check` and stops if the root is inside or contains the vault.

**`case_walk.mjs` walk input.** Defaults: the fake pack (`backend/tests/fixtures/packs/zephyr`), case `generic-literature-1`,
a fresh empty temporary data root; `--data-root-copy` runs on a temporary copy and never touches the original. The case
comes from `<pack>/readiness_cases.toml`; what it does not say comes from `<pack>/case_walks/<case-id>.toml`
(read by the walk only, never by the product):

```toml
paper_file = "paper.md"            # text file in case_walks/ attached to the literature source
[[facts]]                          # one per case fact
quantity = "density"               # case fact name
reported = "density is 997 kg/m^3" # text typed as the reported value
value = 997.0
unit = "kg/m^3"
line_from = 2                      # location in the paper file
line_to = 2
input = "material_density"         # case input this fact binds to
[run]                              # Runs card whose output named like compare.quantity is compared
card_title = "..."
fields = { Temperatura = 298.15 }  # field label -> value
options = { Fluido = "Water" }     # option label -> choice
[verdict]
rationale = "..."
[decision]
title = "..."
text = "..."
rationale = "..."
[answer]                           # retrieval check over a folder holding the paper text
question = "..."
expected = "..."
```

Prerequisites: `(cd frontend && VITE_API_BASE_URL= npm run build)`, a backend virtualenv
(`JARVISOS_PROOF_PYTHON`, default `backend/.venv/bin/python`), and Playwright
(`npm i playwright`, or point `PLAYWRIGHT_MODULE` at an existing install). Every walk accepts `--help`.
Ports are configurable (`JARVISOS_PROOF_PORT`); each walk refuses to start if its port is in use.
