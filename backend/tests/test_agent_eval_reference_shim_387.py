"""Tests for Spec 387 — AGENT-EVAL-REFERENCE-SHIM-1.

Verifies:
1. Arm A standalone CLI runner (execute_claude_cli) command flags, empty cwd,
   DEVNULL stdin, and *_API_KEY environment stripping.
2. Proof-only loopback OpenAI-compatible reference shim (reference_shim.py)
   fail-closed host, port, cloud_ok, vault path, and model validation.
3. Live ephemeral loopback HTTP endpoints (GET /health, GET /slots, GET /v1/models,
   POST /v1/chat/completions) with plain, json_schema, multi-turn, 400, 502, 504.
4. reference-shim CLI parser and vault-guard rejection in runner.py and reference_shim.py.

All tests run offline without network access or GPU interaction.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import vault_guard  # noqa: E402
from scripts.benchmarks.agent_eval.reference_shim import (  # noqa: E402
    build_claude_command,
    format_messages_for_prompt,
    is_allowed_reference_model,
    make_reference_shim_server,
    validate_host,
    validate_port,
    validate_tasks_file,
)
from scripts.benchmarks.agent_eval.reference_shim import (  # noqa: E402
    main as shim_main,
)
from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    build_parser,
    execute_claude_cli,
    set_claude_runner,
)
from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    main as runner_main,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no ambient proxy interferes with loopback requests and clean runner."""
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    set_claude_runner(None)
    yield
    set_claude_runner(None)


def _http_client_opener() -> urllib.request.OpenerDirector:
    """Create opener bypassing any proxy handlers for loopback test server."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _make_cloud_tasks_file(tmp_path: Path, cloud_ok: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps({
            "set_id": "test_set",
            "cloud_ok": cloud_ok,
            "tasks": [{"id": "t1", "prompt": "What is 2+2?", "checks": []}],
        }),
        encoding="utf-8",
    )
    return tasks_file


# -----------------------------------------------------------------------------
# 1. Arm A CLI Runner (execute_claude_cli)
# -----------------------------------------------------------------------------


def test_execute_claude_cli_flags_cwd_and_env_stripping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, Any] = {}

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_kwargs["cmd"] = cmd
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="42\n", stderr="")

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setenv("TEST_API_KEY", "secret-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("SAFE_ENV_VAR", "keep-me")

    out, state, err = execute_claude_cli("Calculate 40 + 2", "haiku", timeout=15.0)

    assert state == "completed"
    assert out == "42"
    assert err is None

    cmd = captured_kwargs["cmd"]
    assert cmd[0] == "claude"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "-m" not in cmd
    assert "--no-session-persistence" in cmd
    assert "--strict-mcp-config" in cmd
    assert "-p" in cmd
    assert cmd[-2:] == ["--", "Calculate 40 + 2"]

    assert captured_kwargs["stdin"] == subprocess.DEVNULL
    assert captured_kwargs["timeout"] == 15.0

    # Cwd must be a temporary directory that existed and was isolated
    cwd = captured_kwargs["cwd"]
    assert isinstance(cwd, str)
    assert not cwd.startswith(str(_REPO_ROOT))

    # Environment must have *_API_KEY stripped
    env = captured_kwargs["env"]
    assert "TEST_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("SAFE_ENV_VAR") == "keep-me"


def test_execute_claude_cli_injected_runner_bypass() -> None:
    def stub_runner(prompt: str, model: str) -> tuple[str, str, str | None]:
        return f"stub: {prompt} on {model}", "completed", None

    set_claude_runner(stub_runner)
    out, state, err = execute_claude_cli("Hello", "haiku", timeout=5.0)
    assert out == "stub: Hello on haiku"
    assert state == "completed"
    assert err is None


def test_execute_claude_cli_failure_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_fail(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="invalid options")

    monkeypatch.setattr(subprocess, "run", mock_fail)
    out, state, err = execute_claude_cli("test", "haiku", timeout=5.0)
    assert state == "failed_terminal"
    assert err == "claude_cli_exit_2"
    assert out == "invalid options"

    def mock_timeout(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 5.0)

    monkeypatch.setattr(subprocess, "run", mock_timeout)
    with pytest.raises(TimeoutError, match="reference task deadline expired"):
        execute_claude_cli("test", "haiku", timeout=5.0)


# -----------------------------------------------------------------------------
# 2. Reference Shim Fail-Closed Validation
# -----------------------------------------------------------------------------


def test_validation_host_and_port() -> None:
    assert validate_host("127.0.0.1") == "127.0.0.1"
    assert validate_host("localhost") == "localhost"
    assert validate_host("::1") == "::1"

    with pytest.raises(ValueError, match="not a loopback interface"):
        validate_host("192.168.1.1")
    with pytest.raises(ValueError, match="not a loopback interface"):
        validate_host("0.0.0.0")  # noqa: S104
    with pytest.raises(ValueError, match="not a loopback interface"):
        validate_host("example.com")

    assert validate_port(8080) == 8080
    assert validate_port(0) == 0

    with pytest.raises(ValueError, match="Port 8000 is refused"):
        validate_port(8000)
    with pytest.raises(ValueError, match="Invalid port"):
        validate_port(-1)
    with pytest.raises(ValueError, match="Invalid port"):
        validate_port(70000)


def test_validation_model() -> None:
    assert is_allowed_reference_model("haiku") is True
    assert is_allowed_reference_model("claude-haiku-4-5-20251001") is True
    assert is_allowed_reference_model("claude-haiku-3-5") is True

    assert is_allowed_reference_model("sonnet") is False
    assert is_allowed_reference_model("claude-sonnet-4-20250514") is False
    assert is_allowed_reference_model("gpt-4o") is False
    assert is_allowed_reference_model("qwen") is False


def test_validation_tasks_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    valid_file = _make_cloud_tasks_file(tmp_path, cloud_ok=True)
    parsed = validate_tasks_file(valid_file)
    assert parsed["cloud_ok"] is True

    invalid_cloud = _make_cloud_tasks_file(tmp_path / "subdir", cloud_ok=False)
    with pytest.raises(ValueError, match="cloud_ok != True"):
        validate_tasks_file(invalid_cloud)

    missing_file = tmp_path / "nonexistent.json"
    with pytest.raises(FileNotFoundError):
        validate_tasks_file(missing_file)

    not_json = tmp_path / "bad.json"
    not_json.write_text("not json content", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse"):
        validate_tasks_file(not_json)

    # Vault path check
    vault_dir = tmp_path / "private_vault"
    vault_dir.mkdir()
    monkeypatch.setenv(vault_guard.VAULT_PATH_ENV, str(vault_dir))
    vault_task_file = vault_dir / "secret_tasks.json"
    vault_task_file.write_text(json.dumps({"cloud_ok": True}), encoding="utf-8")

    with pytest.raises(PermissionError, match=vault_guard.REFUSED_MESSAGE):
        validate_tasks_file(vault_task_file)


# -----------------------------------------------------------------------------
# 3. Message formatting and Command Construction
# -----------------------------------------------------------------------------


def test_format_messages_for_prompt() -> None:
    # Single user string turn
    sys_prompt, prompt = format_messages_for_prompt([{"role": "user", "content": "What is 2+2?"}])
    assert sys_prompt is None
    assert prompt == "What is 2+2?"

    # System + user turn
    sys_prompt, prompt = format_messages_for_prompt([
        {"role": "system", "content": "You are a calculator."},
        {"role": "user", "content": "What is 2+2?"},
    ])
    assert sys_prompt == "You are a calculator."
    assert prompt == "What is 2+2?"

    # Multi-turn conversation with list-of-text parts
    sys_prompt, prompt = format_messages_for_prompt([
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": [{"type": "text", "text": "Turn 1"}]},
        {"role": "assistant", "content": "Response 1"},
        {"role": "tool", "content": "Tool output 1"},
        {"role": "user", "content": "Turn 2"},
    ])
    assert sys_prompt == "System directive"
    assert prompt == "User: Turn 1\n\nAssistant: Response 1\n\nTool: Tool output 1\n\nUser: Turn 2"


def test_build_claude_command() -> None:
    cmd = build_claude_command("Hello", "haiku", system_prompt="Be concise", json_schema='{"type":"object"}')
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--system-prompt" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "Be concise"
    assert "--json-schema" in cmd
    assert cmd[cmd.index("--json-schema") + 1] == '{"type":"object"}'
    assert cmd[-2:] == ["--", "Hello"]


# -----------------------------------------------------------------------------
# 4. Live Loopback HTTP Server Endpoints
# -----------------------------------------------------------------------------


@contextmanager
def running_shim_server(
    runner: Any,
    *,
    model: str = "haiku",
    timeout: float = 10.0,
    tasks_path: Path | None = None,
) -> Iterator[str]:
    server = make_reference_shim_server(
        host="127.0.0.1",
        port=0,
        model=model,
        timeout=timeout,
        runner=runner,
        tasks_path=tasks_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()


def _request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    opener = _http_client_opener()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_shim_health_models_and_slots() -> None:
    def stub_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 0, json.dumps({"result": "pong"}), ""

    with running_shim_server(stub_runner, model="haiku") as base:
        # GET /health
        code, body = _request(f"{base}/health")
        assert code == 200
        assert body == {"status": "ok"}

        # GET /props (required by LlamaCppRuntimeOwner.status())
        code, body = _request(f"{base}/props")
        assert code == 200
        assert body["build_id"] == "reference-shim"
        assert body["default_generation_settings"]["n_ctx"] > 0

        # GET /v1/models
        code, body = _request(f"{base}/v1/models")
        assert code == 200
        assert body["object"] == "list"
        assert body["data"] == [{"id": "haiku", "object": "model"}]

        # GET /slots
        code, body = _request(f"{base}/slots")
        assert code == 200
        assert isinstance(body, list)
        assert body[0]["id"] == 0
        assert body[0]["is_processing"] is False


def test_shim_slots_reflects_active_completion() -> None:
    block_event = threading.Event()
    unblock_event = threading.Event()

    def blocking_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        block_event.set()
        unblock_event.wait(timeout=5.0)
        return 0, json.dumps({"result": "done"}), ""

    with running_shim_server(blocking_runner) as base:
        # Start a completion in a worker thread
        comp_res: dict[str, Any] = {}

        def send_comp() -> None:
            c, b = _request(
                f"{base}/v1/chat/completions",
                method="POST",
                payload={"messages": [{"role": "user", "content": "hi"}]},
            )
            comp_res["status"] = c
            comp_res["body"] = b

        t = threading.Thread(target=send_comp)
        t.start()

        # Wait until runner enters
        assert block_event.wait(timeout=3.0)

        # GET /slots should now report is_processing = True
        code, slots = _request(f"{base}/slots")
        assert code == 200
        assert slots[0]["is_processing"] is True

        # Let completion finish
        unblock_event.set()
        t.join(timeout=3.0)

        assert comp_res["status"] == 200
        assert comp_res["body"]["choices"][0]["message"]["content"] == "done"

        # After completion, is_processing must be False again
        code, slots_idle = _request(f"{base}/slots")
        assert code == 200
        assert slots_idle[0]["is_processing"] is False


def test_shim_chat_completions_plain_text() -> None:
    recorded_cmd: list[str] = []

    def stub_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        recorded_cmd.extend(cmd)
        cli_out = {
            "result": "The answer is 42.",
            "usage": {"input_tokens": 15, "output_tokens": 7},
        }
        return 0, json.dumps(cli_out), ""

    with running_shim_server(stub_runner, model="haiku") as base:
        payload = {
            "model": "haiku",
            "messages": [{"role": "user", "content": "Compute 40 + 2"}],
        }
        code, body = _request(f"{base}/v1/chat/completions", method="POST", payload=payload)
        assert code == 200
        assert body["object"] == "chat.completion"
        assert body["model"] == "haiku"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["choices"][0]["message"]["content"] == "The answer is 42."
        assert body["usage"] == {
            "prompt_tokens": 15,
            "completion_tokens": 7,
            "total_tokens": 22,
        }

        # Verify command flags
        assert "--model" in recorded_cmd
        assert "--no-session-persistence" in recorded_cmd
        assert "--strict-mcp-config" in recorded_cmd
        assert "--tools" in recorded_cmd
        assert "--output-format" in recorded_cmd
        assert recorded_cmd[-1] == "Compute 40 + 2"


def test_shim_chat_completions_json_schema_structured_output() -> None:
    recorded_cmd: list[str] = []

    def stub_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        recorded_cmd.extend(cmd)
        cli_out = {
            "result": "Ignored plain text",
            "structured_output": {"metric": 100, "verified": True},
            "usage": {"input_tokens": 30, "output_tokens": 12},
        }
        return 0, json.dumps(cli_out), ""

    with running_shim_server(stub_runner) as base:
        schema = {
            "type": "object",
            "properties": {"metric": {"type": "integer"}, "verified": {"type": "boolean"}},
            "required": ["metric", "verified"],
        }
        payload = {
            "messages": [{"role": "user", "content": "Output metrics"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "metrics_schema", "schema": schema},
            },
        }
        code, body = _request(f"{base}/v1/chat/completions", method="POST", payload=payload)
        assert code == 200
        # structured_output must be serialized to json string
        content = body["choices"][0]["message"]["content"]
        assert json.loads(content) == {"metric": 100, "verified": True}
        assert body["usage"]["total_tokens"] == 42

        # Verify --json-schema was passed compactly
        assert "--json-schema" in recorded_cmd
        idx = recorded_cmd.index("--json-schema")
        assert json.loads(recorded_cmd[idx + 1]) == schema


def test_shim_chat_completions_errors_and_privacy() -> None:
    # 1. Invalid JSON body -> 400
    opener = _http_client_opener()
    with running_shim_server(lambda *a, **kw: (0, "{}", "")) as base:
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            opener.open(req)
        assert exc_info.value.code == 400

        # 2. Non-list messages -> 400
        code, err = _request(
            f"{base}/v1/chat/completions",
            method="POST",
            payload={"messages": "not a list"},
        )
        assert code == 400
        assert "messages" in err["error"]["message"]

        # 3. Disallowed model -> 400
        code, err = _request(
            f"{base}/v1/chat/completions",
            method="POST",
            payload={"model": "claude-3-opus", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert code == 400
        assert "not permitted" in err["error"]["message"]

        # 3b. Top-level oneOf/allOf/anyOf json_schema -> 400 mentioning response_format/schema
        code, err = _request(
            f"{base}/v1/chat/completions",
            method="POST",
            payload={
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "union", "schema": {"oneOf": [{"type": "object"}]}},
                },
            },
        )
        assert code == 400
        assert "response_format" in err["error"]["message"]
        assert "schema" in err["error"]["message"]

    # 4. CLI failure -> 502 with bounded message that never leaks prompt or secret
    secret_prompt = "TOP_SECRET_PROMPT_DO_NOT_LEAK"

    def fail_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 1, "", "Internal CLI failure details"

    with running_shim_server(fail_runner) as base:
        code, err = _request(
            f"{base}/v1/chat/completions",
            method="POST",
            payload={"messages": [{"role": "user", "content": secret_prompt}]},
        )
        assert code == 502
        err_msg = json.dumps(err)
        assert secret_prompt not in err_msg
        assert "Internal CLI failure details" not in err_msg
        assert err["error"]["type"] == "bad_gateway"

    # 5. CLI is_error=True with exit 0 -> 502 without leaking error details
    def is_error_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 0, json.dumps({"is_error": True, "result": secret_prompt}), ""

    with running_shim_server(is_error_runner) as base:
        code, err = _request(
            f"{base}/v1/chat/completions",
            method="POST",
            payload={"messages": [{"role": "user", "content": secret_prompt}]},
        )
        assert code == 502
        err_msg = json.dumps(err)
        assert secret_prompt not in err_msg
        assert err["error"]["type"] == "bad_gateway"

    # 6. CLI timeout -> 504 with bounded message
    def timeout_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        raise TimeoutError("CLI timed out")

    with running_shim_server(timeout_runner) as base:
        code, err = _request(
            f"{base}/v1/chat/completions",
            method="POST",
            payload={"messages": [{"role": "user", "content": secret_prompt}]},
        )
        assert code == 504
        err_msg = json.dumps(err)
        assert secret_prompt not in err_msg
        assert err["error"]["type"] == "gateway_timeout"


# -----------------------------------------------------------------------------
# 5. CLI Subcommand and Entrypoint Guardrails
# -----------------------------------------------------------------------------


def test_agent_eval_reference_shim_subcommand_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["reference-shim", "--tasks", "tasks.json", "--port", "8081"])
    assert args.subcommand == "reference-shim"
    assert args.tasks == "tasks.json"
    assert args.host == "127.0.0.1"
    assert args.port == 8081
    assert args.model == "haiku"
    assert args.timeout == 60.0


def test_reference_shim_cli_vault_guard_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_dir = tmp_path / "protected_vault"
    vault_dir.mkdir()
    monkeypatch.setenv(vault_guard.VAULT_PATH_ENV, str(vault_dir))

    vault_tasks = vault_dir / "tasks.json"
    vault_tasks.write_text(json.dumps({"cloud_ok": True, "tasks": []}), encoding="utf-8")

    # 1. Test runner.py reference-shim subcommand rejects vault path with REFUSED_EXIT (3)
    err_capture = io.StringIO()
    with redirect_stderr(err_capture):
        code = runner_main(["reference-shim", "--tasks", str(vault_tasks)])
    assert code == vault_guard.REFUSED_EXIT
    assert vault_guard.REFUSED_MESSAGE in err_capture.getvalue()

    # 2. Test reference_shim.py main rejects vault path with REFUSED_EXIT (3)
    err_capture = io.StringIO()
    with redirect_stderr(err_capture):
        code = shim_main(["--tasks", str(vault_tasks)])
    assert code == vault_guard.REFUSED_EXIT
    assert vault_guard.REFUSED_MESSAGE in err_capture.getvalue()


def test_reference_shim_cli_cloud_ok_false_rejection(tmp_path: Path) -> None:
    tasks = _make_cloud_tasks_file(tmp_path, cloud_ok=False)
    err_capture = io.StringIO()
    with redirect_stderr(err_capture):
        code = shim_main(["--tasks", str(tasks)])
    assert code == 1
    assert "cloud_ok != True" in err_capture.getvalue()
