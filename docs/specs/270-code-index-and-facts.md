# 270 — Code index and checked code facts

Status: ready
Depends on: —

## Goal

An agent that is about to change code can print a program-generated index of a file or directory (every class and
function with its line and the first line of its docstring) instead of sampling the source, and every new spec states
the code facts it relies on in a form that CI checks still holds while the spec is `ready`. A wrong assumption about
the code becomes one failing line in CI, not something found during review or after merge.

## Why

Supervisor item S8 of the maintainer's current directive order (S3, F1, S6, S7, S10, S8, S9), points 3 and 4 of
"How an agent can know the code it changes" in the plan behind directive `L20261113011123-36ed`. Several recent specs
already carry a "Code facts" section (262, 263, 266, 267, 268, 269), but in free prose that no program reads; facts
about a 250 KB file are checked only when a reviewer happens to reopen it. `docs/MODULES.md` shows that a generated,
CI-checked map works one level up.

## Code facts checked on `origin/master` at `2c0eefb2`

- `scripts/gen_modules.py::render` builds `docs/MODULES.md` from `module.toml` files with the standard library only;
  `scripts/gen_modules.py::main` supports `--check`, which CI runs (`.github/workflows/ci.yml`, job step running
  `python scripts/gen_modules.py`).
- `scripts/check_doc_paths.py::check` verifies only that backticked paths in five canonical documents exist; it does not
  read spec files and does not resolve symbols.
- `scripts/check_spec_status.py::parse_registry` returns the registry rows keyed by spec id with their `status`;
  `scripts/check_spec_status.py::main` is the CI entry (`--self-test`, `--event`, plain run).
- `.github/workflows/ci.yml` runs `python scripts/check_doc_paths.py` as the only step of the `doc-paths` job (Python
  3.11, no dependencies installed), and its backend `Ruff` step lints the gate scripts by explicit path
  (`../scripts/check_spec_status.py`, `../scripts/check_doc_paths.py`, `../scripts/gen_modules.py`, ...).
- `docs/specs/SPEC_TEMPLATE.md` has no code-facts section.

## Scope

In scope:

1. **Index command.** New `scripts/code_index.py <path>...` prints, for each Python file under the given files or
   directories (sorted, skipping the same build/cache directories as `gen_modules.py`), one line per module-level class
   and function and per method (`Class.method`), with its line number and the first line of its docstring, using `ast`
   only. Output is plain text, deterministic and small (one line per symbol). A file that does not parse is reported on
   stderr and the exit code is 1, after the other files are printed. It is an on-demand reader, not a committed file:
   committing an index with line numbers would go stale on every code change and fail CI on unrelated PRs.
2. **Checked code facts.** New `scripts/check_code_facts.py` reads `docs/specs/STATUS.md` through
   `check_spec_status.parse_registry` and, for every row with status `ready` whose spec number is 270 or higher, opens
   its spec file (the link in the row) and requires a section whose heading starts with `## Code facts` containing at
   least one fact reference. A fact reference is a backticked span `path::Symbol` or `path::Class.method`:
   - `path` must exist relative to the repository root;
   - for a `.py` file, every dotted part must resolve through `ast` to a class, function, async function or a
     module/class-level assignment target of that name;
   - for any other file, the last part must occur in the file as a whole word.
   Errors name the spec file, line and reference. Specs numbered below 270 and rows in other statuses are not checked
   (their prose facts were reviewed by hand; an implementation PR legitimately changes the facts of its own spec when
   the row moves to `in_review`).
3. **Wiring.** CI runs `python scripts/check_code_facts.py` as a second step of the existing `doc-paths` job (no new job,
   no change to required check names) and lints both new scripts in the existing `Ruff` step. `docs/specs/SPEC_TEMPLATE.md` gains a `## Code facts` section explaining the reference form.
   `docs/PROCESS.md` (spec readiness review) says the reviewer checks the code facts first, and `AGENTS.md` "Where to
   look" gains one row for `scripts/code_index.py`. `scripts/module.toml` purpose mentions the two tools;
   `docs/MODULES.md` is regenerated.
4. This spec's own code facts section above uses the checked form and must pass the new check.

Out of scope (binding non-goals):

- No committed index files, no index of TypeScript or other languages, no symbol search server, no new dependency.
- No change to `check_spec_status.py` behaviour or to the registry rules; no rewriting of the prose facts of specs
  below 270.
- No line-number checks in facts (line numbers move; symbols are the stable reference).
- No change to the recovery packet (`devctx.py`).

## Files likely touched

- `scripts/code_index.py` (new), `scripts/check_code_facts.py` (new)
- `backend/tests/test_code_index.py` (new), `backend/tests/test_check_code_facts.py` (new)
- `.github/workflows/ci.yml`, `docs/specs/SPEC_TEMPLATE.md`, `docs/PROCESS.md`, `AGENTS.md`, `scripts/module.toml`,
  `docs/MODULES.md` (generated)

## Design constraints

- Standard library only; both scripts importable with a `main(argv) -> int` and a `ROOT` that tests can override (as
  `check_doc_paths.check(root=...)` does).
- `AGENTS.md` stays within the 120-line limit enforced by `check_doc_paths.py`.
- Mypy ratchet: new code adds no mypy errors.
- Deterministic output: same tree, same bytes.

## Acceptance criteria

1. `python scripts/code_index.py scripts/check_doc_paths.py` prints a line for `candidate`, `resolves`, `tokens`,
   `check` and `main` with their correct line numbers, in source order, and exits 0.
2. Methods appear as `Class.method`; a symbol without a docstring prints without a summary; a syntax-error file is
   reported on stderr, the remaining files are still printed and the exit code is 1.
3. `python scripts/check_code_facts.py` exits 0 on the branch, and the run checks spec 270's references.
4. In a temporary tree, a `ready` spec ≥ 270 with a missing file, a missing symbol, a missing method, no `## Code
   facts` section, or a section with no reference each fails with an error naming the spec and reference; a `planned`
   or below-270 spec with the same defects passes.
5. CI runs the new check on every PR and fails on its errors.

## Required tests

- `backend/tests/test_code_index.py`: index of a temporary package (function, class, method, async function, no
  docstring, syntax error, skipped `__pycache__`).
- `backend/tests/test_check_code_facts.py`: temporary registry and specs covering acceptance criterion 4, plus a
  non-Python file reference found and not found.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, one severe review of the diff
(medium risk: CI gate and tooling, no runtime behaviour), registry row updated by the implementation PR.
