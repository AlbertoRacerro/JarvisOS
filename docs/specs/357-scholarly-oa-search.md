# 357 — SCHOLARLY-OA-SEARCH-1: OpenAlex, Semantic Scholar, Unpaywall OA lookup and bounded PDF text reading in Hermes

Status: ready
Depends on: 352

## Goal

Extend Jarvis's public scientific literature capability in Hermes so it can search OpenAlex and Semantic Scholar
alongside Europe PMC and Crossref, look up open-access PDF locations for a DOI via Unpaywall, and extract bounded
text excerpts from an open-access PDF without saving private or arbitrary web content.

## Why

Directive `L20261124060857-cd99` item 1 (highest value): round-2 agent benchmarks showed that research tasks (sourced
literature values, methods and quantitative parameters) lagged behind a frontier model's web tools because Jarvis
only searched Europe PMC and Crossref metadata and could not read open-access PDF full text when publisher HTML pages
block plain fetches.

## Code facts checked at `2a6af8a2`

- `backend/app/modules/agents/hermes/paper_search_tool.py::execute_paper_search` and
  `backend/app/modules/agents/hermes/paper_search_tool.py::dispatch_paper_search_tool` enforce query sanitization,
  sensitivity floor (`S2`–`S4` blocked), pack-term blocking, live-token blocking, no redirects and bounded JSON reads.
- `backend/app/modules/agents/hermes/paper_search_tool.py::_safe_fetch_json` performs bounded, no-redirect, no-proxy
  JSON fetches with an injectable opener for offline tests.
- `backend/app/modules/memory/pdf_text.py::read_pdf_pages` already extracts bounded PDF text layers via `pypdfium2`
  without OCR.
- `backend/app/modules/agents/hermes/broker_mcp.py::_TOOLS` publishes named Hermes MCP tool schemas.
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` dispatches scoped read capabilities and enforces the
  per-turn Hermes research-call budget.
- `backend/app/modules/ai/thread_service.py::_install_hermes_retrieval_grant` installs per-interaction read grants and
  prompt guidance.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker` forwards tool calls to the broker and bounds the offered
  tool list.

## Scope

In scope:

- **Expanded scholarly search (`paper_search`).** Extend `paper_search_tool.py` to support `openalex` and
  `semantic_scholar` sources alongside `europe_pmc` and `crossref` (and `all`, which merges and deduplicates by
  normalized DOI / lowercased title across sources within `limit`). Fixed HTTPS endpoints only:
  - OpenAlex: `https://api.openalex.org/works` (reconstruct abstract from `abstract_inverted_index` when present,
    include `oa_pdf_url` from `open_access.oa_url` or `best_oa_location.pdf_url` when HTTPS).
  - Semantic Scholar: `https://api.semanticscholar.org/graph/v1/paper/search` (title, authors, year, venue, DOI,
    abstract, `openAccessPdf.url` when HTTPS).
  - Apply the exact existing egress checks (`sanitize_text`, `check_sensitivity_floor`, pack terms, allowed chars,
    blocked tokens), no redirects, no proxies, timeout and 512 KB JSON byte limit. Source failure degrades gracefully
    to remaining sources rather than crashing the whole call.
- **Bounded OA PDF lookup and text read (`jarvis_paper_pdf_read`).** A new read-only tool taking a canonical `doi`
  (validated strictly as `^10\.\d{4,9}/[^\s<>"{}|\\^`]+$`, max 160 chars; no arbitrary URLs or queries) and optional
  `pages` range (default `1-4`, max 8 pages per call) or optional `search_term` (validated through the same egress
  sanitization, used locally only after download to select matching snippets, never sent over the network):
  - Query Unpaywall (`https://api.unpaywall.org/v2/<encoded-doi>?email=hermes@jarvis.local`) under fixed-host,
    no-redirect, no-proxy, timeout and size limits to discover `best_oa_location.url_for_pdf` (or `oa_locations` PDF).
    If no OA PDF is available, return `status: "no_open_access_pdf"` with metadata and landing URL if present.
  - Download the discovered `https://` PDF URL into a temporary file (deleted in `finally`) with explicit timeout,
    strict byte cap (e.g. 12 MB), `Content-Type` / `%PDF-` magic-header check, at most 3 HTTPS-only redirects
    refusing any private/loopback/link-local/non-global IP target (DNS-checked before connect, proxies disabled).
  - Extract text via `backend/app/modules/memory/pdf_text.py::read_pdf_pages` and return bounded page excerpts
    (capped total characters, e.g. 8 000 chars) with `doi`, `pdf_url`, `page_count`, `returned_pages` and
    `observed_at`. Scanned PDFs with no text layer return `status: "no_text_layer"`.
- **Hermes wiring.** Scoped read grant (`jarvis.paper_pdf_read`), broker MCP schema (`jarvis_paper_pdf_read`),
  supervisor dispatch, prompt guidance in `thread_service.py`, small-tool-list routing in `worker_shim.py`, and
  counting against the existing per-turn Hermes research-call budget.
- **Offline tests.** Synthetic JSON and tiny real-PDF-bytes fixtures covering OpenAlex (including inverted-index
  abstract reconstruction), Semantic Scholar, deduplication, graceful partial-source failure, Unpaywall OA hit vs
  closed-access miss, malformed DOI refusal, private-IP/HTTP/oversize/non-PDF refusal, `pypdfium2` page extraction
  bounds, and Hermes grant/budget enforcement.

Out of scope (binding non-goals):

- Arbitrary URL fetching, publisher HTML scraping or headless browser rendering (separate spec).
- OCR for scanned PDFs, paywall bypass, or paid API keys.
- Storing fetched PDFs permanently in workspace literature records automatically (explicit user upload remains the
  owner of workspace literature files).

## Files likely touched

- `backend/app/modules/agents/hermes/paper_search_tool.py`
- `backend/app/modules/agents/hermes/paper_pdf_tool.py` (new)
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/ai/thread_service.py`
- `configs/architecture_enforcement.json` (only if a new bounded fetch function requires an `accepted_owner` AE002 entry)
- `backend/tests/test_scholarly_oa_search_357.py` (new)
- `docs/MODULES.md` (regenerated)

## Design constraints

- Source text and PDF text are untrusted data and never alter tool routing or policy.
- Never send private pack terms, secrets or arbitrary text to external APIs; `jarvis_paper_pdf_read` sends only a
  strictly validated DOI to Unpaywall and fetches only the returned public HTTPS OA PDF (never private/loopback IPs).
- Never log extracted PDF text or user prompts.
- High risk (external egress + PDF parsing): coordinator full-diff review, independent second-session full-diff
  review, exact-head CI, and a live proof on one public OA DOI after merge.

## Acceptance criteria

1. `paper_search` queries OpenAlex, Semantic Scholar, Europe PMC and Crossref (individually or `all` with DOI/title
   deduplication), reconstructs OpenAlex inverted-index abstracts, and degrades cleanly if one source fails.
2. Existing egress checks block secrets, pack terms, paths, URLs and live tokens before any network request.
3. `jarvis_paper_pdf_read` validates DOIs strictly, resolves OA PDFs via Unpaywall, refuses non-HTTPS, private/loopback
   targets, non-PDF bodies and oversized payloads, extracts bounded page text via `read_pdf_pages`, and reports
   `no_open_access_pdf` or `no_text_layer` honestly.
4. Hermes exposes `jarvis_paper_pdf_read` only through its scoped grant and small read-tool set, and both tools count
   against the per-turn research budget.
5. After merge, a live proof on one unrelated public OA DOI records Unpaywall lookup and bounded PDF text extraction.

## Required tests

Focused offline tests for criteria 1–4, existing Hermes tool/broker/thread tests, Ruff, architecture enforcement,
generated-file check and full CI on the exact head.

## Definition of done

Criteria 1–4 and the stated reviews pass; STATUS is reconciled. Criterion 5 is recorded as live proof or owed
activation if external network is unavailable.
