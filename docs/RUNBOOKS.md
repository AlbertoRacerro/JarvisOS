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

## Tests

See `AGENTS.md` for the exact commands (lint, focused tests, sharded suite, frontend build, browser walks).

## Windows-native launchers

`Start-JarvisOS.cmd`, `Start-JarvisOS-Backend.cmd` and `Start-JarvisOS-Frontend.cmd` call `scripts/start-backend.ps1` and
`scripts/start-frontend.ps1` for a Windows-native checkout (backend on port 8000, frontend on port 5173, default data
root `C:\JarvisOS`). They are kept working but are not the primary path and are not changed without Windows evidence.
