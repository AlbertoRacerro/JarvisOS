# AGENTS.md

JarvisOS is a single-user AI engineering workstation: FastAPI + SQLite backend (`backend/app`), React/Vite frontend
(`frontend/`). Models reason; deterministic code owns anything irreversible. This file holds only what the code cannot
tell you. How work is organised (specs, PRs, roles, supervisor): `docs/PROCESS.md`.

## Setup and commands

Work on Linux (WSL2 Ubuntu on the maintainer's machine, Ubuntu in CI), in your own git worktree.

```bash
python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
(cd backend && .venv/bin/python -m ruff check app tests)           # lint
backend/.venv/bin/python -m pytest -q backend/tests/test_packs.py   # focused tests, offline
scripts/test_backend_sharded.sh                                    # whole backend suite, 8 parallel file shards
(cd frontend && npm ci && npm run build)                           # static .mjs tests, then tsc and vite
```

- Run Python tests with `LANG=C.UTF-8 LC_ALL=C.UTF-8` (the sharded script sets it). Compare a failing test with the same
  test on a clean `main` before blaming your change.
- In a worktree, symlink `frontend/node_modules` to the main checkout's instead of `npm ci`.
- Gates CI also runs: `scripts/check_spec_status.py`, `scripts/generate_frontend_contracts.py --check`,
  `scripts/gen_modules.py` (then `git diff --exit-code`), `scripts/check_doc_paths.py`,
  `scripts/check_architecture_enforcement.py`, `scripts/check_typecheck_ratchet.py` (mypy errors may only go down).
- Browser walks (`e2e/README.md`): build the frontend with `VITE_API_BASE_URL= npm run build`, then
  `node e2e/<walk>.mjs <evidence-dir> [<data-root-copy>]`.

## Conventions the code does not show

- **Schema** is additive SQLite in `backend/app/core/schema.py` and the per-owner `*_schema.py`; no Alembic. A new
  migration needs a new `CURRENT_SCHEMA_MIGRATION_ID` and, when tables or columns change, the digest/baseline in
  `backend/tests/test_migration_discipline.py` (rules: `docs/MIGRATIONS.md`).
- **Frontend `.mjs` tests assert literal strings and source patterns.** Changing a label, class or markup means updating
  its test in `frontend/tests/`; a new test file must be added to the `build` chain in `frontend/package.json`.
- **Generated files are never edited by hand**: `docs/MODULES.md` (`python scripts/gen_modules.py`) and
  `frontend/src/api/generated` (`python scripts/generate_frontend_contracts.py`). CI fails on drift.
- **Every module directory has a `module.toml`** (purpose, status, entrypoints). Keep it true; a new module gets one.
- **Data root**: runtime data lives under `JARVISOS_DATA_ROOT` and never enters the repository; code gets paths from
  `backend/app/core/paths.py`. Tests isolate the data root automatically. Never point a process at the maintainer's live
  data root or start anything on port 8000 (the live app). Walks use a temporary copy of a data root and
  `JARVISOS_PROOF_PORT`.
- **Packs**: project-specific content (workspace names, sensitivity terms, examples) lives outside this repository and is
  loaded through `JARVISOS_PACK_PATH` (`backend/app/core/packs.py`). The core must run, and CI runs, without a pack;
  tests use the fake pack in `backend/tests/fixtures/packs/zephyr`.
- **This repository is public.** No private project or customer names, IP, personal paths or machine names, credentials or
  real data in code, tests, fixtures, docs or commit messages. CI has a leak guard; do not try to route around it.
- Tests run offline with fake or mock providers. Never require a paid provider.
- `backend/tests/**/test_*_conformance.py` are frozen evidence: change them only when the task names that exact test.
- Windows launchers (`Start-JarvisOS*.cmd`, `scripts/*.ps1`) are not changed on Linux-only evidence; PARK the change.
- Out-of-scope ideas go to `docs/FUTURE_IMPROVEMENTS.md` as PARK items (no authority to implement). Do not write
  reports, handoffs or status notes into the repository.

## Never

1. `route_class="auto"` never executes an external provider.
2. Product AI calls go through `run_ai_task` and create an `ai_jobs` row.
3. The frontend never calls providers, Ollama, the filesystem or execution tools directly.
4. Safe defaults stay safe: paid AI disabled, budget zero, provider mode `fake`.
5. The local classifier is advisory; it owns no permission, provider, memory or sensitivity decision.
6. No secrets in logs, events, docs, fixtures, commits, model context or frontend responses.
7. Model output is a proposal until a user or a deterministic policy promotes it.
8. Never fabricate outputs, validators, metrics, screenshots or expected values; never skip, silence or relabel a test or
   gate to turn it green. A green workflow is not semantic correctness.
9. Never enable GitHub auto-merge, force-push `main`, or change branch protection, rulesets or required checks.
10. Never add infrastructure, a framework or a dependency the accepted outcome does not need; prefer the smallest
    sufficient change and delete over wrap when code is dead.

If an accepted spec seems to require violating one of these, stop that change and report the conflict.

## Evidence by risk

- **Low** (docs, tests, copy, mechanical): the relevant tests and lint pass.
- **Medium** (feature code with tests): the above, CI green on the exact head, and one severe review of the diff.
- **High** (security, egress, credentials, migrations or data loss, supervisor/relay/sandbox, irreversible operations):
  the above plus a full diff review by a second qualified session, and the mutation proven in the real environment.
- **UI-changing PRs**: an AI browser walk from `e2e/` covering the changed surfaces must pass **before** you ask the
  maintainer to look; attach its evidence directory. Reachability is proven from a clean browser profile at `/` using
  visible controls only. Behaviour that depends on execution, a browser or hardware needs that execution, not a review.

## Where to look

| Question | File |
| --- | --- |
| Roles, spec lifecycle, PR/merge, supervisor, when to ask the maintainer | `docs/PROCESS.md` |
| Which module does what, status, entrypoints (generated) | `docs/MODULES.md` |
| Architecture, authority boundaries, packs | `docs/ARCHITECTURE.md` |
| Open specs, dependencies, implementation PRs | `docs/specs/STATUS.md` (template: `docs/specs/SPEC_TEMPLATE.md`) |
| Durable architecture decisions | `docs/DECISIONS.md` |
| Schema migration rules; data-root recovery | `docs/MIGRATIONS.md`, `docs/DATA_ROOT_RECOVERY.md` |
| Which worker for a delegated task | `scripts/worker_routing.py` |
| Resuming or taking over work | `python3 scripts/devctx.py recover` |
