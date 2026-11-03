# Relay safe context

Stable, cloud-safe context for Relay coding agents launched by Jarvis (spec 157). Jarvis copies this directory read-only to `/workspace/context/stable/` for every run. It sits beside `derivatives/` (operator-approved sanitized facts) and `MANIFEST.json` (what was released, under which classification, and why).

Put only material that may leave the machine here: operating rules, conventions, architecture notes, tool contracts and synthetic fixtures. Strategic or domain IP never belongs here. Neither do real process data, private simulation results, customer data or credentials. Such material reaches an agent only as an approved derivative released through Jarvis. Revoking the release removes it from the next run.

## Environment an agent sees

- Its workspace: a persistent clone of the JarvisOS repository when the repository is classified cloud-safe (S0/S1), or a derivative workspace without source otherwise.
- This context, read-only.
- Network: only the provider, GitHub and package hosts Jarvis allows, through a proxy. Local services are unreachable.
- No Jarvis database, memory, data root, credentials or operator home. They are not mounted, so they cannot be found.
- No publishing: commit locally on the current branch; Jarvis or the operator publishes.

## Working rules

- Follow `AGENTS.md` in the repository when it is present.
- Run backend tests with `backend/.venv/bin/python -m pytest` and the frontend build with `npm run build` in `frontend/`. Dependencies are mounted read-only.
- For work that needs real private data, use or add synthetic fixtures with the same schema. Jarvis tests locally against the real data.
- If a task needs information you cannot see, say what is missing. Jarvis decides whether to release a derivative.
