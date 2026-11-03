@AGENTS.md

## Claude Code

- Model identity is not authority. Roles are capability-based (`docs/PROCESS.md`); act only within what this session was granted.
- On resume or takeover run `python3 scripts/devctx.py recover` first. It narrows what to read and never outranks `AGENTS.md`, `docs/PROCESS.md` or `docs/specs/STATUS.md`.
- Work in a git worktree, never in the checkout the maintainer's app runs from.
- Before delegating implementation run `python3 scripts/worker_routing.py pick --difficulty D --risk R`.
- Open only the spec you are implementing; do not read `docs/specs/` or `docs/strategy/` wholesale.
