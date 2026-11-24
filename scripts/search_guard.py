#!/usr/bin/env python3
"""JarvisOS search query guard proxy (spec 353).

A stdlib HTTP proxy listening on 127.0.0.1 only that forwards allowed GET requests
to a loopback SearXNG instance (default 127.0.0.1:8888) and blocks queries that
violate egress boundaries or match pack sensitivity terms/groups.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import json
import os
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path and BACKEND_DIR.is_dir():
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import packs, paths  # noqa: E402
from app.modules.agents.hermes.ip_boundary import (  # noqa: E402
    check_sensitivity_floor,
    sanitize_text,
)

DEFAULT_GUARD_PORT = 8889
DEFAULT_UPSTREAM_PORT = 8888
DEFAULT_HOST = "127.0.0.1"
MAX_UPSTREAM_BYTES = 4 * 1024 * 1024  # 4 MB response limit
MAX_REQUEST_TARGET = 8192
MAX_REQUEST_HEADERS = 16384
ALLOWED_PATHS = frozenset({"/search", "/healthz", "/config"})


def is_loopback(host: str) -> bool:
    """Only the specified IPv4 loopback interface is supported."""
    return host == DEFAULT_HOST


def pack_is_ready() -> bool:
    """Check that a configured pack loads without exposing its error or path."""
    pack_path = os.getenv(packs.PACK_PATH_ENV, "").strip()
    if not pack_path:
        return True
    try:
        packs._load.cache_clear()
        return bool(packs.load_packs())
    except Exception:  # noqa: BLE001 - any pack failure must fail closed
        return False


def matches_min_groups(
    query: str,
    groups: tuple[tuple[str, ...], ...] | list[list[str]],
    min_groups: int,
) -> bool:
    """Check if query matches at least min_groups of synonyms groups (case-insensitive)."""
    if not groups or min_groups <= 0:
        return False
    lowered = query.lower()
    matched_count = 0
    for group in groups:
        if any(term.lower() in lowered for term in group if term.strip()):
            matched_count += 1
    return matched_count >= min_groups


def evaluate_query(query: str) -> tuple[bool, str]:
    """Evaluate whether a search query is allowed or blocked.

    Returns (allowed, reason_code).
    """
    if not pack_is_ready():
        return False, "pack_load_error"
    try:
        if packs.mentions_pack_term(query):
            return False, "pack_term"
        guard_cfg = packs.search_guard()
        if matches_min_groups(query, guard_cfg.groups, guard_cfg.min_groups):
            return False, "guard_groups"
        if sanitize_text(query) != query:
            return False, "sanitized_diff"
        if check_sensitivity_floor(query) in {"S2", "S3", "S4"}:
            return False, "sensitivity_floor"
    except Exception:  # noqa: BLE001 - any guard failure must fail closed
        return False, "guard_error"

    return True, "ok"


def resolve_log_file() -> Path:
    return paths.build_paths().search_guard_log_file


def log_query(query: str, status: str, log_file: Path | None = None) -> bool:
    """Log one line per query: timestamp, ok/blocked, and sha256 prefix. Never query text."""
    try:
        target_path = log_file or resolve_log_file()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        now_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha_prefix = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16]
        line = f"{now_ts} {status} {sha_prefix}\n"
        with open(target_path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        return True
    except Exception:  # noqa: BLE001 - a logging failure must prevent egress
        return False


class SearchGuardHandler(http.server.BaseHTTPRequestHandler):
    """Proxy handler restricting allowed endpoints and blocking sensitive queries."""

    server: SearchGuardServer

    def log_message(self, format: str, *args: Any) -> None:
        # Override to suppress stderr logging that might print query parameters
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # The base handler can echo a malformed request line, including a query.
        body = b"Request rejected"
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            return

    def send_error_502(self, detail: str = "bad gateway") -> None:
        body = json.dumps({"error": "bad_gateway", "detail": detail}).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self) -> None:
        if len(self.path) > MAX_REQUEST_TARGET or len(self.headers.as_bytes()) > MAX_REQUEST_HEADERS:
            self.send_error_502("request too large")
            return
        parsed = urllib.parse.urlsplit(self.path)
        path_only = parsed.path

        if parsed.scheme or parsed.netloc or parsed.fragment or path_only not in ALLOWED_PATHS:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "9")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b"Not Found")
            except OSError:
                pass
            return

        if path_only == "/healthz":
            if not pack_is_ready():
                body = b'{"status":"not_ready","error":"pack_load_failed"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                return
            if parsed.query:
                self.send_error_502("unexpected query parameters")
                return
            self._forward_to_upstream()
            return

        if path_only == "/config":
            if parsed.query:
                self.send_error_502("unexpected query parameters")
                return
            self._forward_to_upstream()
            return

        if path_only == "/search":
            try:
                query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
            except ValueError:
                query_params = {}
            query_values = query_params.get("q", [])
            query_text = query_values[0] if len(query_values) == 1 else ""
            fmt = query_params.get("format", [""])[0].lower()
            valid_query = (len(query_values) == 1 and bool(query_text)
                           and "query" not in query_params and len(query_params.get("format", [])) <= 1
                           and all(32 <= ord(char) < 127 or ord(char) >= 160
                                   for values in query_params.values() for value in values for char in value))
            allowed, _reason = evaluate_query(urllib.parse.unquote_plus(parsed.query)) if valid_query else (False, "invalid_query")
            if not log_query(query_text, "ok" if allowed else "blocked", self.server.log_file):
                self.send_error_502("query log unavailable")
                return

            if not allowed:
                if fmt == "json":
                    body = b'{"results":[]}'
                    content_type = "application/json; charset=utf-8"
                else:
                    body = (
                        b"<!DOCTYPE html>\n"
                        b"<html>\n"
                        b"<head><title>Search Results</title></head>\n"
                        b"<body>\n"
                        b'<div id="results"></div>\n'
                        b"</body>\n"
                        b"</html>\n"
                    )
                    content_type = "text/html; charset=utf-8"

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                return

            self._forward_to_upstream()

    def do_POST(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_PUT(self) -> None:
        self.do_POST()

    def do_DELETE(self) -> None:
        self.do_POST()

    def do_HEAD(self) -> None:
        self.do_POST()

    def _forward_to_upstream(self) -> None:
        conn = http.client.HTTPConnection(
            self.server.upstream_host,
            self.server.upstream_port,
            timeout=10.0,
        )
        try:
            conn.request("GET", self.path, headers={"Connection": "close", "Accept-Encoding": "identity"})
            resp = conn.getresponse()

            # Redirects from upstream are not followed
            if 300 <= resp.status < 400:
                self.send_error_502("upstream redirect not followed")
                conn.close()
                return

            # Response body is bounded
            body = resp.read(MAX_UPSTREAM_BYTES + 1)
            if len(body) > MAX_UPSTREAM_BYTES:
                self.send_error_502("upstream response exceeded size limit")
                conn.close()
                return

            # Upstream server errors -> 502
            if resp.status >= 500:
                self.send_error_502(f"upstream returned status {resp.status}")
                conn.close()
                return

            self.send_response(resp.status)
            for header_name, header_value in resp.getheaders():
                if header_name.lower() == "content-type":
                    media_type = header_value.split(";", 1)[0].strip().lower()
                    if media_type in {"application/json", "text/html", "text/plain", "application/xml", "text/xml"}:
                        self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, http.client.HTTPException, TimeoutError):
            self.send_error_502("upstream request failed")
        finally:
            conn.close()


class SearchGuardServer(http.server.ThreadingHTTPServer):
    """Threading HTTP server carrying upstream and log configuration."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[http.server.BaseHTTPRequestHandler],
        upstream_host: str = DEFAULT_HOST,
        upstream_port: int = DEFAULT_UPSTREAM_PORT,
        log_file: Path | None = None,
    ) -> None:
        if not is_loopback(server_address[0]):
            raise ValueError("Bind address must be loopback")
        if not is_loopback(upstream_host):
            raise ValueError("Upstream host must be loopback")
        super().__init__(server_address, RequestHandlerClass)
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.log_file = log_file


def create_server(
    bind_address: str = DEFAULT_HOST,
    port: int = DEFAULT_GUARD_PORT,
    upstream_host: str = DEFAULT_HOST,
    upstream_port: int = DEFAULT_UPSTREAM_PORT,
    log_file: Path | None = None,
) -> SearchGuardServer:
    """Create a configured SearchGuardServer instance."""
    return SearchGuardServer(
        (bind_address, port),
        SearchGuardHandler,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        log_file=log_file,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="JarvisOS search query guard proxy")
    parser.add_argument(
        "--bind",
        default=DEFAULT_HOST,
        help="Bind address (default 127.0.0.1, must be loopback)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("SEARCH_GUARD_PORT", str(DEFAULT_GUARD_PORT))),
        help="Port to listen on (default 8889)",
    )
    parser.add_argument(
        "--upstream-host",
        default=os.getenv("SEARCH_GUARD_UPSTREAM_HOST", DEFAULT_HOST),
        help="Upstream host (default 127.0.0.1, must be loopback)",
    )
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=int(os.getenv("SEARCH_GUARD_UPSTREAM_PORT", str(DEFAULT_UPSTREAM_PORT))),
        help="Upstream port (default 8888)",
    )
    args = parser.parse_args()

    upstream_host = args.upstream_host
    upstream_port = args.upstream_port

    if not is_loopback(args.bind):
        sys.stderr.write("Refused: bind address must be loopback\n")
        return 2

    if not is_loopback(upstream_host):
        sys.stderr.write("Refused: upstream host must be loopback\n")
        return 2

    try:
        server = create_server(
            bind_address=args.bind,
            port=args.port,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
    except ValueError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        return 2

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
