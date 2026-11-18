"""Host operations request writer and readiness probe for Strata-served models (spec 302)."""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

REQUEST_SCHEMA = "jarvisos.hostops.request.v1"
RECEIPT_SCHEMA = "jarvisos.hostops.receipt.v1"
DEFAULT_STARTUP_TIMEOUT_S = 600.0


def get_spool_path() -> Path | None:
    spool_str = os.getenv("JARVISOS_HOSTOPS_SPOOL")
    if not spool_str:
        return None
    p = Path(spool_str)
    return p if p.is_dir() else None


def write_service_control_request(
    spool: Path,
    unit: str,
    action: str,
    *,
    requester: str = "jarvisos-backend",
    now: datetime | None = None,
) -> str:
    """Write exactly one jarvisos.hostops.request.v1 file into spool/requests."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"Invalid service.control action: {action}")
    request_id = secrets.token_hex(16)
    timestamp = now or datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    request = {
        "schema": REQUEST_SCHEMA,
        "id": request_id,
        "op": "service.control",
        "args": {"unit": unit, "action": action},
        "sha": None,
        "requested_at": ts_str,
        "requester": requester[:120],
    }
    requests_dir = spool / "requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    temp_file = requests_dir / f".{request_id}.tmp"
    content = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
    temp_file.write_bytes(content)
    temp_file.replace(requests_dir / f"{request_id}.json")
    return request_id


def check_receipt(spool: Path, request_id: str) -> tuple[str, str | None] | None:
    """Return (outcome, error) if a receipt exists for request_id, else None."""
    receipt_path = spool / "receipts" / f"{request_id}.json"
    if not receipt_path.is_file():
        return None
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema") == RECEIPT_SCHEMA:
            outcome = data.get("outcome", "unknown")
            error = data.get("error")
            return outcome, error
    except (OSError, ValueError):
        pass
    return None


def probe_strata_readiness(
    host: str,
    port: int,
    served_model: str,
    *,
    client_factory: Any = httpx.Client,
    timeout_s: float = 2.0,
) -> bool:
    """Loopback GET /v1/models probe; True if served_model is present in response data."""
    origin = f"http://{host}:{port}"
    try:
        with client_factory(timeout=timeout_s) as client:
            resp = client.get(f"{origin}/v1/models")
            if resp.status_code != 200:
                return False
            body = resp.json()
            data = body.get("data", [])
            model_ids = {item.get("id") for item in data if isinstance(item, dict)}
            return served_model in model_ids
    except Exception:
        return False


@dataclass
class StrataModelState:
    state: str  # "stopped", "loading", "ready", "failed", "requires host start"
    elapsed_seconds: int = 0
    reason: str | None = None
    last_request_id: str | None = None
    last_action: str | None = None
    last_request_time: float | None = None


class StrataRuntimeController:
    """Tracks state and interacts with host spool and loopback probe for Strata models."""

    def __init__(
        self,
        *,
        probe_fn: Callable[[str, int, str], bool] | None = None,
        clock_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
    ) -> None:
        self._probe_fn = probe_fn or probe_strata_readiness
        self._clock_fn = clock_fn or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._timeout_s = timeout_s
        self._states: dict[str, StrataModelState] = {}

    def get_state(self, model_id: str, host: str, port: int, served_model: str) -> StrataModelState:
        spool = get_spool_path()
        st = self._states.setdefault(model_id, StrataModelState(state="stopped"))
        if spool is None:
            return StrataModelState(state="requires host start", reason="This model requires host start.")

        ready = self._probe_fn(host, port, served_model)
        if ready:
            st.state = "ready"
            st.reason = None
            return st

        if st.last_action == "start" and st.last_request_time is not None:
            elapsed = int(self._clock_fn() - st.last_request_time)
            st.elapsed_seconds = max(0, elapsed)
            if st.last_request_id:
                receipt = check_receipt(spool, st.last_request_id)
                if receipt is not None:
                    outcome, err = receipt
                    if outcome != "ok":
                        st.state = "failed"
                        st.reason = err or f"Host operation failed ({outcome})"
                        return st

            if elapsed > self._timeout_s:
                st.state = "failed"
                st.reason = "Model startup timed out."
                return st

            st.state = "loading"
            st.reason = f"Strata is loading the model ({elapsed}s elapsed)."
            return st

        if st.state != "failed":
            st.state = "stopped"
        return st

    def start(self, model_id: str, unit: str, host: str, port: int, served_model: str) -> StrataModelState:
        spool = get_spool_path()
        st = self._states.setdefault(model_id, StrataModelState(state="stopped"))
        if spool is None:
            st.state = "requires host start"
            st.reason = "This model requires host start."
            return st

        if self._probe_fn(host, port, served_model):
            st.state = "ready"
            st.reason = None
            return st

        req_id = write_service_control_request(spool, unit, "start")
        st.last_request_id = req_id
        st.last_action = "start"
        st.last_request_time = self._clock_fn()
        st.state = "loading"
        st.elapsed_seconds = 0
        st.reason = "Strata is loading the model (0s elapsed)."
        return st

    def stop(
        self,
        model_id: str,
        unit: str,
        host: str,
        port: int,
        served_model: str,
        wait_timeout_s: float = 30.0,
    ) -> StrataModelState:
        spool = get_spool_path()
        st = self._states.setdefault(model_id, StrataModelState(state="stopped"))
        if spool is None:
            st.state = "requires host start"
            return st

        req_id = write_service_control_request(spool, unit, "stop")
        st.last_request_id = req_id
        st.last_action = "stop"
        st.last_request_time = self._clock_fn()

        deadline = self._clock_fn() + wait_timeout_s
        while self._probe_fn(host, port, served_model) and self._clock_fn() < deadline:
            self._sleep_fn(0.1)

        st.state = "stopped"
        st.reason = None
        return st


_STRATA_CONTROLLER: StrataRuntimeController | None = None


def get_strata_controller() -> StrataRuntimeController:
    global _STRATA_CONTROLLER
    if _STRATA_CONTROLLER is None:
        _STRATA_CONTROLLER = StrataRuntimeController()
    return _STRATA_CONTROLLER


def reset_strata_controller(controller: StrataRuntimeController | None = None) -> None:
    global _STRATA_CONTROLLER
    _STRATA_CONTROLLER = controller
