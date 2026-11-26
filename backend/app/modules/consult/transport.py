"""Relay consult session transport and manifest builder (spec 380 part b).

Consult transport routes through ``run_ai_task`` on ``consult:zone_a`` (invariant 1),
creating ONE ``ai_jobs`` ledger row (invariant 2) and saving ``ai_job_id`` on
``consult_attempts``.
Relay consult sessions run with:
- fresh empty workspace (no .git, no repo, no dependency mounts, no context releases);
- fresh empty agent home holding ONLY credentials;
- egress restricted to the agent's own hosts (no common_egress);
- built-in web tools disabled; only guarded MCP server reachable;
- 5-minute timeout (300s), output capped at 24,000 characters;
- accounting basis 'subscription', cost 0.0.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.database import open_sqlite_connection
from app.core.paths import relay_root
from app.modules.ai.contracts import AIProviderAdapter, AIRequest, AIResponse, AIUsage
from app.modules.ai.execution_types import ProviderBinding
from app.modules.consult.config import TrustedAccount
from app.modules.consult.mcp_server import ConsultMcpServer
from app.modules.consult.service import ConsultTransport
from app.modules.relay_gateway import sandbox
from app.modules.relay_gateway.service import load_config

MAX_CONSULT_OUTPUT_CHARS = 24_000
CONSULT_TIMEOUT_SECONDS = 300
CONSULT_ROUTE_CLASS = "consult:zone_a"
FAKE_TRANSPORT_ENV = "JARVISOS_CONSULT_FAKE_TRANSPORT"


def build_consult_manifest(
    config: dict[str, Any],
    agent: str,
    workspace: Path,
    home: Path,
    context_dir: Path,
    runtime_root: Path,
    *,
    mcp_socket_path: str | None = None,
    mcp_config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Pure construction of the Relay consult sandbox manifest.

    Guarantees:
    - Fresh empty working directory (no .git, no repository, no dependency mounts,
      no context releases, no jarvis-actions).
    - Fresh empty home holding ONLY the agent's credentials.
    - Egress allow: ONLY the agent's configured egress hosts (NO common_egress).
    - Built-in web tools disabled (--restricted, --tools "", --disallowedTools WebSearch,WebFetch,
      --strict-mcp-config, --mcp-config <path>, --effort max).
    - Enforces refuse_vault_sources / deny_roots.
    """
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    context_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    spec = config.get("agents", {}).get(agent, {})
    credentials: list[dict[str, str]] = []
    for relative in spec.get("credentials", []):
        source = Path(os.path.expanduser(f"~/{relative}"))
        if source.is_file():
            credentials.append({"source": str(source), "target": relative})

    agent_sandbox_args = [
        "--restricted",
        "--tools", "",
        "--disallowedTools", "WebSearch,WebFetch",
        "--strict-mcp-config",
        "--effort", "max",
    ]
    if mcp_config_path:
        agent_sandbox_args.extend(["--mcp-config", str(mcp_config_path)])

    agent_spec = {
        "command": spec.get("command", agent),
        "binary": shutil.which(spec.get("command", agent)) or spec.get("command", agent),
        "credentials": credentials,
        "environment": dict(spec.get("environment", {})),
        "sandbox_args": agent_sandbox_args,
    }

    manifest: dict[str, Any] = {
        "version": sandbox.MANIFEST_VERSION,
        "workspace": os.path.realpath(workspace),
        "context_dir": str(context_dir),
        "home": str(home),
        "runtime_root": str(runtime_root),
        "agents": {agent: agent_spec},
        "masked": [],
        "dependency_mounts": [],
        "egress_allow": sorted(spec.get("egress", [])),
        "git_config": None,
        "timeout_seconds": CONSULT_TIMEOUT_SECONDS,
        "output_max_chars": MAX_CONSULT_OUTPUT_CHARS,
    }
    if mcp_socket_path:
        manifest["mcp_socket"] = mcp_socket_path
    if mcp_config_path:
        manifest["mcp_config"] = str(mcp_config_path)

    vault_path = sandbox.vault_root()
    if vault_path is not None:
        manifest["deny_roots"] = [str(vault_path)]
        sandbox.refuse_vault_sources(manifest, agent_spec)

    return manifest


ConsultRunner = Callable[[dict[str, Any], str, float], str]


class RelayConsultAdapter(AIProviderAdapter):
    """Executes a Relay consult session inside bubblewrap or through an injected runner."""

    def __init__(
        self,
        *,
        account: TrustedAccount | None = None,
        runner: ConsultRunner | None = None,
        mcp_server: ConsultMcpServer | None = None,
    ) -> None:
        self.account = account
        self.runner = runner
        self.mcp_server = mcp_server

    def complete(self, request: AIRequest) -> AIResponse:
        agent = self.account.agent if self.account else "claude"
        model = self.account.model if self.account else (request.model_preference or "claude-opus-5-5")
        packet = request.prompt or ""

        # Check for test env or injected runner
        if self.runner is not None:
            answer = self.runner({}, packet, float(CONSULT_TIMEOUT_SECONDS))
        elif os.environ.get(FAKE_TRANSPORT_ENV) == "1":
            answer = (
                "Expert consultation response (simulated):\n"
                "Review the physical mechanisms and relevant literature.\n"
                "Recompute all numbers with Jarvis tools before application."
            )
        else:
            answer = self._run_sandbox_session(agent, model, packet)

        bounded_answer = answer[:MAX_CONSULT_OUTPUT_CHARS]
        return AIResponse(
            request_id=request.request_id,
            text=bounded_answer,
            model_id=model,
            provider_id="relay_consult",
            finish_reason="stop",
            usage=AIUsage(
                provider_id="relay_consult",
                model_id=model,
                input_tokens=len(packet) // 4,
                output_tokens=len(bounded_answer) // 4,
                provider_cost_estimate=0.0,
            ),
        )

    def _run_sandbox_session(self, agent: str, model: str, packet: str) -> str:
        gateway_config = load_config()
        if not gateway_config.get("enabled") and os.getenv("JARVISOS_RELAY_GATEWAY_ENABLED") != "1":
            raise RuntimeError("relay_gateway_disabled")

        root = relay_root() / "consult-runs"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        session_dir = tempfile.mkdtemp(prefix="consult-", dir=root)
        workspace = Path(session_dir) / "workspace"
        home = Path(session_dir) / "home"
        context_dir = Path(session_dir) / "context"
        runtime_root = Path(session_dir) / "sandbox-runs"

        mcp_socket: str | None = None
        if self.mcp_server is not None and self.mcp_server._running:
            mcp_socket = self.mcp_server.socket_path

        mcp_config_path = Path(session_dir) / "mcp-config.json"
        if mcp_socket and os.path.exists(mcp_socket):
            mcp_config = {
                "mcpServers": {
                    "jarvis-consult": {
                        "command": "/usr/bin/python3",
                        "args": ["/run/jarvis/sandbox.py", "mcp-bridge", "/run/jarvis/consult-mcp.sock"],
                    }
                }
            }
        else:
            mcp_config = {"mcpServers": {}}
        mcp_config_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")

        manifest = build_consult_manifest(
            gateway_config,
            agent,
            workspace,
            home,
            context_dir,
            runtime_root,
            mcp_socket_path=mcp_socket,
            mcp_config_path=mcp_config_path,
        )

        manifests_dir = Path(session_dir) / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest_path = manifests_dir / sandbox.manifest_name(str(workspace))
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Run via sandbox.py shim
        cmd = [
            sys.executable,
            os.path.abspath(sandbox.__file__),
            "shim",
            agent,
            str(manifests_dir),
            "--",
            "--print",
            packet,
        ]
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=CONSULT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"consult_process_failed: exit {proc.returncode}: {proc.stderr[:500]}")
        return proc.stdout.strip()


class RelayConsultTransport(ConsultTransport):
    """Default transport for consult attempts.

    Runs via ``run_ai_task`` on route ``consult:zone_a``, recording one ``ai_jobs``
    row and populating ``consult_attempts.ai_job_id``.
    """

    def __init__(
        self,
        *,
        runner: ConsultRunner | None = None,
        mcp_server: ConsultMcpServer | None = None,
    ) -> None:
        self.runner = runner
        self.mcp_server = mcp_server

    def send(self, packet: str, account: TrustedAccount, *, consult_id: str | None = None) -> str:
        # Import lazily to avoid circular imports
        from app.modules.ai.execution import run_ai_task

        binding = ProviderBinding(
            route_class=CONSULT_ROUTE_CLASS,
            provider_id="relay_consult",
            model_id=account.model,
            requires_network=True,
            max_output_tokens=MAX_CONSULT_OUTPUT_CHARS,
            execution_class="external_provider",
            context_window_tokens=32768,
        )
        adapter = RelayConsultAdapter(
            account=account,
            runner=self.runner,
            mcp_server=self.mcp_server,
        )

        outcome = run_ai_task(
            user_prompt=packet,
            task_kind="consult",
            route_class=CONSULT_ROUTE_CLASS,
            max_output_tokens=MAX_CONSULT_OUTPUT_CHARS,
            bindings={CONSULT_ROUTE_CLASS: binding},
            adapters={"relay_consult": adapter},
            metadata={"accounting_basis": "subscription", "consult_id": consult_id},
        )

        if outcome.status != "success" or outcome.response is None or outcome.response.text is None:
            raise RuntimeError(f"consult_execution_failed:{outcome.status}:{outcome.error_type}")

        # Store ai_job_id on the consult_attempts row
        target_consult_id = consult_id
        if target_consult_id is None:
            packet_sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()
            with open_sqlite_connection() as connection:
                row = connection.execute(
                    "SELECT id FROM consult_attempts WHERE packet_sha256 = ? AND state IN ('approved', 'awaiting_approval', 'sent') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (packet_sha,),
                ).fetchone()
                if row is not None:
                    target_consult_id = row[0]

        if target_consult_id is not None:
            with open_sqlite_connection() as connection:
                connection.execute(
                    "UPDATE consult_attempts SET ai_job_id = ? WHERE id = ?",
                    (outcome.ledger_id, target_consult_id),
                )
                connection.commit()

        return outcome.response.text


DEFAULT_CONSULT_TRANSPORT = RelayConsultTransport()
