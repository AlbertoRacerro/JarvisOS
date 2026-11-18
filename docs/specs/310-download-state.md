# 310 — DOWNLOAD-STATE-1: Recorded download state so the Activity panel's download group fills

Status: ready
Depends on: 298

## Goal

A large file the owner needs on this machine (a model weight file, a dataset, an installer) can be fetched through one
small standard-library script that records its state as it goes: name, bytes done, bytes expected when known, state and
outcome. The Activity panel's `Download` group lists those records with a progress bar in megabytes, instead of always
saying `nessun download registrato`.

## Why

Sixth item of the front-end plan in spec 298 (maintainer directive `L20261117141824-ed48` point 2: downloads first among
the job kinds). The owner could not follow model downloads started for him because they ran as ad hoc shell commands
that leave no record. Nothing in the repository downloads such files today, so the smallest honest fix is one recorded
way to do it, read by the panel; downloads started any other way stay invisible, and the empty-group text says so.

## Code facts checked at `ac3bdacc`

- `backend/app/modules/coding/activity.py::read` returns `downloads=[]` and `downloads_empty="nessun download registrato"`.
- `backend/app/modules/coding/activity.py::ActivityProgress` carries `done`, `total` and `unit`;
  `backend/app/modules/coding/activity.py::_dynamic_rows` already builds rows with a progress.
- `backend/app/modules/coding/activity.py::_job_rows` reads registered jobs under
  `backend/app/modules/coding/relay_monitor.py::control_root`.
- `scripts/relay_jobs.py::control_root` resolves the same control root (`RELAY_CONTINUITY_DIR` or the default) and
  `scripts/relay_jobs.py::write_json_atomic` writes a record atomically.
- `frontend/src/components/ActivityPanel.tsx` renders the `Download` section with only the empty text.
- No code under `scripts/` or `backend/app` downloads model or data files (searched for download, pull, hub and gguf).

## Scope

In scope:
- New `scripts/fetch_file.py` (standard library only): `fetch_file.py URL DEST [--sha256 HEX] [--label TEXT]`.
  - HTTPS only (`http://` refused except loopback); follows redirects; streams to `DEST.part` then renames to `DEST`
    only after a complete transfer and, when given, a matching SHA-256; refuses to overwrite an existing `DEST`.
  - Writes `<control_root>/downloads/<id>.json` atomically at start, at most once per second while running, and at
    the end: `id`, `label` (default: the file name of `DEST`), `host` (URL host only), `file_name`, `bytes_done`,
    `bytes_total` (from `Content-Length`, else null), `state` (`running`, `succeeded`, `failed`), `reason` (short,
    on failure), `started_at`, `ended_at`, `pid`, `sha256` (on success).
  - The record never stores the full URL, query strings, headers, tokens or the destination directory.
  - Exit code 0 only on success; a failed or interrupted transfer removes `DEST.part` and records `failed`.
- `backend/app/modules/coding/activity.py`: `_download_rows` reads at most `SCAN_FILES` records, newest first, keeps
  running ones and finished ones from the same recent window as other rows, and fills `downloads` with rows of kind
  `download`: title `Download di <label>`, runner `motore locale`, progress `ActivityProgress(done=MB, total=MB, unit="MB")`
  when `bytes_total` is known, step `sto scaricando` while running, outcome `completato` or `non riuscito` with the
  reason. A `running` record whose `pid` is no longer alive on this host and whose last write is older than two minutes
  shows `da verificare` (never `completato` or `non riuscito`: absence of a process proves nothing).
  `downloads_empty` stays the text shown when the list is empty. `ActivityRow.kind` gains `"download"` (regenerate
  `frontend/src/api/generated` with `scripts/generate_frontend_contracts.py`).
- `frontend/src/components/ActivityPanel.tsx`: render download rows in the `Download` section with the same row
  component as the main list (`ActivityRowView`: progress bar, state word, outcome), without `onOpen`: a download row
  has no detail view in this slice.
- `docs/` is not changed beyond this spec and its STATUS row; the script's `--help` is its documentation.

Out of scope (binding non-goals):
- No backend route that starts, stops or resumes a download; the panel stays read-only.
- No resume of partial transfers, no parallel parts, no mirror selection, no authentication or token handling.
- No change to `scripts/relay_jobs.py` or the supervisor; a long download is started as a registered job with the
  usual `relay_jobs.py start` line when it must outlive a turn.
- No new dependency.

## Design constraints

- Rows pass through the existing `_visible` rule of `activity.py` (no ids, hashes, commands or paths in visible text).
- Malformed or unreadable records are skipped, never raise; at most 50 rows overall as today.
- Writes go only to `DEST.part`, `DEST` and the record file; the script never deletes anything else.

## Acceptance criteria

1. `fetch_file.py` against a loopback HTTP test server produces `DEST` with
   the right bytes and a `succeeded` record with `bytes_done == bytes_total` and `sha256`; a wrong `--sha256` leaves no
   `DEST` and no `.part`, records `failed`, exits non-zero; an existing `DEST` is refused before any request.
2. A non-loopback `http://` URL is refused; the record of any transfer contains no URL path, query or destination
   directory.
3. `activity.read` returns download rows for running, succeeded and failed records with megabyte progress when the
   total is known, `da verificare` for a stale running record of a dead process, and skips malformed files.
4. `ActivityPanel.tsx` shows download rows in the `Download` section and the empty text when there are none.
5. A browser walk from `/` opens the Activity panel with a seeded running and a seeded finished download record in a
   temporary control root and sees both rows, the progress bar and no console or page errors.

## Required tests

- `backend/tests/test_fetch_file.py`: loopback HTTP server fixture; success, checksum mismatch, existing destination,
  refused scheme, interrupted transfer (server closes early), record content without URL path or directory.
- Activity read-model tests for the download rows in the existing activity test file.
- Frontend `.mjs` static test for the `Download` section rows, added to the `build` chain.
- Browser walk in `e2e/` with evidence directory.
