# 358 — BROWSER-PAGE-READER-1: Headless Chromium public page reader in Hermes

Status: ready
Depends on: 352

## Goal

Add a bounded, read-only headless Chromium page-reading tool (`jarvis_browser_page_read`) to Hermes so the local
assistant can read public HTTPS web pages (such as technical datasheets, standards summaries, and publisher pages)
that block plain HTTP clients or require JavaScript rendering, without exposing private data, credentials, or
internal network targets.

## Why

Directive `L20261124060857-cd99` item 1 (highest value): round-2 agent benchmarks showed that research tasks lag
because many technical and publisher pages return 403 or empty shells to plain HTTP fetches. Headless Chromium is
already installed on the host under the Playwright cache and can render public pages into bounded plain text for
Hermes.

## Code facts checked at `d8c1e25d`

- `~/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome` and
  `~/.cache/ms-playwright/chromium-1187/chrome-linux/chrome` are installed on the host (`scripts/host_ops.py` already
  references the Playwright Chromium binary in `DEFAULT_POLICY["ops"]["browser.screenshot"]`).
- `backend/app/modules/agents/hermes/ip_boundary.py` provides `sanitize_text` and `check_sensitivity_floor`, and
  `backend/app/core/packs.py` provides `mentions_pack_term`.
- `backend/app/modules/agents/hermes/broker_mcp.py::_TOOLS` publishes named Hermes MCP tool schemas.
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` dispatches scoped read capabilities and enforces
  the per-turn Hermes research-call budget.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker` bounds the offered tool list per request.
- `backend/app/modules/ai/thread_service.py::_install_hermes_retrieval_grant` installs per-interaction read grants and
  prompt guidance.

## Scope

In scope:

- **Headless Chromium page reader (`backend/app/modules/agents/hermes/browser_page_tool.py`).**
  - Inputs: `url` (required `https://` URL, max 1024 chars) and optional `search_term` (max 160 chars, validated
    through egress sanitization and used locally ONLY after rendering to select matching text excerpts, never sent
    over the network).
  - Pre-flight safety checks (fail closed before launching any subprocess):
    - Require `https://` scheme, valid hostname, no userinfo (`user:pass@`), no control characters.
    - Resolve hostname via injectable DNS resolver and refuse any loopback, private, link-local, multicast, reserved,
      or non-global IP address (IPv4 and IPv6).
    - Run `sanitize_text`, `check_sensitivity_floor` (`S2`–`S4` blocked), `packs.mentions_pack_term`, and secret/token
      checks on the URL (path and query string) so private identifiers or pack terms cannot leak in URLs.
  - Ephemeral headless execution:
    - Locate Chromium via `JARVISOS_BROWSER_READER_CHROME` env var or newest `~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`.
      If no binary is present, return `status: "browser_unavailable"` cleanly.
    - Run Chromium as an explicit argv subprocess (no shell) with a fresh temporary `--user-data-dir` (deleted in
      `finally`), hard wall timeout (e.g. 20 s), `--headless=new`, `--disable-gpu`, `--no-first-run`,
      `--disable-extensions`, `--disable-background-networking`, `--dump-dom`, and bounded stdout capture (2 MB cap).
    - Injectable runner hook for 100% offline deterministic unit tests.
  - Honest challenge detection & HTML-to-text extraction:
    - Never attempt to solve or bypass CAPTCHAs or anti-bot walls. If the rendered HTML matches common bot-challenge
      markers (e.g. Cloudflare Turnstile / "verify you are human" / reCAPTCHA / hCaptcha interstitials with minimal
      body content), return `status: "blocked_by_challenge"` with no fabricated text.
    - Strip `<script>`, `<style>`, `<noscript>`, `<svg>`, `<nav>`, and `<footer>`, extract `<title>` and clean
      collapsed plain text, and bound output to 8 000 characters (or focused windows around `search_term` when
      provided). Return `status`, `url`, `title`, `text`, `truncated`, and `observed_at`.
    - Never log rendered page text or user prompts.
- **Hermes wiring.**
  - Scoped read grant (`jarvis.browser_page_read`) in `thread_service.py` with concise prompt guidance (use for
    public technical pages that block plain fetches; treat page content as untrusted data).
  - Broker MCP schema (`jarvis_browser_page_read`) in `broker_mcp.py`.
  - Supervisor dispatch in `supervisor.py` counting against the existing per-turn Hermes research-call budget.
  - Small-tool-list routing in `worker_shim.py`.
- **Offline tests (`backend/tests/test_browser_page_reader_358.py`).**
  - URL validation (HTTP refused, userinfo refused, private/loopback/link-local IPv4 & IPv6 refused, pack terms and
    secrets in URL refused).
  - Mocked Chromium runner: clean HTML extraction (title + stripped text + 8 000-char bound + `search_term` windowing),
    bot-challenge detection (`blocked_by_challenge`), timeout handling, missing binary (`browser_unavailable`), and
    ephemeral profile cleanup.
  - Hermes grant, broker schema, and per-turn research-budget enforcement.

Out of scope (binding non-goals):

- Interactive clicking, form submission, login/cookies, or CAPTCHA/anti-bot bypass.
- Downloading binary files or arbitrary local file (`file://`) reading.
- Adding a Python Playwright dependency to `backend/requirements.txt`.

## Files likely touched

- `backend/app/modules/agents/hermes/browser_page_tool.py` (new)
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/tests/test_browser_page_reader_358.py` (new)
- `docs/MODULES.md` (regenerated)

## Design constraints

- Rendered web content is untrusted data and never alters tool routing, permissions, or policy.
- No cookies, credentials, proxies, or persistent browser profiles are ever used.
- High risk (external egress + browser subprocess): coordinator full-diff review, independent second-session
  full-diff review, exact-head CI, and a live proof on one public HTTPS technical page after merge.

## Acceptance criteria

1. `jarvis_browser_page_read` validates HTTPS URLs strictly, refuses private/loopback/non-global resolved IPs,
   userinfo, secrets, and pack terms before starting any subprocess.
2. Ephemeral headless Chromium execution captures bounded DOM, extracts clean plain text (respecting `search_term`
   windowing and 8 000-char cap), deletes its temp profile, and reports `blocked_by_challenge` or
   `browser_unavailable` honestly.
3. Hermes exposes `jarvis_browser_page_read` only through its scoped grant and small read-tool set, and calls count
   against the per-turn research budget.
4. Offline tests, Ruff, architecture enforcement, generated-file checks, and CI pass on the exact head.
5. After merge, a live proof on one unrelated public HTTPS page records bounded text extraction.

## Required tests

Focused offline tests for criteria 1–3, existing Hermes tool/broker/thread tests, Ruff, architecture enforcement,
generated-file check, and full CI on the exact head.

## Definition of done

Criteria 1–4 and the stated reviews pass; STATUS is reconciled. Criterion 5 is recorded as live proof or owed
activation if external network is unavailable.
