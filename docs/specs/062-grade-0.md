# 062 — GRADE-0: human quality evidence and complete outcome cohorts

Status: backend implementation complete; frontend result surface pending.
`docs/specs/STATUS.md` remains authoritative. The 062a authority and 062b read-only
cohort slices now provide immutable flow subjects, append-only human grade history,
bounded operator APIs, complete outcome cohorts, reconciliation checks, and external
provider spend per useful eligible outcome. No routing actuation is authorized.

Depends on: 021, 059b, 061

## Goal

Capture low-friction human evidence about whether a completed JarvisOS flow solved the
operator's task, while preserving deterministic execution, external dispatch, external
provider spend, unpriced local-compute, and synthetic-fixture evidence.

Canonical human scale:

- `useful`;
- `partly`;
- `rework`;
- `failed`.

062 supplies solution-quality, rework, reliability, and **external provider spend per
useful outcome** evidence. It does not claim total economic cost when local compute is
unpriced, authorize calls, train models, or directly modify routing.

## Deterministic evidence versus human grade

Every terminal 061 flow has mandatory server-owned evidence independent of feedback:

- terminal state/reason and ordered attempts;
- execution classes and adapter invocation;
- external dispatch states (`not_applicable`, `not_started`, `started`, `unknown`);
- usage sources/accounting bases;
- external provider spend USD;
- local-compute unpriced and synthetic markers;
- finalized accounting digest.

A human grade is optional explicit operator judgment about the result delivered. Only a
trusted human action creates/revises/withdraws it.

Ungraded is not useful, failed, zero, free, or absent. Analytics expose graded, ungraded,
and deterministic evidence separately.

## Grade semantics

### `useful`

Solved the requested task or delivered an acceptable decision/artifact without
substantial additional AI work or human reconstruction.

### `partly`

Materially advanced the task with reusable work, but bounded additions/checks/corrections
remain.

### `rework`

Requires substantial reframing, rerouting, recomputation, or regeneration rather than a
bounded continuation.

### `failed`

Produced no usable result for the task. Deterministic provider/policy/tool failure does
not auto-create this human judgment.

The category is authoritative. 062 defines no hidden numeric score. Future scalarization
belongs to 025 and preserves the category.

## Grade subject

Grade the **top-level terminal flow**, not attempts, fallbacks, continuations, agents,
critics, reviewers, or tools.

A flow is gradeable only when:

- 061 state is terminal, never running/confirmation-required;
- all 059b reservations/egress attempts reconciled;
- all canonical attempts finalized;
- ordered identity complete;
- terminal output digest stable where output exists;
- one immutable finalized outcome snapshot exists.

Create versioned `flow_outcome_digest` over at least:

- flow/workspace/operator identity;
- terminal state/reason;
- terminal and every ordered attempt ID;
- finalized 061 flow accounting digest;
- each ordered attempt's finalized accounting/egress digest or explicit no-egress marker;
- output digest/no-output marker;
- per-attempt execution class and adapter-invoked flag;
- external dispatch states;
- usage-source and accounting-basis counts;
- external provider spend subtotals/total;
- local attempts/tokens/latency/unpriced marker;
- synthetic evidence presence;
- flow execution-composition and provider-dispatch-quality summaries;
- provider/model/route mix;
- policy/config/capability/pricing/accounting versions.

After gradeability the snapshot is immutable. Late reconciliation:

- invalidates the old subject;
- retains the old grade history for audit but excludes that stale grade record, not the
  finalized flow evidence, from promotion cohorts;
- creates a corrected subject version/digest;
- requires a new explicit human grade action.

The result surface returns exact subject version/digest. Every set/revise/withdraw submits
these as expected preconditions. Missing/stale/mismatched/invalid/cross-flow evidence
fails; judgment never silently moves to a newer subject.

The client cannot replace execution class, dispatch, route, provider, usage, accounting,
spend, local-cost marker, output digest, or attempt IDs.

## Grade authority

Accepted sources:

- `operator_ui`;
- `operator_api` explicit human action.

Forbidden sources:

- model/provider/adapter;
- Hermes/sub-agent/critic/reviewer/LLM judge;
- deterministic success/failure alone;
- inferred sentiment/tokens/cost/latency/route/retry;
- CI/benchmark/automatic evaluation.

Grade actions make zero provider/model/tool/sanitizer/MemoryStore/routing/promotion calls
and never mutate 061 evidence.

## Append-only grade history

Use the next additive migration after merged 061; do not freeze number now.

Create dedicated append-only grade rows containing:

- event ID;
- flow/terminal attempt ID;
- subject version/digest;
- action set/withdraw;
- grade for set;
- optional bounded reason codes/note;
- actor/source;
- superseded head;
- subject-scoped idempotency key;
- timestamp/schema/policy version.

Corrections and withdrawals append. Queries derive one current valid head. Writes use a
bounded transaction, exact expected-subject check, and optimistic head check; concurrent
conflicts produce one winner.

## Reason codes and note privacy

A grade alone is sufficient. Optional versioned reason codes may include:

- correct/complete;
- minor edits;
- incomplete/missing evidence;
- wrong reasoning/facts;
- hallucination;
- wrong tool/route;
- too verbose/brief;
- provider/tool failure;
- policy block;
- other.

At most five unique accepted codes. Do not enforce brittle code-to-grade mappings.

Optional note is trimmed UTF-8, max 1,000 chars, local restricted evidence. It never
enters 061/`ai_jobs`, logs/events/status/analytics, prompts/context/providers/tools,
automatic exports, GitHub reports, or telemetry. Only direct operator detail/history
reads return it.

## API and frontend

Add bounded routes under existing `/ai` after implementation-time review.

Set/revise request requires:

- terminal flow/ledger ID;
- grade;
- expected subject version/digest;
- optional reasons/note;
- idempotency key;
- optional expected current-grade ID.

Withdraw requires current head, exact subject precondition, and idempotency evidence.
Reads by flow/terminal ledger return bounded metadata; notes/history require direct
operator detail access.

Frontend presents four unselected choices under terminal result. Submission is optional,
reloads state/subject from server, handles stale-subject/revision conflicts, supports
withdrawal and accessibility, stores no authoritative local state, and makes zero AI/tool
calls.

## Attempt-level evidence

Human grades are flow-level. Execution/accounting evidence is attempt-level. Do not force
them into one denominator.

### Execution class

Expose attempt counts, usage, latency, and spend by:

- none/no executable binding;
- synthetic;
- local compute;
- external provider.

Counts reconcile to canonical attempt count.

### External dispatch state

Expose attempt counts by:

- not applicable;
- not started;
- started;
- unknown.

Counts reconcile to attempt count. `started` and `unknown` are recorded network attempts
for first-use/conservative accounting; `not_started` is not.

### Accounting basis

Expose counts and external USD subtotals for:

- `no_execution`;
- `synthetic_not_economic`;
- `local_compute_unpriced`;
- `external_not_sent`;
- `provider_exact`;
- `conservative_standard_input`;
- `conservative_estimated_usage`.

Counts reconcile to attempt count. External spend subtotals reconcile to finalized
external spend. The first four have zero external spend but distinct meaning. Unknown
dispatch is always in conservative estimated basis.

When grouped by grade, an attempt inherits only the current eligible grade of its parent
flow. Ungraded attempts remain explicit.

## Flow-level execution composition

Derive exactly one bucket, ignoring no-execution attempts when identifying executed
class but reporting them separately:

- `no_adapter_execution`;
- `synthetic_only`;
- `local_compute_only`;
- `external_provider_only`;
- `mixed_executed_classes`.

These reconcile to terminal flow count. Expose no-execution flow/attempt reasons
separately.

Any synthetic invocation sets `synthetic_evidence_present` and excludes the flow from
empirical promotion by default, even if a later real fallback succeeds, unless a future
benchmark contract accepts it.

## Flow-level external dispatch and accounting quality

Derive one dispatch bucket:

- `no_external_dispatch` — no external attempt is started/unknown;
- `external_started_only` — at least one started and no unknown;
- `external_unknown_present` — at least one unknown.

Derive one provider-accounting-quality bucket:

- `no_external_provider_consumption` — no started/unknown dispatch;
- `provider_exact_only` — consumed/potentially consumed external attempts all exact;
- `conservative_only` — all consumed/potentially consumed external attempts conservative;
- `mixed_provider_basis` — exact and conservative attempts coexist.

Each set reconciles to terminal flow count. External-not-sent attempts remain separately
visible but do not count as provider consumption. Unknown dispatch counts as conservative
potential consumption.

## Local economic coverage

Expose:

- local attempt/input/output/latency totals;
- flows with local compute;
- local-cost-unpriced counts;
- future local cost-model version, absent in v0.

Zero external spend does not mean zero total cost.

## Cohort integrity

Every terminal flow contributes deterministic evidence. Promotion eligibility is
server-computed separately.

Default flow-level exclusions from empirical promotion cohorts:

- any synthetic execution;
- CI/smoke/test unless later benchmark contract includes them;
- sanitizer/internal flows;
- incomplete/non-finalized provenance/accounting;
- ambiguous legacy execution/dispatch classification.

Grade-state treatment is separate from flow eligibility:

- a withdrawn grade event is excluded as the current grade, but the finalized flow remains
  in the cohort as ungraded unless a separate flow-level exclusion applies;
- an invalid or stale subject's grade is excluded; when a corrected current subject exists,
  the finalized flow remains in the cohort as ungraded until explicitly regraded;
- withdrawal or subject invalidation never removes finalized 061 spend, attempts,
  deterministic failures, or other flow evidence from denominators.

Keep flow exclusions, grade-state transitions, and audit reasons explicit and stable.

Every bounded cohort exposes:

- terminal and complete/partial/failed/cancelled flow counts;
- gradeable/graded/ungraded/coverage;
- counts/external spend by grade;
- attempt class/source/dispatch/accounting counts/subtotals;
- flow execution composition;
- flow external dispatch/provider-quality buckets;
- local unpriced metrics;
- synthetic/legacy exclusions;
- no-execution/not-sent/unknown counts/reasons;
- revision/withdrawal/invalid-subject counts;
- provider/model/route mix;
- fallback/continuation/latency/token distributions.

Rules:

- deterministic failures remain even ungraded;
- ungraded flows remain in denominators;
- one flow in flow metrics, every attempt in attempt metrics;
- external spend counted once from finalized 061 aggregate;
- local/synthetic/not-sent/unknown evidence cannot be hidden by fallback;
- bases are never relabelled to force reconciliation;
- one-call spend cannot masquerade as spend per solved task.

## Economic metrics

For one eligible cohort:

`external_provider_spend_per_useful_outcome = sum(finalized accounted_provider_spend_usd for every eligible terminal flow) / count(current eligible useful grades)`.

Numerator includes spend from useful, partly, rework, failed, and ungraded eligible flows.
Null when no useful grade.

This is not total economic cost. Always show it with:

- grade coverage/failure rate;
- execution composition;
- dispatch/provider-accounting quality;
- local unpriced counts/tokens/latency;
- synthetic/legacy exclusions;
- cohort inclusion/exclusion.

`total_economic_cost_per_useful_outcome` is unavailable/null in v0. A local cohort cannot
support “local is cheaper” merely because external spend is zero.

## Relationship to 025

062 supplies human labels, deterministic cohorts, dispatch/accounting quality, external
provider spend, local-cost coverage, and synthetic evidence. 025 owns sample size,
holdout, stratification, drift, promotion/reversion, and route tables.

025 cannot promote local/external/synthetic based solely on external spend and cannot
treat unpriced local compute or unknown dispatch as free. Grade submission never actuates
025.

## Required tests

### Subject/authority

- four grades only;
- terminal finalized flows only;
- one subject per top-level flow;
- exact expected subject required for all writes;
- stale/mismatched/invalid/cross-flow fails;
- client cannot replace execution/dispatch/accounting evidence;
- late reconciliation or any finalized 061 flow/attempt accounting-digest change invalidates subject;
- stale-subject invalidation excludes the old grade record while retaining the corrected
  finalized flow as ungraded with all 061 evidence;
- deterministic/model signals never auto-grade.

### Append-only/privacy/UI

- idempotent replay no duplicate;
- revisions/withdrawals append;
- withdrawal removes only the current grade and keeps the finalized flow, spend, attempts,
  and deterministic outcome in cohort denominators as ungraded;
- withdrawal cannot improve failure rate, grade coverage, or provider spend per useful
  outcome by deleting the parent flow from the cohort;
- stale-head conflict;
- notes bounded and absent from prompts/logs/status/tools/exports;
- zero AI/provider/tool calls;
- UI no preselection, server reload, accessible conflict/withdrawal handling.

### Cohort reconciliation

- every flow once in flow metrics;
- every attempt once in attempt metrics;
- class/dispatch/accounting counts reconcile to attempts;
- external not sent has zero spend and is not provider consumption;
- unknown dispatch is conservative potential consumption;
- external spend bases reconcile to finalized spend;
- execution composition, dispatch, and provider-quality buckets each reconcile to flows;
- synthetic excludes by default;
- local remains quality-gradeable but economically unpriced;
- ambiguous legacy excluded.

### Economic honesty

- provider-spend metric includes all eligible cohort spend including failed/rework/
  ungraded;
- zero useful returns null;
- local/synthetic/not-sent never enter provider spend;
- unknown dispatch not silently zeroed;
- local zero external spend never becomes zero total cost;
- total-economic-cost metric unavailable;
- promotion cannot rank classes from external spend alone.

Run full backend/frontend tests, Ruff, status self-test, and BLUECAD proof offline.

## Non-goals

No LLM judge, automatic grade inference, reward/fine-tuning, automatic promotion, local
dollar-cost model, total-economic-cost claim, broad analytics platform, telemetry export,
provider call, Hermes activation, or second attempt ledger.

## Promotion gates

Before 062 may become ready:

1. 061 merged with stable class, invocation, dispatch, usage, accounting, external-spend,
   local-unpriced, and synthetic evidence.
2. Finalized subjects immutable after gradeability.
3. Flow, attempt, human-grade, dispatch, provider-spend, and local-cost cohorts complete.
4. Frontend result surface identified/tested.
5. Migration ID assigned from implementation-time master.
6. 025 remains non-actuating and cannot treat local/unknown as free.
7. Exact-head CI and independent review have no unresolved blockers.
