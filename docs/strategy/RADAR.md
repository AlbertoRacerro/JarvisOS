# Outside technology radar

This public intake aid records candidate families, not product commitments. The code and tests establish current
capabilities; `docs/specs/STATUS.md` is the sole registry of spec state and implementation authority. The radar never
authorizes implementation. Concrete out-of-scope repository observations belong in `docs/FUTURE_IMPROVEMENTS.md`;
link to an existing PARK item when relevant instead of copying or maintaining it here.

## Intake

For each concrete candidate, keep one card with exactly these six fields:

1. **What:** name, version and primary link; check and record the licence and its applicability before a trial or use.
2. **Replaceable slot:** the existing capability or boundary it would occupy.
3. **Change:** what it replaces or which hop it adds.
4. **Gap:** measured gap against the current capability, or `not measured`.
5. **Decision battery:** the relevant repeatable battery and a predeclared win threshold beyond three-run noise.
6. **Total cost:** acquisition, operation, integration, maintenance and exit costs; no added cost is permitted.

Follow the card with exactly one outcome: **TRIAL**, **BORROW**, **WATCH**, or **DROP**. A vendor claim can justify a
trial, never adoption. Without a battery, choose WATCH and build the battery first. A new component needs a measured gap
and a win beyond three-run noise. A cheap isolated trial may measure the gap only when an existing battery is available;
it must still meet the no-added-cost rule. BORROW means consider a specific technique or idea under the same evidence and
licence checks; it does not authorize copying code or implementation. WATCH must name a reopening event and follow the
monthly review in the watchlist section.

Downloads and installations on the user's machine require prior user authorization. GPL/AGPL tools remain separate,
user-installed programs. Run experimental code in isolation on public or synthetic data. Learned physics models enter
through the same checked calculation-engine door as other engines. Preserve the existing source of truth for each fact;
a candidate must not create a second one.

## Slots and batteries

Battery availability must be checked against current evidence before a decision; a slot in this table does not assert
that a suitable battery exists or that any candidate has passed one.

| Slot | Incumbent / current approach (public code) | Deciding battery, if available |
| --- | --- | --- |
| Scientific reasoning | Local runtimes (llama.cpp, Ollama) and configured providers, all through `run_ai_task` | Representative reasoning tasks with checked answers and failure cases |
| Tool use | Hermes governed broker and MCP bridge; the `tools` module is an empty scaffold | Tool selection, arguments, recovery and boundary cases |
| Research/office | Not qualified | Source-grounded research and document tasks |
| Search/memory | SQLite FTS5 lexical search plus a rebuildable embedding index (`retrieval_index`) | Retrieval relevance, provenance and recall on representative queries |
| Calculation engines | Pint units (`process_kernel`); `bluecad` solid CAD (build123d), Gmsh meshing, OpenFOAM pipe CFD and CalculiX static FEM (`fem_*`, in development); `process_stack` steady-state and dynamic engines (CoolProp, fluids/ht, SUNDIALS, DWSIM); runner-hosted model code (`runner`) | Checked calculations, units, numerical accuracy and failure cases |
| Inference/structured output | llama.cpp and Ollama local runtimes; structured-output method not qualified | Task quality, valid output, latency and resource use |
| Machines | Local bounded runner (`runner`); remote execution not qualified | End-to-end workload, transfer and operational cost |
| Coordinator/workers | Hermes isolated local responder and `relay_gateway` sandboxed coding agents | Task completion, coordination overhead and boundary adherence |

## Initial watchlist

These are leads for concrete intake cards, not evaluated products. Licences are unverified until a specific version and
its terms are checked. Gaps and total costs are not measured; battery availability must be verified. Concrete candidate
cards supply a checked primary link and licence.

Monthly review, on the first working day of each month, for every WATCH line:
- If its reopening event has occurred, write an intake card and change its outcome by the intake rules above.
- Otherwise, if three months have passed since its Added date without the event, change its outcome to DROP.
- Remove DROP lines at the next review. For the initial lines, the three-month limit is 2027-02-16.

| Family (starting point) | Slot; potential change | Outcome | Added | Reopening event |
| --- | --- | --- | --- | --- |
| Open models | Scientific reasoning; replace a model | WATCH | 2026-11-16 | A candidate and suitable battery are identified |
| Tool models | Tool use; replace a model | WATCH | 2026-11-16 | A candidate and suitable tool battery are identified |
| Multilingual retrieval | Search/memory; replace retrieval | WATCH | 2026-11-16 | A multilingual gap and battery become measurable |
| Semantic layers | Research/office; add a semantic hop | WATCH | 2026-11-16 | A concrete question requires it and a battery exists |
| Long-context memory | Search/memory; add a context hop | WATCH | 2026-11-16 | A representative long-context battery exists |
| Constrained decoding | Inference/structured output; replace decoding | WATCH | 2026-11-16 | A structured-output gap and battery become measurable |
| Equation discovery | Calculation engines; add a discovery hop | WATCH | 2026-11-16 | A checked equation-discovery battery exists |
| Neural solvers | Calculation engines; replace a solver | WATCH | 2026-11-16 | A checked solver battery and candidate exist |
| Materials foundation models | Calculation engines; add a prediction hop | WATCH | 2026-11-16 | A checked materials battery and candidate exist |
| Second-machine uses | Machines; add a remote execution hop | WATCH | 2026-11-16 | An authorized machine and representative workload are available |
| Host scheduler | Machines; replace or add scheduling | WATCH | 2026-11-16 | A measurable scheduling gap and battery exist |
| Spiking/video models | Scientific reasoning; add a perception hop | WATCH | 2026-11-16 | A representative perception battery and candidate exist |
