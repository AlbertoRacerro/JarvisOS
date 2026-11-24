"""Host operations request writer and readiness probe for Strata-served models (spec 302)."""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REQUEST_SCHEMA = "jarvisos.hostops.request.v1"
RECEIPT_SCHEMA = "jarvisos.hostops.receipt.v1"
DEFAULT_STARTUP_TIMEOUT_S = 600.0
SYSTEMD_UNIT_RE = re.compile(r"^[a-zA-Z0-9@._-]+\.service$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


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
    if not isinstance(unit, str) or not SYSTEMD_UNIT_RE.fullmatch(unit):
        raise ValueError(f"Invalid unit name: {unit}")
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"Invalid service.control action: {action}")
    request_id = secrets.token_hex(16)
    timestamp = now or datetime.now(UTC)
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
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return None
    receipt_path = spool / "receipts" / f"{request_id}.json"
    if not receipt_path.is_file():
        return None
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and data.get("schema") == RECEIPT_SCHEMA
            and data.get("request_id") == request_id
        ):
            outcome = data.get("outcome", "unknown")
            error = data.get("error")
            return outcome, error
    except (OSError, ValueError):
        pass
    return None


ALLOWED_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_port_silent(host: str, port: int, *, timeout_s: float = 0.5) -> bool:
    """Fail-closed loopback silence check.

    Only definite connection refused on an allowed loopback host is proof of silence.
    Unsupported host, DNS uncertainty, timeout, active connection, and other socket
    errors return False (preventing fallback).
    """
    if host not in ALLOWED_LOOPBACK_HOSTS:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return False
    except ConnectionRefusedError:
        return True
    except OSError as err:
        if err.errno == errno.ECONNREFUSED:
            return True
        return False
    except Exception:
        return False


def probe_strata_readiness(
    host: str,
    port: int,
    served_model: str,
    *,
    client_factory: Any = httpx.Client,
    timeout_s: float = 2.0,
) -> bool:
    """Loopback GET /v1/models probe; True if served_model is present in response data."""
    origin = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
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
        port_silence_probe_fn: Callable[[str, int], bool] | None = None,
    ) -> None:
        self._probe_fn = probe_fn or probe_strata_readiness
        self._clock_fn = clock_fn or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._timeout_s = timeout_s
        self._port_silence_probe_fn = port_silence_probe_fn or is_loopback_port_silent
        self._states: dict[str, StrataModelState] = {}

    def probe(self, host: str, port: int, served_model: str) -> bool:
        return self._probe_fn(host, port, served_model)

    def is_port_silent(self, host: str, port: int) -> bool:
        return self._port_silence_probe_fn(host, port)

    def get_state(self, model_id: str, host: str, port: int, served_model: str) -> StrataModelState:
        st = self._states.setdefault(model_id, StrataModelState(state="stopped"))
        ready = self._probe_fn(host, port, served_model)
        if ready:
            st.state = "ready"
            st.reason = None
            st.last_action = None
            st.last_request_time = None
            st.last_request_id = None
            st.elapsed_seconds = 0
            return st

        spool = get_spool_path()
        if spool is None:
            return StrataModelState(state="requires host start", reason="This model requires host start.")

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
        wait_timeout_s: float = 120.0,
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

        # A loading model does not answer the probe yet still holds the card, so "stopped" needs the
        # host's ok receipt for this stop request as well as a silent port.
        deadline = self._clock_fn() + wait_timeout_s
        while True:
            receipt = check_receipt(spool, req_id)
            if receipt is not None and receipt[0] != "ok":
                st.state = "failed"
                st.reason = receipt[1] or f"Host operation failed ({receipt[0]})"
                return st
            if receipt is not None and not self._probe_fn(host, port, served_model):
                break
            if self._clock_fn() >= deadline:
                st.state = "failed"
                st.reason = "The model did not stop in time."
                return st
            self._sleep_fn(0.5)

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
