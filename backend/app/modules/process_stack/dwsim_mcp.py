"""Small, hash-pinned stdio client for the DWSIM MCP server."""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT_S = 10.0


class DwsimMcpError(RuntimeError):
    """Base error for DWSIM MCP process and protocol failures."""


class DwsimExecutableMismatch(DwsimMcpError):
    """The executable does not match the caller-pinned digest."""


class DwsimProcessError(DwsimMcpError):
    """The DWSIM MCP subprocess could not be started or exited unexpectedly."""


class DwsimProtocolError(DwsimMcpError):
    """The peer sent a malformed, missing, or unexpected JSON-RPC response."""


class DwsimToolError(DwsimMcpError):
    """The peer reported an MCP or JSON-RPC tool error."""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


class DwsimTimeout(DwsimMcpError):
    """A bounded MCP request did not receive a response in time."""


class DwsimMcpClient:
    """Synchronous JSON-RPC client over the MCP newline-delimited stdio transport."""

    def __init__(self, executable: Path, expected_sha256: str) -> None:
        self.executable = Path(executable)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("expected_sha256 must be a 64-character SHA-256 digest")
        self.expected_sha256 = expected_sha256.lower()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[bytes | None] = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()

    def __enter__(self) -> DwsimMcpClient:
        self._verify_executable()
        try:
            self._process = subprocess.Popen(
                [str(self.executable)],
                cwd=self.executable.parent,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise DwsimProcessError("DWSIM MCP executable could not be started") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._discard_stderr, daemon=True).start()
        try:
            response = self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "jarvisos-dwsim", "version": "1"},
                },
                _DEFAULT_TIMEOUT_S,
            )
            if not isinstance(response, dict) or not isinstance(response.get("protocolVersion"), str):
                raise DwsimProtocolError("DWSIM MCP initialize response is malformed")
            self._notify("notifications/initialized", {})
        except Exception:
            self._stop()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._stop()

    def call(self, name: str, arguments: dict[str, Any], timeout_s: float) -> Any:
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise ValueError("tool name and arguments are invalid")
        result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout_s)
        if not isinstance(result, dict):
            raise DwsimProtocolError("DWSIM MCP tool response is malformed")
        if result.get("isError") is True:
            error_content = result.get("structuredContent")
            code = error_content.get("code") if isinstance(error_content, dict) else result.get("code")
            raise DwsimToolError("DWSIM MCP tool reported an error", code=code)
        structured = result.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, dict):
                raise DwsimProtocolError("DWSIM MCP tool response is not a JSON object")
            return structured
        content = result.get("content")
        if not isinstance(content, list):
            raise DwsimProtocolError("DWSIM MCP tool response has no content")
        text_blocks = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        if len(text_blocks) != 1 or not isinstance(text_blocks[0], str):
            raise DwsimProtocolError("DWSIM MCP tool response must contain one JSON text block")
        try:
            parsed = json.loads(text_blocks[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise DwsimProtocolError("DWSIM MCP tool response is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise DwsimProtocolError("DWSIM MCP tool response is not a JSON object")
        return parsed

    def _verify_executable(self) -> None:
        digest = hashlib.sha256()
        try:
            with self.executable.open("rb") as executable_file:
                for block in iter(lambda: executable_file.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise DwsimExecutableMismatch("DWSIM MCP executable is unavailable") from exc
        if digest.hexdigest() != self.expected_sha256:
            raise DwsimExecutableMismatch("DWSIM MCP executable SHA-256 does not match")

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                self._responses.put(line)
        finally:
            self._responses.put(None)

    def _discard_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while process.stderr.read(4096):
            pass

    def _request(self, method: str, params: dict[str, Any], timeout_s: float) -> Any:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        with self._lock:
            process = self._require_process()
            self._next_id += 1
            request_id = self._next_id
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                line = self._responses.get(timeout=timeout_s)
            except queue.Empty as exc:
                self._stop()
                raise DwsimTimeout("DWSIM MCP request timed out") from exc
            if line is None:
                code = process.poll()
                raise DwsimProcessError(f"DWSIM MCP process exited unexpectedly ({code})")
            try:
                response = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DwsimProtocolError("DWSIM MCP sent malformed JSON") from exc
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise DwsimProtocolError("DWSIM MCP sent a malformed JSON-RPC response")
            if response.get("id") != request_id:
                raise DwsimProtocolError("DWSIM MCP response ID does not match the request")
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise DwsimToolError("DWSIM MCP returned a JSON-RPC error", code=code)
            if "result" not in response:
                raise DwsimProtocolError("DWSIM MCP response has no result")
            return response["result"]

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise DwsimProcessError("DWSIM MCP input stream is unavailable")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
            process.stdin.flush()
        except OSError as exc:
            raise DwsimProcessError("DWSIM MCP request could not be written") from exc

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            raise DwsimProcessError("DWSIM MCP client is not connected")
        return self._process

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
