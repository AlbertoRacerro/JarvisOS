# 248 — From a numeric value to a usable design value (option 4 + Q3 + Q4)

Status: contract approved with changes by the maintainer (`L20261106093156-1712`); each slice still needs its own spec
Depends on: none

## Goal

There is one deterministic path that every engineering number follows before a calculation or a requirement check can
use it: **source → automatic ingestion → one-time automatic validation → indexed value (usable, or flagged with a red
dot) → requirement check and pre-run coherence check → automatic history**. Simulation results (option 4 of
`L20261105130109-901c`), plant-model KPIs (Q3), literature data and property libraries (Q4) all enter through this path.
It is built once, and each source only adds an extractor. The maintainer acts only on exceptions, never per value.

## Why

Maintainer directive `L20261105222120-78af` item 5 asks for one short contract covering 4+Q3+Q4, so that the path from a
number to a design value is not built twice. The maintainer approved it with changes in `L20261106093156-1712`: no
per-value approval, automatic ingestion with approval by exception, automatic git-like history instead of a snapshot
step, and a pre-run coherence check. Most of the path already exists. Scalar admission (`POST /project-knowledge/scalars`)
re-extracts a value from a run's `output_payload` and refuses what it cannot prove. Requirement validation
(`POST /project-knowledge/validation`) returns pass, fail or not evaluable. Literature `datum` entries
(`backend/app/modules/memory`) already hold a number, a unit and a source, but they cannot reach admission today.

## The contract (binding for every slice below)

1. **Automatic ingestion.** Ingestion starts without a maintainer action when a simulation finishes with validated
   results, when new literature is loaded into Jarvis, or (slice H, later) when Jarvis uses an online source in a
   calculation. Each source kind has one deterministic extractor; models may propose a value, but only the deterministic
   validation below promotes it (Never 7).
2. **Every value carries its provenance.** Source (paper with locator, or simulation run) with a content digest, value,
   unit, validity conditions (for example T, P, composition and the range the source states), and `extractor_id` with
   `extractor_version`. Ingestion never edits the source.
3. **One-time automatic validation at ingestion**, before the value is indexed and placed where it belongs. It checks,
   deterministically: the value is really in the source (re-derived from the digested run payload, the located text or
   table of the paper, or a recomputation with the pinned property library); the unit is consistent with the quantity;
   the value is plausible for its range; and it does not duplicate or contradict an existing value for the same quantity
   and conditions. It runs once per value and version, not again on every use.
4. **A passing value is usable at once.** It enters calculations and requirement checks with no maintainer click.
5. **A failing value is still placed, with a red dot.** It is indexed where it belongs and marked with the failed check.
   - One notification per ingestion batch, never per value: one Jarvis notification and one Slack line, with a link that
     opens the exact place of each flagged value.
   - A calculation or requirement check that uses a flagged value marks its result as based on an unvalidated value.
   - Resolving is one action per exception: correct the value or confirm it. Values that passed are never asked about.
6. **History is automatic and git-like, per project.** Every version of values, runs and calculations is recorded when it
   is created, so any run shows which value versions it used and two versions can be compared. It reuses the existing
   lineage, freshness and run records; there is no snapshot step and no separate snapshot UI. A new version marks the
   dependent checks stale through the existing freshness graph. No Consist, no AiiDA.
7. **Requirement verification stays.** A requirement check returns passes, fails or not evaluable, with the reason. A
   value used outside the conditions it was ingested with gives `not_evaluable`.
8. **Pre-run coherence check.** Before a run, a deterministic check verifies that the models, correlations and
   approximations used together are mutually consistent and inside their validity domains, and flags the inconsistent
   ones before the run starts. This is the validity-domain judgment the Q1 benchmark measures, done deterministically.
9. **Missing inputs are structural.** For a plant model, a deterministic degrees-of-freedom and missing-inputs report
   lists the unset specifications and maps each to a value on this path or reports it as missing. The model never
   decides what is missing.
10. **Design decisions are not overwritten.** Ingestion makes values usable; it does not change a Project Basis parameter
    the maintainer set. A newer value that disagrees with one is a contradiction exception (point 5).
11. **Zero cost and egress.** Docling, PaperQA2 and the property libraries run locally. Slice H downloads only
    open-access or already-accessible sources into the local library, and its search queries use generic terms only, under
    the existing egress rules; it waits for Q5 (destination profiles and the IP map).

## Slices (each one a small spec, `ready` only after its own review)

| # | Slice | Source | Risk |
|---|---|---|---|
| A | Simulation results feed requirement verification: a run finishing with validated results is ingested and validated automatically (point 3), flagged values get the red dot and one batch notification, and requirement checks use the values. Adds the read-only `applicability_set_digest` to `RevalidationRead`. | option 4 | medium |
| B | Automatic history (point 6) over the existing lineage, freshness and run records: which value versions a run used, compare two versions. | contract | high (migration) |
| C | Pre-run coherence check (point 8) and the degrees-of-freedom and missing-inputs report for a plant model on `process_stack` (point 9). | Q3 | medium |
| D | Literature data on the same path: a loaded paper's `datum` with its locator is ingested and validated; the Materials page shows values, sources and red dots. | Q4 | medium |
| E | Property library extractor: `thermo`/`chemicals`, pinned, recomputed at the stated conditions. | Q4 | medium |
| F | Docling extraction of a local PDF into literature values, run once per paper, then validated by point 3. | Q4 | medium |
| G | PaperQA2 over the local library through the local model. It returns cited answers only, never values; a value it finds goes through F/D. | Q4 | medium |
| H | Online source used in a calculation is downloaded into the local library (zero cost, generic queries) and ingested. Waits for Q5. | Q4 + Q5 | high (egress) |
| — | Read-only pilots, with a recorded decision and no product change: an IDAES/Pyomo PBR unit against the current engine; Apache Hamilton as glue for evaluators outside the flowsheet. | Q3 | low |

Order: Q1 execution first (items 3 and 4 of `L20261105222120-78af`), then A, then B and C, then literature and materials
(D, E, F, G), then H.

## Out of scope (binding non-goals)

- Per-value approval prompts, or any prompt for a value that passed validation.
- A maintainer-facing snapshot step or snapshot UI.
- Any model-proposed value promoted without the deterministic validation of point 3.
- Changes to the semantics of validation, revalidation or reconcile beyond points 7 and 8.
- Paid sources, paper monitoring, cloud consultation, and any egress outside slice H.
- A new provenance or workflow framework.

## Acceptance of this contract

The maintainer approved the path with the changes now in points 1–11 (`L20261106093156-1712`). Each slice gets its own
spec with acceptance criteria, tests and, for UI, a browser walk, and becomes `ready` after its own review.
