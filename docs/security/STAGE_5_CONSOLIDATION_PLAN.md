# Stage 5 Consolidation Plan (Stage 5-PRE)

Status: **design contract**. This is the implementation plan for the *future* runtime
consolidation. Nothing here is executed in this slice. Each substage is a separate,
behavior-preserving slice gated by the existing golden tests.

Principle: migrate **one adapter at a time** behind the existing behavior. The canonical
sensitivity vocabulary is introduced first as a pure module; each site is then re-pointed
to it only after a golden test proves behavior is unchanged (or changed deliberately).

## Substages

### Stage 5.0 — Contract accepted, no runtime
- Files: this `docs/security/*` set + fixtures.
- Tests green: all 482.
- Unchanged: everything runtime.
- Deliberate change: none.
- Rollback: delete docs.
- Stop condition: contract not agreed → do not start 5.1.

### Stage 5.1 — Canonical vocabulary module (dormant)
- Files likely touched: new `backend/app/modules/sensitivity/` (pure functions: class enum,
  marker sets, `classify_material()`), plus its own unit tests. **No site re-pointed yet.**
- Tests that must stay green: all existing golden + full suite.
- Expected unchanged: every current site still uses its own logic.
- Deliberately allowed to change: nothing in existing sites.
- Rollback: remove the new module (no caller depends on it).
- Stop condition: the new module cannot reproduce all current golden verdicts → stop,
  reconcile vocabulary first.

### Stage 5.2 — Migrate event redaction adapter
- Files: `events/service.py` re-pointed to the canonical module via an adapter.
- Must stay green: `test_data_infrastructure.py` redaction tests, sensitivity golden.
- Unchanged: redaction output for all currently-tested payloads.
- Deliberate change: only if a golden is explicitly updated (e.g. `token` key) — must be
  called out and re-locked.
- Rollback: revert adapter, keep module.
- Stop: any redaction regression not covered by an intentional golden update.

### Stage 5.3 — Migrate privacy / egress adapter
- Files: `ai/privacy.py` re-pointed.
- Must stay green: `test_ai_fast_dev_policy.py`, `test_ai_smoke_tests.py`, sensitivity golden.
- Unchanged: FAST_DEV behavior **unless** the product decision (below) says to change it.
- Deliberate change: ambiguous→blocked for real cloud egress (requires product sign-off).
- Stop: any change to FAST_DEV egress without an explicit decision record.

### Stage 5.4 — Migrate history scan adapter
- Files: `dev_message_route/smoke_adapter.py` (`scan_history_turn_for_context`).
- Must stay green: `test_dev_local_chat.py`, `test_sensitivity_golden_behavior.py`,
  `test_dev_message_route_lazy_import.py` (lazy-import guarantee must not regress).
- Deliberate change candidates: history bypass exclusion, Italian IP markers.
- Stop: lazy-import isolation weakened, or a history golden changes without re-locking.

### Stage 5.5 — Runner preflight adapter (only if safe)
- Files: `runner/safety.py`.
- Must stay green: `test_python_runner.py`, runner golden.
- Hard rule: **do not pretend the blocklist is a sandbox.** Real protection stays the
  hash-pinned single reviewed script. Migration may unify the marker vocabulary but must
  not claim obfuscation resistance it does not have.
- Stop: any change that implies sandboxing without actually providing it.

### Stage 5.6 — Provider / retrieval / tool policy integration
- Files: provider selection + (future) retrieval/tool entry points.
- Must stay green: all provider/budget tests.
- Deliberate change: introduce provider-neutral classes over existing provider-shaped
  settings; wire the decision order from `PROVIDER_SELECTION_POLICY_PRE.md`.
- Stop: any real provider execution, registry, or API key handling — that is a separate
  milestone, not part of consolidation.

## Drift decision table

Each currently-observed issue (all golden-locked today) gets a disposition.

| Issue | Where (current) | Disposition |
|---|---|---|
| `token` 4-way drift | classify / events / runner / history | **fix in Stage 5** (5.1 vocabulary unifies it) |
| `api key` vs `api_key` | `privacy.classify()` space-sensitive | **fix in Stage 5** (5.1 normalization) |
| FAST_DEV ambiguous → external_allowed | `privacy.py:144` | **needs product decision** (then 5.3) |
| history bypass phrase included (`None`) | `scan_history_turn_for_context` | **fix in Stage 5** (5.4) |
| Italian IP marker missed | `scan_history_turn_for_context` | **fix in later hardening** (bilingual marker set) |
| runner obfuscated import passes | `FORBIDDEN_SCRIPT_MARKERS` | **preserve for now** (hash-pin is the real guard; real fix = sandbox, later/product decision) |
| event redaction key mismatch (`token` not redacted, `secret` is) | `events/service.py` | **fix in Stage 5** (5.2, with re-locked golden) |

## Global stop conditions (apply to every substage)

- A previously green golden turns red without an explicit, reviewed golden update.
- The lazy-import isolation (Stage 1) regresses.
- Any substage starts to require provider execution, network, or API keys.
- A migration would lower sensitivity of any currently-blocked path silently.

## What this plan deliberately does NOT do

- It does not implement the canonical module in this slice (that is 5.1).
- It does not fix FAST_DEV here (needs a product decision first).
- It does not integrate Graphify/DeerFlow/Supabase/MarkItDown/MCP (separate milestones,
  see [MATERIAL_TOOL_MCP_BOUNDARY_PRE.md](MATERIAL_TOOL_MCP_BOUNDARY_PRE.md)).
