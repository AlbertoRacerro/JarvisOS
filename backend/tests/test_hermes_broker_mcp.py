"""Tests for Hermes MCP broker bridge (broker_mcp.py)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.modules.agents.hermes import broker_mcp


def test_mcp_broker_initialize() -> None:
    reply = broker_mcp._reply({"method": "initialize", "id": "init-1", "params": {}})
    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == "init-1"
    assert reply["result"]["serverInfo"]["name"] == "jarvis"
    assert "tools" in reply["result"]["capabilities"]


def test_mcp_broker_tools_list() -> None:
    reply = broker_mcp._reply({"method": "tools/list", "id": "list-1", "params": {}})
    assert reply is not None
    assert reply["id"] == "list-1"
    tools = reply["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert "jarvis_browser_page_read" in tool_names
    assert "jarvis_paper_search" in tool_names
    assert "jarvis_dev_read" in tool_names
    assert "jarvis_repo_read" in tool_names
    assert "jarvis_knowledge_read" in tool_names
    assert "jarvis_context_preview" in tool_names

    browser_tool = next(t for t in tools if t["name"] == "jarvis_browser_page_read")
    schema = browser_tool["inputSchema"]
    assert "grant_id" in schema["required"]
    assert "url" in schema["required"]
    assert "search_term" in schema["properties"]
    assert schema["properties"]["url"]["maxLength"] == 1024
    assert schema["properties"]["search_term"]["maxLength"] == 160


def test_mcp_broker_tools_call_unknown_tool() -> None:
    reply = broker_mcp._reply({
        "method": "tools/call",
        "id": "call-unknown",
        "params": {"name": "nonexistent_tool", "arguments": {}},
    })
    assert reply is not None
    assert reply["id"] == "call-unknown"
    assert reply.get("error", {}).get("code") == -32601
    assert "unknown tool" in reply["error"]["message"]


def test_mcp_broker_tools_call_admitted_tool(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HERMES_BROKER_URL", "http://127.0.0.1:8000/v1/jarvis/tool")
    monkeypatch.setenv("JARVIS_HERMES_BROKER_TOKEN", "mock-token-123")

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"status": "succeeded", "result": {"title": "Test"}}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    with patch("app.modules.agents.hermes.broker_mcp.build_opener", return_value=mock_opener):
        reply = broker_mcp._reply({
            "method": "tools/call",
            "id": "call-1",
            "params": {
                "name": "jarvis_browser_page_read",
                "arguments": {"grant_id": "grant-1", "url": "https://example.com"},
            },
        })
        assert reply is not None
        assert reply["id"] == "call-1"
        assert reply["result"]["isError"] is False
        assert len(reply["result"]["content"]) == 1
        data = json.loads(reply["result"]["content"][0]["text"])
        assert data["status"] == "succeeded"


def test_mcp_broker_tools_call_browser_page_read_alias(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HERMES_BROKER_URL", "http://127.0.0.1:8000/v1/jarvis/tool")
    monkeypatch.setenv("JARVIS_HERMES_BROKER_TOKEN", "mock-token-123")

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"status": "succeeded", "result": {"title": "Test Alias"}}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    with patch("app.modules.agents.hermes.broker_mcp.build_opener", return_value=mock_opener):
        reply = broker_mcp._reply({
            "method": "tools/call",
            "id": "call-alias",
            "params": {
                "name": "browser_page_read",
                "arguments": {"grant_id": "grant-1", "url": "https://example.com"},
            },
        })
        assert reply is not None
        assert reply["id"] == "call-alias"
        assert reply["result"]["isError"] is False


def test_mcp_broker_invalid_method_and_missing_id() -> None:
    assert broker_mcp._reply({"method": "tools/list"}) is None
    unknown = broker_mcp._reply({"method": "custom/random", "id": 99})
    assert unknown is not None
    assert unknown["error"]["code"] == -32601
