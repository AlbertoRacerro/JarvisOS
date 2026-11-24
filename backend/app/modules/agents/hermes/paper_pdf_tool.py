"""Bounded, read-only open-access PDF lookup and text extraction tool.

Per Spec 357:
Resolves canonical DOIs via Unpaywall to discover open-access PDF URLs, downloads
the discovered HTTPS PDF under strict safety bounds (DNS check against private/local IPs,
max 3 HTTPS redirects, magic %PDF- validation, 12 MB byte cap), and extracts bounded page
excerpts via read_pdf_pages without storing permanent files or logging text.
Supports an injectable opener and DNS resolver for 100% offline testing.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.modules.agents.hermes.ip_boundary import (
    _KNOWN_SECRET_RE,
    check_sensitivity_floor,
    sanitize_text,
)
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall
from app.modules.memory.pdf_text import PdfTextError, read_pdf_pages

UNPAYWALL_BASE_URL = "https://api.unpaywall.org/v2"
UNPAYWALL_EMAIL = "hermes@jarvis.local"

MAX_RESPONSE_BYTES = 512 * 1024  # 512 KB
MAX_PDF_BYTES = 12 * 1024 * 1024  # 12 MB
MAX_EXTRACTED_CHARS = 8000
MAX_DOI_CHARS = 160
MAX_SEARCH_TERM_CHARS = 200
MAX_PAGES_COUNT = 8
DEFAULT_PAGES = "1-4"
MAX_REDIRECTS = 3

DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s<>'\"{}|\\^`]+$")

_UUID_OR_HEX_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}"
)
_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
_PATH_RE = re.compile(
    r"(?:^|[\s\"'(\[])("
    r"/(?:[a-zA-Z0-9._-]+/)*[a-zA-Z0-9._-]+"
    r"|(?:\.\.|\.)/[a-zA-Z0-9._/-]+"
    r"|~/[a-zA-Z0-9._/-]+"
    r"|[a-zA-Z]:[\\/][a-zA-Z0-9._\\-]+"
    r"|[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+\.(?:py|json|md|txt|sh|ts|js|html|css|toml|yaml|yml|csv|xml|pdf|doc|docx|log|env|conf|ini|cfg)"
    r"|(?:[a-zA-Z0-9._-]+/){2,}[a-zA-Z0-9._-]+"
    r")"
)
_ALLOWED_CHARS_RE = re.compile(
    r"^[a-zA-Z0-9\s\-\.,:;\(\)'\"/\+\u00C0-\u024F\u0370-\u03FF\u1F00-\u1FFF]+$"
)


class _DenyRedirects(HTTPRedirectHandler):
    """Deny all HTTP redirects to avoid SSRF and protocol escapes."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_DEFAULT_OPENER: Any = None
_DEFAULT_DNS_RESOLVER: Callable[[str], list[str]] | None = None


def set_default_opener(opener: Any) -> None:
    """Set process-wide default opener for paper PDF tool (e.g. for offline test stubs)."""
    global _DEFAULT_OPENER
    _DEFAULT_OPENER = opener


@contextmanager
def override_paper_pdf_opener(opener: Any):
    """Context manager to temporarily override paper PDF opener."""
    global _DEFAULT_OPENER
    prev = _DEFAULT_OPENER
    _DEFAULT_OPENER = opener
    try:
        yield
    finally:
        _DEFAULT_OPENER = prev


def set_default_dns_resolver(resolver: Callable[[str], list[str]] | None) -> None:
    """Set process-wide default DNS resolver (e.g. for offline test stubs)."""
    global _DEFAULT_DNS_RESOLVER
    _DEFAULT_DNS_RESOLVER = resolver


@contextmanager
def override_paper_pdf_dns_resolver(resolver: Callable[[str], list[str]] | None):
    """Context manager to temporarily override DNS resolver."""
    global _DEFAULT_DNS_RESOLVER
    prev = _DEFAULT_DNS_RESOLVER
    _DEFAULT_DNS_RESOLVER = resolver
    try:
        yield
    finally:
        _DEFAULT_DNS_RESOLVER = prev


def _get_opener(custom_opener: Any = None) -> Any:
    if custom_opener is not None:
        return custom_opener
    if _DEFAULT_OPENER is not None:
        return _DEFAULT_OPENER
    return build_opener(ProxyHandler({}), _DenyRedirects())


def _default_dns_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def is_safe_global_ip(ip_str: str) -> bool:
    """Refuse any private, loopback, link-local, multicast, reserved or non-global IP target."""
    try:
        ip = ipaddress.ip_address(ip_str.strip())
        if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or not ip.is_global
        ):
            return False
        return True
    except ValueError:
        return False


def is_safe_global_host(
    hostname: str,
    *,
    dns_resolver: Callable[[str], list[str]] | None = None,
) -> bool:
    """Resolve and verify hostname maps strictly to public global IP addresses."""
    if not hostname:
        return False
    h = hostname.strip().lower()
    if h in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(h)
        return is_safe_global_ip(str(ip))
    except ValueError:
        pass

    resolver = dns_resolver or _DEFAULT_DNS_RESOLVER or _default_dns_resolver
    try:
        ips = resolver(h)
        if not ips:
            return False
        for resolved_ip in ips:
            if not is_safe_global_ip(resolved_ip):
                return False
        return True
    except (socket.gaierror, OSError, ValueError):
        return False


def validate_doi(doi: str) -> bool:
    """Strictly validate canonical DOI (starts with 10., restricted chars, max 160 chars)."""
    cleaned = doi.strip()
    if not cleaned or len(cleaned) > MAX_DOI_CHARS:
        return False
    return bool(DOI_RE.match(cleaned))


def parse_pages_range(pages_spec: str | None) -> tuple[int, int] | None:
    """Parse pages string (e.g. '1-4' or '3') ensuring 1-based, start<=end, and count<=8."""
    if not pages_spec:
        return (1, 4)
    spec = pages_spec.strip()
    if "-" in spec:
        parts = spec.split("-", 1)
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())
        except ValueError:
            return None
        if start < 1 or end < start or (end - start + 1) > MAX_PAGES_COUNT:
            return None
        return (start, end)
    else:
        try:
            val = int(spec)
        except ValueError:
            return None
        if val < 1:
            return None
        return (val, val)


def validate_search_term(
    term: str,
    *,
    blocked_tokens: Iterable[str] | None = None,
) -> bool:
    """Apply full egress sanitization to search_term (fails closed on secrets/private pack terms)."""
    cleaned = term.strip()
    if not cleaned or len(cleaned) > MAX_SEARCH_TERM_CHARS:
        return False
    if _KNOWN_SECRET_RE.search(cleaned):
        return False
    if sanitize_text(cleaned) != cleaned:
        return False
    if check_sensitivity_floor(cleaned) in {"S2", "S3", "S4"}:
        return False
    if not _ALLOWED_CHARS_RE.match(cleaned):
        return False
    if _UUID_OR_HEX_RE.search(cleaned):
        return False
    if _URL_RE.search(cleaned):
        return False
    if _EMAIL_RE.search(cleaned):
        return False
    if _PATH_RE.search(cleaned):
        return False
    if blocked_tokens:
        for token in blocked_tokens:
            if token and token in cleaned:
                return False
    return True


def _safe_fetch_json(url: str, *, opener: Any = None, timeout: float = 10.0) -> dict[str, Any] | None:
    """Fetch URL ensuring no redirects and bounded response size; fails closed."""
    active_opener = _get_opener(opener)
    req = Request(
        url,
        headers={
            "User-Agent": "JarvisOS/1.0 (mailto:hermes@jarvis.local)",
            "Accept": "application/json",
        },
    )
    try:
        with active_opener.open(req, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        return None
                except ValueError:
                    return None
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return None
            return json.loads(body.decode("utf-8", errors="replace"))
    except (HTTPError, URLError, ValueError, TimeoutError, OSError):
        return None


def lookup_unpaywall(
    doi: str,
    *,
    opener: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Query Unpaywall REST API for open access locations for a canonical DOI."""
    encoded_doi = quote(doi, safe="")
    url = f"{UNPAYWALL_BASE_URL}/{encoded_doi}?email={UNPAYWALL_EMAIL}"
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.hostname != "api.unpaywall.org":
        return None
    return _safe_fetch_json(url, opener=opener, timeout=timeout)


def download_pdf(
    url: str,
    target_path: Path,
    *,
    opener: Any = None,
    dns_resolver: Callable[[str], list[str]] | None = None,
    timeout: float = 20.0,
) -> tuple[bool, str | None, str | None]:
    """Download HTTPS PDF to target_path with safe DNS validation, max 3 redirects, magic header check, and byte cap."""
    active_opener = _get_opener(opener)
    current_url = url
    redirects = 0

    while True:
        parsed = urlsplit(current_url)
        if parsed.scheme.lower() != "https":
            return False, "refused_non_https", current_url
        hostname = parsed.hostname
        if not hostname:
            return False, "invalid_url", current_url
        if not is_safe_global_host(hostname, dns_resolver=dns_resolver):
            return False, "refused_private_or_local_target", current_url

        req = Request(
            current_url,
            headers={
                "User-Agent": "JarvisOS/1.0 (mailto:hermes@jarvis.local)",
                "Accept": "application/pdf,application/octet-stream,*/*",
            },
        )
        try:
            with active_opener.open(req, timeout=timeout) as response:
                # Handle possible manual 3xx response from mock or non-raising opener
                status_code = getattr(response, "status", getattr(response, "code", 200))
                if status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        return False, "invalid_redirect", current_url
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        return False, "too_many_redirects", current_url
                    current_url = urljoin(current_url, location)
                    continue

                cl = response.headers.get("Content-Length")
                if cl:
                    try:
                        if int(cl) > MAX_PDF_BYTES:
                            return False, "payload_too_large", current_url
                    except ValueError:
                        pass

                with open(target_path, "wb") as out_file:
                    first_chunk = response.read(1024)
                    if not first_chunk or not first_chunk.startswith(b"%PDF-"):
                        return False, "not_a_pdf", current_url
                    out_file.write(first_chunk)
                    total_bytes = len(first_chunk)
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > MAX_PDF_BYTES:
                            return False, "payload_too_large", current_url
                        out_file.write(chunk)
                return True, None, current_url
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    return False, "invalid_redirect", current_url
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    return False, "too_many_redirects", current_url
                current_url = urljoin(current_url, location)
                continue
            return False, f"http_error_{exc.code}", current_url
        except (URLError, TimeoutError, OSError):
            return False, "network_error", current_url


def execute_paper_pdf_read(
    doi: str,
    *,
    pages: str | None = DEFAULT_PAGES,
    search_term: str | None = None,
    opener: Any = None,
    dns_resolver: Callable[[str], list[str]] | None = None,
    timeout: float = 20.0,
    blocked_tokens: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Execute bounded open-access PDF lookup and extraction."""
    now_iso = datetime.now(UTC).isoformat()
    cleaned_doi = doi.strip()
    if not validate_doi(cleaned_doi):
        return {"error": "invalid_doi", "status": "refused"}

    page_range = parse_pages_range(pages)
    if not page_range:
        return {"error": "invalid_pages", "status": "refused"}

    st_clean: str | None = None
    if search_term is not None and search_term.strip():
        if not validate_search_term(search_term, blocked_tokens=blocked_tokens):
            return {"error": "egress_blocked_private_content", "status": "refused"}
        st_clean = search_term.strip().lower()

    unpaywall_data = lookup_unpaywall(cleaned_doi, opener=opener, timeout=timeout)
    if not unpaywall_data or not isinstance(unpaywall_data, dict):
        return {
            "status": "no_open_access_pdf",
            "doi": cleaned_doi,
            "title": None,
            "landing_url": None,
            "observed_at": now_iso,
        }

    pdf_url: str | None = None
    landing_url: str | None = None
    best_oa = unpaywall_data.get("best_oa_location")
    if isinstance(best_oa, dict):
        landing_url = best_oa.get("url_for_landing_page") or best_oa.get("url")
        c_pdf = best_oa.get("url_for_pdf")
        if isinstance(c_pdf, str) and c_pdf.strip():
            pdf_url = c_pdf.strip()

    if not pdf_url:
        oa_locs = unpaywall_data.get("oa_locations")
        if isinstance(oa_locs, list):
            for loc in oa_locs:
                if isinstance(loc, dict):
                    if not landing_url:
                        landing_url = loc.get("url_for_landing_page") or loc.get("url")
                    c_pdf = loc.get("url_for_pdf")
                    if isinstance(c_pdf, str) and c_pdf.strip():
                        pdf_url = c_pdf.strip()
                        break
                    elif loc.get("url") and str(loc["url"]).strip().lower().endswith(".pdf"):
                        pdf_url = str(loc["url"]).strip()
                        break

    if not landing_url:
        landing_url = unpaywall_data.get("doi_url")

    if not pdf_url:
        return {
            "status": "no_open_access_pdf",
            "doi": cleaned_doi,
            "title": unpaywall_data.get("title"),
            "landing_url": landing_url,
            "observed_at": now_iso,
        }

    if not pdf_url.lower().startswith("https://"):
        return {"error": "refused_non_https", "status": "refused"}

    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        ok, err_code, final_url = download_pdf(
            pdf_url,
            tmp_path,
            opener=opener,
            dns_resolver=dns_resolver,
            timeout=timeout,
        )
        if not ok:
            return {"error": err_code or "download_failed", "status": "refused"}

        try:
            all_pages = read_pdf_pages(tmp_path)
        except PdfTextError:
            return {
                "status": "no_text_layer",
                "doi": cleaned_doi,
                "pdf_url": final_url,
                "page_count": 0,
                "observed_at": now_iso,
            }

        total_pages = len(all_pages)
        if total_pages == 0 or not any(p.strip() for p in all_pages):
            return {
                "status": "no_text_layer",
                "doi": cleaned_doi,
                "pdf_url": final_url,
                "page_count": total_pages,
                "observed_at": now_iso,
            }

        start_p, end_p = page_range
        req_start = max(1, start_p)
        req_end = min(total_pages, end_p)
        if req_start > total_pages:
            selected_pages: list[int] = []
        else:
            selected_pages = list(range(req_start, req_end + 1))

        excerpts: list[dict[str, Any]] = []
        remaining_chars = MAX_EXTRACTED_CHARS

        for p in selected_pages:
            raw_text = all_pages[p - 1]
            if st_clean:
                if st_clean not in raw_text.lower():
                    continue
                snippets: list[str] = []
                for m in re.finditer(re.escape(st_clean), raw_text, re.IGNORECASE):
                    s_start = max(0, m.start() - 200)
                    s_end = min(len(raw_text), m.end() + 200)
                    snippet = raw_text[s_start:s_end].strip()
                    snippets.append(snippet)
                text_to_add = " ... ".join(snippets)
            else:
                text_to_add = raw_text.strip()

            if not text_to_add:
                continue

            if remaining_chars <= 0:
                break
            if len(text_to_add) > remaining_chars:
                text_to_add = text_to_add[:remaining_chars] + "..."
            excerpts.append({"page": p, "text": text_to_add})
            remaining_chars -= len(text_to_add)

        ret_pages = [item["page"] for item in excerpts]
        return {
            "status": "succeeded",
            "doi": cleaned_doi,
            "pdf_url": final_url,
            "page_count": total_pages,
            "returned_pages": ret_pages,
            "excerpts": excerpts,
            "pages": excerpts,
            "observed_at": now_iso,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


def dispatch_paper_pdf_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
    *,
    grant: CapabilityGrantRef | None = None,
    opener: Any = None,
    dns_resolver: Callable[[str], list[str]] | None = None,
    live_grants: dict[str, CapabilityGrantRef] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Broker dispatch for jarvis.paper_pdf_read capability."""
    raw_args = call.arguments
    if not isinstance(raw_args, dict):
        return None, "invalid_arguments"

    doi = raw_args.get("doi")
    if not isinstance(doi, str) or not doi.strip():
        return None, "invalid_arguments"

    pages = raw_args.get("pages", DEFAULT_PAGES)
    if pages is not None and not isinstance(pages, str):
        return None, "invalid_arguments"

    search_term = raw_args.get("search_term")
    if search_term is not None and not isinstance(search_term, str):
        return None, "invalid_arguments"

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

    result = execute_paper_pdf_read(
        doi,
        pages=pages,
        search_term=search_term,
        opener=opener,
        dns_resolver=dns_resolver,
        blocked_tokens=blocked_tokens,
    )
    if result.get("error"):
        if result["error"] == "egress_blocked_private_content":
            return None, "egress_pack_blocked"
        if result["error"] in {"invalid_doi", "invalid_pages"}:
            return None, "invalid_arguments"
        return None, result["error"]

    return result, default_error
