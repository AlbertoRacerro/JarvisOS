# 248 — From a numeric value to an admitted design value (option 4 + Q3 + Q4)

Status: draft
Depends on: none

## Goal

There is one deterministic path that every engineering number follows before a design decision can use it:
**source → value proposal → deterministic admission → design value in Project Basis → requirement check → reconciled
snapshot**. Simulation results (option 4 of `L20261105130109-901c`), plant-model KPIs (Q3), literature data and property
libraries (Q4) all enter through this path. It is built once, and each source only adds an extractor.

## Why

Maintainer directive `L20261105222120-78af` item 5 asks for one short contract covering 4+Q3+Q4, so that the path from a
number to a design value is not built twice. Most of the path already exists. Scalar admission
(`POST /project-knowledge/scalars`) re-extracts a value from a run's `output_payload` and refuses what it cannot prove.
Requirement validation (`POST /project-knowledge/validation`) returns pass, fail or not evaluable. Working revisions,
approval and Final reconcile own Project Basis. Literature `datum` entries (`backend/app/modules/memory`) already hold a
number, a unit and a source, but they cannot reach admission today.

## The contract (binding for every slice below)

1. **One proposal shape.** A value proposal names `source_kind` (`simulation_run`, `plant_model_kpi`, `literature_datum`
   or `property_library`), a source reference with a content digest, the value and its unit, its conditions (for
   example T, P, composition and the validity range the source states), and `extractor_id` with `extractor_version`.
   Models and humans may propose, but a proposal is never a design value (Never 7).
2. **Admission is deterministic and per source.** Each source kind has one extractor. The extractor re-derives the
   value from the digested source and refuses anything it cannot prove:
   - run: the existing payload re-extraction;
   - plant-model KPI: a solved flowsheet result, which is a run;
   - literature: the stored datum, plus its page or table locator, and only when the datum was confirmed by a person;
   - property library: a recomputation with the pinned library version at the stated conditions.
   Admission stores the value, unit, conditions and provenance. It never edits the source.
3. **Design values live in Project Basis.** An admitted value becomes a `parameter` value, or evidence for a
   `requirement`, only through the existing working-revision draft, impact preview and approval. Nothing writes to
   Project Basis outside that flow.
4. **Conditions are checked, not trusted.** When a value is used outside the conditions it was admitted for, the check
   returns `not_evaluable` with the reason. This is the same validity-domain judgment the Q1 benchmark measures, done
   here deterministically.
5. **Freshness is the existing lineage graph.** Every admitted value is a lineage node. A new run, a corrected datum or a
   library version change marks the dependent design values and checks stale, through the flowsheet lineage and
   freshness graph and not through a new store. No Consist, no AiiDA.
6. **Missing inputs are structural.** For a plant model, a deterministic degrees-of-freedom and missing-inputs report
   lists the unset specifications. Each entry either maps to a Project Basis parameter that has an admitted value or is
   reported as missing. The model never decides what is missing.
7. **No egress in this contract.** Docling, PaperQA2 and the property libraries run locally. Monitoring new papers
   sends search queries, which is egress, so it waits for Q5 (destination profiles and the IP map) and is not part of
   these slices.

## Slices (each one a small spec, `ready` only after this contract is approved)

| # | Slice | Source of the outputs | Risk |
|---|---|---|---|
| A | Verify requirements against simulation results in Project Basis: admit a run output, check a requirement, open the reconciled snapshot. Adds the read-only `applicability_set_digest` to `RevalidationRead` (the earlier draft of this spec). | option 4 | medium |
| B | Generalise admission to the proposal shape above. Add `conditions` and `source_kind`, keep the run extractor as is, and add the conditions check (contract point 4). Additive schema. | contract | high (migration) |
| C | Degrees-of-freedom and missing-inputs report for a plant model on `process_stack`, read-only, mapped to Project Basis parameters. | Q3 | medium |
| D | Literature data to admission: a confirmed `datum` with its locator becomes a proposal; the Materials page shows admitted values and their sources. | Q4 | medium |
| E | Property library extractor: `thermo`/`chemicals`, pinned, recomputed at the stated conditions. | Q4 | medium |
| F | Docling extraction of a local PDF into literature `datum` proposals, run once per paper and confirmed by a person. | Q4 | medium |
| G | PaperQA2 over the local library through the local model. It returns cited answers only, never values; a value it finds goes through F/D. | Q4 | medium |
| — | Read-only pilots, with a recorded decision and no product change: an IDAES/Pyomo PBR unit against the current engine; Apache Hamilton as glue for evaluators outside the flowsheet. | Q3 | low |

Order: A, then B, then C/D/E in any order, then F and G. Q1 (items 3 and 4 of the directive) runs before all of these.

## Out of scope (binding non-goals)

- Any model-proposed value or outcome being promoted without deterministic admission and approval.
- Changes to the semantics of validation, revalidation or reconcile beyond the conditions check.
- New egress, paper monitoring, cloud consultation (Q5).
- A new provenance or workflow framework.

## Acceptance of this contract

The maintainer approves the path (points 1–7) and the slice list. Each slice then gets its own spec with acceptance
criteria, tests and, for UI, a browser walk. Draft under maintainer directive `L20261105222120-78af` item 5. It
becomes `ready` only after the maintainer approves it.
