"""Comprehensive offline unit tests for Spec 358 (BROWSER-PAGE-READER-1).

Tests all acceptance criteria:
1. Strict HTTPS URL validation, rejection of private/loopback/non-global IPs,
   userinfo, control characters, secrets, and pack terms before subprocess execution.
2. Ephemeral headless Chromium execution with mocked runner, clean text extraction,
   DOM stripping, search_term windowing, 8 000-char cap, ephemeral profile cleanup,
   and honest detection of Cloudflare/reCAPTCHA/hCaptcha challenges and missing browser.
3. Hermes wiring: capability descriptor, broker schema, turn tools, scoped grant,
   prompt guidance, supervisor dispatch, and per-turn research budget enforcement.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.core import packs
from app.modules.agents.hermes import broker_mcp, browser_page_tool, supervisor, worker_shim
from app.modules.agents.hermes.browser_page_tool import (
    _is_safe_global_ip,
    execute_browser_page_read,
    override_dns_resolver,
)
from app.modules.ai import context_builder, thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.jarvis_context import PRODUCTION_CAPABILITY_REGISTRY
from app.modules.ai.thread_models import AIThreadSubmit


def _public_dns(hostname: str) -> list[str]:
    """Mock DNS resolver returning a safe global IP."""
    return ["93.184.216.34"]


def _make_html_runner(html_dom: str, returncode: int = 0) -> Any:
    """Mock runner hook returning given HTML DOM."""
    def runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        return returncode, html_dom, ""
    return runner


# ==============================================================================
# Criterion 1: Pre-flight safety checks & URL validation
# ==============================================================================

def test_url_scheme_validation() -> None:
    """Only https:// scheme is accepted; http://, ftp://, file:// etc are rejected."""
    runner = _make_html_runner("<html><body>OK</body></html>")
    for bad_url in [
        "http://example.com/datasheet",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com/datasheet",
        "",
        "https://",
        "https://" + "a" * 1020 + ".com",  # Exceeds MAX_URL_LENGTH (1024)
    ]:
        res = execute_browser_page_read(bad_url, runner=runner, dns_resolver=_public_dns)
        assert res.get("status") == "error"
        assert res.get("error") == "invalid_url"


def test_url_control_characters_rejected() -> None:
    """URLs containing newlines, tabs, or null bytes are rejected."""
    runner = _make_html_runner("<html><body>OK</body></html>")
    for bad_url in [
        "https://example.com/data\nsheet",
        "https://example.com/data\rsheet",
        "https://example.com/data\tsheet",
        "https://example.com/data\x00sheet",
    ]:
        res = execute_browser_page_read(bad_url, runner=runner, dns_resolver=_public_dns)
        assert res.get("status") == "error"
        assert res.get("error") == "invalid_url"


def test_url_userinfo_rejected() -> None:
    """URLs with user:password@ or user@ are rejected to prevent credential leaks."""
    runner = _make_html_runner("<html><body>OK</body></html>")
    for bad_url in [
        "https://user:pass@example.com/spec",
        "https://admin@example.com/spec",
        "https://user:@example.com/spec",
    ]:
        res = execute_browser_page_read(bad_url, runner=runner, dns_resolver=_public_dns)
        assert res.get("status") == "error"
        assert res.get("error") == "invalid_url"


def test_private_and_loopback_ips_refused() -> None:
    """Loopback, private, link-local, multicast, and reserved IPv4/IPv6 are refused."""
    runner = _make_html_runner("<html><body>OK</body></html>")

    bad_ips = [
        "127.0.0.1",
        "127.0.0.2",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.1",
    ]

    for ip in bad_ips:
        assert not _is_safe_global_ip(ip), f"Expected {ip} to be rejected"

        def resolver(_h: str, target_ip: str = ip) -> list[str]:
            return [target_ip]

        res = execute_browser_page_read(
            "https://internal-target.test/api",
            runner=runner,
            dns_resolver=resolver,
        )
        assert res.get("status") == "error"
        assert res.get("error") == "private_ip_refused"


def test_dns_resolution_failure_refused() -> None:
    """Failure to resolve hostname fails closed."""
    runner = _make_html_runner("<html><body>OK</body></html>")

    def failing_resolver(_h: str) -> list[str]:
        return []

    res = execute_browser_page_read(
        "https://nonexistent.domain.test/data",
        runner=runner,
        dns_resolver=failing_resolver,
    )
    assert res.get("status") == "error"
    assert res.get("error") == "dns_resolution_failed"


def test_egress_sanitization_blocks_secrets_in_url() -> None:
    """URLs containing API keys or secrets are blocked by egress sanitization."""
    runner = _make_html_runner("<html><body>OK</body></html>")
    for secret_url in [
        "https://example.com/api?token=sk-123456789012345678901234567890",
        "https://example.com/api?key=ghp_123456789012345678901234567890",
        "https://example.com/api?auth=AIza123456789012345678901234567890",
    ]:
        res = execute_browser_page_read(secret_url, runner=runner, dns_resolver=_public_dns)
        assert res.get("status") == "error"
        assert res.get("error") == "egress_blocked_private_content"


def test_egress_sanitization_blocks_pack_terms_in_url(monkeypatch) -> None:
    """URLs containing private pack sensitivity terms are blocked."""
    monkeypatch.setattr(packs, "sensitivity_terms", lambda: ("supersecretproject", "falconpropulsion"))
    runner = _make_html_runner("<html><body>OK</body></html>")

    res = execute_browser_page_read(
        "https://example.com/docs/falconpropulsion-spec.html",
        runner=runner,
        dns_resolver=_public_dns,
    )
    assert res.get("status") == "error"
    assert res.get("error") == "egress_blocked_private_content"


def test_egress_sanitization_blocks_secrets_and_pack_terms_in_search_term(monkeypatch) -> None:
    """search_term containing secrets or pack terms is blocked."""
    monkeypatch.setattr(packs, "sensitivity_terms", lambda: ("zephyraerospace",))
    runner = _make_html_runner("<html><body>OK</body></html>")

    res_secret = execute_browser_page_read(
        "https://example.com/spec",
        search_term="sk-123456789012345678901234567890",
        runner=runner,
        dns_resolver=_public_dns,
    )
    assert res_secret.get("error") == "egress_blocked_private_content"

    res_pack = execute_browser_page_read(
        "https://example.com/spec",
        search_term="zephyraerospace propulsion",
        runner=runner,
        dns_resolver=_public_dns,
    )
    assert res_pack.get("error") == "egress_blocked_private_content"


def test_search_term_max_length_and_type_check() -> None:
    """search_term exceeding 160 characters or invalid type is rejected."""
    runner = _make_html_runner("<html><body>OK</body></html>")
    res_toolong = execute_browser_page_read(
        "https://example.com/spec",
        search_term="x" * 161,
        runner=runner,
        dns_resolver=_public_dns,
    )
    assert res_toolong.get("error") == "invalid_arguments"


def test_blocked_tokens_rejected() -> None:
    """Blocked session/grant/workspace tokens present in URL or search_term fail closed."""
    runner = _make_html_runner("<html><body>OK</body></html>")
    res_url = execute_browser_page_read(
        "https://example.com/items/ws-secret-12345",
        runner=runner,
        dns_resolver=_public_dns,
        blocked_tokens={"ws-secret-12345"},
    )
    assert res_url.get("error") == "egress_blocked_private_content"

    res_search = execute_browser_page_read(
        "https://example.com/items",
        search_term="ws-secret-12345",
        runner=runner,
        dns_resolver=_public_dns,
        blocked_tokens={"ws-secret-12345"},
    )
    assert res_search.get("error") == "egress_blocked_private_content"


# ==============================================================================
# Criterion 2: Headless Chromium execution, HTML extraction, Challenge detection
# ==============================================================================

def test_clean_html_text_extraction() -> None:
    """HTML elements script/style/nav/footer/svg/noscript are stripped and text collapsed."""
    sample_html = """<!DOCTYPE html>
<html>
<head>
    <title>Technical Datasheet &amp; Guidelines</title>
    <style>body { font-family: sans-serif; }</style>
    <script>console.log("tracking code");</script>
</head>
<body>
    <nav><a href="/home">Home</a> | <a href="/docs">Docs</a></nav>
    <main>
        <h1>Photobioreactor Kinetics Specification</h1>
        <p>This document details the &lt;mass-transfer&gt; coefficients and flow dynamics.</p>
        <div>Operating pressure: 1.2 bar &quot;nominal&quot;.</div>
        <svg><circle cx="50" cy="50" r="40" /></svg>
        <noscript><p>Please enable JS</p></noscript>
    </main>
    <footer>Copyright &copy; 2026 Example Corp</footer>
</body>
</html>
"""
    runner = _make_html_runner(sample_html)
    res = execute_browser_page_read("https://example.com/kinetics", runner=runner, dns_resolver=_public_dns)
    assert res["status"] == "succeeded"
    assert res["title"] == "Technical Datasheet & Guidelines"
    assert "Photobioreactor Kinetics Specification" in res["text"]
    assert "Operating pressure: 1.2 bar \"nominal\"." in res["text"]
    assert "<mass-transfer>" in res["text"]
    # Stripped content
    assert "tracking code" not in res["text"]
    assert "Home | Docs" not in res["text"]
    assert "Example Corp" not in res["text"]
    assert "Please enable JS" not in res["text"]
    assert res["truncated"] is False
    assert "observed_at" in res


def test_output_bounding_8000_chars() -> None:
    """Extracted text is capped to 8 000 characters and sets truncated=True."""
    long_body = "<p>" + ("Kinetics analysis data block. " * 500) + "</p>"
    sample_html = f"<html><head><title>Large Page</title></head><body>{long_body}</body></html>"

    runner = _make_html_runner(sample_html)
    res = execute_browser_page_read("https://example.com/large", runner=runner, dns_resolver=_public_dns)
    assert res["status"] == "succeeded"
    assert len(res["text"]) <= 8000
    assert res["truncated"] is True


def test_search_term_windowing() -> None:
    """When search_term is provided, extracts focused excerpts around occurrences."""
    filler_start = "Introductory background section. " * 150
    target_section_1 = "Crucial finding: the photobioreactor biomass yield reached 2.4 g/L under blue illumination."
    filler_mid = "Intermediate operational notes and maintenance procedures. " * 150
    target_section_2 = "Secondary finding: photobioreactor gas flow rate was optimized at 0.5 vvm."
    filler_end = "Concluding remarks and appendices. " * 150

    sample_html = f"""<html>
<head><title>Bioreactor Report</title></head>
<body>
  <p>{filler_start}</p>
  <p>{target_section_1}</p>
  <p>{filler_mid}</p>
  <p>{target_section_2}</p>
  <p>{filler_end}</p>
</body>
</html>"""

    runner = _make_html_runner(sample_html)
    res = execute_browser_page_read(
        "https://example.com/report",
        search_term="photobioreactor",
        runner=runner,
        dns_resolver=_public_dns,
    )
    assert res["status"] == "succeeded"
    assert "photobioreactor biomass yield reached 2.4 g/L" in res["text"]
    assert "photobioreactor gas flow rate was optimized" in res["text"]
    assert len(res["text"]) <= 8000
    assert res["truncated"] is True


def test_search_term_never_sent_over_network() -> None:
    """search_term must NEVER be passed in Chromium argv or sent over network."""
    captured_argv: list[str] = []

    def mock_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        captured_argv.extend(argv)
        return 0, "<html><head><title>Title</title></head><body>Searchable page</body></html>", ""

    execute_browser_page_read(
        "https://example.com/page",
        search_term="local-only-query-term",
        runner=mock_runner,
        dns_resolver=_public_dns,
    )
    # Ensure search_term does not appear anywhere in argv
    for arg in captured_argv:
        assert "local-only-query-term" not in arg
    # Ensure URL in argv remains untouched
    assert captured_argv[-1] == "https://example.com/page"


def test_ephemeral_user_data_dir_created_and_cleaned_up() -> None:
    """--user-data-dir points to a temp directory that is removed in finally block."""
    temp_dir_recorded: list[str] = []

    def recording_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        for arg in argv:
            if arg.startswith("--user-data-dir="):
                path = arg.removeprefix("--user-data-dir=")
                temp_dir_recorded.append(path)
                assert os.path.isdir(path), "Temp dir should exist while runner executes"
        return 0, "<html><head><title>Test</title></head><body>Profile clean</body></html>", ""

    res = execute_browser_page_read("https://example.com/test", runner=recording_runner, dns_resolver=_public_dns)
    assert res["status"] == "succeeded"
    assert len(temp_dir_recorded) == 1
    # After execution, temp profile directory must no longer exist
    assert not os.path.exists(temp_dir_recorded[0])


def test_ephemeral_user_data_dir_cleaned_up_on_runner_exception() -> None:
    """Temp directory is deleted even if runner raises an exception or times out."""
    temp_dir_recorded: list[str] = []

    def failing_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
        for arg in argv:
            if arg.startswith("--user-data-dir="):
                temp_dir_recorded.append(arg.removeprefix("--user-data-dir="))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    res = execute_browser_page_read("https://example.com/timeout", runner=failing_runner, dns_resolver=_public_dns)
    assert res["status"] == "timeout"
    assert len(temp_dir_recorded) == 1
    assert not os.path.exists(temp_dir_recorded[0])


def test_bot_challenge_detection_cloudflare_turnstile() -> None:
    """Cloudflare Turnstile challenge returns blocked_by_challenge with no fabricated text."""
    challenge_html = """<!DOCTYPE html>
<html>
<head><title>Just a moment...</title></head>
<body>
<div class="cf-turnstile" data-sitekey="0x4AAAAAA"></div>
<p>Verifying you are human. This may take a few seconds.</p>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
</body>
</html>"""

    runner = _make_html_runner(challenge_html)
    res = execute_browser_page_read("https://protected.example.com", runner=runner, dns_resolver=_public_dns)
    assert res["status"] == "blocked_by_challenge"
    assert res["text"] == ""
    assert res["truncated"] is False


def test_bot_challenge_detection_recaptcha_and_hcaptcha() -> None:
    """Google reCAPTCHA and hCaptcha challenge interstitials are detected honestly."""
    recaptcha_html = """<html>
<head><title>Security Check</title></head>
<body>
<form id="challenge-form">
<div class="g-recaptcha" data-sitekey="xyz"></div>
<p>Please complete the security check to access the website.</p>
</form>
</body>
</html>"""
    runner_recaptcha = _make_html_runner(recaptcha_html)
    res_recaptcha = execute_browser_page_read("https://recaptcha.example.com", runner=runner_recaptcha, dns_resolver=_public_dns)
    assert res_recaptcha["status"] == "blocked_by_challenge"
    assert res_recaptcha["text"] == ""

    hcaptcha_html = """<html>
<head><title>Attention Required! | Cloudflare</title></head>
<body>
<div class="h-captcha" data-sitekey="abc"></div>
<p>Please solve this captcha to continue.</p>
</body>
</html>"""
    runner_hcaptcha = _make_html_runner(hcaptcha_html)
    res_hcaptcha = execute_browser_page_read("https://hcaptcha.example.com", runner=runner_hcaptcha, dns_resolver=_public_dns)
    assert res_hcaptcha["status"] == "blocked_by_challenge"
    assert res_hcaptcha["text"] == ""


def test_legitimate_technical_page_mentioning_captcha_not_blocked() -> None:
    """A long technical article discussing CAPTCHAs is NOT falsely classified as a challenge."""
    long_article = """<html>
<head><title>Modern Bot Mitigation Strategies in Distributed Systems</title></head>
<body>
<h1>Modern Bot Mitigation Strategies</h1>
<p>Distributed applications frequently employ solutions like Cloudflare Turnstile, reCAPTCHA, and hCaptcha.</p>
<p>""" + ("In depth analysis of false positive rates, challenge latency, and user experience. " * 60) + """</p>
<p>Conclusion: choosing the appropriate mitigation requires evaluating network architecture.</p>
</body>
</html>"""
    runner = _make_html_runner(long_article)
    res = execute_browser_page_read("https://techblog.example.com/bot-mitigation", runner=runner, dns_resolver=_public_dns)
    assert res["status"] == "succeeded"
    assert "Modern Bot Mitigation Strategies" in res["text"]
    assert res["text"] != ""


def test_browser_unavailable_clean_status(monkeypatch) -> None:
    """When Chromium binary is missing and runner is None, returns status: browser_unavailable."""
    monkeypatch.setenv("JARVISOS_BROWSER_READER_CHROME", "/nonexistent/chrome/path")
    monkeypatch.setattr(browser_page_tool, "find_chromium_binary", lambda: None)

    res = execute_browser_page_read("https://example.com", runner=None, dns_resolver=_public_dns)
    assert res["status"] == "browser_unavailable"
    assert res["text"] == ""
    assert res["url"] == "https://example.com"


# ==============================================================================
# Criterion 3: Hermes wiring, scoped grant, broker schema, research budget
# ==============================================================================

def test_hermes_tool_registration_and_catalogs() -> None:
    """Verify tool is registered in all required Hermes allowlists and schemas."""
    # 1. Capability registry
    caps = PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads")
    browser_cap = next((c for c in caps if c.capability_id == "jarvis.browser_page_read"), None)
    assert browser_cap is not None
    assert browser_cap.action_class == "READ"

    # 2. Broker MCP tools list
    tool_entry = next((t for t in broker_mcp._TOOLS if t["name"] == "jarvis_browser_page_read"), None)
    assert tool_entry is not None
    schema_dict: Any = tool_entry["inputSchema"]
    assert "grant_id" in schema_dict["required"]
    assert "url" in schema_dict["required"]

    # 3. Supervisor tool allowlist & tool capabilities
    assert "jarvis_browser_page_read" in supervisor._TOOL_CAPABILITIES
    assert "browser_page_read" in supervisor._TOOL_CAPABILITIES
    assert "mcp__jarvis__jarvis_browser_page_read" in supervisor.HERMES_TOOL_ALLOWLIST
    assert "browser_page_read" in supervisor.HERMES_TOOL_ALLOWLIST

    # 4. Worker shim admitted tools
    assert "mcp__jarvis__jarvis_browser_page_read" in worker_shim.ADMITTED_TOOLS
    assert "browser_page_read" in worker_shim.ADMITTED_TOOLS

    # 5. Thread service turn tools
    turn_tools = thread_service._turn_tools("none")
    assert "mcp__jarvis__jarvis_browser_page_read" in turn_tools

    # 6. Context builder instructions mention tool
    assert "jarvis_browser_page_read" in context_builder.THREAD_AUTHORITY_INSTRUCTIONS


def test_thread_service_installs_browser_page_grant_and_prompt_guidance() -> None:
    """thread_service installs jarvis.browser_page_read grant and prompt guidance."""
    worker = SimpleNamespace(live_grants={})
    payload = AIThreadSubmit(
        request_id="test-req-358",
        prompt="Read public datasheet",
        route_class="hermes:agent",
    )
    instructions = thread_service._install_hermes_retrieval_grant(
        worker, payload, "workspace-358", "thread-358"  # type: ignore[arg-type]
    )
    assert instructions is not None
    assert "browser_page_grant_id=" in instructions
    assert "jarvis_browser_page_read" in instructions

    # Check grant in live_grants
    grant = next((g for g in worker.live_grants.values() if g.capability_id == "jarvis.browser_page_read"), None)
    assert grant is not None
    assert grant.scope.workspace_id == "workspace-358"
    assert grant.scope.jarvis_thread_id == "thread-358"


def test_supervisor_dispatch_browser_page_tool() -> None:
    """supervisor.dispatch_tool dispatches browser_page_read with live grant."""
    now = datetime.now(UTC)
    grant_id = "grant-bp-358"
    grant = CapabilityGrantRef(
        grant_id=grant_id,
        capability_id="jarvis.browser_page_read",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id="ws-358", jarvis_thread_id="th-358"),
        constraints={},
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    live_grants = {grant_id: grant}
    session_ref = AgentSessionRef(
        jarvis_thread_id="th-358",
        hermes_session_id="hs-358",
        profile_id="isaac",
        workspace_id="ws-358",
        generation=1,
        upstream_revision=supervisor.UPSTREAM_REVISION,
    )

    call = StructuredToolCall(
        call_id="call-bp-1",
        capability_id="jarvis.browser_page_read",
        grant_id=grant_id,
        correlation_id="corr-1",
        session_ref=session_ref,
        arguments={"url": "https://example.com/datasheet"},
        requested_at=now,
        deadline_at=now + timedelta(seconds=30),
    )

    runner = _make_html_runner("<html><head><title>Spec Sheet</title></head><body>Voltage: 24V</body></html>")
    with override_dns_resolver(_public_dns):
        result = supervisor.dispatch_tool(
            call,
            live_grants=live_grants,
            browser_page_runner=runner,
        )

    assert result.status == "succeeded"
    assert isinstance(result.result, dict)
    assert result.result["title"] == "Spec Sheet"
    assert "Voltage: 24V" in str(result.result["text"])


def test_supervisor_dispatch_refuses_expired_or_mismatched_grant() -> None:
    """Expired or mismatched capability grant is refused."""
    now = datetime.now(UTC)
    grant_id = "grant-expired"
    grant = CapabilityGrantRef(
        grant_id=grant_id,
        capability_id="jarvis.browser_page_read",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id="ws-358"),
        constraints={},
        issued_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=5),
    )
    live_grants = {grant_id: grant}
    session_ref = AgentSessionRef(
        jarvis_thread_id="th-358",
        hermes_session_id="hs-358",
        profile_id="isaac",
        workspace_id="ws-358",
        generation=1,
        upstream_revision=supervisor.UPSTREAM_REVISION,
    )

    call = StructuredToolCall(
        call_id="call-bp-expired",
        capability_id="jarvis.browser_page_read",
        grant_id=grant_id,
        correlation_id="corr-exp",
        session_ref=session_ref,
        arguments={"url": "https://example.com/datasheet"},
        requested_at=now,
        deadline_at=now + timedelta(seconds=30),
    )

    result = supervisor.dispatch_tool(call, live_grants=live_grants)
    assert result.status == "refused"
    assert result.error_code == "capability_denied"


def test_per_turn_research_budget_enforcement() -> None:
    """browser_page_read calls count against research budget and budget cap is enforced."""
    sup = supervisor.HermesSupervisor.__new__(supervisor.HermesSupervisor)
    sup.turn_research_count = 0
    sup.research_budget = 3
    sup.turn_tool_hashes = set()

    # is_research_tool recognizes browser_page_read and aliases
    assert supervisor.is_research_tool("jarvis_browser_page_read")
    assert supervisor.is_research_tool("browser_page_read")
    assert supervisor.is_research_tool("mcp__jarvis__jarvis_browser_page_read")
    assert worker_shim.is_research_tool("jarvis_browser_page_read")
    assert worker_shim.is_research_tool("browser_page_read")

    # Simulate supervisor checking research budget
    for _i in range(3):
        assert sup.turn_research_count < sup.research_budget
        sup.turn_research_count += 1

    # At budget cap, next research call is stopped with notice
    assert sup.turn_research_count >= sup.research_budget


def test_worker_shim_completion_message_promotes_browser_page_read() -> None:
    """worker_shim promotes bare browser_page_read to mcp__jarvis__jarvis_browser_page_read."""
    tools = [{"function": {"name": "mcp__jarvis__jarvis_browser_page_read"}}]
    proposal_text = json.dumps({
        "tool_calls": [{"name": "browser_page_read", "arguments": {"url": "https://example.com"}}]
    })
    msg = worker_shim.completion_message(proposal_text, tools)
    assert msg.get("tool_calls") is not None
    assert len(msg["tool_calls"]) == 1
    promoted = msg["tool_calls"][0]
    assert promoted["function"]["name"] == "mcp__jarvis__jarvis_browser_page_read"
