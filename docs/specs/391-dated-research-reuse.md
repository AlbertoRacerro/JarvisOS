# 391 — DATED-RESEARCH-REUSE-1

Status: ready
Depends on: none

## Goal

Implement the dated-research reuse behaviour in Hermes's read-only literature views and engineering methodology rules (`backend/app/modules/agents/hermes/knowledge_read_tool.py`, `backend/app/modules/agents/hermes/supervisor.py`, `backend/app/modules/agents/hermes/broker_mcp.py`): expose deterministic source validation dates (`created_at`, `updated_at`, `validated`) and a structured `dated_research_summary` (`validated_count`, `latest_validated_date`, `latest_published_year`, `reuse_guidance`) on `literature_sources` and `literature_entries` reads, allow filtering `literature_sources` by `state`, and instruct Hermes to reuse validated earlier documents first, offer to search for anything newer than the document's date at the end, and search afresh only when stored coverage is incomplete or the user asks.

## Why

Implements Maintainer Directive `L20261127191802-5834` (Behaviour #2 — Reuse dated research, explicitly listed among the clearly-needed pieces that may start in parallel with Phase 1 exploration after the reuse check). Today, `jarvis_knowledge_read(view="literature_sources")` and `jarvis_knowledge_read(view="literature_entries")` omit source validation timestamps (`updated_at`/`created_at`) and do not surface a deterministic freshness-offer date, so Hermes cannot reliably tell when stored research was validated or offer a dated follow-up search instead of re-searching from scratch.

## Code facts checked at `5a24abb6`

- `backend/app/core/literature_schema.py::LITERATURE_SCHEMA_STATEMENTS` already stores `state` (`raw`/`review`/`accepted`), `published_year`, `created_at`, and `updated_at` on `literature_sources`, and `status` and `updated_at` on `literature_entries` (no schema migration required).
- `backend/app/modules/agents/hermes/knowledge_read_tool.py::_VIEW_ALLOWED_KEYS` currently allows `{"view", "workspace_id", "limit", "query"}` for `literature_sources` and `{"view", "workspace_id", "limit", "source_id", "status", "query"}` for `literature_entries`.
- `backend/app/modules/agents/hermes/knowledge_read_tool.py::_handle_literature_sources` selects `s.id, s.title, s.citation, s.publisher, s.published_year, s.source_kind, s.artifact_id, s.state, entry_count` ordered by `s.updated_at DESC, s.id ASC`, omitting `s.created_at` and `s.updated_at` from the returned items.
- `backend/app/modules/agents/hermes/knowledge_read_tool.py::_handle_literature_entries` queries `literature_entries` without joining `literature_sources` for source title/state/date.
- `backend/app/modules/agents/hermes/supervisor.py::ENGINEERING_METHODOLOGY_RULES` defines the concise engineering methodology rules appended to Hermes turn prompts via `_assemble_turn_prompt`.
- `backend/app/modules/agents/hermes/broker_mcp.py` defines the `jarvis_knowledge_read` MCP tool schema and description.

## Scope

In scope:
- **Deterministic dated-research metadata in `backend/app/modules/agents/hermes/knowledge_read_tool.py`:**
  - Add a deterministic pure helper `_build_dated_research_summary(dates: list[str | None], years: list[int | None], validated_count: int) -> dict[str, Any]` that computes:
    - `validated_count: int`
    - `latest_validated_date: str | None` (ISO `YYYY-MM-DD` extracted from the max valid timestamp among validated items, or `None`)
    - `latest_published_year: int | None` (max `published_year` among validated items, or `None`)
    - `reuse_guidance: str` (when `validated_count > 0`: concise instruction citing `latest_validated_date` or `latest_published_year` to reuse the validated stored documents first, offer to search for anything newer than that date at the end, and search afresh only if coverage is incomplete or requested; when `0`: concise note that no validated stored sources matched so fresh search or a labelled estimate is permitted).
  - In `_VIEW_ALLOWED_KEYS["literature_sources"]`, add optional `"state"` (`raw`, `review`, `accepted`; validate and reject unknown values with a clear structured error like existing filters).
  - In `_handle_literature_sources`:
    - Select and expose `created_at`, `updated_at`, and `validated` (`r["state"] == "accepted"`) on each item.
    - Include top-level `"dated_research_summary"` computed over the returned `accepted` sources.
  - In `_handle_literature_entries`:
    - `LEFT JOIN literature_sources s ON s.id = e.source_id AND s.workspace_id = e.workspace_id` to expose `source_title`, `source_state`, `source_updated_at`, and `source_published_year` on each returned entry (keeping all existing keys unchanged for backward compatibility).
    - Include top-level `"dated_research_summary"` computed over entries whose `status == "accepted"` or whose backing `source_state == "accepted"`.
- **MCP schema update in `backend/app/modules/agents/hermes/broker_mcp.py`:**
  - Add optional `state` parameter (`raw`, `review`, `accepted`) to `jarvis_knowledge_read` for `literature_sources` and note that literature views return `dated_research_summary` for dated-research reuse.
- **Methodology rule in `backend/app/modules/agents/hermes/supervisor.py`:**
  - Add one concise bullet to `ENGINEERING_METHODOLOGY_RULES`:
    `"- Reuse dated research: check validated stored literature/documents first, cite their date, offer at the end to search for anything newer than that date, and search afresh only when stored coverage is incomplete or the user asks."`
- **Offline tests in `backend/tests/test_hermes_dated_research_reuse.py`:**
  - Verify `literature_sources` exposes `created_at`, `updated_at`, `validated`, optional `state` filtering (valid + invalid rejection), and `dated_research_summary` (both with accepted sources and with only raw/empty sources).
  - Verify `literature_entries` joins `literature_sources`, exposes `source_title`, `source_state`, `source_updated_at`, `source_published_year`, and computes `dated_research_summary`.
  - Verify `ENGINEERING_METHODOLOGY_RULES` and `_assemble_turn_prompt` include the dated-research reuse rule and existing `test_hermes_knowledge_read_343.py` remains green.

Out of scope (binding non-goals):
- No SQLite schema changes or migrations (`literature_sources` already has `created_at`, `updated_at`, `state`, `published_year`).
- No new MCP tools or external web/search calls.
- No frontend UI changes.

## Files likely touched

- `backend/app/modules/agents/hermes/knowledge_read_tool.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/tests/test_hermes_dated_research_reuse.py` (new)
- `docs/MODULES.md` (regenerated via `python3 scripts/gen_modules.py`)

## Design constraints

- **Strictly read-only:** `jarvis_knowledge_read` stays read-only and respects `_enforce_payload_bounds`.
- **Backward compatible:** All existing keys on `literature_sources` and `literature_entries` responses stay intact; `state` filter is optional.
- **Compact prompt budget:** The added methodology rule is one short sentence (~35 tokens), preserving local Hermes prompt-budget invariants (specs 368–370).
- **Zero private pack literals:** Strictly generic literature/research terminology.

## Acceptance criteria

1. `jarvis_knowledge_read(view="literature_sources")` returns `created_at`, `updated_at`, `validated` per source, supports optional `state` filtering (`raw`/`review`/`accepted`), and returns a deterministic `dated_research_summary` with `validated_count`, `latest_validated_date`, `latest_published_year`, and `reuse_guidance`.
2. `jarvis_knowledge_read(view="literature_entries")` joins `literature_sources` to return `source_title`, `source_state`, `source_updated_at`, `source_published_year` per entry and returns a deterministic `dated_research_summary`.
3. `ENGINEERING_METHODOLOGY_RULES` includes the dated-research reuse rule (reuse validated earlier documents, offer to search for anything newer than their date at the end, search afresh only when incomplete or asked).
4. All offline tests in `backend/tests/test_hermes_dated_research_reuse.py` and `backend/tests/test_hermes_knowledge_read_343.py` pass, and `ruff`, `check_spec_status.py`, `check_code_facts.py`, `gen_modules.py`, and `check_typecheck_ratchet.py` pass cleanly.

## Required tests

- `backend/tests/test_hermes_dated_research_reuse.py`:
  - `literature_sources` with mixed `raw`/`review`/`accepted` sources: verifies `validated`, timestamps, `state="accepted"` filter, invalid `state` rejection, and `dated_research_summary` (`validated_count`, `latest_validated_date`, `latest_published_year`, `reuse_guidance` containing the date).
  - `literature_sources` with zero accepted sources: verifies `validated_count == 0`, `latest_validated_date is None`, and fallback `reuse_guidance`.
  - `literature_entries` join: verifies `source_title`, `source_state`, `source_updated_at`, `source_published_year`, and `dated_research_summary`.
  - `ENGINEERING_METHODOLOGY_RULES` and `_assemble_turn_prompt` contain the dated-research reuse rule.
- Existing `backend/tests/test_hermes_knowledge_read_343.py` passes unchanged.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator review completed, spec status updated, summary written.
