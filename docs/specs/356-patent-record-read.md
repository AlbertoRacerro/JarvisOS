# 356 — PATENT-RECORD-READ-1: Bounded patent publication lookup in Hermes

Status: ready
Depends on: 352

## Goal

Jarvis can look up a public patent publication by its number and return a compact, sourced record. It reads the
publication page on Google Patents and attempts the linked official register for European publications. If the official
register cannot be read, it says so and does not turn Google's legal-status label into a verified legal conclusion.

## Why

Directive `L20261124060857-cd99` item 1: the agent bench found patent fact checks weaker inside Jarvis than with a
frontier model's web tools. A publication-number lookup is useful without sending any private design description to a
search service. A host probe on 2026-11-24 received HTTP 200 from Google Patents and a Cloudflare HTTP 403 challenge
from the EPO Register; the tool must represent that source failure honestly.

## Code facts checked at `49f12981`

- `backend/app/modules/agents/hermes/paper_search_tool.py::dispatch_paper_search_tool` validates arguments and live
  grant tokens before bounded external reads; `execute_paper_search` applies the existing egress checks.
- `backend/app/modules/agents/hermes/broker_mcp.py::_TOOLS` publishes the named tool schema.
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` dispatches the scoped `jarvis.paper_search`
  capability; `HermesSupervisor.turn` owns the per-turn research budget.
- `backend/app/modules/ai/thread_service.py::_install_hermes_retrieval_grant` grants read tools per interaction and supplies the
  tool guidance; `backend/app/modules/agents/hermes/worker_shim.py` limits the offered tool list.
- `backend/app/modules/agents/hermes/ip_boundary.py::sanitize_text` and `check_sensitivity_floor` are existing
  sensitivity checks; patent lookup can narrow input further to a publication number.

## Scope

In scope:

- A read-only `jarvis_patent_read` tool with a required `publication_number`. Accept only a canonical public patent
  identifier of two uppercase country letters, 5–18 digits and an optional uppercase kind letter with optional digit; reject
  whitespace, words, URLs, query strings and all other input. Send only this canonical number to fixed hosts.
- Fetch `https://patents.google.com/patent/<number>/en` with no redirects, fixed host and path, timeout and response
  byte limits. Extract only publication number, title, applicants/assignees, priority date, filing date, publication
  date, Google legal-status label with its disclaimer, first independent claim text when identifiable, and source URL.
  Return `not_found` or `source_unavailable` rather than inventing a field. Bound each text field and the total result.
- For an EP publication, obtain an EPO Register link from the Google page only when its parsed host is exactly
  `register.epo.org` and its application identifier is syntactically valid; attempt to read it under the same timeout,
  redirect and size limits. Extract only an official status and dated events that can be identified reliably. On a
  challenge, 403, changed markup or ambiguous record, return `official_status: unverified` with the register link and
  an error category. Never claim that a grant is in force in a country from the publication kind or Google's label.
- A separate scoped read grant, broker schema, dispatch, prompt guidance and small-tool-list routing. Patent reads
  count against the existing Hermes research-call budget. No new provider route or AI job type.
- Offline tests with fixed synthetic HTML for the two source parsers, blocked redirects/oversize/timeout, malformed
  numbers, 404 and 403, and a forged EPO link. No real patent claims or private project terms in fixtures.

Out of scope:

- A freedom-to-operate conclusion, national validation or unitary-effect verdict, automatic expiry calculation, or
  legal advice. Those require official current records and human review.
- General web browsing, arbitrary URLs, search by concept text, PDF parsing, or a browser challenge bypass.
- EPO OPS credentials or paid services.

## Design constraints

- Source text is untrusted data and cannot give instructions to the model or change tool routing.
- Never log the returned claim text or a user prompt. No private pack terms or design examples in public fixtures.
- High risk because this adds external egress and source parsing: full coordinator diff review, independent second
  session review, exact-head CI and a live proof on unrelated public publications.

## Acceptance criteria

1. A valid publication number returns the available fields with source URL and observed-at time; missing fields remain
   explicitly unknown. A malformed input sends no request.
2. A fixed-host fetch rejects redirects, oversized bodies and off-host EPO links. A 404 is `not_found`; network,
   challenge and parser failures are `source_unavailable` or `unverified`, never a positive status.
3. An EP record carries the Google label separately from the official status. A 403 EPO response leaves official
   status unverified; a parseable official fixture supplies a dated official status with its link.
4. Hermes exposes the tool only through its scoped grant and small read-tool set. The existing per-turn research budget
   applies and all prior egress, scope and token guards stay green.
5. A live proof after merge on two unrelated public publications, one EP and one US, records source reachability and
   output categories. If the EPO Register still challenges the host, the proof records unverified status.

## Required tests

Focused offline tests for criteria 1–4, existing Hermes tool/broker/thread tests, Ruff, architecture enforcement,
generated-file check and full CI on the exact head. The live proof is separate from CI.

## Definition of done

Criteria 1–4 and the stated reviews pass; STATUS is reconciled. Criterion 5 is recorded as live proof or as owed
activation if the host cannot reach a source. A source challenge never becomes a claim of verified legal status.
