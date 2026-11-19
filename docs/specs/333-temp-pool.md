# 333 — TEMP-POOL-1: Temporary OpenAI-compatible worker pools that expire by themselves

Status: merged
Depends on: —

## Goal

A free, time-limited model offer (an OpenAI-compatible endpoint and a key the maintainer created) can join the worker
routing within hours, without a code change, and leaves it by itself at its expiry. Workers on such a pool run through
the installed OpenCode harness, on the public repository only, and earn a tier from a short fixed qualification set.

## Why

Directive `L20261119163228-83fc`: frontier models are offered free for a few days on inference portals; the supervisor
must be able to use them while they last, at zero added cost, without writing a new client. The same mechanism later
serves the free API pools of `L20261118164933-7605` point 3.

## Code facts checked at `08ddf956`

- `scripts/worker_routing.py::MODELS` is a fixed tuple of `Model(key, kind, model, pool, capability)`;
  `scripts/worker_routing.py::read_pools` reads one `Pool` per pool name used by the models;
  `scripts/worker_routing.py::rank` skips a pool that is unreadable or below its reserve.
- `scripts/worker_routing.py::bare_command` builds the worker launch line per `Model.kind` (`codex`, `claude`,
  `copilot`, `agy`); `scripts/worker_routing.py::launch_command` wraps it in `relay_jobs.py start`.
- `scripts/worker_routing.py::cached_summary` is the quota line shown in every coordinator prompt.
- `scripts/worker_routing.py::record` appends worker outcomes; tiers of other pools are earned from those records.
- `scripts/worker_routing.py::default_control_dir` is the private control directory outside the repository.
- `scripts/copilot_worker.py` is the model for a worker wrapper: isolated home under an evidence directory, a child
  environment without coordinator GitHub or Git credentials, evidence written, never commits.
- OpenCode 2.x is installed for the user (`opencode run --model provider/model --format json --auto`); it accepts an
  OpenAI-compatible provider from its config file. Exact config keys are checked by the implementation, not assumed.

## Scope

In scope:
- A private file `temp-pools.json` in the control directory (never in the repository). One entry per pool: `name`,
  `provider`, `base_url` (https only), `model`, `key_file` (absolute path outside the repository), `expires_at`
  (ISO-8601 with zone), `rate_limits` (free text), `data_policy` (free text). A missing or malformed file means no
  temporary pools; a malformed entry is skipped with a reason, never raised.
- `worker_routing.py`: temporary entries become `Model(key="temp-<name>", kind="opencode", pool="temp-<name>")` whose
  capability is the tier earned in qualification (none yet: skipped `unqualified`). Their pool reads free `1.0` while
  `now < expires_at` and the key file exists; otherwise unreadable with reason `expired` or `no_key`. Expired pools
  disappear from routing with no edit. `cached_summary` names a temporary pool with its hours left, and says
  `da qualificare` when its key is present and it has no qualification yet.
- `scripts/opencode_worker.py run --pool <name> --task-file F --worktree W`: refuses when the pool is expired, the key
  is missing, the worktree is not a linked worktree of this repository, or `JARVISOS_PACK_PATH` or any path under the
  control directory's private areas would be visible to the child. Builds an isolated OpenCode config and home under
  an evidence directory with only that provider; passes the key through one environment variable; strips GitHub, Git,
  provider and pack variables from the child; runs one `opencode run` turn bounded by a timeout; writes evidence
  (pool, model, exit, duration, changed files); never commits or pushes.
- `scripts/opencode_worker.py qualify --pool <name>`: runs a fixed qualification set of three bounded tasks kept in
  the repository (`scripts/temp_pool_qualification/`: two tier-1 tasks, one tier-2 task, each with a task text and a
  deterministic check command) in throwaway worktrees of `origin/master`; records each outcome with `record`; the pool
  earns tier 1 when both tier-1 tasks pass and tier 2 when all three pass; a pool that passes neither stays unused.
  Run as a registered `relay_jobs.py` job by the coordinator as soon as the summary says `da qualificare`.

Out of scope (binding non-goals):
- No account, login or key creation by any agent; the Slack request of directive point 5 uses the existing message
  path and is not changed here.
- No new HTTP client; no private pack, maintainer data or private repository ever reaches a temporary pool.
- No supervisor or relay code change; qualification is launched by the coordinator, not by a daemon.

## Files likely touched

- `scripts/worker_routing.py`, `scripts/opencode_worker.py` (new), `scripts/temp_pool_qualification/` (new)
- `backend/tests/test_worker_routing.py` and a new `backend/tests/test_opencode_worker.py`

## Design constraints

- Standard library only, like the other worker scripts.
- The key is never printed, logged, written into evidence or into the generated OpenCode config (environment only).
- Fail closed per pool: any doubt (clock, file, schema) means the pool is skipped.

## Acceptance criteria

1. With a fixture `temp-pools.json` whose entry expires in one hour and whose key file exists, `pick` lists the pool;
   with the expiry in the past, or the key file absent, it is skipped with `expired` or `no_key`.
2. A qualified-at-tier-1 pool is never chosen for a tier-2 task; an unqualified pool is never chosen.
3. `opencode_worker.py run` refuses a worktree outside this repository and refuses when `JARVISOS_PACK_PATH` is set;
   the child environment (captured by a fake `opencode` on `PATH`) holds no `GH_TOKEN`, `GITHUB_TOKEN`, `GIT_*`
   credential or pack variable, and the key appears in no evidence file.
4. `qualify` with a fake `opencode` that solves the tier-1 tasks only records three outcomes and gives tier 1.
5. One live proof when a real offer is active: qualification run on the real endpoint, results in the ledger. Until
   then the row stays `in_review` after merge of the code slice, with this criterion open.

## Required tests

- Unit tests for criteria 1–4 with fakes; no network.

## Definition of done

Criteria 1–4 tested and merged; criterion 5 proven live on the first real offer. Risk: high (worker sandbox and egress
of repository code to a third party): full diff review by a second qualified session before merge.
