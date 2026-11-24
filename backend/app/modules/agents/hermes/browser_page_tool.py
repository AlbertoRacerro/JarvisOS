"""Bounded, read-only headless Chromium public page reader for Hermes.

Per Spec 358:
Reads public HTTPS web pages that block plain HTTP fetches or require JS rendering.
Fails closed on non-HTTPS schemes, private/loopback IPs, userinfo, egress secrets or
pack terms, and bot challenges. Supports injectable DNS resolver and runner hooks
for 100% offline deterministic unit tests.
"""
from __future__ import annotations

import glob
import html
import ipaddress
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from app.core import packs
from app.modules.agents.hermes.ip_boundary import (
    _KNOWN_SECRET_RE,
    check_sensitivity_floor,
    sanitize_text,
)
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall

MAX_URL_LENGTH = 1024
MAX_SEARCH_TERM_LENGTH = 160
MAX_TEXT_CHARS = 8000
MAX_STDOUT_BYTES = 2 * 1024 * 1024  # 2 MB cap
CHROMIUM_TIMEOUT_SECONDS = 20.0

_CHALLENGE_TITLE_PATTERNS = (
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"attention required!?\s*\|\s*cloudflare", re.I),
    re.compile(r"security check", re.I),
    re.compile(r"bot challenge", re.I),
    re.compile(r"verify you are human", re.I),
    re.compile(r"verifying you are human", re.I),
    re.compile(r"ddos-guard", re.I),
)

_CHALLENGE_DOM_PATTERNS = (
    re.compile(r"challenges\.cloudflare\.com", re.I),
    re.compile(r"cf-turnstile", re.I),
    re.compile(r"id=['\"]challenge-form['\"]", re.I),
    re.compile(r"id=['\"]challenge-running['\"]", re.I),
    re.compile(r"cf_chl_opt", re.I),
    re.compile(r"cf-chl-widget", re.I),
    re.compile(r"www\.google\.com/recaptcha", re.I),
    re.compile(r"class=['\"][^'\"]*g-recaptcha[^'\"]*['\"]", re.I),
    re.compile(r"js\.hcaptcha\.com", re.I),
    re.compile(r"class=['\"][^'\"]*h-captcha[^'\"]*['\"]", re.I),
)

_CHALLENGE_TEXT_PATTERNS = (
    re.compile(r"verify you are human", re.I),
    re.compile(r"verifying you are human", re.I),
    re.compile(r"checking your browser before accessing", re.I),
    re.compile(r"please enable javascript and cookies to continue", re.I),
    re.compile(r"please complete the security check to access", re.I),
    re.compile(r"please confirm you are a human", re.I),
    re.compile(r"please solve this captcha", re.I),
    re.compile(r"unusual traffic from your computer network", re.I),
)


class _DOMTextExtractor(HTMLParser):
    """Clean plain-text extractor that strips scripts, styles, svg, and boilerplate chrome."""

    def __init__(self) -> None:
        super().__init__()
        self.ignored_tags = frozenset({"script", "style", "noscript", "svg", "nav", "footer"})
        self.block_tags = frozenset({
            "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
            "li", "tr", "section", "article", "main", "header", "blockquote", "hr",
        })
        self.in_ignored = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self.ignored_tags:
            self.in_ignored += 1
        elif t == "title":
            self.in_title = True
        elif t in self.block_tags and self.in_ignored == 0:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.ignored_tags:
            self.in_ignored = max(0, self.in_ignored - 1)
        elif t == "title":
            self.in_title = False
        elif t in self.block_tags and self.in_ignored == 0:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_ignored > 0:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def _extract_dom_text(html_content: str) -> tuple[str, str]:
    """Parse HTML and return clean (title, collapsed_plain_text)."""
    extractor = _DOMTextExtractor()
    extractor.feed(html_content)
    raw_title = "".join(extractor.title_parts)
    title = " ".join(html.unescape(raw_title).split())

    raw_text = "".join(extractor.text_parts)
    lines = [line.strip() for line in html.unescape(raw_text).splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line:
            collapsed.append(re.sub(r"[ \t]+", " ", line))
        elif collapsed and collapsed[-1] != "":
            collapsed.append("")

    clean_text = "\n".join(collapsed).strip()
    return title, clean_text


def _detect_bot_challenge(title: str, text: str, raw_dom: str) -> bool:
    """Identify anti-bot challenge / captcha / Cloudflare Turnstile interstitials."""
    for pattern in _CHALLENGE_TITLE_PATTERNS:
        if pattern.search(title):
            return True

    has_dom_marker = any(pattern.search(raw_dom) for pattern in _CHALLENGE_DOM_PATTERNS)
    has_text_marker = any(pattern.search(text) for pattern in _CHALLENGE_TEXT_PATTERNS)

    if has_dom_marker and (has_text_marker or len(text) < 2500):
        return True

    if has_text_marker and len(text) < 2000:
        return True

    return False


def _is_safe_global_ip(ip_str: str) -> bool:
    """Validate that an IP is global/public and not loopback, private, link-local, multicast or reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False

    if not ip.is_global:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            mapped = ip.ipv4_mapped
            if not _is_safe_global_ip(str(mapped)):
                return False
        # RFC 6052 NAT64 Well-Known Prefix 64:ff9b::/96 embeds an IPv4 in the lower 32 bits
        if ip in ipaddress.IPv6Network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            if not _is_safe_global_ip(str(embedded)):
                return False
        if ip.is_site_local:
            return False

    return True


def _is_numeric_ip(host: str) -> list[str]:
    """Return list of canonical IP strings if host is a numeric IP literal (IPv4 or IPv6), else empty."""
    clean = host.strip("[]")
    try:
        ip = ipaddress.ip_address(clean)
        return [str(ip)]
    except ValueError:
        pass

    try:
        gai = socket.getaddrinfo(clean, None, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)
        return [str(sa[4][0]) for sa in gai]
    except (socket.gaierror, OSError):
        pass

    return []


class _ValidatingProxyServer:
    """Ephemeral, localhost-bound forward HTTP CONNECT proxy for headless Chromium.

    Enforces network containment:
    1. Blocks any plain HTTP requests (only CONNECT method allowed).
    2. Enforces destination port == 443 (standard HTTPS only).
    3. Blocks any connection attempts to loopback, private, link-local, multicast,
       unspecified, or non-global IP addresses (IPv4 and IPv6).
    4. Handles IP literals (IPv4 dotted-quad, octal, hex, int, IPv6, IPv4-mapped, NAT64).
    5. Resolves hostnames via the provided DNS resolver; blocks if resolution fails
       or if ANY resolved IP address is private/loopback/non-global.
    6. Verifies hostnames against egress pack terms, sensitivity terms, and blocked tokens.
    7. Upstream TCP connection connects directly to the validated IP literal,
       preventing DNS rebinding (TOCTOU) at the TCP connection layer.
    8. Records any blocked SSRF attempts.
    """

    def __init__(
        self,
        *,
        dns_resolver: Callable[[str], list[str]] | None = None,
        blocked_tokens: Iterable[str] | None = None,
        upstream_connector: Callable[[str, int], socket.socket] | None = None,
    ) -> None:
        self.dns_resolver = _get_dns_resolver(dns_resolver)
        self.blocked_tokens = set(blocked_tokens or ())
        self.upstream_connector = upstream_connector
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", 0))
        self.port: int = self.server_sock.getsockname()[1]
        self.server_sock.listen(128)
        self.running = False
        self.open_sockets: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.ssrf_blocked = False
        self.ssrf_block_reasons: list[str] = []

    def start(self) -> _ValidatingProxyServer:
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True, name="jarvis-proxy-accept")
        self.thread.start()
        return self

    def _record_ssrf_block(self, reason: str) -> None:
        with self.lock:
            self.ssrf_blocked = True
            self.ssrf_block_reasons.append(reason)

    def _register_socket(self, s: socket.socket) -> None:
        with self.lock:
            self.open_sockets.add(s)

    def _unregister_socket(self, s: socket.socket) -> None:
        with self.lock:
            self.open_sockets.discard(s)

    def _accept_loop(self) -> None:
        while self.running:
            try:
                self.server_sock.settimeout(0.25)
                client_sock, _ = self.server_sock.accept()
            except (TimeoutError, OSError):
                continue
            with self.lock:
                if not self.running:
                    try:
                        client_sock.close()
                    except OSError:
                        pass
                    break
                self.open_sockets.add(client_sock)
            t = threading.Thread(
                target=self._handle_client, args=(client_sock,), daemon=True, name="jarvis-proxy-conn"
            )
            t.start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        upstream_sock: socket.socket | None = None
        try:
            client_sock.settimeout(5.0)
            req_data = b""
            while b"\r\n\r\n" not in req_data and len(req_data) < 8192:
                try:
                    chunk = client_sock.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                req_data += chunk
            if not req_data:
                return

            first_line = req_data.split(b"\r\n")[0].decode("utf-8", errors="replace").strip()
            parts = first_line.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                self._record_ssrf_block("plain_http_refused")
                try:
                    client_sock.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\nBlocked: HTTPS (CONNECT) required.\r\n"
                    )
                except OSError:
                    pass
                return

            target = parts[1]
            if target.startswith("["):
                idx = target.find("]")
                if idx == -1:
                    self._record_ssrf_block("invalid_ipv6_target")
                    client_sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    return
                host = target[1:idx]
                rest = target[idx + 1:]
                if not rest.startswith(":"):
                    self._record_ssrf_block("missing_port")
                    client_sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    return
                port_str = rest[1:]
            else:
                if ":" not in target:
                    self._record_ssrf_block("missing_port")
                    client_sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    return
                host, port_str = target.split(":", 1)

            try:
                port = int(port_str)
            except ValueError:
                self._record_ssrf_block("invalid_port")
                client_sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return

            if port != 443:
                self._record_ssrf_block(f"non_443_port_{port}")
                try:
                    client_sock.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\nBlocked: only port 443 allowed.\r\n"
                    )
                except OSError:
                    pass
                return

            # Check if host is numeric IP literal
            numeric_ips = _is_numeric_ip(host)
            target_ip: str | None = None
            if numeric_ips:
                for nip in numeric_ips:
                    if not _is_safe_global_ip(nip):
                        self._record_ssrf_block(f"private_ip_literal_{nip}")
                        try:
                            client_sock.sendall(
                                b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\nBlocked: private IP refused.\r\n"
                            )
                        except OSError:
                            pass
                        return
                target_ip = numeric_ips[0]
            else:
                # Egress checks on hostname
                if (
                    _KNOWN_SECRET_RE.search(host)
                    or sanitize_text(host) != host
                    or check_sensitivity_floor(host) in {"S2", "S3", "S4"}
                    or packs.mentions_pack_term(host)
                ):
                    self._record_ssrf_block(f"egress_term_{host}")
                    try:
                        client_sock.sendall(
                            b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\nBlocked: sensitive term in host.\r\n"
                        )
                    except OSError:
                        pass
                    return
                if self.blocked_tokens:
                    for tok in self.blocked_tokens:
                        if tok and tok in host:
                            self._record_ssrf_block(f"blocked_token_{host}")
                            try:
                                client_sock.sendall(
                                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\nBlocked: token in host.\r\n"
                                )
                            except OSError:
                                pass
                            return

                # Resolve hostname
                resolved = self.dns_resolver(host)
                if not resolved:
                    self._record_ssrf_block(f"dns_resolution_failed_{host}")
                    try:
                        client_sock.sendall(
                            b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\nDNS resolution failed.\r\n"
                        )
                    except OSError:
                        pass
                    return

                for rip in resolved:
                    if not _is_safe_global_ip(rip):
                        self._record_ssrf_block(f"resolved_private_ip_{rip}")
                        try:
                            client_sock.sendall(
                                b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\nBlocked: resolved private IP refused.\r\n"
                            )
                        except OSError:
                            pass
                        return
                target_ip = resolved[0]

            assert target_ip is not None
            # Connect upstream directly to verified IP literal (curing DNS rebinding)
            try:
                if self.upstream_connector is not None:
                    upstream_sock = self.upstream_connector(target_ip, port)
                else:
                    family = socket.AF_INET6 if ":" in target_ip else socket.AF_INET
                    upstream_sock = socket.socket(family, socket.SOCK_STREAM)
                    self._register_socket(upstream_sock)
                    upstream_sock.settimeout(10.0)
                    upstream_sock.connect((target_ip, port))
            except Exception:
                try:
                    client_sock.sendall(
                        b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\nUpstream connect failed.\r\n"
                    )
                except OSError:
                    pass
                return

            try:
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            except OSError:
                return

            self._pipe(client_sock, upstream_sock, timeout=CHROMIUM_TIMEOUT_SECONDS)
        except Exception:
            pass
        finally:
            self._unregister_socket(client_sock)
            try:
                client_sock.close()
            except OSError:
                pass
            if upstream_sock is not None:
                self._unregister_socket(upstream_sock)
                try:
                    upstream_sock.close()
                except OSError:
                    pass

    def _pipe(self, s1: socket.socket, s2: socket.socket, timeout: float = CHROMIUM_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        s1.setblocking(False)
        s2.setblocking(False)
        socks = [s1, s2]
        bufsize = 65536
        while self.running and socks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                r, _, _ = select.select(socks, [], [], min(remaining, 0.5))
            except (OSError, ValueError):
                break
            for s in r:
                other = s2 if s is s1 else s1
                try:
                    data = s.recv(bufsize)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    socks.clear()
                    break
                try:
                    other.sendall(data)
                except OSError:
                    socks.clear()
                    break

    def stop(self) -> None:
        self.running = False
        try:
            self.server_sock.close()
        except OSError:
            pass
        with self.lock:
            for s in list(self.open_sockets):
                try:
                    s.close()
                except OSError:
                    pass
            self.open_sockets.clear()
        if self.thread is not None:
            self.thread.join(timeout=1.0)


def _default_dns_resolver(hostname: str) -> list[str]:
    """Default DNS resolver using standard socket.getaddrinfo."""
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return []

    ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in results:
        ip = str(sockaddr[0])
        if ip not in ips:
            ips.append(ip)
    return ips


_DEFAULT_DNS_RESOLVER: Callable[[str], list[str]] | None = None
_DEFAULT_RUNNER: Callable[[list[str], float], tuple[int, str, str]] | None = None


def set_default_dns_resolver(resolver: Callable[[str], list[str]] | None) -> None:
    """Set process-wide DNS resolver for tests."""
    global _DEFAULT_DNS_RESOLVER
    _DEFAULT_DNS_RESOLVER = resolver


@contextmanager
def override_dns_resolver(resolver: Callable[[str], list[str]]):
    """Context manager for temporary DNS resolver override in unit tests."""
    global _DEFAULT_DNS_RESOLVER
    prev = _DEFAULT_DNS_RESOLVER
    _DEFAULT_DNS_RESOLVER = resolver
    try:
        yield
    finally:
        _DEFAULT_DNS_RESOLVER = prev


def _get_dns_resolver(custom_resolver: Callable[[str], list[str]] | None = None) -> Callable[[str], list[str]]:
    if custom_resolver is not None:
        return custom_resolver
    if _DEFAULT_DNS_RESOLVER is not None:
        return _DEFAULT_DNS_RESOLVER
    return _default_dns_resolver


def set_default_runner(runner: Callable[[list[str], float], tuple[int, str, str]] | None) -> None:
    """Set process-wide Chromium runner for tests."""
    global _DEFAULT_RUNNER
    _DEFAULT_RUNNER = runner


@contextmanager
def override_browser_runner(runner: Callable[[list[str], float], tuple[int, str, str]]):
    """Context manager for temporary runner override in unit tests."""
    global _DEFAULT_RUNNER
    prev = _DEFAULT_RUNNER
    _DEFAULT_RUNNER = runner
    try:
        yield
    finally:
        _DEFAULT_RUNNER = prev


def _get_runner(
    custom_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None
) -> Callable[[list[str], float], tuple[int, str, str]] | None:
    if custom_runner is not None:
        return custom_runner
    if _DEFAULT_RUNNER is not None:
        return _DEFAULT_RUNNER
    return None


def _default_subprocess_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Execute Chromium as explicit argv subprocess with bounded incremental stdout capture."""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    chunks: list[bytes] = []
    total_bytes = 0
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    fd = proc.stdout.fileno()

    def _kill_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_group()
                proc.wait(timeout=5)
                raise subprocess.TimeoutExpired(argv, timeout)
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(fd, min(65536, MAX_STDOUT_BYTES - total_bytes + 1))
            if not chunk:
                break
            remaining_cap = MAX_STDOUT_BYTES - total_bytes
            if len(chunk) > remaining_cap:
                chunks.append(chunk[:remaining_cap])
                total_bytes = MAX_STDOUT_BYTES
                _kill_group()
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
        proc.stdout.close()
        remaining = max(0.1, deadline - time.monotonic())
        try:
            returncode = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_group()
            proc.wait(timeout=5)
            raise
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        _kill_group()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise

    stdout = b"".join(chunks).decode("utf-8", errors="replace")
    return returncode, stdout, ""


def find_chromium_binary() -> str | None:
    """Locate headless Chromium binary via env var or newest Playwright installation."""
    env_bin = os.getenv("JARVISOS_BROWSER_READER_CHROME")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    pattern = os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")
    matches = glob.glob(pattern)
    if not matches:
        return None

    valid = [p for p in matches if os.path.isfile(p) and os.access(p, os.X_OK)]
    if not valid:
        return None

    valid.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
    return valid[0]


def _process_text_and_search_term(
    text: str, search_term: str | None, max_chars: int = MAX_TEXT_CHARS
) -> tuple[str, bool]:
    """Window plain text around search_term if provided, bounded to max_chars."""
    if not search_term or not search_term.strip():
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    term = search_term.strip()
    term_lower = term.lower()
    text_lower = text.lower()

    matches: list[int] = []
    pos = 0
    while True:
        idx = text_lower.find(term_lower, pos)
        if idx == -1:
            break
        matches.append(idx)
        pos = idx + len(term_lower)

    if not matches:
        # Search term not present; return top of page bounded to max_chars
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    window_margin = 500
    intervals: list[tuple[int, int]] = []
    for idx in matches:
        start = max(0, idx - window_margin)
        end = min(len(text), idx + len(term) + window_margin)
        intervals.append((start, end))

    merged: list[tuple[int, int]] = []
    for s, e in intervals:
        if not merged:
            merged.append((s, e))
        else:
            prev_s, prev_e = merged[-1]
            if s <= prev_e:
                merged[-1] = (prev_s, max(prev_e, e))
            else:
                merged.append((s, e))

    parts: list[str] = []
    for s, e in merged:
        part = text[s:e].strip()
        prefix = "[...] " if s > 0 else ""
        suffix = " [...]" if e < len(text) else ""
        parts.append(f"{prefix}{part}{suffix}")

    excerpt = "\n\n[...]\n\n".join(parts)
    if len(excerpt) > max_chars:
        return excerpt[:max_chars], True

    is_truncated = len(excerpt) < len(text)
    return excerpt, is_truncated


def execute_browser_page_read(
    url: str,
    *,
    search_term: str | None = None,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    dns_resolver: Callable[[str], list[str]] | None = None,
    blocked_tokens: Iterable[str] | None = None,
    upstream_connector: Callable[[str, int], socket.socket] | None = None,
) -> dict[str, Any]:
    """Perform pre-flight safety checks and execute headless Chromium page read."""
    now_iso = datetime.now(UTC).isoformat()

    # 1. Input format & length validation
    if not isinstance(url, str):
        return {"error": "invalid_url", "status": "error"}
    clean_url = url.strip()
    if not clean_url or len(clean_url) > MAX_URL_LENGTH:
        return {"error": "invalid_url", "status": "error"}

    # Refuse control characters
    if any(ord(c) < 32 or ord(c) == 127 for c in clean_url):
        return {"error": "invalid_url", "status": "error"}

    # 2. Scheme & userinfo validation
    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except ValueError:
        return {"error": "invalid_url", "status": "error"}

    if parsed.scheme.lower() != "https":
        return {"error": "invalid_url", "status": "error"}

    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return {"error": "invalid_url", "status": "error"}

    if parsed.port is not None and parsed.port != 443:
        return {"error": "invalid_url", "status": "error"}

    hostname = parsed.hostname
    if not hostname or not hostname.strip():
        return {"error": "invalid_url", "status": "error"}

    # 3. DNS resolution & IP safety check
    numeric_ips = _is_numeric_ip(hostname)
    if numeric_ips:
        for nip in numeric_ips:
            if not _is_safe_global_ip(nip):
                return {"error": "private_ip_refused", "status": "error"}
    else:
        resolver = _get_dns_resolver(dns_resolver)
        resolved_ips = resolver(hostname)
        if not resolved_ips:
            return {"error": "dns_resolution_failed", "status": "error"}

        for ip_str in resolved_ips:
            if not _is_safe_global_ip(ip_str):
                return {"error": "private_ip_refused", "status": "error"}

    # 4. Egress sanitization checks on URL (raw and iteratively URL-decoded forms)
    url_variants: list[str] = [clean_url]
    cur = clean_url
    for _ in range(3):
        dec_pct = urllib.parse.unquote(cur)
        dec_plus = urllib.parse.unquote_plus(cur)
        added = False
        for candidate in (dec_pct, dec_plus):
            if candidate not in url_variants:
                url_variants.append(candidate)
                added = True
        cur = dec_plus
        if not added:
            break
    for variant in url_variants:
        norm_variant = " ".join(variant.split())
        if _KNOWN_SECRET_RE.search(variant):
            return {"error": "egress_blocked_private_content", "status": "error"}
        if sanitize_text(variant) != variant or sanitize_text(norm_variant) != norm_variant:
            return {"error": "egress_blocked_private_content", "status": "error"}
        if check_sensitivity_floor(variant) in {"S2", "S3", "S4"} or check_sensitivity_floor(norm_variant) in {"S2", "S3", "S4"}:
            return {"error": "egress_blocked_private_content", "status": "error"}
        if packs.mentions_pack_term(variant) or packs.mentions_pack_term(norm_variant):
            return {"error": "egress_blocked_private_content", "status": "error"}
        if blocked_tokens:
            for tok in blocked_tokens:
                if tok and (tok in variant or tok in norm_variant):
                    return {"error": "egress_blocked_private_content", "status": "error"}

    # 5. Search term validation & egress checks (search_term is local only, never sent to network)
    clean_search_term: str | None = None
    if search_term is not None:
        if not isinstance(search_term, str):
            return {"error": "invalid_arguments", "status": "error"}
        clean_search_term = search_term.strip()
        if len(clean_search_term) > MAX_SEARCH_TERM_LENGTH:
            return {"error": "invalid_arguments", "status": "error"}
        if clean_search_term:
            if _KNOWN_SECRET_RE.search(clean_search_term):
                return {"error": "egress_blocked_private_content", "status": "error"}
            if sanitize_text(clean_search_term) != clean_search_term:
                return {"error": "egress_blocked_private_content", "status": "error"}
            if check_sensitivity_floor(clean_search_term) in {"S2", "S3", "S4"}:
                return {"error": "egress_blocked_private_content", "status": "error"}
            if packs.mentions_pack_term(clean_search_term):
                return {"error": "egress_blocked_private_content", "status": "error"}
            if blocked_tokens:
                for tok in blocked_tokens:
                    if tok and tok in clean_search_term:
                        return {"error": "egress_blocked_private_content", "status": "error"}

    # 6. Locate Chromium binary or runner
    active_runner = _get_runner(runner)
    chromium_bin: str | None = None
    run_func: Callable[[list[str], float], tuple[int, str, str]]
    if active_runner is None:
        chromium_bin = find_chromium_binary()
        if not chromium_bin:
            return {
                "status": "browser_unavailable",
                "url": clean_url,
                "title": "",
                "text": "",
                "truncated": False,
                "observed_at": now_iso,
            }
        run_func = _default_subprocess_runner
    else:
        chromium_bin = find_chromium_binary() or "chromium"
        run_func = active_runner

    # 7. Ephemeral headless Chromium execution with validating proxy
    resolver = _get_dns_resolver(dns_resolver)
    proxy_server = _ValidatingProxyServer(
        dns_resolver=resolver,
        blocked_tokens=blocked_tokens,
        upstream_connector=upstream_connector,
    )
    proxy_server.start()
    temp_dir = tempfile.mkdtemp(prefix="jarvis_browser_")
    try:
        argv = [
            chromium_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-default-apps",
            "--mute-audio",
            "--disable-quic",
            "--disable-webrtc",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--blink-settings=imagesEnabled=false",
            f"--proxy-server=http://127.0.0.1:{proxy_server.port}",
            "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            f"--user-data-dir={temp_dir}",
            "--dump-dom",
            clean_url,
        ]
        try:
            returncode, stdout, _stderr = run_func(argv, CHROMIUM_TIMEOUT_SECONDS)
        except (TimeoutError, subprocess.TimeoutExpired):
            return {
                "status": "timeout",
                "url": clean_url,
                "title": "",
                "text": "",
                "truncated": False,
                "observed_at": now_iso,
            }
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "url": clean_url,
                "title": "",
                "text": "",
                "truncated": False,
                "observed_at": now_iso,
            }

        # Cap stdout capture at 2 MB
        if len(stdout) > MAX_STDOUT_BYTES:
            stdout = stdout[:MAX_STDOUT_BYTES]

        if proxy_server.ssrf_blocked and not stdout.strip():
            return {
                "status": "error",
                "error": "private_ip_refused",
                "url": clean_url,
                "title": "",
                "text": "",
                "truncated": False,
                "observed_at": now_iso,
            }

        if not stdout and returncode != 0:
            return {
                "status": "error",
                "error": f"process_exited_{returncode}",
                "url": clean_url,
                "title": "",
                "text": "",
                "truncated": False,
                "observed_at": now_iso,
            }

        # 8. HTML extraction & Challenge detection
        title, clean_text = _extract_dom_text(stdout)
        if _detect_bot_challenge(title, clean_text, stdout):
            return {
                "status": "blocked_by_challenge",
                "url": clean_url,
                "title": title,
                "text": "",
                "truncated": False,
                "observed_at": now_iso,
            }

        final_text, is_truncated = _process_text_and_search_term(clean_text, clean_search_term)
        return {
            "status": "succeeded",
            "url": clean_url,
            "title": title,
            "text": final_text,
            "truncated": is_truncated,
            "observed_at": now_iso,
        }
    finally:
        proxy_server.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)


def dispatch_browser_page_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
    *,
    grant: CapabilityGrantRef | None = None,
    runner: Any = None,
    dns_resolver: Any = None,
    live_grants: dict[str, CapabilityGrantRef] | None = None,
    upstream_connector: Any = None,
) -> tuple[dict[str, Any] | None, str]:
    """Broker dispatch for jarvis.browser_page_read capability."""
    raw_args = call.arguments
    if not isinstance(raw_args, dict):
        return None, "invalid_arguments"

    url = raw_args.get("url")
    if not isinstance(url, str) or not url.strip() or len(url.strip()) > MAX_URL_LENGTH:
        return None, "invalid_arguments"

    search_term = raw_args.get("search_term")
    if search_term is not None and (
        not isinstance(search_term, str) or len(search_term.strip()) > MAX_SEARCH_TERM_LENGTH
    ):
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

    result = execute_browser_page_read(
        url,
        search_term=search_term,
        runner=runner,
        dns_resolver=dns_resolver,
        blocked_tokens=blocked_tokens,
        upstream_connector=upstream_connector,
    )

    if result.get("error"):
        err = result["error"]
        if err == "egress_blocked_private_content":
            return None, "egress_pack_blocked"
        if err == "private_ip_refused":
            return None, "private_ip_refused"
        if err == "invalid_url":
            return None, "invalid_arguments"
        if err == "dns_resolution_failed":
            return None, "dns_resolution_failed"
        return None, err

    return result, default_error
