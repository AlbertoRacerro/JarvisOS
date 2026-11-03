# docs/

The code and its tests say what the system does; `specs/STATUS.md` says what is open; `../AGENTS.md` and `PROCESS.md`
say how to work. When a document disagrees with the code, the code wins and the document is fixed.

| File or directory | What it is |
| --- | --- |
| `../AGENTS.md`, `PROCESS.md` | Commands, conventions, "never" rules, evidence by risk; roles, specs, PRs, supervisor |
| `MODULES.md` | Generated module table: status, purpose, entrypoints, size (`scripts/gen_modules.py`) |
| `ARCHITECTURE.md`, `DECISIONS.md` | Authority boundaries and system shape; durable architecture decisions |
| `specs/` | Open specs and their registry (`specs/STATUS.md`; template `specs/SPEC_TEMPLATE.md`) |
| `RUNBOOKS.md`, `runbooks/` | Install, run, health checks; task runbooks |
| `MIGRATIONS.md`, `DATA_ROOT_RECOVERY.md` | Schema rules; data-root snapshot and restore |
| `runner-trust-boundary.md`, `security/` | Execution and sensitivity/egress design contracts |
| `FUTURE_IMPROVEMENTS.md` | PARK list: ideas with no authority to implement |
| `implementation/` | Build notes and the supervisor operations runbook |
| `design/`, `design-references/`, `agent-skills/` | Visual identity, approved UI references, frontend agent skills |
| `strategy/`, `relay-context/` | Dated roadmap context (advisory); cloud-safe context for sandboxed agents |

Anything with a date in its name is evidence about that day, not about today.
