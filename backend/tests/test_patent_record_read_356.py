"""Offline tests for Spec 356 — PATENT-RECORD-READ-1: Bounded patent publication lookup in Hermes.

Covers Acceptance Criteria 1–4:
1. Valid publication number returns available fields with source URL and observed_at time;
   missing fields remain explicitly unknown; malformed input sends no request.
2. Fixed-host fetch rejects redirects, oversized bodies and off-host EPO links.
   A 404 is not_found; network, challenge and parser failures are source_unavailable or unverified,
   never a positive status.
3. EP record carries Google label separately from official status; 403 EPO response leaves official
   status unverified; parseable official fixture supplies dated official status with its link.
4. Hermes exposes the tool only through its scoped grant and small read-tool set; existing per-turn
   research budget applies; egress, scope and token guards stay green.
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
from app.modules.agents.hermes.patent_read_tool import (
    GOOGLE_PATENTS_BASE_URL,
    GOOGLE_STATUS_DISCLAIMER,
    MAX_RESPONSE_BYTES,
    PATENT_ID_RE,
    _DenyRedirects,
    dispatch_patent_read_tool,
    execute_patent_read,
    parse_epo_register_html,
    parse_google_patents_html,
    validate_epo_register_url,
    validate_publication_number,
)
from app.modules.agents.hermes.supervisor import (
    _TOOL_CAPABILITIES,
    HERMES_TOOL_ALLOWLIST,
    PRODUCTION_CAPABILITY_REGISTRY,
    UPSTREAM_REVISION,
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

NOW = datetime.now(UTC)
SESSION = AgentSessionRef(
    jarvis_thread_id="thread-356",
    hermes_session_id="hermes-356",
    profile_id="isaac",
    workspace_id="workspace-356",
    generation=1,
    upstream_revision=UPSTREAM_REVISION,
)


class _FakeHTTPResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None, status: int = 200):
        self._stream = io.BytesIO(data)
        self.headers = headers or {"Content-Length": str(len(data)), "Content-Type": "text/html; charset=utf-8"}
        self.status = status

    def read(self, amt: int = -1) -> bytes:
        return self._stream.read(amt)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeOpener:
    def __init__(self, routes: dict[str, _FakeHTTPResponse | Exception]):
        self.routes = routes
        self.calls: list[str] = []

    def open(self, req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        for pattern, response in self.routes.items():
            if pattern in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise URLError(f"unexpected fake url: {url}")


def _existing_connection(conn: sqlite3.Connection):
    class Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            pass

    return Ctx()


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE ai_threads (id TEXT PRIMARY KEY, workspace_id TEXT, last_activity_at TEXT)")
    connection.execute("INSERT INTO ai_threads VALUES ('thread-356', 'workspace-356', '2000-01-01T00:00:00+00:00')")
    connection.execute(
        "CREATE TABLE ai_thread_interactions (id TEXT, thread_id TEXT, request_id TEXT, "
        "request_digest TEXT, interaction_index INTEGER, user_text TEXT, assistant_text TEXT, "
        "assistant_text_truncated INTEGER, flow_id TEXT, persistence_state TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, workspace_id TEXT, event_type TEXT, "
        "actor TEXT, target_type TEXT, target_id TEXT, payload TEXT, created_at TEXT)"
    )
    return connection


def _make_grant(
    capability_id: str = "jarvis.patent_read",
    grant_id: str = "grant-356",
    workspace_id: str = "workspace-356",
    thread_id: str = "thread-356",
) -> CapabilityGrantRef:
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        constraints={"route_class": "fake", "is_local": True},
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _make_call(
    arguments: dict[str, Any],
    capability_id: str = "jarvis.patent_read",
    grant_id: str = "grant-356",
) -> StructuredToolCall:
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id=grant_id,
        correlation_id=str(uuid4()),
        session_ref=SESSION,
        arguments=arguments,
        requested_at=NOW,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
    )


# --- Synthetic HTML Fixtures ---

SYNTHETIC_US_HTML = b"""<!DOCTYPE html>
<html>
<head>
  <meta name="DC.title" content="Synthetic Method for Sensor Calibration">
  <meta name="DC.contributor" scheme="assignee" content="Synthetic Dynamics LLC">
  <meta name="DC.date" scheme="priority" content="2018-03-12">
  <meta name="DC.date" scheme="dateSubmitted" content="2019-03-10">
  <meta name="DC.date" scheme="issue" content="2021-08-24">
</head>
<body>
  <dd itemprop="legalStatus">Active</dd>
  <section itemprop="claims">
    <div class="claim" num="1">
      <div class="claim-text">1. A calibration method comprising receiving a reference reading and adjusting a gain factor.</div>
    </div>
  </section>
</body>
</html>
"""

SYNTHETIC_EP_HTML = b"""<!DOCTYPE html>
<html>
<head>
  <meta name="DC.title" content="Synthetic Acoustic Transducer Assembly">
  <meta name="DC.contributor" scheme="assignee" content="Synthetic Aero SA">
  <meta name="DC.date" scheme="priority" content="2017-05-15">
  <meta name="DC.date" scheme="dateSubmitted" content="2018-05-10">
  <meta name="DC.date" scheme="issue" content="2020-11-04">
</head>
<body>
  <span itemprop="legalStatus">Pending</span>
  <div class="claim" num="1">
    <div class="claim-text">1. An acoustic transducer comprising a housing and a synthetic diaphragm.</div>
  </div>
  <a href="https://register.epo.org/application?number=EP18123456">European Patent Register</a>
</body>
</html>
"""

SYNTHETIC_EP_OFFICIAL_REGISTER_HTML = b"""<!DOCTYPE html>
<html>
<head><title>European Patent Register - EP18123456</title></head>
<body>
  <span id="status">Granted</span>
  <table class="epoline_events">
    <tr><td>2020-11-04</td><td>Mention of grant published</td></tr>
    <tr><td>2019-09-12</td><td>Substantive examination started</td></tr>
    <tr><td>2018-11-20</td><td>European search report published</td></tr>
  </table>
</body>
</html>
"""

SYNTHETIC_MINIMAL_HTML = b"""<!DOCTYPE html>
<html>
<head>
  <meta name="DC.title" content="Minimal Synthetic Record">
</head>
<body>
</body>
</html>
"""


# ===========================================================================
# 1. Canonical publication number validation tests
# ===========================================================================


def test_canonical_publication_number_validation() -> None:
    """Acceptance criterion 1: valid numbers pass; malformed input rejected."""
    assert PATENT_ID_RE.match("EP1234567A1") is not None
    assert PATENT_ID_RE.match("US10123456B2") is not None

    # Valid forms
    assert validate_publication_number("EP1234567A1") == "EP1234567A1"
    assert validate_publication_number("US10123456B2") == "US10123456B2"
    assert validate_publication_number("WO2020123456A1") == "WO2020123456A1"
    assert validate_publication_number("EP1234567") == "EP1234567"
    assert validate_publication_number("EP1234567B") == "EP1234567B"
    assert validate_publication_number("DE102020123456A1") == "DE102020123456A1"

    # Malformed inputs
    assert validate_publication_number("ep1234567a1") is None  # lowercase
    assert validate_publication_number(" EP1234567A1") is None  # leading space
    assert validate_publication_number("EP1234567A1 ") is None  # trailing space
    assert validate_publication_number("EP 1234567 A1") is None  # inner space
    assert validate_publication_number("https://patents.google.com/patent/EP1234567A1/en") is None
    assert validate_publication_number("EP1234") is None  # fewer than 5 digits
    assert validate_publication_number("EP1234567890123456789") is None  # 19 digits (max 18)
    assert validate_publication_number("EP1234567A12") is None  # 2 digits in kind code
    assert validate_publication_number("SELECT * FROM patents") is None  # words / injection
    assert validate_publication_number("") is None
    assert validate_publication_number(None) is None
    assert validate_publication_number(1234567) is None


def test_malformed_number_sends_no_network_request() -> None:
    """Acceptance criterion 1: malformed input sends NO network request."""
    fake_opener = _FakeOpener({})
    malformed_inputs = [
        "ep1234567a1",
        " EP1234567A1",
        "https://patents.google.com/patent/EP1234567A1/en",
        "EP 1234567",
        "DROP TABLE users",
        "",
    ]
    for bad in malformed_inputs:
        res = execute_patent_read(bad, opener=fake_opener)
        assert res["status"] == "error"
        assert res["error"] == "invalid_arguments"

    assert len(fake_opener.calls) == 0, "No network request must be made for malformed numbers"


# ===========================================================================
# 2. Synthetic Google Patents Parsing: US & EP
# ===========================================================================


def test_valid_us_publication_lookup() -> None:
    """Acceptance criterion 1: valid US publication returns compact sourced record with all fields."""
    url = f"{GOOGLE_PATENTS_BASE_URL}/US10123456B2/en"
    fake_opener = _FakeOpener({url: _FakeHTTPResponse(SYNTHETIC_US_HTML)})

    res = execute_patent_read("US10123456B2", opener=fake_opener)

    assert res["status"] == "succeeded"
    assert res["publication_number"] == "US10123456B2"
    assert res["title"] == "Synthetic Method for Sensor Calibration"
    assert res["applicants"] == ["Synthetic Dynamics LLC"]
    assert res["priority_date"] == "2018-03-12"
    assert res["filing_date"] == "2019-03-10"
    assert res["publication_date"] == "2021-08-24"
    assert res["google_legal_status"] == "Active"
    assert res["google_legal_status_disclaimer"] == GOOGLE_STATUS_DISCLAIMER
    assert "calibration method comprising" in res["claim_1"]
    assert res["source_url"] == url
    assert res["observed_at"] is not None
    # For US, official register is not queried
    assert res["official_status"] is None
    assert res["register_url"] is None
    assert res["dated_events"] == []
    assert len(fake_opener.calls) == 1


def test_parse_google_patents_html_directly() -> None:
    """Acceptance criterion 1: direct parser extracts fields correctly."""
    parsed = parse_google_patents_html(SYNTHETIC_US_HTML.decode("utf-8"), "US10123456B2")
    assert parsed["title"] == "Synthetic Method for Sensor Calibration"
    assert parsed["applicants"] == ["Synthetic Dynamics LLC"]
    assert parsed["google_legal_status"] == "Active"


def test_missing_fields_stay_explicitly_unknown() -> None:
    """Acceptance criterion 1: missing fields remain explicitly unknown or empty; never invented."""
    url = f"{GOOGLE_PATENTS_BASE_URL}/US9999999B1/en"
    fake_opener = _FakeOpener({url: _FakeHTTPResponse(SYNTHETIC_MINIMAL_HTML)})

    res = execute_patent_read("US9999999B1", opener=fake_opener)

    assert res["status"] == "succeeded"
    assert res["title"] == "Minimal Synthetic Record"
    assert res["applicants"] == []
    assert res["priority_date"] == "unknown"
    assert res["filing_date"] == "unknown"
    assert res["publication_date"] == "unknown"
    assert res["google_legal_status"] == "unknown"
    assert res["claim_1"] == "unknown"
    assert res["google_legal_status_disclaimer"] == GOOGLE_STATUS_DISCLAIMER


# ===========================================================================
# 3. Fixed-host Fetch: Redirects, Oversize, Timeout, 404
# ===========================================================================


def test_404_returns_not_found() -> None:
    """Acceptance criterion 2: 404 from Google Patents returns status not_found."""
    url = f"{GOOGLE_PATENTS_BASE_URL}/EP4040404A1/en"
    http_404 = HTTPError(url, 404, "Not Found", {}, None)
    fake_opener = _FakeOpener({url: http_404})

    res = execute_patent_read("EP4040404A1", opener=fake_opener)

    assert res["status"] == "not_found"
    assert res["error"] == "not_found"
    assert res["publication_number"] == "EP4040404A1"
    assert res["source_url"] == url


def test_redirect_blocked_fails_closed() -> None:
    """Acceptance criterion 2: HTTP redirects are blocked and fail closed."""
    url = f"{GOOGLE_PATENTS_BASE_URL}/EP3020000A1/en"
    redirect_err = HTTPError(url, 302, "Found", {"Location": "https://evil.com"}, None)
    fake_opener = _FakeOpener({url: redirect_err})

    res = execute_patent_read("EP3020000A1", opener=fake_opener)

    assert res["status"] == "source_unavailable"
    assert res["error"] == "source_unavailable"
    assert res["detail"] == "redirect_blocked"


def test_deny_redirects_handler() -> None:
    """Acceptance criterion 2: _DenyRedirects handler refuses redirection."""
    handler = _DenyRedirects()
    req = MagicMock()
    assert handler.redirect_request(req, MagicMock(), 302, "Found", {}, "https://example.com") is None


def test_oversized_response_blocked() -> None:
    """Acceptance criterion 2: oversized response (> 512 KB) fails closed."""
    url = f"{GOOGLE_PATENTS_BASE_URL}/EP5120000A1/en"
    huge_body = b"X" * (MAX_RESPONSE_BYTES + 10)
    fake_opener = _FakeOpener({url: _FakeHTTPResponse(huge_body)})

    res = execute_patent_read("EP5120000A1", opener=fake_opener)

    assert res["status"] == "source_unavailable"
    assert res["error"] == "source_unavailable"
    assert res["detail"] == "oversize_response"


def test_network_timeout_fails_closed() -> None:
    """Acceptance criterion 2: timeout returns source_unavailable."""
    url = f"{GOOGLE_PATENTS_BASE_URL}/EP1000000A1/en"
    fake_opener = _FakeOpener({url: TimeoutError("connection timed out")})

    res = execute_patent_read("EP1000000A1", opener=fake_opener)

    assert res["status"] == "source_unavailable"
    assert res["error"] == "source_unavailable"


# ===========================================================================
# 4. EP Record & Official Register (Criteria 2 & 3)
# ===========================================================================


def test_ep_record_with_parseable_official_register() -> None:
    """Acceptance criterion 3: EP record supplies dated official status with its link."""
    google_url = f"{GOOGLE_PATENTS_BASE_URL}/EP18123456A1/en"
    epo_url = "https://register.epo.org/application?number=EP18123456"

    fake_opener = _FakeOpener({
        google_url: _FakeHTTPResponse(SYNTHETIC_EP_HTML),
        epo_url: _FakeHTTPResponse(SYNTHETIC_EP_OFFICIAL_REGISTER_HTML),
    })

    res = execute_patent_read("EP18123456A1", opener=fake_opener)

    assert res["status"] == "succeeded"
    assert res["publication_number"] == "EP18123456A1"
    assert res["google_legal_status"] == "Pending"
    assert res["google_legal_status_disclaimer"] == GOOGLE_STATUS_DISCLAIMER
    # Official status is separate from Google label
    assert res["official_status"] == "Granted"
    assert res["official_status_error"] is None
    assert res["register_url"] == epo_url
    assert len(res["dated_events"]) == 3
    assert res["dated_events"][0] == {"date": "2020-11-04", "event": "Mention of grant published"}
    assert len(fake_opener.calls) == 2


def test_parse_epo_register_html_directly() -> None:
    """Acceptance criterion 3: direct EPO parser extracts status and events."""
    status, events, err = parse_epo_register_html(SYNTHETIC_EP_OFFICIAL_REGISTER_HTML.decode("utf-8"))
    assert status == "Granted"
    assert err is None
    assert len(events) == 3


def test_ep_record_with_403_official_register_leaves_unverified() -> None:
    """Acceptance criterion 3: 403 EPO response leaves official status unverified with error category."""
    google_url = f"{GOOGLE_PATENTS_BASE_URL}/EP18123456A1/en"
    epo_url = "https://register.epo.org/application?number=EP18123456"
    http_403 = HTTPError(epo_url, 403, "Forbidden", {}, None)

    fake_opener = _FakeOpener({
        google_url: _FakeHTTPResponse(SYNTHETIC_EP_HTML),
        epo_url: http_403,
    })

    res = execute_patent_read("EP18123456A1", opener=fake_opener)

    # Patent record still succeeds from Google Patents
    assert res["status"] == "succeeded"
    assert res["publication_number"] == "EP18123456A1"
    assert res["google_legal_status"] == "Pending"
    # But official status is unverified
    assert res["official_status"] == "unverified"
    assert res["official_status_error"] == "http_403_challenge"
    assert res["register_url"] == epo_url
    assert res["dated_events"] == []


def test_ep_record_with_cloudflare_challenge_on_official_register() -> None:
    """Acceptance criterion 3: Cloudflare challenge on EPO Register leaves official status unverified."""
    google_url = f"{GOOGLE_PATENTS_BASE_URL}/EP18123456A1/en"
    epo_url = "https://register.epo.org/application?number=EP18123456"
    cf_challenge = b"<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"

    fake_opener = _FakeOpener({
        google_url: _FakeHTTPResponse(SYNTHETIC_EP_HTML),
        epo_url: _FakeHTTPResponse(cf_challenge),
    })

    res = execute_patent_read("EP18123456A1", opener=fake_opener)

    assert res["status"] == "succeeded"
    assert res["official_status"] == "unverified"
    assert res["official_status_error"] == "challenge_detected"
    assert res["register_url"] == epo_url


def test_forged_off_host_epo_link_rejected() -> None:
    """Acceptance criterion 2: off-host or malformed EPO link is rejected; no off-host request sent."""
    forged_google_html = b"""<!DOCTYPE html>
<html><head><meta name="DC.title" content="Synthetic Test"></head>
<body>
  <a href="https://evil.attacker.com/fake?number=EP18123456">European Patent Register</a>
</body></html>
"""

    google_url = f"{GOOGLE_PATENTS_BASE_URL}/EP18123456A1/en"
    fake_opener = _FakeOpener({google_url: _FakeHTTPResponse(forged_google_html)})

    res = execute_patent_read("EP18123456A1", opener=fake_opener)

    assert res["status"] == "succeeded"
    assert res["official_status"] == "unverified"
    assert res["official_status_error"] == "off_host_link_rejected"
    assert res["register_url"] is None
    # No second network call made to evil.attacker.com
    assert len(fake_opener.calls) == 1
    assert "evil.attacker.com" not in fake_opener.calls[0]


def test_validate_epo_register_url_rejects_insecure_http_and_subdomains() -> None:
    """Validate strict HTTPS, exact host register.epo.org and valid application syntax."""
    assert validate_epo_register_url("https://register.epo.org/application?number=EP18123456") is not None
    assert validate_epo_register_url("https://register.epo.org/ipfwretrieve?ap=18123456") is not None

    # Insecure HTTP
    assert validate_epo_register_url("http://register.epo.org/application?number=EP18123456") is None
    # Subdomain spoofing
    assert validate_epo_register_url("https://register.epo.org.attacker.com/app") is None
    # Different host
    assert validate_epo_register_url("https://epo.org/application?number=EP18123456") is None
    # Missing valid application identifier
    assert validate_epo_register_url("https://register.epo.org/application?number=foo_bar") is None
    assert validate_epo_register_url("https://register.epo.org/evil/path") is None


# ===========================================================================
# 5. Hermes Integration: Scoped Grant, Broker, Supervisor, Research Budget
# ===========================================================================


def test_dispatch_patent_read_tool_requires_valid_grant() -> None:
    """Acceptance criterion 4: tool dispatch enforces valid scoped grant."""
    call = _make_call({"publication_number": "US10123456B2"})

    # 1. No grant provided in live_grants -> refused
    res_no_grant = dispatch_tool(call, live_grants={})
    assert res_no_grant.status == "refused"
    assert res_no_grant.error_code == "capability_denied"

    # 2. Expired grant -> refused
    expired_grant = CapabilityGrantRef(
        grant_id="grant-expired",
        capability_id="jarvis.patent_read",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id="workspace-356", jarvis_thread_id="thread-356"),
        constraints={"route_class": "fake", "is_local": True},
        issued_at=NOW - timedelta(minutes=20),
        expires_at=NOW - timedelta(minutes=5),
    )
    call_exp = _make_call({"publication_number": "US10123456B2"}, grant_id="grant-expired")
    res_expired = dispatch_tool(call_exp, live_grants={"grant-expired": expired_grant})
    assert res_expired.status == "refused"

    # 3. Wrong capability grant -> refused
    paper_grant = _make_grant("jarvis.paper_search", "grant-paper")
    call_wrong = _make_call({"publication_number": "US10123456B2"}, grant_id="grant-paper")
    res_wrong = dispatch_tool(call_wrong, live_grants={"grant-paper": paper_grant})
    assert res_wrong.status == "refused"

    # 4. Valid grant + fake opener -> succeeded
    valid_grant = _make_grant("jarvis.patent_read", "grant-valid")
    call_valid = _make_call({"publication_number": "US10123456B2"}, grant_id="grant-valid")
    url = f"{GOOGLE_PATENTS_BASE_URL}/US10123456B2/en"
    fake_opener = _FakeOpener({url: _FakeHTTPResponse(SYNTHETIC_US_HTML)})

    res_valid = dispatch_tool(
        call_valid, live_grants={"grant-valid": valid_grant}, patent_read_opener=fake_opener
    )
    assert res_valid.status == "succeeded"
    assert res_valid.result["title"] == "Synthetic Method for Sensor Calibration"


def test_dispatch_patent_read_tool_directly() -> None:
    """Acceptance criterion 4: direct dispatch_patent_read_tool argument checking."""
    # Malformed arguments -> invalid_arguments
    bad_call = _make_call({"publication_number": "malformed_number"})
    res, err = dispatch_patent_read_tool(bad_call, "workspace-356", "default_err")
    assert res is None
    assert err == "invalid_arguments"


def test_patent_read_research_budget_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion 4: patent reads count against Hermes research budget."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: _existing_connection(conn))
    supervisor_inst = HermesSupervisor("unused", runner=MagicMock())
    supervisor_inst.session = SESSION
    grant = _make_grant("jarvis.patent_read", "grant-r")
    supervisor_inst.live_grants = {"grant-r": grant}
    supervisor_inst.research_budget = 2

    # Opener returns valid HTML for distinct patent numbers
    routes = {
        f"{GOOGLE_PATENTS_BASE_URL}/US1000000{i}B2/en": _FakeHTTPResponse(SYNTHETIC_US_HTML)
        for i in range(5)
    }
    supervisor_inst.patent_read_opener = _FakeOpener(routes)

    sent_frames: list[dict[str, Any]] = []
    supervisor_inst._send = lambda f: sent_frames.append(f)

    # Call 1 & 2 succeed within budget of 2
    for i in range(2):
        frame = {
            "id": f"call-{i}",
            "session_ref": SESSION.model_dump(mode="json"),
            "arguments": {
                "tool_name": "jarvis_patent_read",
                "grant_id": "grant-r",
                "publication_number": f"US1000000{i}B2",
            },
        }
        supervisor_inst._handle_tool(frame)

    assert supervisor_inst.turn_research_count == 2
    assert sent_frames[0]["tool_result"]["status"] == "succeeded"
    assert sent_frames[1]["tool_result"]["status"] == "succeeded"

    # Call 3 exceeds research budget
    frame_exceed = {
        "id": "call-exceed",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {
            "tool_name": "jarvis_patent_read",
            "grant_id": "grant-r",
            "publication_number": "US10000003B2",
        },
    }
    supervisor_inst._handle_tool(frame_exceed)
    res_exceed = sent_frames[-1]["tool_result"]
    assert "Research budget used up (2 searches/reads). Do not search again" in str(res_exceed["result"])
    assert "answer now with what you have" in str(res_exceed["result"]).lower()


def test_is_research_tool_includes_patent_read() -> None:
    """Acceptance criterion 4: is_research_tool recognizes patent_read variants."""
    assert is_research_tool("patent_read")
    assert is_research_tool("jarvis_patent_read")
    assert is_research_tool("mcp__jarvis__patent_read")
    assert is_research_tool("mcp__jarvis__jarvis_patent_read")
    assert "jarvis_patent_read" in _TOOL_CAPABILITIES
    assert "mcp__jarvis__jarvis_patent_read" in HERMES_TOOL_ALLOWLIST
    assert any(c.capability_id == "jarvis.patent_read" for c in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads"))


def test_broker_mcp_publishes_patent_read_tool() -> None:
    """Acceptance criterion 4: Broker MCP registers jarvis_patent_read schema."""
    tool_names = [tool["name"] for tool in broker_mcp._TOOLS]
    assert "jarvis_patent_read" in tool_names

    patent_tool = next(t for t in broker_mcp._TOOLS if t["name"] == "jarvis_patent_read")
    schema = patent_tool["inputSchema"]
    assert schema["type"] == "object"
    assert "publication_number" in schema["properties"]
    assert "grant_id" in schema["properties"]
    assert set(schema["required"]) == {"grant_id", "publication_number"}


def test_worker_shim_admits_and_promotes_patent_read() -> None:
    """Acceptance criterion 4: worker shim admits jarvis_patent_read and promotes patent_read alias."""
    assert "jarvis_patent_read" in worker_shim.ADMITTED_TOOLS
    assert "mcp__jarvis__jarvis_patent_read" in worker_shim.ADMITTED_TOOLS

    # Bare 'patent_read' proposal is promoted to permitted 'mcp__jarvis__jarvis_patent_read'
    proposal = json.dumps({"tool_calls": [{"name": "patent_read", "arguments": {"publication_number": "US10123456B2"}}]})
    permitted = [{"function": {"name": "mcp__jarvis__jarvis_patent_read"}}]
    res = worker_shim.completion_message(proposal, permitted)
    assert res.get("tool_calls") is not None
    assert res["tool_calls"][0]["function"]["name"] == "mcp__jarvis__jarvis_patent_read"


def test_thread_service_installs_patent_read_grant_and_guidance() -> None:
    """Acceptance criterion 4: thread_service installs jarvis.patent_read grant and prompt guidance."""
    from types import SimpleNamespace

    worker = SimpleNamespace(live_grants={})
    payload = MagicMock()
    payload.route_class = "hermes:agent"
    payload.task_kind = "general"
    payload.jarvis_context = None

    instructions = thread_service._install_hermes_retrieval_grant(
        worker, payload, "workspace-356", "thread-356"
    )

    # Scoped grant issued
    patent_grant = next(
        (g for g in worker.live_grants.values() if g.capability_id == "jarvis.patent_read"), None
    )
    assert patent_grant is not None
    assert patent_grant.scope.workspace_id == "workspace-356"
    assert patent_grant.scope.jarvis_thread_id == "thread-356"

    # Prompt guidance includes grant id and tool name
    assert "patent_read_grant_id=" in instructions
    assert "jarvis_patent_read" in instructions

    # Turn tools list includes mcp__jarvis__jarvis_patent_read
    turn_tools = thread_service._turn_tools("general", "Read patent EP1234567")
    assert "mcp__jarvis__jarvis_patent_read" in turn_tools


def test_egress_and_secret_blocking() -> None:
    """Acceptance criterion 4: egress checks fail closed on secrets and private content."""
    secret_pub_num = "EP123456789AKIAIOSFODNN7EXAMPLE"  # Contains AWS key pattern
    res = execute_patent_read(secret_pub_num)
    assert res["status"] == "error"
    # Even if regex rejects it first or egress boundary rejects it, status is error
    assert res["error"] in {"invalid_arguments", "egress_blocked_private_content"}


def test_trailing_newline_userinfo_fragment_and_html_entity_epo_link() -> None:
    """Regression: reject trailing newline, URL userinfo, fragments; unescape &amp; in EPO href."""
    assert validate_publication_number("EP1234567A1\n") is None
    assert validate_epo_register_url("https://register.epo.org/application?number=EP18123456\n") is None
    assert validate_epo_register_url("https://evil.com@register.epo.org/application?number=EP18123456") is None
    assert validate_epo_register_url("https://register.epo.org/application?number=EP18123456#frag") is None

    html_doc = (
        '<html><body><a href="https://register.epo.org/application?number=EP18123456&amp;tab=main">'
        "EPO Register</a></body></html>"
    )
    parsed = parse_google_patents_html(html_doc, "EP1234567A1")
    assert parsed["epo_link"] == "https://register.epo.org/application?number=EP18123456&tab=main"
    assert validate_epo_register_url(parsed["epo_link"]) == parsed["epo_link"]
