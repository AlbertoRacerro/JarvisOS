# PR checklist

**Spec gate:** implementation NNN | definition NNN | N/A

- `implementation NNN`: `docs/specs/STATUS.md` shows `in_review`, this PR number and only merged hard dependencies.
- `definition NNN`: the registry row exists as `planned`, `blocked` or `ready`; its PR column stays `—`.
- `N/A`: the PR neither implements nor defines a numbered spec.

## What changed

<summary per file group>

## Deviations from the spec

<"None", or every simplification, placeholder, partial implementation or unmet criterion>

## Evidence

Risk: low | medium | high (see `AGENTS.md`, "Evidence by risk").

- [ ] Lint and the tests that cover the change pass; frontend build if the frontend changed
- [ ] UI change: an AI browser walk from `e2e/` passed on the changed surfaces (evidence attached)
- [ ] Nothing from `AGENTS.md` "Never" is weakened; no secrets, private names or personal paths added

<paste the relevant output tail or link the CI run>

## Residual risk

<anything the reviewer should look at closely>
