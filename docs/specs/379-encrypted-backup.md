# 379 — ENCRYPTED-BACKUP-1: Daily encrypted backup with a monthly restore test and a status line

Status: in_review
Depends on: none

## Goal

The operator's machine makes a daily encrypted backup of the paths the operator lists (the pack, the vault, the JarvisOS
working state) to one or more restic repositories (the external drive, and optionally a cloud copy that only ever holds
ciphertext). The key exists only in the maintainer's password manager and reaches restic only through a
password command. A monthly restore test proves a backup can be read back. Settings > System shows
"Ultimo backup: …" and the last restore test, and nothing else about the backup.

## Why

Directive `L20261125114331-7ab2` (storage split), item 5: "Encrypted backup (before GitHub is removed) … restic or an
equivalent, with the key held only by the maintainer … daily … monthly restore test and a status line in the UI". The
maintainer's own steps 2–4 (check the backup, then revoke tokens and archive the private remote) wait for it.

## Code facts checked at `8fdb9f7d`

- `scripts/host_ops.py::UNIT_TEMPLATES` holds the user units the host broker may install, and
  `op_service_install` installs only `<name>.service` for names the host policy's `service.install.templates` allows.
  There is no timer support.
- `backend/app/api/system.py::system_info` returns the system facts the Settings page reads.
- `frontend/src/pages/Settings.tsx` shows a "System" card with a `settings-facts` list (Environment, Data root,
  Database, Provider configured).
- restic is a separately installed BSD-2-Clause program; nothing is imported or bundled.

## Scope

1. **Runner.** New `scripts/backup.py`, standard library only, run on the host (never in a sandbox):
   - reads `~/.config/jarvisos-hostops/backup.toml` (operator-written, never in a repository): `repositories` (one or
     more restic repository strings), `paths` (absolute directories), optional `exclude` globs, and
     `password_command` (the command that prints the key from the password manager);
   - refuses to run when `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE` is set, when the config has a literal password
     key, or when `password_command` names a file inside any listed path, a repository checkout or
     `JARVISOS_VAULT_PATH`; it passes the key to restic only as `--password-command`;
   - `run`: `restic backup` of the listed paths to each repository, then `restic forget --keep-daily 14
     --keep-weekly 8 --keep-monthly 12 --prune` and `restic check`;
   - `restore-test`: runs `restic check --read-data-subset 5%`, restores the latest snapshot of the first repository
     into a fresh temporary directory, checks every restored file's size against `restic ls --json`, compares the
     SHA-256 of a random sample of at least 50 restored files with the live files whose modification time is not later
     than the snapshot time, then deletes the temporary directory;
   - `init`: initialises a listed repository that does not exist yet (the maintainer runs it once);
   - writes `~/.local/state/jarvisos-backup/status.json` atomically after every command: last success and last failure
     time per repository, snapshot id, bytes added, last restore test time and result. No path, file name, key or
     restic output text is stored there; a failure keeps only a short reason code.
2. **Schedule.** `UNIT_TEMPLATES` gains `jarvisos-backup` (oneshot `backup.py run`) and `jarvisos-backup-restore-test`
   (oneshot `backup.py restore-test`), each with a timer (`OnCalendar=daily`, `Persistent=true`; monthly for the
   restore test). `op_service_install` installs and enables the `.timer` when the template has one. The host policy
   allows the two templates.
3. **Status.** `system_info` adds `backup: {"state": "never" | "ok" | "failed" | "stale", "last_success_at",
   "last_restore_test_at", "last_restore_test_ok"}` from the status file (path overridable by
   `JARVISOS_BACKUP_STATUS_FILE` for tests); `stale` when the last success is older than 48 hours. Settings > System
   adds "Ultimo backup" (date and time, or "mai", or "non riuscito", or "vecchio") and "Ultimo test di ripristino".
4. **Runbook.** A section in `docs/RUNBOOKS.md`: install restic, put the key in the password manager, write
   `backup.toml`, `init`, install the units through the host broker, run `restore-test` once by hand, and what to do
   when the status line says "non riuscito" or "vecchio".

Out of scope (binding non-goals):

- Choosing the maintainer's password manager, drive or cloud account; any agent reading the key or the vault.
- Revoking tokens or archiving the private remote (the maintainer's own steps).
- Backing up through Relay, the sandbox or any cloud model session.

## Files likely touched

- `scripts/backup.py` (new), `scripts/host_ops.py`
- `backend/app/api/system.py`, `frontend/src/pages/Settings.tsx`, the Settings `.mjs` test in `frontend/tests/`,
  and `frontend/src/api/generated` if the system contract is generated (regenerate, never hand-edit)
- `docs/RUNBOOKS.md`
- `backend/tests/test_encrypted_backup_379.py` (new), an `e2e/` walk or an extension of the Settings walk

## Design constraints

- The key never touches disk inside a repository, a sandbox mount, a log, the status file or the API.
- A missing or unreadable status file reads as `never`; a malformed one as `failed`. The API never errors on it.
- Tests use a fake `restic` executable on `PATH` and temporary directories; CI never needs restic.
- Standard library only.

## Acceptance criteria

1. With a fake restic, `run` calls `backup`, `forget … --prune` and `check` for each repository with
   `--password-command`, never with a password in argv or env, and writes an `ok` status without paths.
2. Each forbidden key source (`RESTIC_PASSWORD`, `RESTIC_PASSWORD_FILE`, literal password key, password command
   inside a listed path or the vault) refuses before any restic call.
3. `restore-test` reports failure when the fake restic restores a file whose hash differs, and success otherwise; the
   temporary directory is gone afterwards in both cases.
4. `system_info.backup` gives `never`, `ok`, `failed` and `stale` for the matching status files; the Settings card shows
   the Italian line for each.
5. `op_service_install` installs and enables the timer for a template that has one, and still refuses templates the
   policy does not allow.
6. Lint, focused tests, the frontend build and CI pass; the browser walk shows the status line from a clean profile.

## Required tests

- `backend/tests/test_encrypted_backup_379.py`: fake restic, the refusals, status file contents, restore-test
  pass/fail and cleanup, `system_info.backup` states, timer install.
- The Settings `.mjs` test for the new labels; a browser walk covering the System card.

## Definition of done

Merged with green CI on the exact head after one severe coordinator review and an independent second review (high risk:
credentials and data loss), the browser walk evidence attached, and a host proof: the maintainer (or a host session he
starts) runs `init`, `run` and `restore-test` against the external drive and the status line shows the result.
