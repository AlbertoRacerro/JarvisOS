# 249 — Semantic-judgment benchmark cases for local model qualification

Status: ready
Depends on: none

## Goal

A fixed, versioned set of engineering judgment cases with known answers and a deterministic grader exists under
`eval/local/semantic_judgment/`. Each case gives a problem and correct solver outputs and asks the model one closed
question. The grader scores a reply without any model and reports results per case and per category. The prompts can be
exported for a later host-run evaluation.

## Why

Maintainer directive `L20261105222120-78af` item 2 (Q1 preparation). It points back to `L20261105221512-23a8` Q1:
qualify local models on the judgment that is left once Jarvis's calculations are deterministic. A model must notice a
correlation used outside its validity domain, see when data are not enough to conclude, and recognise a contradiction
and say what could explain it. Grading must use known answers and deterministic checks, not another model's opinion.

## Scope

In scope:
1. **Case set** `eval/local/semantic_judgment/cases.json` (schema version 1), at least 30 cases:
   - `validity_domain` (at least 8): the solver outputs are given and arithmetically correct, but one hypothesis or
     correlation is used outside its stated validity range. Examples: Dittus–Boelter at Re < 10 000; Hagen–Poiseuille
     in turbulent flow; ideal gas near the critical point; lumped capacitance with Bi > 0.1; Henry's law at high solute
     fraction; Beer–Lambert in a dense, scattering culture; an Arrhenius fit extrapolated far outside its data range.
   - `insufficient_data` (at least 8): the right answer is "cannot conclude", and the reply must name the missing input
     or inputs.
   - `contradiction` (at least 8): the outputs or measurements contradict each other or a conservation law. The reply
     must name the contradiction and pick the plausible explanation.
   - `control` (at least 6): everything is valid and the answer is "no issue". These catch a model that flags
     everything.
   Every case carries: `id`, `category`, `title`, `context` (problem text and the given numbers with units), `question`,
   `options` (a closed list of 3–6 labelled answer choices for the issue, and for `insufficient_data` a closed list of
   candidate missing inputs), `answer` (the correct verdict, the correct option(s) and the required missing inputs), and
   `check`, a deterministic consistency check (see design constraints) proving the known answer from the given numbers.
2. **Prompt rendering and export** `eval/local/semantic_judgment/semantic_eval.py`:
   - `--emit-prompts <file>` writes one JSON list of `{name, prompt}` plus the grading inputs, with no network access;
   - `--dry-run` prints the prompts.
   Each prompt asks for a single JSON object `{"verdict": ..., "options": [...], "missing": [...]}` drawn only from the
   labels in the case.
3. **Deterministic grader** in the same module: `grade(case, reply_text) -> {"verdict_ok", "options_ok",
   "missing_ok", "score", "parse_error"}`. It extracts the first JSON object from the reply, which may follow thinking
   text or a fenced block. It never calls a model. A `--grade <replies.json>` mode reads `{name: reply_text}` and writes
   per-case and per-category totals.
4. A short `README.md` in that directory: the case schema, how to add a case, and how to export and grade.

Out of scope (binding non-goals):
- No change to `scripts/host_ops.py`, `local_eval.run`, Relay, the supervisor or any product code. Wiring the cases
  into a host-run evaluation, thinking on/off, token budgets and RAM/VRAM measurement are the Q1 execution slice.
- No model calls, no network, no new dependency (standard library only for the eval module; tests use pytest).
- No project-specific, private or pack content: generic textbook engineering only. This repository is public.
- No LLM-as-judge.

## Files likely touched

- `eval/local/semantic_judgment/cases.json`, `semantic_eval.py`, `README.md` (new)
- `eval/local/README.md` (one table row)
- `backend/tests/test_semantic_judgment_cases.py` (new)

## Design constraints

- **Known answers are computed, not asserted.** Each case's `check` names one of a small set of pure functions in
  `semantic_eval.py`, such as `reynolds`, `biot`, `reduced_temperature`, `mass_balance_residual`, `energy_balance_residual`,
  `optical_depth` or `arrhenius_extrapolation_ratio`. It also gives that function's inputs taken from the case numbers,
  a threshold and the expected outcome. Example: `reynolds(rho, v, D, mu) < 10000`, so Dittus–Boelter is out of range.
  The test recomputes every check. A case whose check does not support its known answer fails the test.
- Inputs inside `check` must match the numbers stated in `context`. The test checks that each check input value
  appears in the context text.
- Options are shuffled deterministically per case (a fixed seed from the case id), so the correct option is not always
  first. The rendered prompt is stable across runs.
- Scoring: a `validity_domain` or `contradiction` case scores 1 only if both the verdict and the option set are
  correct. An `insufficient_data` case scores 1 only if the verdict is `cannot_conclude` and the `missing` set contains
  every required item and no item marked `distractor`. A `control` case scores 1 only for verdict `no_issue` with no
  options. A reply that does not parse scores 0 with `parse_error: true`.
- Verdict vocabulary is fixed: `no_issue`, `invalid_assumption`, `cannot_conclude`, `contradiction`.

## Acceptance criteria

1. `cases.json` holds at least 30 cases meeting the per-category minimums. Ids are unique and every field is present.
2. For every case, its check recomputed from its inputs gives the expected outcome, and every check input value
   appears in the case context.
3. For every case, the grader scores the case's own known answer (rendered as a JSON reply) 1. It scores 0 for a
   wrong verdict, a wrong option, a missing required item, an included distractor, and an unparseable reply.
4. A reply with thinking text before the JSON object, or with the JSON in a fenced code block, is graded the same as
   the bare JSON.
5. `--emit-prompts` writes valid JSON with one entry per case. Every prompt contains the case's option labels and the
   reply format instruction. Two runs produce byte-identical output.
6. Lint passes, and the new test passes offline.

## Required tests

- `backend/tests/test_semantic_judgment_cases.py`: schema and minimums, check recomputation, context-number
  consistency, grader positive and negative cases per category, thinking-prefix and fenced-block parsing, and
  deterministic export.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, STATUS row updated.
