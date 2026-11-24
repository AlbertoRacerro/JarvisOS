"""Bounded, read-only literature metadata search tool (OpenAlex, Semantic Scholar, Europe PMC & Crossref).

Per Spec 352 & Spec 357:
Queries OpenAlex, Semantic Scholar, Europe PMC, and Crossref public REST APIs for scientific
paper metadata (title, authors, year, journal, doi, abstract truncated to safe bound, oa_pdf_url, source).
Fails closed on invalid queries, secrets/private pack content (egress boundary),
HTTP redirects (never follow redirects!), or oversized responses.
Supports an injectable opener for 100% offline testing.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.modules.agents.hermes.ip_boundary import (
    _KNOWN_SECRET_RE,
    check_sensitivity_floor,
    sanitize_text,
)
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall

EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

VALID_SOURCES = frozenset({"all", "europe_pmc", "crossref", "openalex", "semantic_scholar"})

MAX_RESPONSE_BYTES = 512 * 1024  # 512 KB
MAX_ABSTRACT_CHARS = 500
MAX_QUERY_CHARS = 300
DEFAULT_LIMIT = 5
MAX_LIMIT = 20

_XML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
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


def set_default_opener(opener: Any) -> None:
    """Set process-wide default opener for paper search (e.g. for offline test stubs)."""
    global _DEFAULT_OPENER
    _DEFAULT_OPENER = opener


@contextmanager
def override_paper_search_opener(opener: Any):
    """Context manager to temporarily override paper search opener."""
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


def _clean_abstract(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = _XML_TAG_RE.sub(" ", raw)
    normalized = " ".join(cleaned.split())
    if not normalized:
        return None
    if len(normalized) > MAX_ABSTRACT_CHARS:
        return normalized[:MAX_ABSTRACT_CHARS] + "..."
    return normalized


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    cleaned = doi.strip().lower()
    cleaned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", cleaned)
    cleaned = re.sub(r"^doi:", "", cleaned)
    return cleaned.strip() or None


def _normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = " ".join(title.lower().split())
    cleaned = cleaned.rstrip(".,;:!?")
    return cleaned or None


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


def search_europe_pmc(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    opener: Any = None,
    error_collector: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Query Europe PMC REST search endpoint and parse into compact records."""
    params = urlencode({
        "query": query,
        "format": "json",
        "pageSize": str(max(1, min(limit, MAX_LIMIT))),
        "resultType": "core",
    })
    url = f"{EUROPE_PMC_SEARCH_URL}?{params}"
    data = _safe_fetch_json(url, opener=opener)
    if data is None or not isinstance(data, dict):
        if error_collector is not None:
            error_collector["europe_pmc"] = "fetch_failed"
        return []

    records: list[dict[str, Any]] = []
    result_list = data.get("resultList", {})
    results = result_list.get("result", []) if isinstance(result_list, dict) else []
    if not isinstance(results, list):
        return []

    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        author_string = str(item.get("authorString") or "").strip()
        if not author_string and isinstance(item.get("authorList"), dict):
            authors_data = item["authorList"].get("author", [])
            if isinstance(authors_data, list):
                author_string = ", ".join(
                    str(a.get("fullName") or a.get("lastName", "")).strip()
                    for a in authors_data
                    if isinstance(a, dict) and (a.get("fullName") or a.get("lastName"))
                )

        pub_year_val = item.get("pubYear")
        year: int | str | None = None
        if pub_year_val:
            try:
                year = int(pub_year_val)
            except (ValueError, TypeError):
                year = str(pub_year_val)[:10]

        journal = item.get("journalTitle")
        if not journal and isinstance(item.get("journalInfo"), dict):
            j_info = item["journalInfo"].get("journal", {})
            if isinstance(j_info, dict):
                journal = j_info.get("title")
        journal = str(journal).strip() if journal else None

        doi = item.get("doi")
        doi_str = str(doi).strip() if doi else None

        abstract = _clean_abstract(item.get("abstractText"))

        records.append({
            "title": title,
            "authors": author_string,
            "year": year,
            "journal": journal,
            "doi": doi_str,
            "abstract": abstract,
            "oa_pdf_url": None,
            "source": "europe_pmc",
        })
    return records


def search_crossref(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    opener: Any = None,
    error_collector: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Query Crossref REST works endpoint and parse into compact records."""
    params = urlencode({
        "query": query,
        "rows": str(max(1, min(limit, MAX_LIMIT))),
    })
    url = f"{CROSSREF_SEARCH_URL}?{params}"
    data = _safe_fetch_json(url, opener=opener)
    if data is None or not isinstance(data, dict):
        if error_collector is not None:
            error_collector["crossref"] = "fetch_failed"
        return []

    message = data.get("message", {})
    items = message.get("items", []) if isinstance(message, dict) else []
    if not isinstance(items, list):
        return []

    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        titles = item.get("title", [])
        title = ""
        if isinstance(titles, list) and titles:
            title = str(titles[0]).strip()
        elif isinstance(item.get("title"), str):
            title = str(item.get("title")).strip()

        author_list = item.get("author", [])
        author_names: list[str] = []
        if isinstance(author_list, list):
            for a in author_list:
                if isinstance(a, dict):
                    fam = str(a.get("family", "")).strip()
                    giv = str(a.get("given", "")).strip()
                    if fam and giv:
                        author_names.append(f"{fam} {giv}")
                    elif fam or giv:
                        author_names.append(fam or giv)
                    elif a.get("name"):
                        author_names.append(str(a.get("name")).strip())
        authors = ", ".join(author_names)

        issued = item.get("issued", {})
        year: int | str | None = None
        if isinstance(issued, dict):
            date_parts = issued.get("date-parts", [])
            if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list) and date_parts[0]:
                try:
                    year = int(date_parts[0][0])
                except (ValueError, TypeError):
                    year = str(date_parts[0][0])

        containers = item.get("container-title", [])
        journal: str | None = None
        if isinstance(containers, list) and containers:
            journal = str(containers[0]).strip()
        elif isinstance(item.get("container-title"), str):
            journal = str(item.get("container-title")).strip()

        doi = item.get("DOI")
        doi_str = str(doi).strip() if doi else None

        abstract = _clean_abstract(item.get("abstract"))

        records.append({
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi_str,
            "abstract": abstract,
            "oa_pdf_url": None,
            "source": "crossref",
        })
    return records


def search_openalex(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    opener: Any = None,
    error_collector: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Query OpenAlex works endpoint, reconstruct abstract from inverted index, and extract oa_pdf_url."""
    params = urlencode({
        "search": query,
        "per-page": str(max(1, min(limit, MAX_LIMIT))),
    })
    url = f"{OPENALEX_SEARCH_URL}?{params}"
    data = _safe_fetch_json(url, opener=opener)
    if data is None or not isinstance(data, dict):
        if error_collector is not None:
            error_collector["openalex"] = "fetch_failed"
        return []

    results = data.get("results", [])
    if not isinstance(results, list):
        return []

    records: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()

        author_names: list[str] = []
        authorships = item.get("authorships")
        if isinstance(authorships, list):
            for a in authorships:
                if isinstance(a, dict):
                    author = a.get("author")
                    name = ""
                    if isinstance(author, dict) and author.get("display_name"):
                        name = str(author["display_name"]).strip()
                    elif a.get("raw_author_name"):
                        name = str(a["raw_author_name"]).strip()
                    if name:
                        author_names.append(name)
        authors = ", ".join(author_names)

        pub_year = item.get("publication_year")
        year: int | str | None = None
        if pub_year:
            try:
                year = int(pub_year)
            except (ValueError, TypeError):
                year = str(pub_year)[:10]

        journal: str | None = None
        primary_loc = item.get("primary_location")
        if isinstance(primary_loc, dict):
            src = primary_loc.get("source")
            if isinstance(src, dict) and src.get("display_name"):
                journal = str(src["display_name"]).strip()
        if not journal and isinstance(item.get("host_venue"), dict):
            journal = str(item["host_venue"].get("name") or "").strip() or None

        raw_doi = item.get("doi")
        doi_str: str | None = None
        if raw_doi:
            clean = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(raw_doi).strip(), flags=re.IGNORECASE)
            doi_str = re.sub(r"^doi:", "", clean, flags=re.IGNORECASE).strip() or None

        # Reconstruct abstract from abstract_inverted_index if present
        abstract_raw: str | None = None
        inverted_index = item.get("abstract_inverted_index")
        if isinstance(inverted_index, dict) and inverted_index:
            word_positions: list[tuple[int, str]] = []
            for word, positions in inverted_index.items():
                if isinstance(positions, list):
                    for pos in positions:
                        if isinstance(pos, int) and pos >= 0:
                            word_positions.append((pos, str(word)))
            if word_positions:
                word_positions.sort(key=lambda x: x[0])
                abstract_raw = " ".join(w for _, w in word_positions)
        elif isinstance(item.get("abstract"), str):
            abstract_raw = item["abstract"]

        abstract = _clean_abstract(abstract_raw)

        # oa_pdf_url from best_oa_location.pdf_url or open_access.oa_url when HTTPS
        oa_pdf_url: str | None = None
        best_oa = item.get("best_oa_location")
        if isinstance(best_oa, dict) and isinstance(best_oa.get("pdf_url"), str):
            cand = best_oa["pdf_url"].strip()
            if cand.lower().startswith("https://"):
                oa_pdf_url = cand
        if not oa_pdf_url and isinstance(item.get("open_access"), dict):
            cand = item["open_access"].get("oa_url")
            if isinstance(cand, str) and cand.strip().lower().startswith("https://"):
                oa_pdf_url = cand.strip()

        records.append({
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi_str,
            "abstract": abstract,
            "oa_pdf_url": oa_pdf_url,
            "source": "openalex",
        })
    return records


def search_semantic_scholar(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    opener: Any = None,
    error_collector: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Query Semantic Scholar search endpoint and parse into compact records."""
    params = urlencode({
        "query": query,
        "limit": str(max(1, min(limit, MAX_LIMIT))),
        "fields": "title,authors,year,venue,externalIds,abstract,openAccessPdf",
    })
    url = f"{SEMANTIC_SCHOLAR_SEARCH_URL}?{params}"
    data = _safe_fetch_json(url, opener=opener)
    if data is None or not isinstance(data, dict):
        if error_collector is not None:
            error_collector["semantic_scholar"] = "fetch_failed"
        return []

    data_list = data.get("data", [])
    if not isinstance(data_list, list):
        return []

    records: list[dict[str, Any]] = []
    for item in data_list:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()

        authors_list = item.get("authors", [])
        author_names: list[str] = []
        if isinstance(authors_list, list):
            for a in authors_list:
                if isinstance(a, dict) and a.get("name"):
                    author_names.append(str(a["name"]).strip())
        authors = ", ".join(author_names)

        year_val = item.get("year")
        year: int | str | None = None
        if year_val:
            try:
                year = int(year_val)
            except (ValueError, TypeError):
                year = str(year_val)[:10]

        venue = item.get("venue")
        if not venue and isinstance(item.get("journal"), dict):
            venue = item["journal"].get("name")
        journal = str(venue).strip() if venue else None

        doi_str: str | None = None
        ext_ids = item.get("externalIds")
        if isinstance(ext_ids, dict) and ext_ids.get("DOI"):
            doi_str = str(ext_ids["DOI"]).strip()
        elif item.get("doi"):
            doi_str = str(item["doi"]).strip()
        if doi_str:
            clean = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_str, flags=re.IGNORECASE)
            doi_str = re.sub(r"^doi:", "", clean, flags=re.IGNORECASE).strip() or None

        abstract = _clean_abstract(item.get("abstract"))

        oa_pdf_url: str | None = None
        oa_pdf = item.get("openAccessPdf")
        if isinstance(oa_pdf, dict) and isinstance(oa_pdf.get("url"), str):
            cand = oa_pdf["url"].strip()
            if cand.lower().startswith("https://"):
                oa_pdf_url = cand

        records.append({
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi_str,
            "abstract": abstract,
            "oa_pdf_url": oa_pdf_url,
            "source": "semantic_scholar",
        })
    return records


def execute_paper_search(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    source: str = "all",
    opener: Any = None,
    blocked_tokens: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Execute bounded public paper search across OpenAlex, Semantic Scholar, Europe PMC and/or Crossref."""
    cleaned_query = query.strip()
    if not cleaned_query or len(cleaned_query) > MAX_QUERY_CHARS:
        return {"error": "invalid_query", "papers": [], "count": 0}

    # Egress check: fail closed on secrets and private pack terms
    if _KNOWN_SECRET_RE.search(cleaned_query):
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}
    if sanitize_text(cleaned_query) != cleaned_query:
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}
    if check_sensitivity_floor(cleaned_query) in {"S2", "S3", "S4"}:
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    # Conservative character set check
    if not _ALLOWED_CHARS_RE.match(cleaned_query):
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    # UUID-like or hex-token strings (>= 16 hex chars)
    if _UUID_OR_HEX_RE.search(cleaned_query):
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    # URLs
    if _URL_RE.search(cleaned_query):
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    # Email addresses
    if _EMAIL_RE.search(cleaned_query):
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    # File paths
    if _PATH_RE.search(cleaned_query):
        return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    # Blocked tokens (live grant ids, session/thread/workspace ids)
    if blocked_tokens:
        for token in blocked_tokens:
            if token and token in cleaned_query:
                return {"error": "egress_blocked_private_content", "papers": [], "count": 0}

    limit = max(1, min(limit, MAX_LIMIT))
    source_clean = source.strip().lower()

    if source_clean not in VALID_SOURCES:
        return {"error": "invalid_source", "papers": [], "count": 0}

    source_errors: dict[str, str] = {}
    candidates: list[dict[str, Any]] = []

    if source_clean == "all":
        # Query all 4 sources and merge/interleave gracefully
        epmc_list = search_europe_pmc(cleaned_query, limit=limit, opener=opener, error_collector=source_errors)
        crossref_list = search_crossref(cleaned_query, limit=limit, opener=opener, error_collector=source_errors)
        openalex_list = search_openalex(cleaned_query, limit=limit, opener=opener, error_collector=source_errors)
        s2_list = search_semantic_scholar(cleaned_query, limit=limit, opener=opener, error_collector=source_errors)

        # Round-robin interleaving
        lists = [epmc_list, crossref_list, openalex_list, s2_list]
        max_len = max((len(lst) for lst in lists), default=0)
        for idx in range(max_len):
            for lst in lists:
                if idx < len(lst):
                    candidates.append(lst[idx])
    elif source_clean == "europe_pmc":
        candidates.extend(search_europe_pmc(cleaned_query, limit=limit, opener=opener, error_collector=source_errors))
    elif source_clean == "crossref":
        candidates.extend(search_crossref(cleaned_query, limit=limit, opener=opener, error_collector=source_errors))
    elif source_clean == "openalex":
        candidates.extend(search_openalex(cleaned_query, limit=limit, opener=opener, error_collector=source_errors))
    elif source_clean == "semantic_scholar":
        candidates.extend(search_semantic_scholar(cleaned_query, limit=limit, opener=opener, error_collector=source_errors))

    # Deduplicate by normalized DOI and normalized title
    seen_dois: set[str] = set()
    seen_titles: set[str] = set()
    papers: list[dict[str, Any]] = []

    for item in candidates:
        doi = _normalize_doi(item.get("doi"))
        title = _normalize_title(item.get("title"))

        if doi and doi in seen_dois:
            continue
        if title and title in seen_titles:
            continue

        if doi:
            seen_dois.add(doi)
        if title:
            seen_titles.add(title)

        papers.append(item)
        if len(papers) >= limit:
            break

    result: dict[str, Any] = {
        "query": cleaned_query,
        "count": len(papers),
        "papers": papers,
    }
    if source_errors:
        result["errors"] = source_errors
        result["warnings"] = [f"Source {s} failed: {err}" for s, err in source_errors.items()]

    return result


def dispatch_paper_search_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
    *,
    grant: CapabilityGrantRef | None = None,
    opener: Any = None,
    live_grants: dict[str, CapabilityGrantRef] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Broker dispatch for jarvis.paper_search capability."""
    raw_args = call.arguments
    if not isinstance(raw_args, dict):
        return None, "invalid_arguments"

    query = raw_args.get("query")
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > MAX_QUERY_CHARS:
        return None, "invalid_arguments"

    limit = raw_args.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= MAX_LIMIT):
        return None, "invalid_arguments"

    source = raw_args.get("source", "all")
    if not isinstance(source, str) or source.strip().lower() not in VALID_SOURCES:
        return None, "invalid_arguments"

    cleaned_query = query.strip()

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

    result = execute_paper_search(
        cleaned_query,
        limit=limit,
        source=source,
        opener=opener,
        blocked_tokens=blocked_tokens,
    )
    if result.get("error"):
        if result["error"] == "egress_blocked_private_content":
            return None, "egress_pack_blocked"
        return None, result["error"]

    return result, default_error
