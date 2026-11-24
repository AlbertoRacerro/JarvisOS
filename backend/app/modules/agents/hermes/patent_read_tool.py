"""Bounded, read-only patent publication lookup tool (Google Patents & EPO Register).

Per Spec 356 (PATENT-RECORD-READ-1):
Looks up a public patent publication by canonical publication number from Google Patents
(https://patents.google.com/patent/<number>/en) and attempts the official register for
European (EP) publications (https://register.epo.org).
Extracts compact, sourced records while bounding all fields and failing closed on invalid
numbers, redirects, timeouts, oversize responses, off-host links, or source challenges.
If the official register cannot be read, it explicitly leaves official_status as 'unverified'
and never turns Google's legal-status label into a verified legal conclusion.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.modules.agents.hermes.ip_boundary import (
    _KNOWN_SECRET_RE,
    check_sensitivity_floor,
    sanitize_text,
)
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall

GOOGLE_PATENTS_BASE_URL = "https://patents.google.com/patent"
EPO_REGISTER_BASE_HOST = "register.epo.org"

# Canonical patent identifier: 2 uppercase country letters, 5-18 digits, optional kind letter with optional digit
PATENT_ID_RE = re.compile(r"^[A-Z]{2}[0-9]{5,18}(?:[A-Z][0-9]?)?$")

MAX_RESPONSE_BYTES = 512 * 1024  # 512 KB
MAX_TITLE_CHARS = 500
MAX_CLAIM_CHARS = 2000
MAX_STATUS_CHARS = 100
MAX_APPLICANTS = 20
MAX_APPLICANT_CHARS = 200
MAX_EVENTS = 20
DEFAULT_TIMEOUT_SECONDS = 10.0

GOOGLE_STATUS_DISCLAIMER = (
    "Google Patents legal status is an unverified advisory label, not official legal status."
)

_XML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_WS_RE = re.compile(r"\s+")
_EPO_APP_PARAM_RE = re.compile(r"^(?:EP)?[0-9]{7,12}(?:\.[0-9])?$")
_SAFE_URL_PATH_QUERY_RE = re.compile(r"^[A-Za-z0-9\-_\./\?=&]+$")


class _DenyRedirects(HTTPRedirectHandler):
    """Deny all HTTP redirects to avoid SSRF and protocol escapes."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_DEFAULT_OPENER: Any = None


def set_default_opener(opener: Any) -> None:
    """Set process-wide default opener for patent lookup (e.g. for offline test stubs)."""
    global _DEFAULT_OPENER
    _DEFAULT_OPENER = opener


@contextmanager
def override_patent_read_opener(opener: Any):
    """Context manager to temporarily override patent lookup opener."""
    global _DEFAULT_OPENER
    prev = _DEFAULT_OPENER
    _DEFAULT_OPENER = opener
    try:
        yield
    finally:
        _DEFAULT_OPENER = prev


def _get_opener(custom_opener: Any = None) -> Any:
    if custom_opener is not None:
        return custom_opener
    if _DEFAULT_OPENER is not None:
        return _DEFAULT_OPENER
    return build_opener(ProxyHandler({}), _DenyRedirects())


def validate_publication_number(raw: Any) -> str | None:
    """Validate canonical publication number: exactly 2 uppercase letters, 5-18 digits, optional kind code.

    Rejects whitespace, words, URLs, query strings, lowercase, or any other non-canonical input.
    """
    if not isinstance(raw, str):
        return None
    if not PATENT_ID_RE.match(raw):
        return None
    return raw


def validate_epo_register_url(raw_url: str) -> str | None:
    """Validate an EPO Register link extracted from Google Patents HTML.

    Must be https scheme, strict register.epo.org host, port None or 443, safe characters,
    and have a syntactically valid application identifier.
    """
    if not raw_url or not isinstance(raw_url, str):
        return None
    clean_url = raw_url.strip()
    try:
        parsed = urlsplit(clean_url)
    except ValueError:
        return None

    if parsed.scheme != "https":
        return None
    if parsed.hostname != EPO_REGISTER_BASE_HOST:
        return None
    if parsed.port not in (None, 443):
        return None

    full_path_query = parsed.path + ("?" + parsed.query if parsed.query else "")
    if not _SAFE_URL_PATH_QUERY_RE.match(full_path_query):
        return None

    # Syntactic application identifier check
    query_params = parse_qs(parsed.query)
    app_id_found = False
    for param_name in ("number", "ap", "appNumber", "application"):
        if param_name in query_params:
            for val in query_params[param_name]:
                if _EPO_APP_PARAM_RE.match(val):
                    app_id_found = True
                    break
            if app_id_found:
                break

    if not app_id_found:
        # Check path for embedded application identifier (e.g. /application/EP1234567 or /ipfwretrieve?ap=...)
        path_parts = [p for p in parsed.path.split("/") if p]
        for part in path_parts:
            if _EPO_APP_PARAM_RE.match(part):
                app_id_found = True
                break

    if not app_id_found:
        return None

    return clean_url


def _is_cloudflare_challenge(text: str) -> bool:
    """Detect Cloudflare anti-bot challenge or verification screen."""
    if not text:
        return False
    lower = text.lower()
    markers = (
        "just a moment...",
        "cf-browser-verification",
        "turnstile",
        "attention required! | cloudflare",
        "checking your browser before accessing",
        "ray id:",
    )
    return any(marker in lower for marker in markers)


def _clean_text(raw: str | None, max_chars: int) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = _XML_TAG_RE.sub(" ", raw)
    unescaped = html.unescape(cleaned)
    normalized = _WS_RE.sub(" ", unescaped).strip()
    if not normalized:
        return None
    if len(normalized) > max_chars:
        return normalized[:max_chars].rstrip() + "..."
    return normalized


def _safe_fetch_html(
    url: str,
    *,
    opener: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int | None, str | None, str | None]:
    """Fetch URL with strict bounds: no redirects, no proxies, timeout and response size limits.

    Returns (status_code, body_text, error_code).
    """
    active_opener = _get_opener(opener)
    req = Request(
        url,
        headers={
            "User-Agent": "JarvisOS/1.0 (patent-read; mailto:hermes@jarvis.local)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with active_opener.open(req, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        return None, None, "oversize_response"
                except ValueError:
                    return None, None, "oversize_response"

            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return None, None, "oversize_response"

            text = body.decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
            return status, text, None
    except HTTPError as exc:
        if exc.code == 404:
            return 404, None, "not_found"
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, None, "redirect_blocked"
        if exc.code == 403:
            return 403, None, "http_403_challenge"
        return exc.code, None, f"http_error_{exc.code}"
    except (URLError, TimeoutError, OSError):
        return None, None, "network_error"
    except Exception:
        return None, None, "source_unavailable"


def parse_google_patents_html(html_text: str, publication_number: str) -> dict[str, Any]:
    """Parse Google Patents HTML into extracted fields; missing fields stay explicitly 'unknown' or []."""
    # 1. Title
    title: str | None = None
    title_matches = [
        re.search(r'<meta\s+name=["\']DC\.title["\']\s+content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']DC\.title["\']', html_text, re.I),
        re.search(r'<meta\s+name=["\']title["\']\s+content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<span\s+id=["\']title["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
        re.search(r'<span\s+itemprop=["\']title["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
        re.search(r'<title>(.*?)(?: - Google Patents)?</title>', html_text, re.I | re.S),
    ]
    for tm in title_matches:
        if tm:
            candidate = _clean_text(tm.group(1), MAX_TITLE_CHARS)
            if candidate:
                title = candidate
                break

    # 2. Applicants / Assignees
    applicants: list[str] = []
    seen_applicants: set[str] = set()

    contributor_matches = re.findall(
        r'<meta\s+name=["\']DC\.contributor["\'][^>]*content=["\']([^"\']+)["\']',
        html_text,
        re.I,
    )
    for c in contributor_matches:
        cleaned_c = _clean_text(c, MAX_APPLICANT_CHARS)
        if cleaned_c and cleaned_c not in seen_applicants:
            seen_applicants.add(cleaned_c)
            applicants.append(cleaned_c)
            if len(applicants) >= MAX_APPLICANTS:
                break

    if len(applicants) < MAX_APPLICANTS:
        elem_matches = re.findall(
            r'<(?:dd|span)\s+itemprop=["\'](?:assigneeSearch|applicantSearch|assignee|applicant)["\'][^>]*>(.*?)</(?:dd|span)>',
            html_text,
            re.I | re.S,
        )
        for em in elem_matches:
            cleaned_em = _clean_text(em, MAX_APPLICANT_CHARS)
            if cleaned_em and cleaned_em not in seen_applicants:
                seen_applicants.add(cleaned_em)
                applicants.append(cleaned_em)
                if len(applicants) >= MAX_APPLICANTS:
                    break

    # 3. Priority date
    priority_date: str | None = None
    p_matches = [
        re.search(r'<meta\s+name=["\']DC\.date["\'][^>]*scheme=["\']priority["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+scheme=["\']priority["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<time\s+itemprop=["\']priorityDate["\'][^>]*datetime=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+name=["\']priority_date["\']\s+content=["\']([^"\']+)["\']', html_text, re.I),
    ]
    for pm in p_matches:
        if pm:
            candidate = _clean_text(pm.group(1), 32)
            if candidate:
                priority_date = candidate
                break

    # 4. Filing date
    filing_date: str | None = None
    f_matches = [
        re.search(r'<meta\s+name=["\']DC\.date["\'][^>]*scheme=["\']dateSubmitted["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+scheme=["\']dateSubmitted["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<time\s+itemprop=["\']filingDate["\'][^>]*datetime=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+name=["\']filing_date["\']\s+content=["\']([^"\']+)["\']', html_text, re.I),
    ]
    for fm in f_matches:
        if fm:
            candidate = _clean_text(fm.group(1), 32)
            if candidate:
                filing_date = candidate
                break

    # 5. Publication date
    publication_date: str | None = None
    pub_matches = [
        re.search(r'<meta\s+name=["\']DC\.date["\'][^>]*scheme=["\']issue["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+name=["\']DC\.date["\'][^>]*scheme=["\']publication["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+scheme=["\'](?:issue|publication)["\'][^>]*content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<time\s+itemprop=["\']publicationDate["\'][^>]*datetime=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<meta\s+name=["\']publication_date["\']\s+content=["\']([^"\']+)["\']', html_text, re.I),
    ]
    for pubm in pub_matches:
        if pubm:
            candidate = _clean_text(pubm.group(1), 32)
            if candidate:
                publication_date = candidate
                break

    # 6. Google legal status
    google_legal_status: str | None = None
    status_matches = [
        re.search(r'<span\s+itemprop=["\']legalStatus["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
        re.search(r'<dd\s+itemprop=["\']legalStatus["\'][^>]*>(.*?)</dd>', html_text, re.I | re.S),
        re.search(r'<span\s+class=["\'][^"\']*legal-status[^"\']*["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
        re.search(r'<meta\s+name=["\']legal-status["\']\s+content=["\']([^"\']+)["\']', html_text, re.I),
        re.search(r'<span\s+itemprop=["\']status["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
    ]
    for sm in status_matches:
        if sm:
            candidate = _clean_text(sm.group(1), MAX_STATUS_CHARS)
            if candidate:
                google_legal_status = candidate
                break

    # 7. Claim 1 (first independent claim)
    claim_1: str | None = None
    claim_matches = [
        re.search(r'<div\s+class=["\']claim["\'][^>]*num=["\']1["\'][^>]*>(.*?)</div>', html_text, re.I | re.S),
        re.search(r'<div\s+itemprop=["\']claim["\'][^>]*num=["\']1["\'][^>]*>(.*?)</div>', html_text, re.I | re.S),
        re.search(r'<div\s+class=["\']claim-text["\'][^>]*>(.*?)</div>', html_text, re.I | re.S),
        re.search(r'<section\s+itemprop=["\']claims["\'][^>]*>.*?<div\s+class=["\']claim["\'][^>]*>(.*?)</div>', html_text, re.I | re.S),
        re.search(r'<claim\s+id=["\'][^"\']*1["\'][^>]*>(.*?)</claim>', html_text, re.I | re.S),
    ]
    for clm in claim_matches:
        if clm:
            candidate = _clean_text(clm.group(1), MAX_CLAIM_CHARS)
            if candidate:
                claim_1 = candidate
                break

    # 8. EPO Register link (if present)
    epo_link: str | None = None
    anchor_matches = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_text, re.I | re.S)
    for href, text in anchor_matches:
        href_lower = href.lower()
        text_lower = text.lower()
        if EPO_REGISTER_BASE_HOST in href_lower:
            epo_link = href
            break
        if ("epo" in href_lower or "register" in href_lower or "epo" in text_lower or "register" in text_lower):
            if not epo_link:
                epo_link = href

    return {
        "title": title or "unknown",
        "applicants": applicants,
        "priority_date": priority_date or "unknown",
        "filing_date": filing_date or "unknown",
        "publication_date": publication_date or "unknown",
        "google_legal_status": google_legal_status or "unknown",
        "claim_1": claim_1 or "unknown",
        "epo_link": epo_link,
    }


def parse_epo_register_html(html_text: str) -> tuple[str | None, list[dict[str, str]], str | None]:
    """Parse EPO Register HTML into (official_status, dated_events, error_category)."""
    if _is_cloudflare_challenge(html_text):
        return "unverified", [], "challenge_detected"

    # Status
    status: str | None = None
    status_matches = [
        re.search(r'<span\s+id=["\']status["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
        re.search(r'<td\s+class=["\']status["\'][^>]*>(.*?)</td>', html_text, re.I | re.S),
        re.search(r'<div\s+class=["\'][^"\']*epoline_status[^"\']*["\'][^>]*>(.*?)</div>', html_text, re.I | re.S),
        re.search(r'<span\s+class=["\'][^"\']*epoline_status[^"\']*["\'][^>]*>(.*?)</span>', html_text, re.I | re.S),
        re.search(r'Status:\s*</td>\s*<td[^>]*>(.*?)</td>', html_text, re.I | re.S),
        re.search(r'<dt>\s*Status\s*</dt>\s*<dd[^>]*>(.*?)</dd>', html_text, re.I | re.S),
    ]
    for sm in status_matches:
        if sm:
            candidate = _clean_text(sm.group(1), MAX_STATUS_CHARS)
            if candidate:
                status = candidate
                break

    # Dated events
    events: list[dict[str, str]] = []
    # Match table rows: date cell followed by description cell
    row_matches = re.findall(
        r'<tr[^>]*>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td[^>]*>(.*?)</td>\s*</tr>',
        html_text,
        re.I | re.S,
    )
    for date_raw, desc_raw in row_matches[:MAX_EVENTS]:
        cleaned_date = _clean_text(date_raw, 32)
        cleaned_desc = _clean_text(desc_raw, 256)
        if cleaned_date and cleaned_desc:
            events.append({"date": cleaned_date, "event": cleaned_desc})

    if not status:
        return "unverified", events, "changed_markup"

    return status, events, None


def execute_patent_read(
    publication_number: str,
    *,
    opener: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    blocked_tokens: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Execute bounded public patent publication lookup across Google Patents and EPO Register."""
    # 1. Canonical validation: must reject whitespace, words, lowercase, URLs, query strings, etc.
    canonical_number = validate_publication_number(publication_number)
    if not canonical_number:
        return {
            "error": "invalid_arguments",
            "status": "error",
            "message": "Malformed publication number. Expected 2 uppercase letters followed by 5-18 digits and optional kind code.",
        }

    # Egress check: fail closed on secrets and private content
    if _KNOWN_SECRET_RE.search(canonical_number):
        return {"error": "egress_blocked_private_content", "status": "error"}
    if sanitize_text(canonical_number) != canonical_number:
        return {"error": "egress_blocked_private_content", "status": "error"}
    if check_sensitivity_floor(canonical_number) in {"S2", "S3", "S4"}:
        return {"error": "egress_blocked_private_content", "status": "error"}

    if blocked_tokens:
        for token in blocked_tokens:
            if token and token == canonical_number:
                return {"error": "egress_blocked_private_content", "status": "error"}

    # 2. Fetch Google Patents
    google_url = f"{GOOGLE_PATENTS_BASE_URL}/{canonical_number}/en"
    observed_at = datetime.now(UTC).isoformat()

    status_code, body_text, error_code = _safe_fetch_html(google_url, opener=opener, timeout=timeout)

    if error_code == "not_found" or status_code == 404:
        return {
            "error": "not_found",
            "status": "not_found",
            "publication_number": canonical_number,
            "source_url": google_url,
            "observed_at": observed_at,
        }

    if error_code is not None or status_code != 200 or not body_text:
        return {
            "error": "source_unavailable",
            "status": "source_unavailable",
            "detail": error_code or "http_status_not_ok",
            "publication_number": canonical_number,
            "source_url": google_url,
            "observed_at": observed_at,
        }

    if _is_cloudflare_challenge(body_text):
        return {
            "error": "source_unavailable",
            "status": "source_unavailable",
            "detail": "challenge_detected",
            "publication_number": canonical_number,
            "source_url": google_url,
            "observed_at": observed_at,
        }

    # 3. Parse Google Patents HTML
    parsed_google = parse_google_patents_html(body_text, canonical_number)

    # 4. Official register lookup for EP publications
    is_ep = canonical_number.startswith("EP")
    official_status: str | None = None
    official_status_error: str | None = None
    register_url: str | None = None
    dated_events: list[dict[str, str]] = []

    if is_ep:
        raw_epo_link = parsed_google.get("epo_link")
        if not raw_epo_link:
            official_status = "unverified"
            official_status_error = "no_register_link_found"
        else:
            validated_epo_url = validate_epo_register_url(raw_epo_link)
            if not validated_epo_url:
                official_status = "unverified"
                official_status_error = "off_host_link_rejected"
            else:
                register_url = validated_epo_url
                epo_status_code, epo_body, epo_error = _safe_fetch_html(
                    validated_epo_url, opener=opener, timeout=timeout
                )
                if epo_status_code == 403 or epo_error == "http_403_challenge":
                    official_status = "unverified"
                    official_status_error = "http_403_challenge"
                elif epo_error is not None or epo_status_code != 200 or not epo_body:
                    official_status = "unverified"
                    official_status_error = epo_error or "source_unavailable"
                else:
                    parsed_status, parsed_events, parse_err = parse_epo_register_html(epo_body)
                    official_status = parsed_status
                    dated_events = parsed_events
                    official_status_error = parse_err

    return {
        "status": "succeeded",
        "publication_number": canonical_number,
        "title": parsed_google["title"],
        "applicants": parsed_google["applicants"],
        "priority_date": parsed_google["priority_date"],
        "filing_date": parsed_google["filing_date"],
        "publication_date": parsed_google["publication_date"],
        "google_legal_status": parsed_google["google_legal_status"],
        "google_legal_status_disclaimer": GOOGLE_STATUS_DISCLAIMER,
        "claim_1": parsed_google["claim_1"],
        "source_url": google_url,
        "observed_at": observed_at,
        "official_status": official_status,
        "official_status_error": official_status_error,
        "register_url": register_url,
        "dated_events": dated_events,
    }


def dispatch_patent_read_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
    *,
    grant: CapabilityGrantRef | None = None,
    opener: Any = None,
    live_grants: dict[str, CapabilityGrantRef] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Broker dispatch for jarvis.patent_read capability."""
    raw_args = call.arguments
    if not isinstance(raw_args, dict):
        return None, "invalid_arguments"

    publication_number = raw_args.get("publication_number")
    if not isinstance(publication_number, str) or not PATENT_ID_RE.match(publication_number):
        return None, "invalid_arguments"

    # Collect blocked private tokens: live grants, session, thread, workspace
    blocked_tokens: set[str] = set()
    if workspace_id:
        blocked_tokens.add(workspace_id)
    if grant and grant.grant_id:
        blocked_tokens.add(grant.grant_id)
    if call.grant_id:
        blocked_tokens.add(call.grant_id)
    if call.session_ref:
        if call.session_ref.workspace_id:
            blocked_tokens.add(call.session_ref.workspace_id)
        if call.session_ref.jarvis_thread_id:
            blocked_tokens.add(call.session_ref.jarvis_thread_id)
        if call.session_ref.hermes_session_id:
            blocked_tokens.add(call.session_ref.hermes_session_id)
    if live_grants:
        for gid, g in live_grants.items():
            if gid:
                blocked_tokens.add(gid)
            if hasattr(g, "grant_id") and g.grant_id:
                blocked_tokens.add(g.grant_id)

    result = execute_patent_read(
        publication_number,
        opener=opener,
        blocked_tokens=blocked_tokens,
    )
    if result.get("error"):
        if result["error"] == "egress_blocked_private_content":
            return None, "egress_pack_blocked"
        return None, result["error"]

    return result, default_error
