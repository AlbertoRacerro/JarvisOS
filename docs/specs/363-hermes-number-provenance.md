# 363 — HERMES-NUMBER-PROVENANCE-1: Deterministic number provenance and verification badges in Jarvis chat

Status: ready
Depends on: 362

## Goal

Every numeric claim in a Hermes chat answer is checked deterministically against the tool results (bench/card runs, knowledge/literature reads, paper/patent/browser reads, and recorded plan findings) produced during that turn, reusing the numeric parser and comparison rules of `literature_checks.py`. Verified numbers carry a compact provenance link to their originating tool run or source; unverified numbers are explicitly flagged as unverified so the engineer can inspect or challenge them directly from the sidecar chat.

## Why

Directive `L20261124094804-a929` (item 2, reinforced by `L20261124095650-7ca4` item 4): local models can hallucinate or mis-transcribe numbers when synthesizing tool outputs. Deterministic server-owned provenance checking guarantees that every number in a chat answer is either traced back to a tool result or source passage from that turn, or visibly marked unverified, with no frontier model needed at run time.

## Code facts checked at `bb6734aa`

- `backend/app/modules/memory/literature_checks.py::number_tokens` extracts numeric values (plain decimals, comma decimals, scientific notation with `e`/`×10^`/superscripts) from text.
- `backend/app/modules/memory/literature_checks.py::_same_number` compares two floats within `RELATIVE_TOLERANCE` / `ZERO_ABSOLUTE_TOLERANCE`.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_tool` executes broker tools and logs `hermes.tool_result` events with `evidence_refs` for the active `interaction_id`.
- `backend/app/modules/agents/hermes/supervisor.py::_tool_evidence_refs` extracts bounded evidence identifiers from a structured tool result.
- `backend/app/modules/ai/thread_service.py::_submit_hermes_interaction` persists the final assistant response and `hermes.interaction_flows` event for a completed Hermes turn.
- `backend/app/modules/ai/thread_service.py::_interaction_from_row` builds `AIThreadInteractionRead` from the stored interaction and associated events.
- `backend/app/modules/ai/thread_models.py::AIThreadInteractionRead` defines the interaction read contract serialized to the frontend.
- `frontend/src/components/ai/useJarvisSidecar.tsx` renders each assistant interaction in the sidecar chat.

## Scope

In scope:
- **Per-turn numeric evidence ledger** (`backend/app/modules/agents/hermes/provenance.py`, `supervisor.py`):
  - During a Hermes turn, extract and retain a bounded table of numeric tokens from every succeeded tool result (`jarvis_bench_run`, `jarvis_bench_read`, `jarvis_knowledge_read`, `jarvis_paper_search`, `jarvis_paper_pdf_read`, `jarvis_patent_read`, `jarvis_browser_page_read`, `jarvis_context_preview`, `jarvis_turn_plan`), keyed by tool call id, tool name, and safe source/run ref (e.g. `bench:<run_id>`, `literature:<source_id>`, `doi:<doi>`, `patent:<pub_no>`, `tool:<call_id>`). Never store raw secrets or private filesystem paths.
- **Deterministic answer checker** (`backend/app/modules/agents/hermes/provenance.py`, `thread_service.py`):
  - On turn completion (`_submit_hermes_interaction`), parse numeric values in the final assistant response using `literature_checks.number_tokens` (skipping trivial structural tokens such as list indices `1.`, `2.`, section headers, dates/years in ISO format, and markdown headings).
  - Match each extracted number against the turn's numeric evidence ledger using `literature_checks._same_number` (plus a bounded rounding tolerance for display-rounded outputs, e.g. 1% relative when the evidence value rounds to the displayed precision, with exact matches preferred).
  - Persist a bounded `hermes.number_provenance` event (`interaction_id`, `workspace_id`, `verified_count`, `unverified_count`, and up to 40 structured entries `{raw, value, status: "verified" | "unverified", source_ref?, tool_name?, route_hint?}`) in SQLite `events` (no schema migration).
- **Interaction read projection** (`thread_models.py`, `thread_service.py`, regenerated frontend contracts):
  - Add optional `number_provenance` summary + entries to `AIThreadInteractionRead` (default `None` when absent).
  - Map safe `route_hint` values for verifiable Jarvis screens (e.g. bench run -> `/runs`, literature source -> `/memory/literature`, memory/knowledge -> `/memory/search`) so the user can jump directly to the backing screen.
- **Sidecar provenance display** (`frontend/src/components/ai/useJarvisSidecar.tsx`, `frontend/src/components/ai/JarvisSidecar.css`, `frontend/tests/`):
  - Display a compact provenance bar beneath completed assistant answers that contain checked numbers: verified count + unverified count badge, and an expandable chip list showing each number, its source/run link (or `"Non verificato"` warning badge when unverified).
  - Add a frontend static `.mjs` test (registered in `frontend/package.json`) and an `e2e/` browser walk covering verified and unverified number badges in the sidecar.

Out of scope (binding non-goals):
- No SQLite schema migration (use existing `events` table).
- No LLM-based or frontier-model verification pass at runtime.
- No automatic rewriting or censoring of assistant prose; provenance is additive annotation.
- No changes to tool permissions, egress policy, or sandbox boundaries.

## Files likely touched

- `backend/app/modules/agents/hermes/provenance.py` (new)
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/ai/thread_models.py`
- `backend/app/modules/ai/thread_service.py`
- `frontend/src/api/generated/` (regenerated via `scripts/generate_frontend_contracts.py`)
- `frontend/src/api/threads.ts`
- `frontend/src/components/ai/useJarvisSidecar.tsx`
- `frontend/src/components/ai/JarvisSidecar.css`
- `backend/tests/test_hermes_number_provenance_363.py` (new)
- `frontend/tests/` static test + `frontend/package.json`
- `e2e/` browser walk covering sidecar number provenance display

## Design constraints

- Additive, backward-compatible API contract: `number_provenance` defaults to `None` when no checked numbers or event exist.
- Reuse `literature_checks.number_tokens` and `literature_checks._same_number` rather than duplicating number parsing.
- Strict bounds: max 40 provenance entries per interaction, `raw` <= 48 chars, `source_ref` <= 120 chars; never emit secrets or absolute filesystem paths.
- Medium risk (feature code + UI, no egress/migration/auth change): coordinator severe review, CI green on exact head, and passing `e2e/` browser walk before merge.

## Acceptance criteria

1. Numbers present in a Hermes turn's succeeded tool outputs (bench runs, literature/knowledge reads, paper/patent reads) are matched deterministically and marked `verified` with their `source_ref` and `route_hint`.
2. Numbers appearing in the final assistant prose that do not appear in any tool output or located source from that turn are marked `unverified`; trivial list/date indices are not false-positively flagged.
3. `AIThreadInteractionRead` projects `number_provenance` from the persisted `hermes.number_provenance` event without breaking existing thread responses.
4. The sidecar chat renders the provenance summary and verified/unverified badges with navigation links for backing screens; static frontend test and `e2e/` browser walk pass.
5. Existing Hermes, literature check, and thread tests remain green; frontend contracts check passes.

## Required tests

- Offline backend tests in `backend/tests/test_hermes_number_provenance_363.py` covering criteria 1–3 (fake inference, no network).
- Frontend static `.mjs` test covering provenance summary, source link, and unverified badge rendering.
- `e2e/` browser walk proving provenance badges are visible in the sidecar from `/` using visible controls only.

## Definition of done

Test gate green (see `AGENTS.md`), browser walk evidence recorded, coordinator severe review complete, CI green on exact head, STATUS row reconciled.
