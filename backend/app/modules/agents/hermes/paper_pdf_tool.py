"""Bounded, read-only open-access PDF lookup and text extraction tool.

Per Spec 357:
Resolves canonical DOIs via Unpaywall to discover open-access PDF URLs, downloads
the discovered HTTPS PDF under strict safety bounds (DNS check against private/local IPs,
max 3 HTTPS redirects, magic %PDF- validation, 12 MB byte cap), and extracts bounded page
excerpts via read_pdf_pages without storing permanent files or logging text.
Supports an injectable opener and DNS resolver for 100% offline testing.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

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


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-verified global IP address.

    Prevents DNS rebinding and TOCTOU attacks by connecting the TCP socket
    directly to the verified IP, while enforcing TLS SNI and server certificate
    hostname verification against the original hostname.
    """

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        pinned_ip: str,
        server_hostname: str,
        ssl_context: ssl.SSLContext | None = None,
        connection_factory: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._pinned_ip = pinned_ip
        self._server_hostname = server_hostname
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._connection_factory = connection_factory
        super().__init__(host, port, **kwargs)

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self._pinned_ip, self.port)
        if self._connection_factory is not None:
            sock = self._connection_factory(
                (self._pinned_ip, self.port), timeout=self.timeout
            )
        else:
            sock = socket.create_connection(
                (self._pinned_ip, self.port), timeout=self.timeout
            )

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        self.sock = self._ssl_context.wrap_socket(
            sock,
            server_hostname=self._server_hostname,
        )


class PinnedHTTPSHandler(HTTPSHandler):
    """urllib HTTPS handler that delegates to PinnedHTTPSConnection."""

    def __init__(
        self,
        pinned_ip: str,
        server_hostname: str,
        *,
        context: ssl.SSLContext | None = None,
        connection_factory: Callable[..., Any] | None = None,
        check_hostname: bool | None = None,
    ) -> None:
        self._pinned_context = context or ssl.create_default_context()
        super().__init__(context=self._pinned_context, check_hostname=check_hostname)
        self.pinned_ip = pinned_ip
        self.server_hostname = server_hostname
        self.connection_factory = connection_factory

    def https_open(self, req: Any) -> Any:
        return self.do_open(
            lambda host, **kwargs: PinnedHTTPSConnection(
                host,
                pinned_ip=self.pinned_ip,
                server_hostname=self.server_hostname,
                ssl_context=self._pinned_context,
                connection_factory=self.connection_factory,
                **kwargs,
            ),
            req,
            context=self._pinned_context,
        )


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


def _build_pinned_opener(
    pinned_ip: str,
    server_hostname: str,
    *,
    context: ssl.SSLContext | None = None,
    connection_factory: Callable[..., Any] | None = None,
) -> Any:
    handler = PinnedHTTPSHandler(
        pinned_ip=pinned_ip,
        server_hostname=server_hostname,
        context=context,
        connection_factory=connection_factory,
    )
    return build_opener(ProxyHandler({}), _DenyRedirects(), handler)


def _default_dns_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def is_safe_global_ip(ip_str: str) -> bool:
    """Refuse any private, loopback, link-local, multicast, reserved or non-global IP target."""
    if not ip_str or not isinstance(ip_str, str):
        return False
    raw = ip_str.strip()
    if "%" in raw:
        return False
    try:
        ip = ipaddress.ip_address(raw)
        if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.version == 6:
            # Deprecated IPv4-compatible IPv6 addresses (::/96)
            if ip in ipaddress.IPv6Network("::/96"):
                if ip in {ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")}:
                    return False
                ip = ipaddress.IPv4Address(int(ip))
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
    except (ValueError, TypeError):
        return False


def resolve_and_verify_host(
    hostname: str,
    *,
    dns_resolver: Callable[[str], list[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Resolve and verify hostname maps strictly to public global IP addresses.

    Returns (True, [verified_ip, ...]) on success, or (False, []) on failure.
    """
    if not hostname or not isinstance(hostname, str):
        return False, []
    h = hostname.strip().lower()
    if not h or h in {"localhost", "localhost.localdomain"}:
        return False, []
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]

    # Check if host is already an IP address literal
    try:
        ip_obj = ipaddress.ip_address(h)
        ip_str = str(ip_obj)
        if is_safe_global_ip(ip_str):
            return True, [ip_str]
        return False, []
    except ValueError:
        pass

    # Normalize IDN / Punycode domain name
    try:
        ascii_host = h.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return False, []

    resolver = dns_resolver or _DEFAULT_DNS_RESOLVER or _default_dns_resolver
    try:
        ips = resolver(ascii_host)
        if not ips:
            return False, []
        for ip in ips:
            if not is_safe_global_ip(ip):
                return False, []
        return True, list(ips)
    except Exception:
        return False, []


def is_safe_global_host(
    hostname: str,
    *,
    dns_resolver: Callable[[str], list[str]] | None = None,
) -> bool:
    """Resolve and verify hostname maps strictly to public global IP addresses."""
    ok, _ = resolve_and_verify_host(hostname, dns_resolver=dns_resolver)
    return ok


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
    ssl_context: ssl.SSLContext | None = None,
    connection_factory: Callable[..., Any] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Download HTTPS PDF to target_path with safe DNS validation, IP connection pinning, max 3 redirects, magic header check, and byte cap."""
    current_url = url
    redirects = 0

    while True:
        try:
            parsed = urlsplit(current_url)
        except ValueError:
            return False, "invalid_url", current_url

        if parsed.scheme.lower() != "https":
            return False, "refused_non_https", current_url

        netloc = parsed.netloc or ""
        if parsed.username is not None or parsed.password is not None or "@" in netloc:
            return False, "refused_userinfo", current_url

        raw_hostname = parsed.hostname
        if not raw_hostname:
            return False, "invalid_url", current_url

        try:
            port = parsed.port if parsed.port is not None else 443
        except ValueError:
            return False, "invalid_url", current_url

        if not (1 <= port <= 65535):
            return False, "invalid_url", current_url

        ok, verified_ips = resolve_and_verify_host(raw_hostname, dns_resolver=dns_resolver)
        if not ok or not verified_ips:
            return False, "refused_private_or_local_target", current_url

        pinned_ip = verified_ips[0]

        h_clean = raw_hostname
        if h_clean.startswith("[") and h_clean.endswith("]"):
            h_clean = h_clean[1:-1]
        try:
            server_hostname = h_clean.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return False, "invalid_url", current_url

        if opener is not None:
            active_opener = opener
        elif _DEFAULT_OPENER is not None:
            active_opener = _DEFAULT_OPENER
        else:
            active_opener = _build_pinned_opener(
                pinned_ip=pinned_ip,
                server_hostname=server_hostname,
                context=ssl_context,
                connection_factory=connection_factory,
            )

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
                    loc_clean = location.strip()
                    if "\r" in loc_clean or "\n" in loc_clean:
                        return False, "invalid_redirect", current_url
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        return False, "too_many_redirects", current_url
                    current_url = urljoin(current_url, loc_clean)
                    continue

                if status_code >= 400:
                    return False, f"http_error_{status_code}", current_url

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
                        target_path.unlink(missing_ok=True)
                        return False, "not_a_pdf", current_url
                    out_file.write(first_chunk)
                    total_bytes = len(first_chunk)
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > MAX_PDF_BYTES:
                            target_path.unlink(missing_ok=True)
                            return False, "payload_too_large", current_url
                        out_file.write(chunk)
                return True, None, current_url
        except HTTPError as exc:
            try:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        return False, "invalid_redirect", current_url
                    loc_clean = location.strip()
                    if "\r" in loc_clean or "\n" in loc_clean:
                        return False, "invalid_redirect", current_url
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        return False, "too_many_redirects", current_url
                    current_url = urljoin(current_url, loc_clean)
                    continue
                return False, f"http_error_{exc.code}", current_url
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError):
            target_path.unlink(missing_ok=True)
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
    ssl_context: ssl.SSLContext | None = None,
    connection_factory: Callable[..., Any] | None = None,
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
            ssl_context=ssl_context,
            connection_factory=connection_factory,
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
    ssl_context: ssl.SSLContext | None = None,
    connection_factory: Callable[..., Any] | None = None,
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
        ssl_context=ssl_context,
        connection_factory=connection_factory,
    )
    if result.get("error"):
        if result["error"] == "egress_blocked_private_content":
            return None, "egress_pack_blocked"
        if result["error"] in {"invalid_doi", "invalid_pages"}:
            return None, "invalid_arguments"
        return None, result["error"]

    return result, default_error
