# 061 — TOKEN-FLOW-0: complete useful tasks within bounded economics

Status: planned full-spec draft. `docs/specs/STATUS.md` is authoritative. This
contract must not become `ready` until 059b is implemented and merged, the exact
post-059b execution/accounting ownership is rechecked, and the required execution-class
and capability metadata contract is accepted as 061-owned implementation scope.

Depends on: 021, 059b

## Goal

Minimize **external provider spend and rework per solved task** without making local
compute or synthetic fixtures look economically free.

JarvisOS must not impose arbitrary task-kind output targets that make a paid or local
response predictably incomplete. It should reduce waste through qualified binding
selection, relevant context, deterministic tools, literature retrieval, exact external
provider accounting, visible local-compute evidence, bounded continuation, and less
rework.

061 owns:

- top-level flow and ordered per-binding attempt identity;
- technical/context output ceilings;
- direct length-stop continuation;
- adapter invocation and external dispatch evidence;
- usage normalization;
- external provider spend aggregation;
- explicit disclosure of unpriced local compute and non-economic synthetic execution.

061 does not implement Hermes, choose an empirical routing policy, or pretend to model
total economic cost. A future accepted local-compute cost model may add energy/hardware
accounting; until then `local_compute` is visibly unpriced.

## Maintainer decisions

The implementation must preserve these decisions:

- no task-kind table of 4k/8k/16k output targets;
- no generic monthly token cap used as a proxy for money;
- no silent shrinking of a request merely to make it cheaper;
- hard monthly monetary authority remains the existing 059b external-provider USD
  accounting contract;
- local and synthetic execution do not consume that external-provider budget;
- local compute is not reported as zero total economic cost;
- fake/synthetic execution is not empirical model-quality or economic evidence;
- every external fallback and continuation receives a fresh 059b evaluation and
  reservation;
- every local/synthetic fallback and continuation receives a fresh server-owned binding,
  capability, context, flow-limit, and policy evaluation;
- direct continuations are eligible only after an exact normalized `length` stop;
- `max_direct_continuations` is server-owned, editable from the existing AI settings UI,
  defaults temporarily to `8`, and is validated within `0..16`;
- the setting is snapshotted at flow creation and cannot change a running or paused flow;
- future Hermes child-agent calls share flow evidence but are not direct continuations;
- future optimization uses 062 human outcomes plus deterministic reliability, execution
  class, external spend, and local-cost-coverage evidence.

## Canonical execution classification

Extend each enabled provider registry record with one required server-owned
`execution_class`:

- `synthetic` — deterministic fake/test/dry-run adapter; no external egress and no real
  model-compute economics;
- `local_compute` — a real local model/runtime invocation, including loopback or local IPC,
  with no external provider egress;
- `external_provider` — a paid/quota-bearing external provider invocation governed by
  059b.

Do not infer the class from provider ID, route prefix, URL, model name, adapter type, or
caller input. Environment model overrides must resolve to a complete registry model
record under the same accepted provider and execution class or fail closed.

Registry invariants:

- `synthetic` and `local_compute` require the legacy external-egress flag
  `requires_network=false`; this does not mean a local adapter cannot use loopback/IPC;
- `external_provider` requires `requires_network=true` and complete 059b ordinary
  input/output pricing;
- provider kind, execution class, external-egress flag, credential requirements, and
  endpoint locality must be mutually consistent;
- unknown or contradictory classification fails registry loading;
- fallback entries resolve execution class again from the concrete registry record.

The existing `requires_network` name continues to mean **external-provider egress** for
059b compatibility, not “any socket or IPC was used.”

## Provider/model capability and pricing contract

Each enabled real model binding (`local_compute` or `external_provider`) requires
accepted positive:

- `context_window_tokens`;
- `max_output_tokens`, meaning the technical model/output ceiling.

Synthetic fixtures require bounded deterministic limits but are not accepted capability
evidence for real routing.

For external providers, retain 059b ordinary USD prices and add optional nonnegative
`cache_read_input_usd_per_million` only when accepted provider evidence proves a separate
cache-read billing rate and the adapter identifies the subset unambiguously.

Rules:

- `max_output_tokens <= context_window_tokens` for real models;
- missing/invalid real-model capability fails before adapter invocation;
- capability/pricing values come from accepted configuration evidence, never runtime
  scraping;
- a cache-read price cannot exceed ordinary input price unless a maintainer-approved
  contract explicitly documents it;
- absence of cache price is irrelevant when cache-read tokens are absent/null/zero;
- positive external cache reads without accepted cache pricing are charged at ordinary
  input price and marked conservative;
- local/synthetic records do not require external prices and cannot carry an external
  exact/conservative pricing basis.

## Canonical flow, attempt, and dispatch model

Use these concepts:

- **attempt** — one canonical per-binding `ai_jobs` row, including routing/config/context/
  059b denial, pause, or budget stop before adapter invocation;
- **adapter invocation** — an attempt for which `adapter.complete()` actually began,
  including synthetic, local, and external adapters even if they fail;
- **external dispatch** — the normalized evidence about whether an external provider
  request left JarvisOS.

Persist `external_dispatch_state`:

- `not_applicable` — no external-provider binding was invoked;
- `not_started` — an external binding existed, but provider dispatch is proven not to
  have begun;
- `started` — external provider dispatch is proven to have begun;
- `unknown` — an external adapter was invoked, but a failure prevents proving whether
  provider dispatch began.

A **flow** is one top-level operator task containing every ordered attempt, pre-adapter
outcome, fallback, direct continuation, and future orchestrated child call.

`ai_jobs` remains the canonical per-attempt ledger. Add a local `ai_flows` operational
record for state/aggregation; it is not a second attempt ledger.

Each attempt persists safe evidence including:

- flow ID and optional parent attempt/turn;
- fallback and continuation indexes;
- concrete provider/model and execution class, or `none` before executable binding;
- `adapter_invoked`;
- `external_dispatch_state`;
- requested/effective output ceilings where computed;
- normalized finish reason where a model/provider result exists;
- usage source and accounting basis;
- accounted external provider spend USD;
- stable pre-adapter/execution outcome reason.

Invariants:

- execution class `none`, `synthetic`, and `local_compute` require dispatch
  `not_applicable`;
- external pre-adapter rows require `not_started`;
- dispatch `started` or `unknown` implies `adapter_invoked=true` and class
  `external_provider`;
- `adapter_invoked=false` implies usage `none`; accounting is `no_execution` for
  non-external/no-binding rows or `external_not_sent` for a concrete external row;
- an external adapter may be invoked with dispatch `not_started` only when the adapter
  contract proves a local pre-send failure;
- an unclassifiable external adapter exception uses `unknown`, never a guessed false;
- local loopback/IPC remains `not_applicable`, not external dispatch;
- dispatch `started`/`unknown` is treated as a recorded network attempt for 059b first-use
  and conservative reservation purposes; `not_started` is not.

A pre-adapter denial, budget stop, or confirmation pause cannot fall outside the flow.

## Adapter dispatch contract

External adapters must return or attach normalized safe dispatch evidence on every
success/error path. The wrapper records the state before provider response parsing.

Rules:

- mark `started` immediately after the request is handed to the external transport;
- mark `not_started` only for a proven failure before that handoff;
- if an exception crosses a boundary without reliable evidence, mark `unknown`;
- do not infer state from HTTP status, elapsed time, response body, exception text, token
  counts, or credential error wording;
- an external `unknown` attempt is conservatively treated as potentially consumed;
- adapter retry remains forbidden; another attempt returns to the shared spine.

## Flow state and durable resumable content

Canonical flow states:

- `running`;
- `confirmation_required` — resumable pause with valid 059b ticket and canonical
  external-not-sent attempt row;
- `complete`;
- `partial_terminal`;
- `failed_terminal`;
- `cancelled_terminal`.

`confirmation_required` is nonterminal and not gradeable by 062.

A length-stopped result can accumulate content before the next external continuation
requires confirmation. Add a protected `ai_flow_segments` or equivalent store containing:

- flow and ordered segment identity;
- originating adapter-invoked attempt ID;
- exact body needed for assembly/continuation;
- body digest and byte/token counts;
- sensitivity/policy binding;
- creation/expiry timestamps.

Rules:

- segment bodies never enter `ai_jobs`, normal events/logs/status/grade analytics;
- only the exact workspace/operator flow service may load them;
- caller cannot submit/replace accumulated output on resume;
- every continuation treats accumulated output as untrusted current prompt material and
  reruns current policy/capability/context checks;
- external continuations rerun complete 059b packet/decision/trigger/reservation;
- retention is bounded to resume/terminal recovery;
- missing/altered/expired/digest-mismatched content produces honest terminal partial;
- reuse existing protected result/session storage if available post-059b.

A ticket always binds exact flow, attempt, policy, packet, and continuation-guard
identity. Segment and continuation identity are required only when accumulated
continuation content exists. Initial or other pre-adapter confirmation tickets use
null segment and continuation fields and never create dummy segments. Paused state
changes only via server-owned ticket consumption/revalidation, expiry, cancellation,
or deterministic failure.

## Per-attempt output ceiling and external budget

For each executable binding, compute effective output ceiling in order:

1. verified technical output ceiling;
2. remaining context capacity after exact input estimate;
3. optional lower trusted operator/caller ceiling;
4. any lower future server-owned versioned flow-plan ceiling.

Untrusted callers, Hermes, models, adapters, and provider responses cannot raise it.

For `external_provider`, project conservative worst-case USD spend for the exact 059b
packet and ceiling using ordinary prices and no assumed cache discount. If it does not
fit remaining monthly budget:

- do not silently shrink output;
- record the denied concrete attempt with dispatch `not_started`;
- permit evaluation of a separate qualified cheaper binding with its own classification,
  packet, decision, and reservation;
- if none fits, return an honest pre-adapter budget stop.

Local/synthetic execution does not consume external-provider USD budget, but remains
subject to capability, context, continuation, concurrency, timeout, and future local-
resource policies. 061 invents no local dollar estimate.

## Usage normalization

Extend normalized usage with nullable subset fields:

- `cache_read_tokens`, included in input;
- `reasoning_tokens`, included in output.

Total tokens equal input plus output.

Usage-source enum:

- `actual` — adapter/runtime/provider supplied both top-level counts;
- `mixed` — one supplied and one estimated;
- `estimated` — adapter invocation or potentially consumed external dispatch occurred,
  but neither top-level count was supplied;
- `none` — no model/provider token consumption is known or conservatively assumed.

Permitted combinations:

- synthetic normally `estimated`;
- local may be `actual` or `estimated`;
- external `started` may be actual/mixed/estimated;
- external `unknown` with no reliable usage is `estimated` using conservative request/
  ceiling evidence;
- external `not_started` is `none`;
- no adapter invocation is `none`.

Parse only tested explicit aliases. Equal cache aliases count once; conflicts fail.
Negative, boolean, fractional, or subset-greater-than-parent values fail. Explicit cache
zero is valid no-cache evidence.

## Accounting-basis normalization

Persist exactly one `accounting_basis`:

- `no_execution` — no executable adapter invocation and no external concrete dispatch;
- `synthetic_not_economic` — deterministic synthetic invocation;
- `local_compute_unpriced` — real local invocation, local cost unmodelled;
- `external_not_sent` — concrete external attempt proven `not_started`;
- `provider_exact` — external `started` with fully actual usage, ordinary prices, and
  accepted cache price when positive cache reads exist;
- `conservative_standard_input` — external `started` with fully actual usage and positive
  cache reads but no accepted cache discount;
- `conservative_estimated_usage` — external `started` or `unknown` with estimated top-
  level usage or otherwise conservatively reconciled consumption.

Invariants:

- class none maps only `no_execution`;
- synthetic maps only `synthetic_not_economic`;
- local maps only `local_compute_unpriced` in v0;
- external `not_started` maps only `external_not_sent`;
- external `unknown` maps only `conservative_estimated_usage`;
- external `started` maps one provider exact/conservative basis;
- absent/null/zero cache reads require no cache price for exact basis;
- positive cache reads require accepted cache price for exact basis;
- local/synthetic tokens never enter external provider spend.

## External provider spend reconciliation

Persist `accounted_provider_spend_usd` as exact decimal/integer micro-USD. It means
external provider spend evidence only, not total economic cost.

For each attempt:

1. persist concrete decision/class/flow linkage;
2. no adapter/no external concrete attempt records `no_execution`, zero spend;
3. external denied/paused/proven pre-send records `external_not_sent`, zero spend, and
   releases any never-started reservation;
4. synthetic/local invocation records usage/latency, corresponding basis, zero external
   provider spend;
5. authorized external call projects spend and moves reservation in-flight before
   invocation;
6. adapter wrapper persists dispatch `started`, `not_started`, or `unknown` honestly;
7. normalize reported/conservative usage;
8. `started` reconciles from usage/pricing basis;
9. `unknown` reconciles conservatively against request evidence/reservation and never to
   zero merely because no response arrived;
10. write usage/class/dispatch/basis/pricing/spend to the same attempt row;
11. reconcile 059b reservation/egress-attempt evidence;
12. recompute flow aggregates from every attempt.

A failed external `started`/`unknown` attempt may consume provider resources and cannot be
rewritten as not sent. A failed local invocation remains local unpriced. A failed
synthetic invocation remains synthetic evidence.

## Flow aggregates and economic honesty

Required safe flow aggregates include:

- every ordered attempt ID;
- state/terminal reason/current-terminal attempt;
- continuation count/snapshot;
- counts by execution class, adapter invocation, and external dispatch state;
- usage/tokens/latency by class/source;
- counts by accounting basis;
- external provider spend by provider/model/basis and total;
- local-compute attempts/tokens/latency and `local_compute_cost_unpriced` marker;
- synthetic count/evidence marker;
- external-not-sent and unknown-dispatch counts/reasons;
- policy/config/capability/pricing versions;
- final accounting and output digests.

External provider spend is summed exactly once. Never sum native currencies, unpriced
local compute, or synthetic fixtures into USD.

061 exposes no generic total-cost field. Local flows are zero external spend plus
explicit unpriced local compute, not zero total cost. Synthetic flows are not zero-cost
solved-task evidence.

## Finish reason and continuation

Normalize exact provider/model finish reasons:

- `stop`;
- `length`;
- `content_filter`;
- `tool_call`;
- `error`;
- `unknown`.

Automatic direct continuation requires:

- preceding adapter invocation returned non-empty text;
- finish reason exactly `length`;
- complete persisted flow/segment/attempt evidence;
- remaining snapshotted guard;
- fresh concrete binding authorization.

No continuation follows filter/tool/error/unknown/empty/pre-adapter/unhandled exception.

Add server-owned `max_direct_continuations`:

- integer `0..16`;
- default `8`;
- zero disables;
- booleans/fractions/out-of-range/unknown fields fail;
- UI/API cannot exceed versioned maximum;
- value snapshotted at flow creation.

Each continuation:

1. loads exact accumulated state;
2. creates bounded no-repetition instruction;
3. creates a new canonical attempt;
4. resolves concrete binding/class;
5. reruns capability/context/policy;
6. for external, reruns full 059b packet/decision/trigger/reservation;
7. persists dispatch and segment if invoked;
8. retains denied/paused/not-sent rows in flow.

Guard exhaustion produces `partial_terminal` with
`partial_continuation_guard_stop`.

## Status and API surface

Expose additive safe metadata:

- flow state/reason/ordered attempts;
- execution class, adapter-invoked, external-dispatch state per attempt;
- continuation count/snapshot;
- usage by class/source;
- accounting-basis counts;
- external provider spend/subtotals and monthly budget/reservations;
- local token/latency and unpriced marker;
- synthetic counts;
- external not-sent/unknown counts and safe reasons;
- current continuation setting/range/version.

Do not expose prompt/context/segment/credential/raw provider bodies.

## Record capture

Assemble complete terminal response before `jarvis-records` parsing. Create MemoryStore
proposals once only after eligible `complete` finalized flow. Confirmation, partial,
failed/cancelled, pre-adapter, and synthetic fixture outcomes do not automatically create
proposals without a later explicit test-only contract.

## Persistence and migration discipline

Use next additive migration after merged 059b; do not freeze number now.

Expected persistence:

- provider execution class and real-model context capability;
- flow state/protected segments;
- attempt flow/fallback/continuation linkage;
- execution class, adapter-invoked, external-dispatch state;
- usage/subsets/finish reason;
- accounting basis and external spend USD;
- output ceilings;
- local-unpriced/synthetic/dispatch aggregates;
- continuation setting/snapshot.

Migration is additive/idempotent and does not reinterpret historical rows without
deterministic evidence. Ambiguous legacy rows receive explicit legacy/unknown markers and
are excluded from empirical economic cohorts.

## Required tests

### Classification and dispatch

- registry requires valid class and rejects contradictory class/kind/egress metadata;
- overrides cannot change class implicitly;
- every per-binding row belongs to one flow;
- no-binding/no-adapter rows use no execution/not applicable;
- external pre-adapter rows use not started/external not sent;
- synthetic adapter: invoked, dispatch not applicable, synthetic basis;
- Ollama/local: invoked, dispatch not applicable, local-unpriced even over loopback HTTP;
- external handoff sets started;
- proven pre-send failure sets not started;
- ambiguous exception sets unknown and is conservatively reconciled;
- only started/unknown count as recorded network attempt for 059b first-use.

### Capability, budget, and fallback

- no arbitrary task-kind target;
- caller lowers but cannot raise ceiling;
- context capacity enforced;
- external over-budget denied/not sent, not shrunk;
- local/synthetic do not consume external budget;
- each fallback resolves fresh class/capability/pricing;
- reservation never assumes cache discount;
- concurrent external reservations cannot oversubscribe cap.

### Usage and accounting

- usage source is consistent with invocation/dispatch;
- external not started has none/zero spend;
- external unknown has conservative estimated usage/spend, not silent zero;
- local actual/estimated usage remains local/unpriced;
- synthetic estimates never become economic evidence;
- cache aliases/zero/positive pricing rules hold;
- external exact/conservative bases reconcile;
- no-execution/synthetic/local/not-sent have zero external spend with distinct meaning;
- provider spend reconciles to external started/unknown attempts and flow total;
- local/synthetic/not-sent/unknown evidence cannot be hidden by fallback;
- native currencies never directly summed.

### Continuation and durable state

- guard default/range/snapshot;
- only exact length continues;
- each continuation creates fresh classified attempt;
- external continuation gets fresh 059b evidence;
- local continuation remains local/unpriced;
- process restart resumes confirmation state;
- missing/expired/mutated segments fail honestly;
- confirmation nonterminal and distinct from partial;
- no adapter hidden retry/invocation.

### Privacy and regression

- segments stay out of safe ledgers/logs/status/grades;
- cleanup preserves attempt/accounting evidence;
- record capture runs once after eligible complete flow;
- fake, local Ollama, external, 059b, MemoryStore, BLUECAD tests remain green;
- full backend/frontend settings, Ruff, status self-test, BLUECAD proof pass offline.

## Non-goals

No local dollar-cost model, total-economic-cost claim, EUR/FX change, Hermes install,
sub-agent orchestration, streaming, background worker, provider addition, automatic
route promotion, LLM judging, reward training, second attempt ledger, or frontend
redesign.

## Definition promotion gates

Before this definition may become `ready` for implementation:

1. 059b is merged and the current per-binding `ai_jobs`, reservation, egress-attempt,
   ticket, and accounting ownership is rechecked against this contract.
2. The implementation boundary explicitly assigns the registry, adapter-dispatch,
   protected-segment, confirmation-resume, settings, migration, and fixture work to 061
   without requiring those 061-owned changes to exist beforehand.
3. The next additive migration is selected from implementation-time `master`, not frozen
   by this definition PR.
4. Exact-head CI and independent review have no unresolved blockers.

## Implementation acceptance gates

The 061 implementation is not complete until:

1. the registry persists execution class, rejects contradictions, and binds accepted
   real-model capabilities and external pricing;
2. fake, local Ollama, and external fixtures prove class, invocation, dispatch, usage,
   accounting, and ambiguous-failure behavior;
3. external adapters expose reliable normalized dispatch evidence or conservative
   `unknown`;
4. protected storage supports restart-safe segments without exposing bodies through safe
   ledgers or status surfaces;
5. initial and continuation confirmation resume, expiry, and settings control are proven;
6. the additive migration and all required regression, privacy, accounting, and BLUECAD
   evidence pass on the implementation exact head.
