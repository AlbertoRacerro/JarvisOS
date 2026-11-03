# Architecture

The shape of JarvisOS and the authority boundaries that must keep holding. Which module does what, with status and
entrypoints, is in the generated `MODULES.md`; spec state is in `specs/STATUS.md`. Where this file and the code disagree,
the code wins and this file is fixed.

## 1. Principle

JarvisOS is a local-first, human-operated engineering workstation: design, simulate, dimension, optimize and validate
process plants and their equipment, with AI assistants working under the same domain and command authority as the
engineer. The first use case is a microalgae photobioreactor. JarvisOS is not a general numerical solver, a model
provider, or an autonomous engineering authority.

```text
Engineer           = final engineering responsibility
JarvisOS           = authority boundary: state, policy, provenance, execution identity
AI / agents        = proposals, reasoning, tool intents, explanations
Engineering tools  = replaceable numerical and mechanical backends
Evidence           = results + provenance + fidelity and qualification context
```

1. An AI or agent proposal is not a canonical state change; a tool output is not accepted engineering truth.
2. Capability authority, information-flow (egress) authority and state-commit authority are separate concerns.
3. Canonical state changes only through JarvisOS-controlled transitions.
4. Sunk cost is zero: existing code gets no preference for existing. When a qualified upstream project solves a generic
   problem better, wrap it, replace with it, or delete the in-house version.

## 2. Authority boundary

Authority is distributed across backend modules rather than held by one class. JarvisOS owns canonical project and
engineering state, record lifecycle and promotion, workspace identity, units and provenance, AI route/budget/egress
policy, capability permissions, run/artifact/evidence identity, sensitivity and external-packet lineage, and the audit
trail. Replaceable mechanics belong to external systems: LLM inference, agent loops, retrieval, sandbox implementation,
property packages, solvers, CAD kernels, meshers, CFD and FEM engines. None of them owns canonical state, and an agent's
answer returns to JarvisOS as a response, proposal or tool intent; there is no path from an agent to canonical state that
skips a JarvisOS transition.

```text
Engineer <-> JarvisOS authority boundary <-> AI / agent runtime
                  | authorized execution request
                  v
        engineering / software capability
                  | result + artifacts + diagnostics
                  v
        evidence / run records -> inspection, explicit promotion
```

## 3. AI execution

All provider execution goes through `run_ai_task` and the JarvisOS routing, egress, budget and ledger boundaries
(`backend/app/modules/ai`). `route_class="auto"` is local-only; an external call needs the accepted server-side path, a
bounded context packet, sensitivity and sanitization checks, and a recorded disclosure. Credentials never reach the
frontend. Local models (`local_ai`) are advisory classifiers and responders. Hermes (`backend/app/modules/agents/hermes`) runs as an isolated
local agent responder; its output stays advisory until an owning product action accepts a proposal. `relay_gateway` runs
sandboxed coding agents under the same egress rules. `AGENTS.md` lists the invariants.

## 4. Context, retrieval and memory

Canonical state and retrieval memory are different systems. The context broker assembles bounded, digest-stamped context
packs from canonical records, runs and evidence; retrieval indexes are rebuildable and non-authoritative, and canonical
state wins any disagreement. The `memory` module holds the proposal and promotion lifecycle for engineering records.
`project_knowledge` and `flowsheet` (a record dependency, provenance and freshness graph, not a process flowsheet) are
live and marked for redesign in `MODULES.md`.

## 5. Known write-path debt

Records enter through two paths: the proposal/promotion lifecycle in `memory`, and direct CRUD in `modeling` (assumptions,
parameters, requirements). The target is one transition service with one set of semantics, not forcing every human entry
through an artificial AI-proposal state. Parameters also mix record lifecycle (`proposed`, `accepted`, `rejected`,
`superseded`) with value quality (candidate, literature, measured, validated); keep the two distinct, and require both an
accepted lifecycle state and an explicitly permitted quality state before a parameter becomes authoritative AI context.

## 6. Engineering and evidence

- **Evaluators and evidence**: `engineering` defines the shared contracts (evaluator contract, evidence and qualification
  records, multi-fidelity selection, deterministic inner-loop studies). A common abstraction must not erase solver-specific
  semantics or pretend incompatible analyses are interchangeable.
- **Process**: `process_stack` wraps selected upstream owners (CoolProp, fluids/ht, SUNDIALS, DWSIM) behind the evaluator
  boundary and hosts process drafts and the dynamic engine; `photobioreactor` holds the generic, literature-grade culture,
  light, gas-exchange and shear models; `process_kernel` is the shared unit registry. Add no new generic solver
  functionality in-house when an upstream owner exists.
- **Physical design** (`bluecad`): typed `GeometrySpec` to build123d/OCP, then STEP/STL/GLB with manifests; registry-bound
  Gmsh meshing; registry-bound CalculiX static FEM (in development); OpenFOAM pipe cases; results stored as runs,
  artifacts and typed evidence. `GeometrySpec` is a bounded vocabulary, not a general CAD replacement. A bounded AI repair
  loop may propose a new spec after a failed check; the proposal owns no project truth.
- **Studio** (`studio`, `studio_runtime`): engineer-authored models with Python source, units and equations side by side;
  user code runs in an isolated worker, not in the API process.
- **Evidence**: provenance is necessary but not sufficient. Executing without error is not validity; a result needs its
  fidelity, validity domain, qualification state and exclusions before it is used in a decision.

## 7. Human-operated target

The engineer can perform every important visual engineering operation personally; the assistant (Bubu, plus the Jarvis
chat surface) uses the same governed command surfaces, never a hidden mutation path. Interaction mode may differ,
engineering authority may not. Routine calculation construction is agent-first and human-verifiable: the AI proposes, a
deterministic engine executes, validators check, a governed transition records, and the engineer sees the same structure
that ran (inputs with units and sources, assumptions, ordered equations, results, validity). Editing creates a new
revision, preserves the old one and invalidates dependent results. Whole upstream engines are wrapped before generic
replacements are built; JarvisOS owns the project-specific coupling, qualification, revisions and operator experience.

## 8. Storage, execution and platform

The FastAPI backend is the durable boundary; state is SQLite under the data root (`backend/app/core/paths.py`), events are audit
records and the system is not event-sourced. The frontend only calls backend APIs: no providers, local model runtimes,
filesystem paths or executables. The bounded Python runner suits reviewed deterministic scripts; it is not a hostile-code
sandbox, and untrusted code needs the isolated Studio runtime or a fresh isolation design (`runner-trust-boundary.md`).
Linux (WSL2 on the maintainer's machine) is the primary platform; the Windows `.cmd` launchers are secondary.

## 9. Project packs

The core is project-neutral. Everything specific to one project lives in an optional pack: a directory, normally its own
repository, found through `JARVISOS_PACK_PATH` (directories separated by `os.pathsep`) and described by a
`jarvis_pack.toml` (`backend/app/core/packs.py`). A pack declares:

- the default workspace (`[workspace] slug, name`), used for the first-boot seed and wherever a request names no workspace;
- extra sensitivity terms (`[sensitivity] terms`), which the egress guard treats as confidential and, next to an IP noun, as S3;
- free-form UI labels (`[ui_labels]`), published by `POST /system/initialize` as `packs[].ui_labels`;
- a directory of Studio example models (`[studio] examples_dir`).

Without a pack the core uses the workspace `main`, no extra terms and no examples. An invalid pack stops startup with a
`PackError`. The seed creates the default workspace only on an empty database and never adds a second one to existing
data. The core must not name any particular project; generic literature science (such as the tubular photobioreactor
kinetics in `photobioreactor`) stays in the core under a neutral name.
