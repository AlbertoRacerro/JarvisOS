# 262 — RELEASE-SELF-INSTALL-1: the supervisor installs its own release, with guards

Status: ready
Depends on: none

## Goal

When a new commit lands on GitHub `master` with its required checks green, the supervisor installs it without a person
at a host shell. Installation happens only at a task boundary. The previous release is kept. If the new release is not
healthy within a bounded time, the previous one is restored automatically and the maintainer is told once. This covers
the three supervisor processes: the host-ops executor, the observer (`watch`) and the mission owner.

## Why

Every supervisor fix waits for the maintainer to reinstall it by hand. On 2026-11-13 the installed executor and
watcher were 44 commits behind `master`. The maintainer approved self-installation on 2026-11-13 with exactly these
guards: green required checks on `master`, a task boundary, the previous release kept, automatic return to it.

## Code facts (verified at `0e5135e0`)

- `scripts/host_ops.py` `op_release_install` already installs a `master` commit, but only if the host policy has
  `release.install.trust_master: true`. `DEFAULT_POLICY` sets it to `False`.
- `install_release` checks that the commit is on `master` (`compare/{sha}...master` is `ahead` or `identical`). It does
  **not** check CI. It extracts only `scripts/` into `releases/<sha>`, switches the `current` symlink atomically, and
  never deletes older releases.
- `op_release_install` then sets `ctx.restart_required`. `serve` exits 75 and the unit has `RestartForceExitStatus=75`,
  so systemd restarts the executor from `current`. The op also restarts `jarvisos-relay-watch.service` if it is active.
  Nothing checks health afterwards. Nothing returns to the previous release.
- The executor unit (`SERVICE_UNIT`) and the watcher unit (`UNIT_TEMPLATES["jarvisos-relay-watch"]`) both run from
  `~/.local/share/jarvisos-hostops/current/scripts/`. The sandbox cannot see that directory (`Paths.install`).
  `Paths.published` (`~/.local/share/jarvisos/hostops`) is visible read-only inside the sandbox.
- The mission owner does **not** run from a release. Its unit (`docs/implementation/relay-continuity-runbook.md`,
  "Persistent owner") runs `jarvis-sandbox python3 scripts/relay_continuity.py serve` in the main checkout. Today the
  owner's code changes only when someone fast-forwards that checkout and restarts the owner.
- `relay_continuity.py` finds its sibling modules through `Path(__file__).resolve().parent`. `repo_root()` uses
  `git rev-parse --git-common-dir` from the working directory, not `__file__`. `worker_routing.py` lines 537 and 554
  derive the repository root from `__file__`. Whoever implements must check every such use before running the owner
  from a release directory.
- `command_serve` reaches a task boundary only between `serve_step` calls. A provider turn never spans that point. The
  owner already stops at a boundary when `STOP` exists.
- `OWNER_ACTIONS` allows only `start` of the owner unit. A stop or restart would kill a running turn.
- The required checks come from the active ruleset (`gh api repos/{repo}/rules/branches/master`, type
  `required_status_checks`). Classic branch protection has none (`enforcement_level: off`). At `0e5135e0` they are
  `backend`, `gen-check`, `doc-paths` and `leak-guard`.

## Scope

In scope:

1. **Guarded install (executor).** `release.install` refuses unless every required check of the ruleset has a
   completed check run on that exact commit with conclusion `success`, read at install time. A ruleset that cannot be
   read, or that has no required checks, refuses: it never counts as green. It also refuses a commit older than the
   installed one. Before it switches `current`, it runs the candidate's own preflight in a subprocess
   (`python3 -I releases/<sha>/scripts/host_ops.py preflight`). The preflight checks that the release imports, loads
   the live policy, and finds every operation that the policy enables. Only then does `current` move.
2. **Pending marker and confirmation.** Before it switches, the executor writes `state/release-pending.json` (previous
   sha, candidate sha, deadline, request id, attempts). The new executor confirms the release only after three
   things: one full `step` with heartbeat, `problem: null` in the heartbeat, and the watcher `active` for 60 s with no
   new restarts, if it was active before. On confirmation it moves the marker to `state/release-history.jsonl`.
3. **Automatic return.** Each executor start first runs a guard from a pinned copy of the previous release
   (`ExecStartPre=`, a copy written by the outgoing executor before the switch). It returns to the previous release
   when a marker is pending and either its deadline (default 10 min) has passed or the start count exceeds 3. To
   return, it moves `current` back and restores the watcher. It also returns when the new executor runs but finds
   itself unhealthy. Either way it sends one Slack message per failed candidate. That candidate is then remembered as
   refused and is never retried automatically.
4. **Owner from the release.** The executor also publishes the release's `scripts/` read-only under `Paths.published`.
   The owner unit runs `relay_continuity.py` from that published `current`, with the main checkout as its working
   directory. At a task boundary the owner requests `release.install` for `origin/master`, but only when the head is
   newer than its own release and has green required checks. It then exits 0 without taking a new task. The executor
   installs. After the executor confirms, it starts the owner (the `start` action is already allowed). Within the
   deadline the owner must report its release sha in its serve state. If not, the executor returns the published link
   to the previous release, starts the owner again and notifies once. When an install the owner requested is refused
   or fails, the executor still starts the owner on the release it had, so an owner never stays stopped because of
   an install.
5. **One host action, asked once.** `host_ops.py install --sha <sha> --self-install` writes the new executor unit (with
   the guard), the owner unit template, and `trust_master: true`. The coordinator asks the maintainer to run it, with
   the exact command, but only after acceptance criteria 1–7 are proven in tests and the real install below has
   passed with `trust_master` still false. Until then releases are installed as today.
6. Releases are pruned to the current one, the previous one and the pinned guard copy, never fewer.
7. **Main checkout follows `master` (amendment, defect F5).** Sessions run `scripts/devctx.py` and the other scripts
   from the main checkout, so a current owner alone leaves them stale. At a task boundary, after any release request
   is settled, the owner runs `git fetch origin master` and `git merge --ff-only origin/master` in the main checkout,
   and only when the checkout is
   on branch `master` and `git status --porcelain --untracked-files=no` is empty. It never resets, checks out, stashes
   or merges anything else. A refused or failed fast-forward is logged once per `origin/master` sha and is not fatal.
   Chosen over naming the published release path in the cycle text (option (b) of F5): it also keeps `AGENTS.md`,
   `docs/PROCESS.md` and the specs that sessions read current, and the checkout was fast-forwarded by hand twice on
   2026-11-13 with no incident.

Out of scope (binding non-goals):

- No other change to the main checkout. Beyond the fast-forward of scope 7 the supervisor never moves, resets or
  checks out the main checkout, because the maintainer's app runs from it.
- No install during a turn, and no stop or restart of a running owner. `OWNER_ACTIONS` stays `start` only.
- No new dependency, no new daemon, no sd_notify watchdog. A hung executor that keeps a stale heartbeat is reported by
  the existing heartbeat checks, not repaired (residual risk, stated in the runbook).
- No change to which commits may reach `master` (branch protection, rulesets, required checks).

## Files likely touched

- `scripts/host_ops.py` (install, preflight, guard, confirmation, publish, units, `install --self-install`)
- `scripts/relay_continuity.py` (`command_serve`: request at a task boundary, report the release sha)
- `backend/tests/test_host_ops.py`, `backend/tests/test_relay_continuity*.py`
- `docs/implementation/relay-continuity-runbook.md` (owner unit, self-install, rollback)

## Design constraints

- `host_ops.py` stays stdlib-only and imports nothing from a checkout an agent can write.
- The return to the previous release runs code from the previous release, never from the candidate.
- Every install, confirmation, return and refusal ends in a signed receipt or a history line. Nothing is silent.
- A candidate is judged only from GitHub (ruleset and check runs on the exact sha). A local test run never counts.

## Acceptance criteria

1. `release.install` refuses a `master` commit with any required check missing, pending or not `success`. It refuses
   when the ruleset cannot be read or lists no required checks. It refuses a commit behind the installed one.
2. A candidate whose preflight fails never becomes `current`.
3. A candidate that crashes at start is replaced by the previous release within the deadline. The previous release
   then runs and confirms itself, and exactly one notice is sent.
4. A candidate that starts but reports a heartbeat problem, or whose watcher does not stay active, is returned the
   same way.
5. A healthy candidate is confirmed, the marker moves to history, and the previous release directory still exists.
6. The owner never requests or undergoes an install while a turn runs. It resumes from the new release and reports
   its sha. If it does not report in time, it is returned to the previous release.
7. A refused candidate is not retried automatically. The next newer green commit is.
7a. At a task boundary the owner fast-forwards a clean main checkout on `master` to `origin/master`; a dirty tree,
   another branch or a non-fast-forward leaves the checkout untouched and logs once; nothing runs during a turn.
8. **Real-environment proof (high risk).** Install a real green `master` commit on the host executor with the
   maintainer's current policy (`trust_master: false`). This proves the refusal path. Then prove a guarded install
   and a forced return on a temporary home with real `systemd --user` units. Attach both receipts.

## Required tests

Offline, with fake `gh`, a fake `systemctl` and a temporary home. Cover every acceptance criterion from 1 to 7a: green,
red, pending and missing checks; an unreadable ruleset; preflight failure; crash loop then return; unhealthy then
return; confirmation; the owner boundary (no request mid-turn); a refused candidate not retried; pruning keeps the
previous release.

## Definition of done

Test gate green, CI green on the exact head, full diff review by a second qualified session, and criterion 8 proven.
The maintainer has been asked once for the host action, with the exact command.
