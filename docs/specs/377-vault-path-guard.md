# 377 — VAULT-PATH-GUARD-1: Sandboxes, jobs, eval runs and walks refuse the private vault

Status: ready
Depends on: none

## Goal

When the operator names a private vault with `JARVISOS_VAULT_PATH`, no sandbox mount, registered job, agent-eval run or
browser walk can read from it, copy it or write into it. Each of those entry points checks its paths against the vault
with one shared standard-library helper and refuses, fail closed, when a path is inside the vault or contains it. Proof
and eval backends therefore keep running on fresh data roots and never on a copy of the real one once the real data
root lives in the vault.

## Why

Directive `L20261125114331-7ab2` (storage split), items 3 and 4: proof and eval backends never copy the real data root;
"a guard refuses any sandbox mount, worker command or proof script that touches the vault path". Item 2 moves the real
data root into the vault, so a vault guard is also the guard against copying the real data root. Spec 376 adds the
overlay that the app reads from the same variable; this spec adds no reader, only refusals.

## Code facts checked at `27a7c032`

- `backend/app/modules/relay_gateway/service.py::build_manifest` writes the per-workspace sandbox manifest: workspace,
  `context_dir`, `home`, `runtime_root`, per-agent `tool_roots` and `credentials` (from `_agent_spec`), and
  `dependency_mounts` resolved under `runtime.source_repo`.
- `backend/app/modules/relay_gateway/sandbox.py` is standard library only and runs as the host shim.
  `load_manifest` fails closed with `ManifestError`; `run_shim` prints `jarvis-sandbox: refused: …` and returns
  `MANIFEST_MISSING_EXIT`. `build_bwrap_args` turns every manifest path into a `--bind` or `--ro-bind`.
- `scripts/relay_jobs.py::start` refuses a `cwd` outside `allowed_roots()` and digests `--input` files; it does not look
  at the paths in the command itself.
- `scripts/benchmarks/agent_eval/runner.py` takes `run --data-root` and never starts a backend itself; the operator's
  gate scripts already build fresh roots (settings files only, then `initialize_storage(seed_default=True)`).
- Walks that copy or snapshot a source data root take it as `process.argv[3]` or `--data-root-copy`:
  `bubu_guided_tour_walk.mjs`, `case_walk.mjs`, `cfd_field_3d_walk.mjs`, `design_discovery_walk.mjs`,
  `design_visual_walk.mjs`, `freecad_bridge_walk.mjs`, `freecad_reference_walk.mjs`, `grounding_walk.mjs`,
  `guided_flow_walk.mjs`, `guided_process_reach_walk.mjs`, `owner_predicate_walk.mjs`,
  `process_dynamic_physics_walk.mjs` (re-list with `grep -n "argv\[3\]\|data-root-copy" e2e/*.mjs` at the base).
- The supervisor's own sandbox launcher is installed on the host outside this repository.

## Scope

1. **Helper.** New `scripts/vault_guard.py`, standard library only:
   - `vault_root(env=os.environ) -> Path | None`: the resolved real path of `JARVISOS_VAULT_PATH`, or `None` when unset or
     blank. A set value that cannot be resolved still yields the absolute normalised path, so the guard stays on.
   - `touches_vault(path, vault) -> bool`: true when the real path of `path` equals the vault, is inside it, or contains
     it (a parent directory of the vault). Symlinks are resolved on both sides.
   - CLI `python3 scripts/vault_guard.py check PATH...`: exit 0 when no path touches the vault (or no vault is set),
     exit 3 with `vault_guard: refused: path touches the private vault` otherwise. The message never prints the vault
     path.
2. **Relay gateway sandbox.** `build_manifest` adds `"deny_roots": [vault]` when a vault is set and raises
   `RelayGatewayError("relay_mount_vault_refused")` if the workspace, context dir, home, runtime root, any tool root,
   credential source or dependency mount touches it. `sandbox.build_bwrap_args` re-checks every bind source against
   `manifest["deny_roots"]` and against `JARVISOS_VAULT_PATH` in its own environment, and raises `ManifestError`;
   `run_shim` catches it and refuses like a missing manifest. `sandbox.py` keeps its own copy of the containment check
   (it runs outside the repo's import path); a test pins both implementations to the same cases.
3. **Registered jobs.** `relay_jobs.start` refuses (`ValueError`) when the `cwd`, any `--input`, or any command
   argument that is an absolute path or a `--flag=/abs/path` value touches the vault.
4. **Eval runs.** `agent_eval run` refuses a `--data-root` and an `--out` that touch the vault (exit 1, no request sent).
5. **Walks.** Each walk listed above runs `scripts/vault_guard.py check` on its source data root before reading it and
   stops with the helper's message on exit 3. `e2e/README.md` states that walks and eval runs use fresh roots or
   copies of non-vault roots only.

Out of scope (binding non-goals):

- Moving the data root (item 2), the encrypted backup (item 5), and the supervisor launcher's mount list, which lives on
  the host outside this repository. Pack push and token removal there is an operator host step.
- Any reader of vault content (spec 376) and any consult code.
- Changing which settings files the operator's gate scripts copy into fresh roots.

## Files likely touched

- `scripts/vault_guard.py` (new)
- `backend/app/modules/relay_gateway/service.py`, `backend/app/modules/relay_gateway/sandbox.py`
- `scripts/relay_jobs.py`
- `scripts/benchmarks/agent_eval/runner.py`
- the walks listed above and `e2e/README.md`
- `backend/tests/test_vault_path_guard_377.py` (new)

## Design constraints

- Fail closed: a set variable always guards, even if the directory is missing. An unset variable changes nothing.
- No refusal message, log line, ledger entry or job record contains the vault path.
- Standard library only; no new dependency.
- Existing relay gateway, sandbox, relay_jobs and agent_eval tests pass unchanged with the variable unset.

## Acceptance criteria

1. `touches_vault` is true for the vault, a file inside it, a symlink pointing into it and a parent of it; false for a
   sibling whose name shares a prefix (`vault-x` next to `vault`) and for unrelated paths.
2. With a fake vault set, `build_manifest` raises `relay_mount_vault_refused` for a dependency mount or credential
   source inside it, and `build_bwrap_args` raises `ManifestError` for a hand-edited manifest whose bind source is
   inside it.
3. `relay_jobs.start` refuses a job whose cwd, input or absolute command argument touches the fake vault, and the
   job is not registered.
4. `agent_eval run --data-root <inside vault>` exits 1 without any HTTP request.
5. `vault_guard.py check` exits 3 for a vault path and 0 for a temporary fresh root; no output contains the vault path.
6. Lint, focused tests and CI pass.

## Required tests

- `backend/tests/test_vault_path_guard_377.py`: offline, with `tmp_path` vaults and `monkeypatch` env; covers criteria
  1–5, including the parity of the sandbox copy of the check, and that every refusal text omits the vault path.

## Definition of done

Merged with green CI on the exact head after one severe coordinator review and an independent second review (high risk:
supervisor/relay/sandbox code), and proven in the real environment by one refused job start and one refused manifest
with a temporary vault variable on the host.
