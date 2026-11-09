# 259 — HERMES-UPGRADE-1: qualify Hermes Agent v0.23.0

Status: ready
Depends on: none (runtime authority stays with 146; umbrella 060)

## Goal

JarvisOS qualifies Hermes Agent v0.23.0 (tag `v0.23.0`, commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, released
2026-11-05) instead of v0.21.4 (`d337b736aa1e8ebecfab043842d13e4a2d2f48a3`). Every pin, version check and contract
fixture names the new release, and the Hermes path keeps the same toolset gating, memory adapter and isolation.

## Why

Maintainer scheduling directive `L20261109220757-0951` item 1: move the local-model R&D into Jarvis, starting with the
Hermes upgrade. Named roles and the sub-agent view (items 2 and 3) build on the new release's profile and delegation
behaviour.

## Upstream changes that touch JarvisOS (from the v0.22.0–v0.23.0 release notes)

- Python 3.14 is the managed runtime; the source still accepts `>=3.11,<3.15`.
- The source tree's `pyproject.toml` says `version = "0.0.0"`; the release version comes from an install stamp
  (`hermes_cli.version_info`). `importlib.metadata.version("hermes-agent")` on a source install is therefore not
  `0.23.0`, so the version check must read the release identity Hermes itself reports, still bound to the pinned git
  revision.
- The ten `browser_*` tools are removed (`browser_exec` only); delegation backends are a bundled plugin; external memory
  providers moved to catalog plugins; `hermes mcp serve` moved to a plugin. JarvisOS enables none of these.

## Scope

In scope:
- `UPSTREAM_REVISION` and the version check in `backend/app/modules/agents/hermes/worker_shim.py` and `supervisor.py`;
  the availability probe in `backend/app/modules/ai/thread_routes.py`; `upstream_revision` in
  `backend/tests/test_foundation_contracts.py` and any other test or fixture that names the old release.
- Keeping `ENABLED_TOOLSETS` an allowlist: a toolset or tool added upstream since v0.21.4 is not reachable by the agent.
  Names in `DISABLED_TOOLSETS` that no longer exist may be removed or kept; nothing may move from disabled to enabled.
- The built-in memory adapter in `backend/app/modules/ai/retrieval_index.py`: verify the v0.23.0 on-disk format of
  `memories/MEMORY.md` / `USER.md` against the upstream source and update the docstring (and code, only if the format
  changed).
- Any `run_agent` / `AIAgent` call-signature change the shim depends on.

Out of scope (binding non-goals):
- Changing the live app's `JARVIS_HERMES_VENV` or installing anything into the maintainer's runtime. Building a
  side-by-side v0.23.0 venv is host prep for the proof; switching the live app is owed live activation after merge.
- Enabling delegation, plugins, browser, terminal, file, web or any new toolset; named roles (item 2); sub-agent view
  (item 3).
- Desktop, gateway, cron, group-chat or cloud features.

## Acceptance criteria

1. No source, test or fixture outside `docs/` and `eval/` names `0.21.4` or `d337b736…` as the qualified release.
2. The worker shim and the availability probe accept exactly v0.23.0 at commit `2bd378ed…` and refuse any other
   version or revision (tests cover accept, wrong version, wrong revision).
3. The toolset allowlist is unchanged or narrower; a test asserts the agent is constructed with only the enabled
   toolsets.
4. The memory adapter reads the v0.23.0 format; its existing tests pass, and a test fixture reflects any format change.
5. `scripts/test_backend_sharded.sh`, ruff and the CI gates pass.
6. Proof (high risk, runtime boundary): with a side-by-side v0.23.0 checkout and venv, `verify_upstream()` passes and
   one offline Hermes turn through the shim with the fake/local route completes; the evidence directory is recorded.
