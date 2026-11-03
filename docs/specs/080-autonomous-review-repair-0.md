# Spec 080 — AUTONOMOUS-REVIEW-REPAIR-0

Definition status: planning kernel; registry remains `planned`.

Depends on: 004, 017, 019, 022, 079

## 1. Purpose

Define a removable capability, separate from scheduled continuation, that can request an independent review, represent current findings, authorize bounded correction on the same PR branch, and request a re-review.

080 exists because continuation and review/repair have different authority, state, cost, and rollback boundaries. Removing 080 must leave spec 079 daily continuation intact.

## 2. Candidate scope

- exact-head review request;
- normalized finding identity and severity;
- open, fixed, rebutted, superseded, and stale dispositions;
- implementer/reviewer role separation;
- bounded fix and re-review rounds;
- deterministic gates before each review;
- no automatic merge.

## 3. Explicit non-goals

- scheduled continuation;
- selecting or authorizing a new specification;
- merge or auto-merge;
- new provider accounts or credentials;
- hidden state outside GitHub;
- unbounded review loops;
- treating model findings as authority;
- combining this specification back into 079.

## 4. Minimum-necessary boundary

080 is not required to satisfy 079's continuation acceptance criterion. It is therefore not implemented as part of 079. Promotion requires a separate demonstrated need and a separate minimum-necessary test.

## 5. Promotion blockers

Before a full specification:

1. prove the smallest reliable same-branch review and repair route using existing integrations;
2. select a deterministic representation for unresolved review threads using available GitHub APIs;
3. define maximum rounds and no-progress termination;
4. define exact-head staleness for CI and review evidence;
5. preserve advisory review and agent-owned merge only after deterministic gates are green and no current blocking finding remains.