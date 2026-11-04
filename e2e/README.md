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
| `design_discovery_walk.mjs` | A fresh profile reaches every Design capability through visible controls only (role + accessible name) | `node e2e/design_discovery_walk.mjs <evidence-dir> <source-data-root>` |
| `design_visual_walk.mjs` | Visual proof: Workshop, code and math rendering, CFD field inspection, charts, status language, Light/Dark at two viewports | `node e2e/design_visual_walk.mjs <evidence-dir> <source-data-root>` |
| `cfd_pipe_walk.mjs` | Smooth-tube CFD path with a real OpenFOAM toolchain (`cfd_operator_registry.py` builds the operator registry) | `node e2e/cfd_pipe_walk.mjs [evidence-dir]` |
| `cfd_field_3d_walk.mjs` | Clean-profile 3D field, computed and mirrored probes on a retained finished run | `node e2e/cfd_field_3d_walk.mjs <evidence-dir> <source-data-root>` |
| `grounding_walk.mjs` | Bubu grounding: pointer targets resolved against the real Design screens (uses `frontend/src/app/designTargets.ts`) | `node e2e/grounding_walk.mjs <evidence-dir> <source-data-root>` |
| `owner_predicate_walk.mjs` | Measures the Process owner surface after operator actions (uses `frontend/src/app/ownerPredicates.ts`) | `node e2e/owner_predicate_walk.mjs <evidence-dir> <source-data-root>` |
| `design_feature_inspector_walk.mjs` | Spec 235 feature inspector: add a tube, rebuild, 3D preview, change length, revision lineage, client refusal, undo (fresh temporary data root) | `node e2e/design_feature_inspector_walk.mjs <evidence-dir>` |
| `coding_supervisor_walk.mjs` | Coding → Runtime supervisor controls (pause, resume, graceful restart) and open maintainer question answers | `node e2e/coding_supervisor_walk.mjs <evidence-dir>` |
| `provider_quota_walk.mjs` | Coding → Runtime provider usage columns, quota rings, Claude windows, and failed-refresh retention | `node e2e/provider_quota_walk.mjs <evidence-dir>` |

Prerequisites: `(cd frontend && VITE_API_BASE_URL= npm run build)`, a backend virtualenv
(`JARVISOS_PROOF_PYTHON`, default `backend/.venv/bin/python`), and Playwright
(`npm i playwright`, or point `PLAYWRIGHT_MODULE` at an existing install). Every walk accepts `--help`.
Ports are configurable (`JARVISOS_PROOF_PORT`); each walk refuses to start if its port is in use.
