"""Guarded Consult MCP Server (spec 380 part b).

Exposes ONLY 3 MCP tools: ``web_search``, ``paper_search``, and ``read_page``.
No knowledge, no retrieval, no workspace reads, no filesystem tools.
Every tool runs ``check_sensitivity_floor`` (blocks S2-S4) and pack/vault search-guard
checks before any outbound call.
Runs outside the bubblewrap sandbox as a Unix-domain socket JSON-RPC server.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from app.core import packs, vault
from app.modules.agents.hermes.browser_page_tool import execute_browser_page_read
from app.modules.agents.hermes.ip_boundary import check_sensitivity_floor, sanitize_text
from app.modules.agents.hermes.paper_search_tool import execute_paper_search

DEFAULT_SEARCH_GUARD_URL = "http://127.0.0.1:8889/search"

_DIM_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*([a-zA-Z°µ%][a-zA-Z°µ%0-9]*(?:/[a-zA-Z°µ%][a-zA-Z°µ%0-9]*)?)"
)

_UNIT_ALIASES: dict[str, str] = {
    "degc": "°c",
    "celsius": "°c",
    "c": "°c",
    "deg_c": "°c",
    "k": "k",
    "kelvin": "k",
    "bar": "bar",
    "barg": "bar",
    "pa": "pa",
    "kpa": "kpa",
    "mpa": "mpa",
    "m": "m",
    "mm": "mm",
    "cm": "cm",
    "kg": "kg",
    "g": "g",
    "mg": "mg",
    "s": "s",
    "sec": "s",
    "second": "s",
    "seconds": "s",
    "min": "min",
    "minute": "min",
    "minutes": "min",
    "h": "h",
    "hr": "h",
    "hour": "h",
    "hours": "h",
    "w": "w",
    "kw": "kw",
    "mw": "mw",
    "j": "j",
    "kj": "kj",
    "mj": "mj",
    "%": "%",
    "percent": "%",
}


def _normalize_unit(unit: str) -> str:
    cleaned = unit.lower().strip()
    return _UNIT_ALIASES.get(cleaned, cleaned)


def _matches_exact_combination(lowered_query: str, combinations: tuple[tuple[str, ...], ...]) -> bool:
    for combination in combinations:
        if not combination:
            continue
        if all(" ".join(term.lower().split()) in lowered_query for term in combination if term.strip()):
            return True
    return False


def _matches_min_groups(query: str, groups: tuple[tuple[str, ...], ...] | list[list[str]], min_groups: int) -> bool:
    if not groups or min_groups <= 0:
        return False
    lowered = " ".join(query.lower().split())
    matched_count = 0
    for group in groups:
        if any(" ".join(term.lower().split()) in lowered for term in group if term.strip()):
            matched_count += 1
    return matched_count >= min_groups


def _matches_protected_dimension(query: str, guard_cfg: packs.SearchGuardConfig) -> bool:
    if not guard_cfg.protected_dimensions:
        return False
    lowered = " ".join(query.lower().split())
    if guard_cfg.groups:
        has_context = any(
            any(" ".join(term.lower().split()) in lowered for term in group if term.strip())
            for group in guard_cfg.groups
        )
        if not has_context:
            return False
    for m in _DIM_RE.finditer(query):
        try:
            val = float(m.group(1))
            unit = _normalize_unit(m.group(2))
        except (ValueError, IndexError):
            continue
        for dim in guard_cfg.protected_dimensions:
            if _normalize_unit(dim.unit) == unit:
                min_val = dim.value * (1.0 - dim.tolerance_rel) - 1e-9
                max_val = dim.value * (1.0 + dim.tolerance_rel) + 1e-9
                if min_val <= val <= max_val:
                    return True
    return False


def evaluate_search_guard(query: str) -> tuple[bool, str]:
    """Check query against sensitivity floor (S2-S4 blocked) and pack/vault search guard rules.

    Returns (allowed, reason_code).
    """
    clean_query = query.strip()
    if not clean_query:
        return False, "query_empty"
    floor = check_sensitivity_floor(clean_query)
    if floor in {"S2", "S3", "S4"}:
        return False, f"sensitivity_floor_{floor.lower()}"
    if sanitize_text(clean_query) != clean_query:
        return False, "sanitized_diff"
    normalized = " ".join(clean_query.split())
    if packs.mentions_pack_term(normalized):
        return False, "pack_term"

    for pack in packs.load_packs():
        guard_cfg = pack.search_guard
        if guard_cfg.exact_combinations and _matches_exact_combination(normalized.lower(), guard_cfg.exact_combinations):
            return False, "exact_combination"
        if guard_cfg.protected_dimensions and _matches_protected_dimension(normalized, guard_cfg):
            return False, "protected_dimension"
        if guard_cfg.groups and _matches_min_groups(normalized, guard_cfg.groups, guard_cfg.min_groups):
            return False, "guard_groups"

    vault_dimensions = vault.vault_protected_dimensions()
    if vault_dimensions:
        merged = packs.search_guard()
        vault_cfg = packs.SearchGuardConfig(
            groups=merged.groups,
            protected_dimensions=vault_dimensions,
            allow_generic_ranges=merged.allow_generic_ranges,
        )
        if _matches_protected_dimension(normalized, vault_cfg):
            return False, "protected_dimension"

    return True, "ok"


def execute_web_search(
    query: str,
    *,
    count: int = 5,
    opener: Any = None,
    search_guard_url: str = DEFAULT_SEARCH_GUARD_URL,
) -> dict[str, Any]:
    """Execute guarded web search calling the local search guard proxy."""
    allowed, reason = evaluate_search_guard(query)
    if not allowed:
        return {"error": f"search_guard_blocked:{reason}", "results": [], "count": 0}

    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{search_guard_url}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-Consult-MCP/1.0"})
    try:
        if opener is not None:
            resp = opener.open(req, timeout=15)
        else:
            resp = urllib.request.urlopen(req, timeout=15)
        with resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])[:count]
        return {"query": query, "results": results, "count": len(results)}
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"error": f"search_proxy_error:{type(exc).__name__}", "results": [], "count": 0}


TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": "Search the web through the local search guard proxy. Only public, cloud-safe queries pass.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "count": {"type": "integer", "description": "Maximum number of results to return", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "paper_search",
        "description": "Search scientific literature (OpenAlex, Semantic Scholar, Europe PMC, Crossref).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Literature search query"},
                "limit": {"type": "integer", "description": "Maximum papers to return", "default": 5},
                "source": {
                    "type": "string",
                    "description": "Source database (all, europe_pmc, crossref, openalex, semantic_scholar)",
                    "default": "all",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_page",
        "description": "Read a public HTTPS web page using headless Chromium.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public HTTPS web page URL to read"},
                "search_term": {"type": "string", "description": "Optional search term to locate specific sections"},
            },
            "required": ["url"],
        },
    },
]

ALLOWED_TOOL_NAMES = frozenset({"web_search", "paper_search", "read_page"})


class ConsultMcpServer:
    """Unix-domain socket JSON-RPC server exposing only guarded web_search, paper_search, and read_page."""

    def __init__(
        self,
        socket_path: str,
        *,
        web_opener: Any = None,
        paper_opener: Any = None,
        browser_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.web_opener = web_opener
        self.paper_opener = paper_opener
        self.browser_runner = browser_runner
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def handle_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jarvis-consult-mcp", "version": "1.0.0"},
                },
            }

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOL_DEFINITIONS}}

        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}

        if method == "tools/call":
            params = message.get("params") or {}
            tool_name = params.get("name")
            if tool_name not in ALLOWED_TOOL_NAMES:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: tool {tool_name!r} is not exposed"},
                }
            args = params.get("arguments") or {}
            return self._execute_tool(request_id, tool_name, args)

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method!r}"},
        }

    def _execute_tool(self, request_id: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "web_search":
            query = str(args.get("query") or "")
            count = int(args.get("count", 5))
            res = execute_web_search(query, count=count, opener=self.web_opener)
            is_error = bool(res.get("error"))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": is_error},
            }

        if name == "paper_search":
            query = str(args.get("query") or "")
            limit = int(args.get("limit", 5))
            source = str(args.get("source", "all"))
            allowed, reason = evaluate_search_guard(query)
            if not allowed:
                res: dict[str, Any] = {"error": f"search_guard_blocked:{reason}", "papers": [], "count": 0}
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": True},
                }
            res = execute_paper_search(query, limit=limit, source=source, opener=self.paper_opener)
            is_error = bool(res.get("error"))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": is_error},
            }

        if name == "read_page":
            url = str(args.get("url") or "")
            search_term = args.get("search_term")
            term_str = str(search_term) if search_term is not None else None
            allowed, reason = evaluate_search_guard(url)
            if not allowed:
                res = {"error": f"search_guard_blocked:{reason}", "status": "error"}
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": True},
                }
            if term_str is not None:
                term_allowed, term_reason = evaluate_search_guard(term_str)
                if not term_allowed:
                    res = {"error": f"search_guard_blocked:{term_reason}", "status": "error"}
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": True},
                    }
            res = execute_browser_page_read(url, search_term=term_str, runner=self.browser_runner)
            is_error = bool(res.get("error")) or res.get("status") == "error"
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}], "isError": is_error},
            }

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unknown tool {name}"},
        }

    def start(self) -> None:
        if self._running:
            return
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        sock_dir = os.path.dirname(os.path.abspath(self.socket_path))
        os.makedirs(sock_dir, exist_ok=True, mode=0o700)
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server_sock.listen(16)
        self._server_sock = server_sock
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._server_sock is not None
        while self._running:
            try:
                client_sock, _ = self._server_sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        buffer = b""
        with client_sock:
            while self._running:
                try:
                    chunk = client_sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line.decode("utf-8"))
                            resp = self.handle_request(msg)
                            if resp is not None:
                                payload = json.dumps(resp, separators=(",", ":")).encode("utf-8") + b"\n"
                                client_sock.sendall(payload)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                except OSError:
                    break

    def stop(self) -> None:
        self._running = False
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def __enter__(self) -> ConsultMcpServer:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
