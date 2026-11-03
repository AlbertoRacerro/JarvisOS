# 231 — Bubu navigation legibility

State: **ready**, subject to the live registry in [STATUS.md](STATUS.md). This bounded contract implements the Bubu
quick wins in [the 2026-10-30 roadmap](../strategy/ROADMAP_COMSOL_DIRECTION_2026-10-30.md), §1.1–1.3. It does not
authorize later roadmap items. Ordinary implementation, review, proof, merge and activation may proceed autonomously
under the repository constitution; the maintainer's final usability test is an outcome check, not an intermediate
approval gate.

## Outcome and present evidence

An operator asking Bubu where to go receives a readable sentence and a useful first pointer. In the recorded C1 CFD
live test, Bubu named a path but pointed to C1-horizontal rather than C1-CFD; directive removal also left “Click first
on.” and omitted the name of “Back to <project name>”. Existing BUBU-3 provides page targets, a route-scoped app map and an
on-screen pointer, but its live pointing measurement was not completed: the server returned 401 without the local
completion token ([record](../implementation/evidence/bubu3-pointing/README.md)). No reliability claim follows from
that dry run.

The current frontend collects bounded visible DOM targets in `frontend/src/components/bubu/pageContext.ts`;
`pointerDirectives.ts` parses the reply and `useBubuIntegration.tsx` renders its remaining text and pointer. Design's
component tree receives component data already available to the page. The backend's `thread_ui_context.py` renders
bounded, defused page data and `thread_app_map.py` supplies navigation reference text. The existing pointing evaluator
scores only the first directive against hand-built scenarios; it is useful evidence, not a complete usability proof.

## Scope and acceptance

1. Render a valid point directive as readable, safely escaped text in the reply, using the current visible target's
   name when available or the directive label as fallback, with appropriate emphasis. A missing, stale or unnamed
   target must not invent a name, leak raw control syntax into the sentence, or break the reply; use a truthful readable
   fallback. Preserve pointer resolution, dismiss controls, keyboard access and ordinary Markdown/LaTeX safety. Handle
   repeated and multiple directives and malformed input without creating additional pointers or executable markup.
2. Give the model concise, truthful per-component facts already held by the current Design page, sufficient to
   distinguish the CFD component from a different process component when the underlying data supports it. Unknown or
   unavailable state must remain unknown; do not infer run counts or results from names, issue new data fetches, or
   imply readiness from a missing value. Facts follow current page/project/version and cannot survive a context switch
   as stale facts. Retain existing visibility, disclosure, size, sanitization and prompt-injection boundaries: page
   content remains data, never authority. Do not expose hidden data merely to improve a pointer.
3. Add a small set of accurate route-scoped examples to the existing app map, including the C1 CFD first-target case
   and the return-to-project case, without teaching the model to point at an unavailable target. Keep page descriptions
   bounded and the existing rule to point only to an available first step. Examples are reference data, not actions.
4. Run a before/after live local pointing evaluation on the same model, scenarios and conditions using the existing
   evaluator or an equivalent recorded harness. Include the reported C1-CFD versus C1-horizontal ambiguity and the
   return-to-project case, with expected first targets grounded in the actual snapshot. Record exact head, model,
   conditions, per-scenario first target, verdict and totals, including wrong, plausible and missing directives. The
   candidate must correctly select the first target in both reported cases and must not regress the number of correct
   first targets across the shared scenarios. Report other failures honestly; do not turn plausible or missing into
   correct. A dry run, mocked completion, HTTP health check or missing credential is not a live evaluation.
5. Verify in real Chromium from the ordinary `/` entry using visible controls and a clean profile: reach the C1 Design
   context, inspect component facts, and demonstrate readable replies and corresponding pointers for the reported CFD
   and return cases, including an unavailable target fallback and pointer dismissal. Evidence must identify whether
   replies are live or controlled; controlled replies prove rendering only, and do not substitute for the local model
   evaluation. Cover relevant narrow viewport and keyboard use. Run focused automated checks for parser/rendering,
   context isolation and sanitization, plus applicable backend/frontend checks. Record commands and results.

The evaluator currently sends completion requests directly to the existing local llama server and requires
`JARVISOS_LLAMACPP_API_KEY`. Use only an authorized, available local evaluation credential without exposing it in
commands, logs, evidence or model context. If unavailable, record the failed or skipped proof and its reason; do not
claim acceptance or invent a substitute score. A required credential that cannot be obtained through an authorized
route is a human-action blocker. No provider escalation is authorized by this slice.

## Boundaries and completion

This slice changes reply presentation, existing page-local context and reference examples only. It creates no provider
or routing behavior, new model call path, memory/retrieval, execution authority, guide runtime, step registry, overlay
system or computer-use action. Product AI calls retain `run_ai_task` and `ai_jobs` ownership; `auto` never executes an
external provider. The evaluation's existing direct local-server measurement is development evidence, not a product
AI path. Existing model output remains a proposal and pointing does not click or change the page.

ADR-061 and spec 224's merged Design/Process owner-surface facts and predicate remain authoritative for that slice.
This work neither replaces those facts nor grants readiness for 224's deferred guide runtime or Literature/BlueCad owner
facts, or for spec 225 computer use. The open draft #966 is not implementation authority.

After required checks and exact-head review, merge and activate through the ordinary authorized workflow, verify the
running version and preserve evidence. Then request the maintainer's final unaided usability test on the activated
app. A failed required proof, serious security or data-loss risk, missing required credential, or other constitutionally
mandated interruption is reported and handled rather than represented as success. No additional maintainer approval is
required for routine engineering decisions.
