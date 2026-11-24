"""Comprehensive offline tests for Spec 357 (SCHOLARLY-OA-SEARCH-1).

Covers:
1. Paper search expansion: OpenAlex (including inverted-index abstract reconstruction),
   Semantic Scholar, Europe PMC, Crossref, deduplication across sources, and graceful
   degradation when a source fails.
2. Egress boundary checks: query and search_term sanitization blocking secrets, pack terms,
   file paths, URLs, and live tokens.
3. Open-access PDF lookup & text extraction (jarvis_paper_pdf_read): canonical DOI validation,
   Unpaywall OA hit vs closed-access miss, DNS validation against private/loopback targets,
   HTTPS redirects (max 3), non-PDF magic byte refusal, oversized payload refusal,
   no_text_layer detection, snippet filtering with search_term, 8 000 char bounding,
   and tempfile cleanup.
4. Hermes integration: scoped grant, broker MCP schema, supervisor dispatch,
   research budget counting, and no text logging in events.
"""
from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest

from app.modules.agents.hermes import broker_mcp, worker_shim
from app.modules.agents.hermes.paper_pdf_tool import (
    MAX_EXTRACTED_CHARS,
    MAX_PDF_BYTES,
    download_pdf,
    execute_paper_pdf_read,
    is_safe_global_host,
    is_safe_global_ip,
    parse_pages_range,
    resolve_and_verify_host,
    validate_doi,
    validate_search_term,
)
from app.modules.agents.hermes.paper_search_tool import (
    CROSSREF_SEARCH_URL,
    EUROPE_PMC_SEARCH_URL,
    OPENALEX_SEARCH_URL,
    SEMANTIC_SCHOLAR_SEARCH_URL,
    execute_paper_search,
    search_openalex,
    search_semantic_scholar,
)
from app.modules.agents.hermes.supervisor import (
    _TOOL_CAPABILITIES,
    HERMES_TOOL_ALLOWLIST,
    HermesSupervisor,
    dispatch_tool,
    is_research_tool,
)
from app.modules.ai import thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.jarvis_context import PRODUCTION_CAPABILITY_REGISTRY

# ---------------------------------------------------------------------------
# Test Fixtures & Synthetic Data
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    """Mock HTTP response matching urllib.request context manager."""

    def __init__(
        self,
        body: bytes,
        headers: dict[str, str] | None = None,
        status: int = 200,
    ):
        self._body = body
        self._stream = io.BytesIO(body)
        self.headers = headers or {"Content-Length": str(len(body))}
        self.status = status
        self.code = status

    def read(self, amt: int = -1) -> bytes:
        return self._stream.read(amt)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeOpener:
    """Mock urllib opener routing URLs to synthetic responses or errors."""

    def __init__(self, routes: dict[str, _FakeHTTPResponse | Exception | Any]):
        self.routes = routes
        self.calls: list[str] = []

    def open(self, req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        for pattern, response in self.routes.items():
            if pattern in url:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(req)
                return response
        raise URLError(f"unexpected fake url: {url}")


def make_test_pdf(texts: list[str]) -> bytes:
    """Generate minimal valid PDF bytes with selectable text for each page."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids: list[str] = []
    for text in texts:
        page = len(objects) + 1
        kids.append(f"{page} 0 R")
        stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
        objects.extend([
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
                f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
                f"/Contents {page + 1} 0 R >>"
            ).encode(),
            b"<< /Length " + str(len(stream)).encode("utf-8") + b" >>\nstream\n" + stream + b"\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


_FAKE_OPENALEX_JSON = json.dumps({
    "results": [
        {
            "title": "Cyanobacterial photobioreactor hydrodynamic kinetics",
            "authorships": [
                {"author": {"display_name": "Ada Lovelace"}},
                {"author": {"display_name": "Alan Turing"}},
            ],
            "publication_year": 2024,
            "primary_location": {
                "source": {"display_name": "Journal of Biotechnology"},
            },
            "doi": "https://doi.org/10.1016/j.biortech.2024.1001",
            "abstract_inverted_index": {
                "Photobioreactor": [0],
                "kinetics": [1],
                "demonstrate": [2],
                "high": [3],
                "biomass": [4],
                "productivity.": [5],
            },
            "best_oa_location": {
                "pdf_url": "https://oa.example.org/openalex_paper.pdf",
            },
            "open_access": {
                "is_oa": True,
                "oa_url": "https://oa.example.org/fallback.pdf",
            },
        }
    ]
}).encode("utf-8")


_FAKE_SEMANTIC_SCHOLAR_JSON = json.dumps({
    "data": [
        {
            "title": "Annular photobioreactor irradiance modeling",
            "authors": [
                {"name": "Marie Curie"},
                {"name": "Pierre Curie"},
            ],
            "year": 2023,
            "venue": "Bioresource Technology",
            "externalIds": {
                "DOI": "10.1016/j.biortech.2023.2002",
            },
            "abstract": "Irradiance modeling in annular geometries yields accurate growth predictions.",
            "openAccessPdf": {
                "url": "https://s2.example.org/annular_model.pdf",
                "status": "HYBRID",
            },
        }
    ]
}).encode("utf-8")


_FAKE_UNPAYWALL_OA_JSON = json.dumps({
    "doi": "10.1016/j.biortech.2024.1001",
    "is_oa": True,
    "title": "Cyanobacterial photobioreactor hydrodynamic kinetics",
    "best_oa_location": {
        "url_for_pdf": "https://oa.example.org/openalex_paper.pdf",
        "url_for_landing_page": "https://publisher.example.org/article/1001",
    },
    "oa_locations": [
        {
            "url_for_pdf": "https://oa.example.org/openalex_paper.pdf",
            "url": "https://oa.example.org/openalex_paper.pdf",
        }
    ],
}).encode("utf-8")


_FAKE_UNPAYWALL_CLOSED_JSON = json.dumps({
    "doi": "10.1000/closed123",
    "is_oa": False,
    "title": "Proprietary photobioreactor mechanics",
    "best_oa_location": None,
    "oa_locations": [],
    "doi_url": "https://doi.org/10.1000/closed123",
}).encode("utf-8")


SESSION = AgentSessionRef(
    jarvis_thread_id="thread-357",
    hermes_session_id="hermes-357",
    profile_id="isaac",
    workspace_id="workspace-357",
    generation=1,
    upstream_revision="2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19",
)


def _make_grant(
    capability_id: str = "jarvis.paper_pdf_read",
    grant_id: str = "grant-357",
    workspace_id: str = "workspace-357",
    thread_id: str = "thread-357",
) -> CapabilityGrantRef:
    now = datetime.now(UTC)
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        constraints={"max_items": 10},
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def _make_call(
    arguments: dict[str, Any],
    capability_id: str = "jarvis.paper_pdf_read",
    grant_id: str = "grant-357",
) -> StructuredToolCall:
    now = datetime.now(UTC)
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id=grant_id,
        correlation_id=str(uuid4()),
        session_ref=SESSION,
        arguments=arguments,
        requested_at=now,
        deadline_at=now + timedelta(seconds=60),
    )


def _existing_connection(conn: sqlite3.Connection):
    class Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *args: Any):
            pass

    return Ctx()


def _make_supervisor_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, workspace_id TEXT, "
        "event_type TEXT, actor TEXT, target_type TEXT, target_id TEXT, "
        "payload TEXT, created_at TEXT)"
    )
    return connection


# ===========================================================================
# 1. Paper Search Expansion & Deduplication Tests (Criterion 1)
# ===========================================================================

def test_openalex_abstract_reconstruction_and_parsing() -> None:
    """Criterion 1: OpenAlex inverted index abstract is reconstructed in proper word order."""
    fake_opener = _FakeOpener({OPENALEX_SEARCH_URL: _FakeHTTPResponse(_FAKE_OPENALEX_JSON)})
    records = search_openalex("photobioreactor", limit=5, opener=fake_opener)

    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "Cyanobacterial photobioreactor hydrodynamic kinetics"
    assert rec["authors"] == "Ada Lovelace, Alan Turing"
    assert rec["year"] == 2024
    assert rec["journal"] == "Journal of Biotechnology"
    assert rec["doi"] == "10.1016/j.biortech.2024.1001"
    assert rec["abstract"] == "Photobioreactor kinetics demonstrate high biomass productivity."
    assert rec["oa_pdf_url"] == "https://oa.example.org/openalex_paper.pdf"
    assert rec["source"] == "openalex"


def test_openalex_fallback_abstract_and_oa_url() -> None:
    """Criterion 1: fallback to plain abstract text and open_access.oa_url if inverted index absent."""
    payload = json.dumps({
        "results": [{
            "title": "Fallback Work",
            "abstract": "Direct abstract string without inverted index.",
            "publication_year": "2022",
            "open_access": {"oa_url": "https://oa.example.org/direct.pdf"},
        }]
    }).encode("utf-8")
    fake_opener = _FakeOpener({OPENALEX_SEARCH_URL: _FakeHTTPResponse(payload)})
    records = search_openalex("fallback", limit=2, opener=fake_opener)
    assert len(records) == 1
    assert records[0]["abstract"] == "Direct abstract string without inverted index."
    assert records[0]["oa_pdf_url"] == "https://oa.example.org/direct.pdf"


def test_semantic_scholar_parsing() -> None:
    """Criterion 1: parses Semantic Scholar JSON into compact records with DOI and OA PDF URL."""
    fake_opener = _FakeOpener({SEMANTIC_SCHOLAR_SEARCH_URL: _FakeHTTPResponse(_FAKE_SEMANTIC_SCHOLAR_JSON)})
    records = search_semantic_scholar("annular photobioreactor", limit=5, opener=fake_opener)

    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "Annular photobioreactor irradiance modeling"
    assert rec["authors"] == "Marie Curie, Pierre Curie"
    assert rec["year"] == 2023
    assert rec["journal"] == "Bioresource Technology"
    assert rec["doi"] == "10.1016/j.biortech.2023.2002"
    assert "Irradiance modeling in annular geometries" in rec["abstract"]
    assert rec["oa_pdf_url"] == "https://s2.example.org/annular_model.pdf"
    assert rec["source"] == "semantic_scholar"


def test_paper_search_all_deduplication_by_doi_and_title() -> None:
    """Criterion 1: merges across all 4 sources and deduplicates by normalized DOI and lowercased title."""
    # Source 1: Europe PMC has DOI 10.1000/182 and title "Cyanobacteria Kinetics"
    epmc_json = json.dumps({
        "resultList": {"result": [{"title": "Cyanobacteria Kinetics", "doi": "10.1000/182", "pubYear": "2021"}]}
    }).encode("utf-8")

    # Source 2: Crossref has same DOI with different case and prefix
    crossref_json = json.dumps({
        "message": {"items": [{"title": ["Different Title Same DOI"], "DOI": "https://doi.org/10.1000/182"}]}
    }).encode("utf-8")

    # Source 3: OpenAlex has duplicate title (case/whitespace insensitive) without DOI
    openalex_json = json.dumps({
        "results": [{"title": "  cyanobacteria kinetics.  ", "publication_year": 2021}]
    }).encode("utf-8")

    # Source 4: Semantic Scholar has a genuinely unique paper
    s2_json = json.dumps({
        "data": [{"title": "Unique Novel Paper", "year": 2022, "externalIds": {"DOI": "10.2000/unique"}}]
    }).encode("utf-8")

    fake_opener = _FakeOpener({
        EUROPE_PMC_SEARCH_URL: _FakeHTTPResponse(epmc_json),
        CROSSREF_SEARCH_URL: _FakeHTTPResponse(crossref_json),
        OPENALEX_SEARCH_URL: _FakeHTTPResponse(openalex_json),
        SEMANTIC_SCHOLAR_SEARCH_URL: _FakeHTTPResponse(s2_json),
    })

    result = execute_paper_search("cyanobacteria", limit=10, source="all", opener=fake_opener)
    assert result["count"] == 2
    titles = [p["title"] for p in result["papers"]]
    assert "Cyanobacteria Kinetics" in titles
    assert "Unique Novel Paper" in titles


def test_paper_search_graceful_degradation_on_partial_failure() -> None:
    """Criterion 1: when one or two sources fail, degrades gracefully and records source errors."""
    fake_opener = _FakeOpener({
        EUROPE_PMC_SEARCH_URL: _FakeHTTPResponse(_FAKE_OPENALEX_JSON),  # returns valid structure
        CROSSREF_SEARCH_URL: HTTPError(CROSSREF_SEARCH_URL, 503, "Service Unavailable", {}, None),  # type: ignore[arg-type]
        OPENALEX_SEARCH_URL: _FakeHTTPResponse(_FAKE_OPENALEX_JSON),
        SEMANTIC_SCHOLAR_SEARCH_URL: URLError("Connection refused"),
    })

    result = execute_paper_search("photobioreactor", limit=5, source="all", opener=fake_opener)
    assert result["count"] > 0
    assert "errors" in result
    assert "crossref" in result["errors"]
    assert "semantic_scholar" in result["errors"]
    assert "warnings" in result
    assert any("crossref" in w for w in result["warnings"])


# ===========================================================================
# 2. Egress Boundary Tests (Criterion 2)
# ===========================================================================

def test_paper_search_egress_blocks_secrets_and_pack_terms() -> None:
    """Criterion 2: query egress checks block secrets, pack terms, paths, URLs, and live tokens."""
    opener = _FakeOpener({})

    # 1. API key secret
    res1 = execute_paper_search("sk-1234567890abcdef1234567890", opener=opener)
    assert res1.get("error") == "egress_blocked_private_content"

    # 2. Live grant token in query
    res2 = execute_paper_search("photobioreactor grant-secret-token", opener=opener, blocked_tokens=["grant-secret-token"])
    assert res2.get("error") == "egress_blocked_private_content"

    # 3. File path
    res3 = execute_paper_search("/etc/passwd", opener=opener)
    assert res3.get("error") == "egress_blocked_private_content"

    # 4. URL
    res4 = execute_paper_search("https://attacker.org/leak", opener=opener)
    assert res4.get("error") == "egress_blocked_private_content"


def test_paper_pdf_read_egress_blocks_search_term_secrets_and_pack_terms() -> None:
    """Criterion 2: search_term egress checks fail closed on secrets and private content."""
    # Secrets in search_term
    assert not validate_search_term("sk-1234567890abcdef1234567890")
    # File path
    assert not validate_search_term("/home/user/secret.txt")
    # URL
    assert not validate_search_term("http://leak.example.com")
    # Blocked token
    assert not validate_search_term("kinetics token123", blocked_tokens=["token123"])
    # Allowed clean scientific term
    assert validate_search_term("cyanobacterial growth rate")


def test_paper_pdf_read_search_term_never_sent_over_network() -> None:
    """Criterion 2: search_term is used only locally for snippet selection and never sent in URLs."""
    pdf_bytes = make_test_pdf(["Page 1 text", "Page 2 with cyanobacteria kinetics"])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(pdf_bytes),
    })

    res = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        search_term="cyanobacteria",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )

    assert res["status"] == "succeeded"
    for call_url in fake_opener.calls:
        assert "cyanobacteria" not in call_url


# ===========================================================================
# 3. Open-Access PDF Lookup & Bounded Text Read Tests (Criterion 3)
# ===========================================================================

def test_paper_pdf_read_doi_strict_validation() -> None:
    """Criterion 3: strictly validates canonical DOI (starts with 10., restricted chars, max 160 chars)."""
    assert validate_doi("10.1016/j.biortech.2024.1001")
    assert validate_doi("10.1000/182")
    assert validate_doi("10.1002/bit.28001")

    # Invalid DOIs
    assert not validate_doi("https://doi.org/10.1000/182")  # URL prefix not allowed
    assert not validate_doi("http://evil.com/10.1000/182")
    assert not validate_doi("10.1/short")  # registrant must be 4-9 digits
    assert not validate_doi("10.1000/with spaces in doi")
    assert not validate_doi("10.1000/with\"quotes\"")
    assert not validate_doi("10.1000/with<angle>brackets")
    assert not validate_doi("not_a_doi")
    assert not validate_doi("10." + "1" * 160)  # > 160 chars


def test_paper_pdf_read_unpaywall_oa_hit_and_pdf_extraction() -> None:
    """Criterion 3: resolves OA PDF via Unpaywall, downloads safely, and extracts text excerpts."""
    pdf_bytes = make_test_pdf([
        "Introduction to photobioreactor modeling.",
        "Methods and kinetic equations for cyanobacteria.",
        "Results: biomass yield reached 2.4 g/L.",
        "Discussion and conclusions.",
    ])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(pdf_bytes),
    })

    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        pages="1-4",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )

    assert result["status"] == "succeeded"
    assert result["doi"] == "10.1016/j.biortech.2024.1001"
    assert result["pdf_url"] == "https://oa.example.org/openalex_paper.pdf"
    assert result["page_count"] == 4
    assert result["returned_pages"] == [1, 2, 3, 4]
    assert len(result["excerpts"]) == 4
    assert "Introduction" in result["excerpts"][0]["text"]
    assert "biomass yield" in result["excerpts"][2]["text"]
    assert "observed_at" in result


def test_paper_pdf_read_unpaywall_closed_access_miss() -> None:
    """Criterion 3: closed-access or unavailable DOI reports status 'no_open_access_pdf' honestly."""
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_CLOSED_JSON),
    })

    result = execute_paper_pdf_read(
        "10.1000/closed123",
        opener=fake_opener,
    )

    assert result["status"] == "no_open_access_pdf"
    assert result["doi"] == "10.1000/closed123"
    assert result["title"] == "Proprietary photobioreactor mechanics"
    assert result["landing_url"] == "https://doi.org/10.1000/closed123"


def test_paper_pdf_read_pages_range_parsing_and_bounds() -> None:
    """Criterion 3: parses page ranges, enforces default 1-4, max 8 pages, and rejects invalid specs."""
    assert parse_pages_range(None) == (1, 4)
    assert parse_pages_range("") == (1, 4)
    assert parse_pages_range("1-4") == (1, 4)
    assert parse_pages_range("2-6") == (2, 6)
    assert parse_pages_range("3") == (3, 3)
    assert parse_pages_range("1-8") == (1, 8)

    # Invalid page ranges
    assert parse_pages_range("1-9") is None  # 9 pages > 8 max
    assert parse_pages_range("0-4") is None  # page 0 invalid
    assert parse_pages_range("5-2") is None  # start > end
    assert parse_pages_range("abc") is None


def test_paper_pdf_read_search_term_snippet_filtering() -> None:
    """Criterion 3: search_term selects matching snippets locally from downloaded PDF."""
    pdf_bytes = make_test_pdf([
        "General introduction without keyword.",
        "Specific section discussing cyanobacteria growth rate and kinetics.",
        "Unrelated conclusion.",
    ])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(pdf_bytes),
    })

    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        pages="1-3",
        search_term="cyanobacteria",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )

    assert result["status"] == "succeeded"
    assert result["returned_pages"] == [2]
    assert len(result["excerpts"]) == 1
    assert "cyanobacteria" in result["excerpts"][0]["text"].lower()


def test_paper_pdf_read_scanned_pdf_no_text_layer() -> None:
    """Criterion 3: scanned or blank PDF with no extracted text reports status 'no_text_layer'."""
    blank_pdf = make_test_pdf(["", ""])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(blank_pdf),
    })

    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )

    assert result["status"] == "no_text_layer"
    assert result["doi"] == "10.1016/j.biortech.2024.1001"
    assert result["page_count"] == 2


def test_paper_pdf_read_refuses_non_https_pdf_url() -> None:
    """Criterion 3: refuses any plain HTTP PDF location returned by Unpaywall."""
    http_unpaywall = json.dumps({
        "is_oa": True,
        "best_oa_location": {"url_for_pdf": "http://insecure.example.org/paper.pdf"},
    }).encode("utf-8")
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(http_unpaywall),
    })

    result = execute_paper_pdf_read("10.1016/j.biortech.2024.1001", opener=fake_opener)
    assert result.get("error") == "refused_non_https"


def test_paper_pdf_read_refuses_private_and_loopback_ips() -> None:
    """Criterion 3: DNS check refuses private, loopback, link-local or non-global target IPs."""
    # Direct IP checks
    assert not is_safe_global_ip("127.0.0.1")
    assert not is_safe_global_ip("::1")
    assert not is_safe_global_ip("10.0.0.1")
    assert not is_safe_global_ip("192.168.1.1")
    assert not is_safe_global_ip("172.16.0.1")
    assert not is_safe_global_ip("169.254.169.254")
    assert is_safe_global_ip("93.184.216.34")

    # Hostname resolving to loopback
    assert not is_safe_global_host("evil-redirect.local", dns_resolver=lambda _: ["127.0.0.1"])
    assert not is_safe_global_host("localhost")

    # execute_paper_pdf_read refusal
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
    })
    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        opener=fake_opener,
        dns_resolver=lambda _: ["127.0.0.1"],
    )
    assert result.get("error") == "refused_private_or_local_target"


def test_paper_pdf_read_refuses_non_pdf_magic_bytes() -> None:
    """Criterion 3: refuses non-PDF body (e.g. publisher HTML block) lacking %PDF- magic bytes."""
    html_body = b"<!DOCTYPE html><html><body>Blocked by publisher paywall</body></html>"
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(html_body),
    })

    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )
    assert result.get("error") == "not_a_pdf"


def test_paper_pdf_read_refuses_oversized_payload() -> None:
    """Criterion 3: refuses oversized PDF payload exceeding 12 MB cap."""
    oversized_data = b"%PDF-1.4\n" + b"x" * (MAX_PDF_BYTES + 1024)
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(oversized_data),
    })

    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )
    assert result.get("error") == "payload_too_large"


def test_paper_pdf_read_follows_at_most_3_https_redirects(tmp_path: Any) -> None:
    """Criterion 3: follows at most 3 HTTPS-only redirects to safe hosts; refuses 4th redirect."""
    redirect_chain = _FakeOpener({
        "https://oa.example.org/step1.pdf": HTTPError("step1", 302, "Found", {"Location": "https://oa.example.org/step2.pdf"}, None),  # type: ignore[arg-type]
        "https://oa.example.org/step2.pdf": HTTPError("step2", 302, "Found", {"Location": "https://oa.example.org/step3.pdf"}, None),  # type: ignore[arg-type]
        "https://oa.example.org/step3.pdf": HTTPError("step3", 302, "Found", {"Location": "https://oa.example.org/step4.pdf"}, None),  # type: ignore[arg-type]
        "https://oa.example.org/step4.pdf": HTTPError("step4", 302, "Found", {"Location": "https://oa.example.org/final.pdf"}, None),  # type: ignore[arg-type]
    })

    target_file = tmp_path / "test.pdf"
    ok, err, _ = download_pdf(
        "https://oa.example.org/step1.pdf",
        target_file,
        opener=redirect_chain,
        dns_resolver=lambda _: ["93.184.216.34"],
    )
    assert not ok
    assert err == "too_many_redirects"


def test_paper_pdf_read_caps_extracted_text_bound() -> None:
    """Criterion 3: total extracted characters across all pages are strictly bounded at 8 000 chars."""
    large_page = "Long paragraph text with data. " * 300  # ~9 300 chars
    pdf_bytes = make_test_pdf([large_page])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(pdf_bytes),
    })

    result = execute_paper_pdf_read(
        "10.1016/j.biortech.2024.1001",
        pages="1",
        opener=fake_opener,
        dns_resolver=lambda _: ["93.184.216.34"],
    )

    assert result["status"] == "succeeded"
    total_chars = sum(len(ex["text"]) for ex in result["excerpts"])
    assert total_chars <= MAX_EXTRACTED_CHARS + 3  # allowing for "..." suffix


# ===========================================================================
# 4. Hermes Integration & Research Budget Tests (Criterion 4)
# ===========================================================================

def test_hermes_tool_registration_catalogs() -> None:
    """Criterion 4: jarvis_paper_pdf_read registered in broker_mcp, supervisor, worker_shim, thread_service."""
    # 1. broker_mcp
    pdf_tool = next((t for t in broker_mcp._TOOLS if t["name"] == "jarvis_paper_pdf_read"), None)
    assert isinstance(pdf_tool, dict)
    schema = pdf_tool["inputSchema"]
    assert isinstance(schema, dict)
    assert schema["required"] == ["grant_id", "doi"]

    # 2. supervisor._TOOL_CAPABILITIES
    assert _TOOL_CAPABILITIES.get("jarvis_paper_pdf_read") == "jarvis.paper_pdf_read"
    assert _TOOL_CAPABILITIES.get("paper_pdf_read") == "jarvis.paper_pdf_read"

    # 3. supervisor.HERMES_TOOL_ALLOWLIST
    assert "jarvis_paper_pdf_read" in HERMES_TOOL_ALLOWLIST
    assert "mcp__jarvis__jarvis_paper_pdf_read" in HERMES_TOOL_ALLOWLIST

    # 4. PRODUCTION_CAPABILITY_REGISTRY
    cap = next((c for c in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads") if c.capability_id == "jarvis.paper_pdf_read"), None)
    assert cap is not None
    assert cap.action_class == "READ"

    # 5. worker_shim.ADMITTED_TOOLS
    assert "jarvis_paper_pdf_read" in worker_shim.ADMITTED_TOOLS
    assert "mcp__jarvis__jarvis_paper_pdf_read" in worker_shim.ADMITTED_TOOLS

    # 6. thread_service._turn_tools
    turn_tools = thread_service._turn_tools("none")
    assert "mcp__jarvis__jarvis_paper_pdf_read" in turn_tools
    assert "mcp__jarvis__jarvis_paper_search" in turn_tools


def test_hermes_scoped_grant_and_prompt_guidance() -> None:
    """Criterion 4: thread_service installs paper_pdf_read grant and prompt guidance."""
    from app.modules.ai.thread_models import AIThreadSubmit

    class _MockWorker:
        def __init__(self) -> None:
            self.live_grants: dict[str, CapabilityGrantRef] = {}

    worker = _MockWorker()
    payload = AIThreadSubmit(request_id="req-357", prompt="Investigate literature", role_id="isaac")
    prompt = thread_service._install_hermes_retrieval_grant(
        worker,  # type: ignore[arg-type]
        payload,
        workspace_id="ws-123",
        thread_id="thread-456",
    )

    # Scoped grant installed
    pdf_grant = next((g for g in worker.live_grants.values() if g.capability_id == "jarvis.paper_pdf_read"), None)
    assert pdf_grant is not None
    assert pdf_grant.scope.workspace_id == "ws-123"

    # Prompt guidance references paper_pdf_read
    assert prompt is not None
    assert "paper_pdf_grant_id=" in prompt
    assert "jarvis_paper_pdf_read" in prompt
    assert len(prompt) <= 2000


def test_supervisor_dispatch_and_research_budget_enforcement() -> None:
    """Criterion 4: both paper_search and jarvis_paper_pdf_read count against Hermes research budget."""
    assert is_research_tool("jarvis_paper_pdf_read")
    assert is_research_tool("paper_pdf_read")
    assert is_research_tool("mcp__jarvis__jarvis_paper_pdf_read")
    assert is_research_tool("jarvis_paper_search")

    from app.modules.agents.hermes.paper_pdf_tool import override_paper_pdf_dns_resolver

    pdf_bytes = make_test_pdf(["Sample page text."])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(pdf_bytes),
    })

    grant = _make_grant(capability_id="jarvis.paper_pdf_read", grant_id="grant-test")
    live_grants = {grant.grant_id: grant}

    call = _make_call(
        arguments={"doi": "10.1016/j.biortech.2024.1001", "grant_id": "grant-test"},
        capability_id="jarvis.paper_pdf_read",
        grant_id="grant-test",
    )

    with override_paper_pdf_dns_resolver(lambda _: ["93.184.216.34"]):
        result = dispatch_tool(call, live_grants=live_grants, paper_search_opener=fake_opener)
    assert result.status == "succeeded"
    assert result.result is not None
    assert result.result["status"] == "succeeded"


def test_no_extracted_pdf_text_logged_in_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Design constraint: supervisor event logging never logs extracted PDF text or user search terms."""
    from app.modules.agents.hermes.paper_pdf_tool import override_paper_pdf_dns_resolver

    conn = _make_supervisor_db()
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: _existing_connection(conn))
    supervisor = HermesSupervisor("unused", runner=MagicMock())
    supervisor.session = SESSION
    monkeypatch.setattr(supervisor, "_send", MagicMock())

    pdf_bytes = make_test_pdf(["Secret extracted literature evidence that should never be in logs."])
    fake_opener = _FakeOpener({
        "api.unpaywall.org": _FakeHTTPResponse(_FAKE_UNPAYWALL_OA_JSON),
        "oa.example.org": _FakeHTTPResponse(pdf_bytes),
    })
    supervisor.paper_search_opener = fake_opener

    grant = _make_grant(capability_id="jarvis.paper_pdf_read", grant_id="grant-123")
    supervisor.live_grants[grant.grant_id] = grant

    frame = {
        "id": "frame-1",
        "session_ref": supervisor.session.model_dump(mode="json"),
        "arguments": {
            "tool_name": "jarvis_paper_pdf_read",
            "grant_id": "grant-123",
            "doi": "10.1016/j.biortech.2024.1001",
            "search_term": "sensitive_user_prompt_query",
        },
    }

    with override_paper_pdf_dns_resolver(lambda _: ["93.184.216.34"]):
        supervisor._handle_tool(frame)

    rows = conn.execute("SELECT event_type, payload FROM events").fetchall()
    assert len(rows) > 0
    for r in rows:
        payload_str = r["payload"]
        assert "Secret extracted literature evidence" not in payload_str
        assert "sensitive_user_prompt_query" not in payload_str


# ===========================================================================
# 5. Causal SSRF & Connection Pinning Tests (Criterion 3 Security Hardening)
# ===========================================================================


def test_paper_pdf_dns_rebinding_pinned_to_verified_global_ip(tmp_path: Any) -> None:
    """SSRF Prevention: connection is pinned to verified global IP; prevents DNS rebinding TOCTOU."""
    call_count = 0

    def rebinding_dns(host: str) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ["93.184.216.34"]  # Verified public global IP on preflight
        return ["127.0.0.1"]  # Rebinding attempt to loopback on connect

    connected_targets: list[tuple[str, int]] = []
    sent_request_bytes: list[bytes] = []

    def mock_factory(addr: tuple[str, int], timeout: Any = None, source_address: Any = None) -> Any:
        connected_targets.append(addr)
        mock_sock = MagicMock()
        mock_sock.sendall.side_effect = lambda b: sent_request_bytes.append(b)
        mock_sock.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 9\r\n"
            b"Content-Type: application/pdf\r\n"
            b"\r\n"
            b"%PDF-1.4\n"
        )
        return mock_sock

    mock_ssl_ctx = MagicMock()
    mock_ssl_ctx.wrap_socket.side_effect = lambda sock, server_hostname=None: sock

    target_file = tmp_path / "pinned.pdf"
    ok, err, final_url = download_pdf(
        "https://rebind.example.org/doc.pdf",
        target_file,
        dns_resolver=rebinding_dns,
        ssl_context=mock_ssl_ctx,
        connection_factory=mock_factory,
    )

    assert ok is True
    assert err is None
    assert final_url == "https://rebind.example.org/doc.pdf"
    assert target_file.read_bytes() == b"%PDF-1.4\n"

    # DNS was resolved exactly once during preflight; no second rebinding lookup occurred
    assert call_count == 1
    # Socket connected directly to the pinned public IP (not loopback)
    assert connected_targets == [("93.184.216.34", 443)]
    # SNI hostname matches target host
    assert mock_ssl_ctx.wrap_socket.call_args[1]["server_hostname"] == "rebind.example.org"
    # HTTP Host header matches target host
    req_text = b"".join(sent_request_bytes).decode("latin1")
    assert "Host: rebind.example.org" in req_text


def test_paper_pdf_redirect_to_private_target_refused(tmp_path: Any) -> None:
    """SSRF Prevention: redirects to private/loopback/non-https targets are strictly refused."""
    target_file = tmp_path / "redirect_priv.pdf"

    # 1. Redirect to private RFC 1918 host
    redirect_to_private = _FakeOpener({
        "https://oa.example.org/start.pdf": HTTPError(
            "start", 302, "Found", {"Location": "https://internal.corp/secret.pdf"}, None  # type: ignore[arg-type]
        ),
    })

    def fake_dns(host: str) -> list[str]:
        if host == "oa.example.org":
            return ["93.184.216.34"]
        if host == "internal.corp":
            return ["10.0.0.1"]
        return ["127.0.0.1"]

    ok, err, _ = download_pdf(
        "https://oa.example.org/start.pdf",
        target_file,
        opener=redirect_to_private,
        dns_resolver=fake_dns,
    )
    assert not ok
    assert err == "refused_private_or_local_target"

    # 2. Redirect to IPv6 loopback literal
    redirect_to_ipv6_loopback = _FakeOpener({
        "https://oa.example.org/start.pdf": HTTPError(
            "start", 302, "Found", {"Location": "https://[::1]/secret.pdf"}, None  # type: ignore[arg-type]
        ),
    })
    ok, err, _ = download_pdf(
        "https://oa.example.org/start.pdf",
        target_file,
        opener=redirect_to_ipv6_loopback,
        dns_resolver=fake_dns,
    )
    assert not ok
    assert err == "refused_private_or_local_target"

    # 3. Redirect downgrade to HTTP
    redirect_to_http = _FakeOpener({
        "https://oa.example.org/start.pdf": HTTPError(
            "start", 302, "Found", {"Location": "http://oa.example.org/doc.pdf"}, None  # type: ignore[arg-type]
        ),
    })
    ok, err, _ = download_pdf(
        "https://oa.example.org/start.pdf",
        target_file,
        opener=redirect_to_http,
        dns_resolver=fake_dns,
    )
    assert not ok
    assert err == "refused_non_https"

    # 4. Redirect with userinfo
    redirect_with_userinfo = _FakeOpener({
        "https://oa.example.org/start.pdf": HTTPError(
            "start", 302, "Found", {"Location": "https://admin:pass@oa.example.org/doc.pdf"}, None  # type: ignore[arg-type]
        ),
    })
    ok, err, _ = download_pdf(
        "https://oa.example.org/start.pdf",
        target_file,
        opener=redirect_with_userinfo,
        dns_resolver=fake_dns,
    )
    assert not ok
    assert err == "refused_userinfo"


def test_paper_pdf_userinfo_and_port_parsing_refusals(tmp_path: Any) -> None:
    """SSRF Prevention: userinfo in URLs and out-of-bounds/invalid ports are refused."""
    target_file = tmp_path / "test_userinfo.pdf"

    # Userinfo rejected
    ok, err, _ = download_pdf("https://user:pass@oa.example.org/paper.pdf", target_file)
    assert not ok
    assert err == "refused_userinfo"

    ok, err, _ = download_pdf("https://user@oa.example.org/paper.pdf", target_file)
    assert not ok
    assert err == "refused_userinfo"

    ok, err, _ = download_pdf("https://oa.example.org@evil.com/paper.pdf", target_file)
    assert not ok
    assert err == "refused_userinfo"

    # Port out of bounds (> 65535 or <= 0)
    ok, err, _ = download_pdf("https://oa.example.org:99999/paper.pdf", target_file)
    assert not ok
    assert err == "invalid_url"

    ok, err, _ = download_pdf("https://oa.example.org:0/paper.pdf", target_file)
    assert not ok
    assert err == "invalid_url"

    ok, err, _ = download_pdf("https://oa.example.org:notaport/paper.pdf", target_file)
    assert not ok
    assert err == "invalid_url"

    # Valid custom port (e.g. 8443) connected properly
    connected_targets: list[tuple[str, int]] = []

    def mock_factory(addr: tuple[str, int], timeout: Any = None, source_address: Any = None) -> Any:
        connected_targets.append(addr)
        mock_sock = MagicMock()
        mock_sock.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n%PDF-1.4\n"
        )
        return mock_sock

    mock_ssl_ctx = MagicMock()
    mock_ssl_ctx.wrap_socket.side_effect = lambda sock, server_hostname=None: sock

    ok, err, _ = download_pdf(
        "https://oa.example.org:8443/paper.pdf",
        target_file,
        dns_resolver=lambda _: ["93.184.216.34"],
        ssl_context=mock_ssl_ctx,
        connection_factory=mock_factory,
    )
    assert ok is True
    assert connected_targets == [("93.184.216.34", 8443)]


def test_paper_pdf_ipv6_and_special_addresses_comprehensive() -> None:
    """SSRF Prevention: loopback, link-local, private, broadcast, multicast, and IPv4-mapped/compatible IPv6 are refused."""
    # Loopback
    assert not is_safe_global_ip("127.0.0.1")
    assert not is_safe_global_ip("127.0.0.2")
    assert not is_safe_global_ip("::1")

    # Unspecified / Broadcast
    assert not is_safe_global_ip("0.0.0.0")
    assert not is_safe_global_ip("::")
    assert not is_safe_global_ip("255.255.255.255")

    # Private IPv4 & IPv6 (Unique Local)
    assert not is_safe_global_ip("10.0.0.1")
    assert not is_safe_global_ip("172.16.0.1")
    assert not is_safe_global_ip("192.168.1.1")
    assert not is_safe_global_ip("fc00::1")
    assert not is_safe_global_ip("fd00::1")

    # Link-local & interface indexes
    assert not is_safe_global_ip("169.254.169.254")
    assert not is_safe_global_ip("fe80::1")
    assert not is_safe_global_ip("fe80::1%eth0")

    # Multicast & Reserved
    assert not is_safe_global_ip("224.0.0.1")
    assert not is_safe_global_ip("ff02::1")
    assert not is_safe_global_ip("240.0.0.1")

    # CGNAT & Documentation & Benchmarking
    assert not is_safe_global_ip("100.64.0.1")
    assert not is_safe_global_ip("198.18.0.1")
    assert not is_safe_global_ip("2001:db8::1")

    # IPv4-mapped IPv6
    assert not is_safe_global_ip("::ffff:127.0.0.1")
    assert not is_safe_global_ip("::ffff:10.0.0.1")
    assert not is_safe_global_ip("::ffff:169.254.169.254")

    # Deprecated IPv4-compatible IPv6 (::/96)
    assert not is_safe_global_ip("::127.0.0.1")
    assert not is_safe_global_ip("::10.0.0.1")

    # Public Global IPs are permitted
    assert is_safe_global_ip("93.184.216.34")
    assert is_safe_global_ip("2606:4700:4700::1111")
    assert is_safe_global_ip("::ffff:93.184.216.34")
    assert is_safe_global_ip("::93.184.216.34")

    # Mixed DNS result (dual-homed public + private) fails closed
    ok, ips = resolve_and_verify_host(
        "split.example.org",
        dns_resolver=lambda _: ["93.184.216.34", "10.0.0.1"],
    )
    assert not ok
    assert ips == []


def test_paper_pdf_tls_certificate_hostname_mismatch_fails_closed(tmp_path: Any) -> None:
    """SSRF Prevention: TLS certificate hostname verification mismatch fails closed."""
    target_file = tmp_path / "cert_fail.pdf"

    mock_ssl_ctx = MagicMock()
    # Simulate SSLCertVerificationError / SSLError raised when server certificate does not match server_hostname
    mock_ssl_ctx.wrap_socket.side_effect = URLError("certificate verify failed: Hostname mismatch")

    def mock_factory(addr: tuple[str, int], timeout: Any = None, source_address: Any = None) -> Any:
        return MagicMock()

    ok, err, _ = download_pdf(
        "https://mismatch.example.org/paper.pdf",
        target_file,
        dns_resolver=lambda _: ["93.184.216.34"],
        ssl_context=mock_ssl_ctx,
        connection_factory=mock_factory,
    )
    assert not ok
    assert err == "network_error"
    assert not target_file.exists()
