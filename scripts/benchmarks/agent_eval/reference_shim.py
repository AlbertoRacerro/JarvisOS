#!/usr/bin/env python3
"""Proof-only loopback OpenAI-compatible reference shim for Arm B.

Stdlib only. Exposes an OpenAI-compatible HTTP interface that translates chat completion
requests into Claude CLI invocations for Gate Zero reference evaluation (Spec 387).
Runs strictly on loopback interfaces, refuses port 8000, enforces cloud_ok tasks files,
and restricts models to Haiku reference models.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts import vault_guard
from scripts.benchmarks.agent_eval.runner import is_loopback_host

ALLOWED_MODEL_PREFIX = "claude-haiku-"
ALLOWED_MODEL_EXACT = "haiku"


def is_allowed_reference_model(model: str) -> bool:
    """Return True if model is 'haiku' or starts with 'claude-haiku-'."""
    clean = model.strip()
    return clean == ALLOWED_MODEL_EXACT or clean.startswith(ALLOWED_MODEL_PREFIX)


def validate_host(host: str) -> str:
    """Validate loopback host; refuse non-loopback addresses."""
    clean = host.strip()
    if not clean or not is_loopback_host(clean):
        raise ValueError(f"Host '{host}' is not a loopback interface; only loopback is permitted")
    return clean


def validate_port(port: int) -> int:
    """Validate port number; refuse port 8000."""
    if port == 8000:
        raise ValueError("Port 8000 is refused; proof shim cannot run on the live app port")
    if port < 0 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    return port


def validate_tasks_file(tasks_path: str | Path) -> dict[str, Any]:
    """Validate task file exists, is not in vault, and has cloud_ok=True."""
    vault = vault_guard.vault_root()
    if vault_guard.touches_vault(tasks_path, vault):
        raise PermissionError(vault_guard.REFUSED_MESSAGE)
    path = Path(tasks_path)
    if not path.is_file():
        raise FileNotFoundError(f"Tasks file not found: {tasks_path}")
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as exc:
        raise ValueError(f"Failed to parse tasks file JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Tasks file must contain a JSON object")
    if data.get("cloud_ok") is not True:
        raise ValueError("Tasks file has cloud_ok != True; reference shim requires cloud_ok: true")
    return data


def extract_text_content(content: Any) -> str:
    """Extract plain text from string or OpenAI list-of-parts content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


def format_messages_for_prompt(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Parse messages into system prompt and deterministic prompt text.

    Extracts system role messages into system prompt.
    Formats remaining user/assistant/tool turns deterministically into prompt text.
    Handles both string content and list-of-text-parts content.
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user")).lower()
        content = extract_text_content(msg.get("content", ""))
        if role == "system":
            if content.strip():
                system_parts.append(content.strip())
        else:
            turns.append((role, content))

    system_prompt = "\n\n".join(system_parts) if system_parts else None

    if not turns:
        prompt_text = ""
    elif len(turns) == 1 and turns[0][0] == "user":
        prompt_text = turns[0][1]
    else:
        formatted = [f"{role.capitalize()}: {text}" for role, text in turns]
        prompt_text = "\n\n".join(formatted)

    return system_prompt, prompt_text


def build_claude_command(
    prompt: str,
    model: str,
    *,
    system_prompt: str | None = None,
    json_schema: str | None = None,
) -> list[str]:
    """Build claude CLI command matching Spec 387."""
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "json",
    ]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    if json_schema:
        cmd.extend(["--json-schema", json_schema])
    cmd.append(prompt)
    return cmd


def default_claude_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
    """Execute claude CLI command in a fresh temporary directory with sanitized env."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
    with tempfile.TemporaryDirectory() as tmp_dir:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_dir,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr


class ShimState:
    """Thread-safe state for the reference shim server."""

    def __init__(
        self,
        model: str = "haiku",
        timeout: float = 60.0,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.runner = runner or default_claude_runner
        self._lock = threading.Lock()
        self._active_completions = 0

    @property
    def is_processing(self) -> bool:
        with self._lock:
            return self._active_completions > 0

    def enter_completion(self) -> None:
        with self._lock:
            self._active_completions += 1

    def exit_completion(self) -> None:
        with self._lock:
            self._active_completions = max(0, self._active_completions - 1)


class ReferenceShimHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing loopback OpenAI chat completions for Arm B."""

    server: ReferenceShimServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default stderr logging to keep test and eval logs clean
        pass

    def _send_json(self, status_code: int, body: dict[str, Any] | list[Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status_code: int, message: str, err_type: str) -> None:
        self._send_json(
            status_code,
            {
                "error": {
                    "message": message,
                    "type": err_type,
                }
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/slots":
            self._send_json(200, [{"id": 0, "is_processing": self.server.state.is_processing}])
            return
        if path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": self.server.state.model, "object": "model"}],
                },
            )
            return
        self._send_error_json(404, "Not Found", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_error_json(404, "Not Found", "not_found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw_body)
        except Exception:
            self._send_error_json(400, "Invalid JSON body", "invalid_request_error")
            return

        if not isinstance(body, dict):
            self._send_error_json(400, "Request body must be a JSON object", "invalid_request_error")
            return

        messages = body.get("messages")
        if not isinstance(messages, list):
            self._send_error_json(400, "Field 'messages' must be a list", "invalid_request_error")
            return

        req_model = str(body.get("model") or self.server.state.model).strip()
        if not is_allowed_reference_model(req_model):
            self._send_error_json(
                400,
                f"Model '{req_model}' is not permitted; only haiku models are allowed",
                "invalid_request_error",
            )
            return

        system_prompt, prompt_text = format_messages_for_prompt(messages)

        json_schema_arg: str | None = None
        response_format = body.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
            js = response_format.get("json_schema")
            if isinstance(js, dict):
                schema = js.get("schema")
                if isinstance(schema, dict):
                    json_schema_arg = json.dumps(schema, separators=(",", ":"))

        cmd = build_claude_command(
            prompt_text,
            req_model,
            system_prompt=system_prompt,
            json_schema=json_schema_arg,
        )

        state = self.server.state
        state.enter_completion()
        try:
            try:
                res = state.runner(cmd, timeout=state.timeout)
            except (TimeoutError, subprocess.TimeoutExpired):
                self._send_error_json(504, "Reference CLI invocation timed out", "gateway_timeout")
                return
            except (OSError, subprocess.SubprocessError):
                self._send_error_json(502, "Reference CLI execution failed", "bad_gateway")
                return

            if isinstance(res, dict):
                cli_data = res
            elif isinstance(res, tuple) and len(res) >= 3:
                code, stdout, _stderr = res[:3]
                if code != 0:
                    self._send_error_json(502, f"Reference CLI failed with code {code}", "bad_gateway")
                    return
                try:
                    cli_data = json.loads(stdout)
                except Exception:
                    self._send_error_json(502, "Reference CLI output was not valid JSON", "bad_gateway")
                    return
            else:
                self._send_error_json(502, "Reference CLI returned invalid result format", "bad_gateway")
                return

            if not isinstance(cli_data, dict):
                self._send_error_json(502, "Reference CLI output must be a JSON object", "bad_gateway")
                return

            structured_output = cli_data.get("structured_output")
            if structured_output is not None:
                assistant_content = json.dumps(structured_output)
            else:
                assistant_content = str(cli_data.get("result", ""))

            usage_raw = cli_data.get("usage")
            usage_dict = usage_raw if isinstance(usage_raw, dict) else {}
            input_tokens = int(usage_dict.get("input_tokens") or 0)
            output_tokens = int(usage_dict.get("output_tokens") or 0)
            total_tokens = input_tokens + output_tokens

            completion_payload = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": assistant_content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": total_tokens,
                },
            }
            self._send_json(200, completion_payload)
        finally:
            state.exit_completion()


class ReferenceShimServer(ThreadingHTTPServer):
    """Threading HTTPServer for the reference shim with IPv6 and loopback support."""

    def __init__(self, server_address: tuple[str, int], state: ShimState) -> None:
        host, _ = server_address
        try:
            ip = ipaddress.ip_address(host)
            if ip.version == 6:
                self.address_family = socket.AF_INET6
        except ValueError:
            pass
        self.state = state
        self.daemon_threads = True
        super().__init__(server_address, ReferenceShimHandler)


def make_reference_shim_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    model: str = "haiku",
    timeout: float = 60.0,
    runner: Callable[..., Any] | None = None,
    tasks_path: str | Path | None = None,
) -> ReferenceShimServer:
    """Factory creating a validated ReferenceShimServer."""
    validate_host(host)
    validate_port(port)
    if not is_allowed_reference_model(model):
        raise ValueError(f"Model '{model}' is not an allowed reference model; only 'haiku' or 'claude-haiku-*' permitted")
    if tasks_path is not None:
        validate_tasks_file(tasks_path)
    state = ShimState(model=model, timeout=timeout, runner=runner)
    return ReferenceShimServer((host, port), state)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for reference shim."""
    parser = argparse.ArgumentParser(
        prog="reference_shim",
        description="Proof-only loopback OpenAI-compatible reference shim for Arm B (Haiku in Jarvis Hermes harness)",
    )
    parser.add_argument("--tasks", required=True, help="Path to task set JSON file (requires cloud_ok=true)")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default 8080, port 8000 refused)")
    parser.add_argument("--model", default="haiku", help="Reference model (default 'haiku')")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds (default 60)")
    return parser


def run_shim_cli(
    tasks: str | Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    model: str = "haiku",
    timeout: float = 60.0,
    runner: Callable[..., Any] | None = None,
) -> int:
    """CLI execution entrypoint with fail-closed validation."""
    vault = vault_guard.vault_root()
    if vault_guard.touches_vault(tasks, vault):
        print(vault_guard.REFUSED_MESSAGE, file=sys.stderr)
        return vault_guard.REFUSED_EXIT
    try:
        validate_tasks_file(tasks)
        server = make_reference_shim_server(
            host=host,
            port=port,
            model=model,
            timeout=timeout,
            runner=runner,
        )
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return vault_guard.REFUSED_EXIT
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    actual_port = server.server_port
    print(f"Reference shim listening on http://{host}:{actual_port} (model: {model})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down reference shim...", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main CLI function for python3 -m scripts.benchmarks.agent_eval.reference_shim."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_shim_cli(
        tasks=args.tasks,
        host=args.host,
        port=args.port,
        model=args.model,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
