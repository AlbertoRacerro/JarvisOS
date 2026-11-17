# 298 — ACTIVITY-PANEL-1: One read-only Activity panel for long-running work, first slice of the front-end plan

Status: draft
Depends on: none

## Goal

Every screen has an "Attività" control in the app shell. It opens one panel listing running and recently finished
long work, one row per job: what it is in plain Italian words, who runs it, elapsed time, a progress bar only when a
total is recorded, the current step in one line, and the outcome when finished. A row opens a detail with the last
readable log lines and links to what the job produced. One compact line shows quota headroom per pool. The panel only
reads existing state; it starts, stops and changes nothing.

## Why

The owner cannot follow what is running: neither work started for him nor the supervisor's work. Most of what was built
in the last week has no screen, and the front-end plan that will give it one needs a place where long work becomes
visible first. Reading only state that the backend, the relay and the supervisor already write keeps this slice small
and honest.

## Code facts checked at `4b073b55`

- `backend/app/modules/coding/relay_monitor.py::snapshot` returns the supervisor's semantic state, or `unknown` with a
  reason; `backend/app/modules/coding/relay_monitor.py::_checked` refuses a payload carrying a secret and scrubs paths.
- `scripts/relay_observe.py::snapshot` already lists the registered jobs a waiting turn depends on and maps states to
  Italian phrases (`jobs_ready`, `jobs_recoverable`).
- `scripts/relay_jobs.py::all_jobs` reads every registered job record (`label`, `status`, `started_at`, `ended_at`,
  `exit_code`, `spec`, `log`); `scripts/relay_jobs.py::effective_status` is the only status to show (absence of a PID
  never implies success or failure).
- `backend/app/modules/process_stack/dynamic_jobs.py::list_jobs` lists file-backed dynamic simulation jobs, scoped to one
  workspace and one draft.
- `backend/app/modules/coding/provider_quota.py::read_all` reads the quota of each worker pool without running a model
  turn.
- The `ai_jobs` and `runner_jobs` tables in `backend/app/core/schema.py` hold status and timestamps of AI calls and
  script runs; neither records a progress total.
- Model or file downloads have no recorded state in `backend/app` (searched for download, gguf and hub calls; none
  found). Not checked: downloads run by scripts outside the backend.

## Scope

In scope:
- One backend read model `activity` that merges, newest first and bounded (at most 50 rows, finished rows from the last
  24 hours): supervisor missions and the worker each executes (relay snapshot and registered jobs), dynamic simulation
  jobs and script runs, and AI jobs that are still running or failed in the window.
- One `GET` route returning rows with: kind, plain title, runner (`supervisione`, `modello di lavoro`, `motore locale`,
  `lavoro notturno`), started and ended time, state word, optional `progress {done,total,unit}`, current step, outcome,
  and links (PR, run, file) as in-app routes or the PR URL.
- One detail `GET` returning the last 40 log lines of one row, passed through the same secret refusal and path scrub
  as `relay_monitor._checked`.
- Shell control "Attività" with a count of running rows, a panel with rows and detail, polling every 5 s while open and
  every 30 s while closed.
- One quota line from `provider_quota.read_all` with percent free per pool.

Out of scope (binding non-goals):
- Starting, cancelling or retrying anything from the panel.
- Recording download progress: downloads appear only once a later slice gives them a recorded state; this slice shows
  an empty "Download" group with "nessun download registrato".
- New tables or migrations; any change to how jobs are recorded.
- Ids, SHAs, cycle numbers or commands in visible text (they may appear in the detail's link targets only).

## Files likely touched

- `backend/app/modules/coding/activity.py` (new), `backend/app/modules/coding/runtime_routes.py`
- `frontend/src/components/ActivityPanel.tsx` (new), the app shell, `frontend/src/api/generated` (regenerated)
- `backend/tests/test_activity.py`, `frontend/tests/activity-panel.test.mjs`, `frontend/package.json`

## Design constraints

- Read only. Each source is read independently; an unreadable source yields one row group marked `non disponibile`
  with a reason, never an error for the whole panel.
- Status words come from a fixed table per source; `relay_jobs.effective_status` and the relay snapshot are the
  authority for supervisor rows.
- Visible language follows the Slack message rules: Italian, "sto facendo X", no ids, SHAs, cycles or commands.
- Progress bar only when the source records both done and total; otherwise an indeterminate spinner and elapsed time.

## Acceptance criteria

1. With fixtures for each source, the route returns rows from all of them, newest first, at most 50.
2. A source that raises yields a group marked `non disponibile` and the other groups unchanged.
3. A log line containing a secret is refused in the detail; a local path is scrubbed.
4. No visible text in the route's titles, steps or state words matches an id, a 7+ hex SHA or a command pattern.
5. The panel is reachable from `/` and from every main screen through the visible "Attività" control; the browser walk
   opens it, opens a row detail and closes it.

## Required tests

- Backend: fixture job records and relay state under a temporary control root; one per source, merge order, bound,
  failing source, secret refusal, path scrub, visible-text pattern check.
- Frontend `.mjs`: the control label, the empty-download text, the indeterminate state without a total.
- Browser walk in `e2e/` from a clean profile at `/`, attached as evidence.

## Front-end plan (directive placement, later slices, not part of this spec)

Each later slice gets its own spec and walk; the place is where the guided path would use it.

1. Activity panel (this spec).
2. Checks and verdict as status lights on every evaluation result (solids, mesh, static structure, closed-loop energy
   closure) on the Runs and Design result views.
3. Source and check state as a chip on every numeric input (literature datum, ingested value, result, typed).
4. Memory: any-word search with readable record summaries, document folders with a folder picker instead of the
   operator command, retrieval quality figure.
5. Runs: bench results and solids from authored geometry code with their named ports.
6. Download state recorded so the Activity panel's download group fills.
