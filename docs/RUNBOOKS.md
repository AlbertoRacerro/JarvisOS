# Runbooks

Operating JarvisOS locally. Linux is the primary platform (WSL2 Ubuntu on the maintainer's machine); JarvisOS is a local
developer build, not a hosted service.

## Install

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
(cd frontend && npm ci)
```

Python 3.11 or newer and a current Node LTS are required. Rebuild either environment the same way if it is deleted.

## Run

Maintainer machine: `python3 scripts/jarvisos_launcher.py start` serves the built frontend and the API from one loopback
port, starts the optional managed local model, and opens the browser. `status` and `stop` do the obvious. Machine-specific
settings live in the private `~/.config/jarvisos/operator.env` (`setup` creates it). The launcher only fast-forwards a
clean checkout and leaves a dirty or diverged one as it is.

For managed llama-server, set `JARVISOS_LLAMACPP_API_KEY_FILE` in `operator.env` to an absolute path in an existing
private directory (for host-side evaluation, the host-ops config directory). Each launch replaces that file with its
current API key at mode 0600.

Development, two terminals:

```bash
cd backend && .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
cd frontend && npm run dev            # http://127.0.0.1:5173
```

The database is created on first start. Check it:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/system/info      # database.ready should be true
```

Never start a second backend against the data root of a running app, and never use port 8000 from an agent session: use a
temporary `JARVISOS_DATA_ROOT` and another port.

## Data root

Runtime data (SQLite database, workspaces, artifacts, logs, secrets) lives under `JARVISOS_DATA_ROOT`; the repository never
contains it. Snapshot and restore: `docs/DATA_ROOT_RECOVERY.md`. Schema rules: `docs/MIGRATIONS.md`.

## Encrypted backup

`scripts/backup.py` (spec 379) backs up the directories you list with restic, on the host only, never through Relay, a
sandbox or a cloud session. The key lives only in your password manager.

1. Install restic from your distribution (`sudo apt install restic`); it is a separate program, not part of JarvisOS.
2. Put the backup key in your password manager and check that one command prints it, for example
   `pass show jarvisos/restic`.
3. Write `~/.config/jarvisos-hostops/backup.toml` with `repositories` (the external drive, and optionally a cloud
   repository: it only ever receives ciphertext), `paths` (absolute directories: the pack, the vault, the JarvisOS working
   state), optional `exclude` globs and `password_command`. Never write the key itself there; `backup.py` refuses a
   password key in the file, `RESTIC_PASSWORD`/`RESTIC_PASSWORD_FILE` in the environment, and a password command that
   names a file inside a backed-up directory, a git checkout or the vault.
4. Run `python3 scripts/backup.py init` once (it creates only repositories that do not exist yet), then
   `python3 scripts/backup.py run` and `python3 scripts/backup.py restore-test` by hand. The restore test restores
   into a temporary directory under `TMPDIR`: it needs free space for the latest snapshot.
5. Install the schedules through the host broker: `python3 scripts/host_ops.py request service.install --args
   '{"template": "jarvisos-backup"}'` (daily) and the same with `jarvisos-backup-restore-test` (monthly). Each enables
   a systemd user timer.
6. Settings > System shows "Ultimo backup" and "Ultimo test di ripristino" from
   `~/.local/state/jarvisos-backup/status.json`, which holds only times, snapshot ids, sizes and reason codes.

When the line says **non riuscito**, read the reason code in the status file (`backup_failed`, `forget_failed`,
`check_failed` per repository, or the refused key source) and run `python3 scripts/backup.py run` by hand. Common causes: the
drive is not mounted, or the password command fails. When it says **vecchio**, the last success is more than 48 hours
old: check `systemctl --user list-timers jarvisos-backup.timer`; the timer is `Persistent`, so a run missed while the
machine was off happens at the next start.

## Tests

See `AGENTS.md` for the exact commands (lint, focused tests, sharded suite, frontend build, browser walks).

## Windows-native launchers

`Start-JarvisOS.cmd`, `Start-JarvisOS-Backend.cmd` and `Start-JarvisOS-Frontend.cmd` call `scripts/start-backend.ps1` and
`scripts/start-frontend.ps1` for a Windows-native checkout (backend on port 8000, frontend on port 5173, default data
root `C:\JarvisOS`). They are kept working but are not the primary path and are not changed without Windows evidence.

## Expert consult (zone A)

Configuring and operating the trusted cloud consult path (spec 380):

1. **Vault configuration (`consult.toml`)**:
   In `JARVISOS_VAULT_PATH`, create or edit `consult.toml` alongside `vault.toml`:
   ```toml
   consult_mode = "ask"  # "off" | "ask" | "auto"
   brief_sha256 = "c2a84..."  # sha256 digest of the pack's approved brief file

   [[trusted_accounts]]
   id = "anthropic-claude"
   agent = "claude"
   model = "claude-opus-5-5"
   training_off_verified_on = "2026-11-25"  # ISO date; required for zone A
   ```

2. **Computing `brief_sha256`**:
   The brief is loaded from the pack specified by `[consult]` in `jarvis_pack.toml`. Compute its SHA256 digest:
   ```bash
   sha256sum <path-to-pack>/brief.md
   ```
   Paste this 64-character hex digest as `brief_sha256` in `consult.toml`. Any modification to the brief invalidates
   the approval and stops consults until the maintainer re-computes and approves the new digest.

3. **Trusted account verification**:
   Record `training_off_verified_on` with the date the account was verified to have model training turned off. An entry
   without a valid date is considered zone C and is never used for expert consults.

4. **Consult mode (`consult_mode`)**:
   - `off`: Consults disabled. Inkling answers with local tools only.
   - `ask`: Every consult packet requires maintainer approval in the chat UI before dispatch.
   - `auto`: Consult packets passing the deterministic gate are dispatched immediately; 1 in 5 attempts is flagged for audit.

Settings > AI shows the "Consulente" card with the current status, brief approval status, trusted accounts, and recent
audit records.

