#!/usr/bin/env python3
"""Repeatable switch-latency breakdown benchmark for Strata-served models (spec 384).

Standard-library only, loopback-only (refuses port 8000, respects vault_guard),
offline-testable script measuring both directions (default -> specialist, specialist -> default)
across N runs on an idle host. Breaks down each switch into explicit phases:
- stop_old_s
- start_dispatch_s
- model_ready_s
- first_token_s
- optional Strata log/API-monitor sub-phases (weight_read_s, vram_upload_s, engine_init_s)
- host RAM snapshot and optional page-cache residency (mincore)
- optional decode-speed check (decode_tok_s) before and after

Emits machine-readable result.json plus a plain-Italian summary table.
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import mmap
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path for vault_guard and backend for app
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from scripts import vault_guard  # noqa: E402

ALLOWED_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "ip6-localhost", "ip6-loopback"})


def is_loopback_host(hostname: str) -> bool:
    """Return True if hostname is a loopback address."""
    clean = hostname.strip().lower()
    if clean in ALLOWED_LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(clean)
        return ip.is_loopback
    except ValueError:
        return False


def validate_loopback_url(url_str: str) -> urllib.parse.ParseResult:
    """Validate loopback URL and strictly refuse port 8000."""
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}'; expected http or https")
    hostname = (parsed.hostname or "").lower()
    if not hostname or not is_loopback_host(hostname):
        raise ValueError(f"Host '{hostname}' is not a loopback interface; only loopback is permitted")
    if parsed.port == 8000:
        raise ValueError("Port 8000 is refused; proof backend must run on an isolated proof port")
    return parsed


# --- Host RAM Snapshot ---

@dataclass
class HostRamSnapshot:
    mem_total_kb: int = 0
    mem_available_kb: int = 0
    mem_free_kb: int = 0
    mem_total_gib: float = 0.0
    mem_available_gib: float = 0.0


def get_host_ram_snapshot(meminfo_path: str | Path = "/proc/meminfo") -> HostRamSnapshot:
    """Read host RAM metrics from /proc/meminfo."""
    p = Path(meminfo_path)
    if not p.is_file():
        return HostRamSnapshot()
    total_kb = 0
    available_kb = 0
    free_kb = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available_kb = int(line.split()[1])
            elif line.startswith("MemFree:"):
                free_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    gib_factor = 1024.0 * 1024.0
    return HostRamSnapshot(
        mem_total_kb=total_kb,
        mem_available_kb=available_kb,
        mem_free_kb=free_kb,
        mem_total_gib=round(total_kb / gib_factor, 2) if total_kb else 0.0,
        mem_available_gib=round(available_kb / gib_factor, 2) if available_kb else 0.0,
    )


# --- Page Cache Residency (mincore) ---

class _PyBuffer(ctypes.Structure):
    _fields_ = [
        ("buf", ctypes.c_void_p),
        ("obj", ctypes.py_object),
        ("len", ctypes.c_ssize_t),
        ("itemsize", ctypes.c_ssize_t),
        ("readonly", ctypes.c_int),
        ("ndim", ctypes.c_int),
        ("format", ctypes.c_char_p),
        ("shape", ctypes.POINTER(ctypes.c_ssize_t)),
        ("strides", ctypes.POINTER(ctypes.c_ssize_t)),
        ("suboffsets", ctypes.POINTER(ctypes.c_ssize_t)),
        ("internal", ctypes.c_void_p),
    ]


def get_file_page_cache_residency(file_path: str | Path) -> dict[str, Any] | None:
    """Determine page-cache residency of a file via libc mincore where available."""
    p = Path(file_path)
    if not p.is_file():
        return None
    try:
        libc = ctypes.CDLL(None)
        if not hasattr(libc, "mincore") or not hasattr(os, "sysconf"):
            return None
        page_size = os.sysconf("SC_PAGESIZE")
        size = p.stat().st_size
        if size == 0:
            return {"file_size_bytes": 0, "resident_bytes": 0, "residency_ratio": 1.0}

        n_pages = (size + page_size - 1) // page_size
        vec = (ctypes.c_ubyte * n_pages)()

        with open(p, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                view = _PyBuffer()
                ctypes.pythonapi.PyObject_GetBuffer(ctypes.py_object(mm), ctypes.byref(view), 0)
                try:
                    ret = libc.mincore(ctypes.c_void_p(view.buf), ctypes.c_size_t(size), vec)
                    if ret != 0:
                        return None
                    resident_pages = sum(1 for b in vec if (b & 1))
                    resident_bytes = min(size, resident_pages * page_size)
                    ratio = resident_pages / n_pages if n_pages else 0.0
                    return {
                        "file_size_bytes": size,
                        "resident_bytes": resident_bytes,
                        "resident_pages": resident_pages,
                        "total_pages": n_pages,
                        "residency_ratio": round(ratio, 4),
                    }
                finally:
                    ctypes.pythonapi.PyBuffer_Release(ctypes.byref(view))
            finally:
                mm.close()
    except (OSError, ValueError, TypeError, AttributeError):
        return None


# --- Strata Log / API Monitor Parser ---

_WEIGHT_READ_RE = re.compile(
    r"(?:weights?\s+read(?:\s+in)?|read(?:ing)?\s+weights?|weight_read(?:_s)?)(?:[:\s=]+|\s+in\s+)([0-9.]+)\s*(s|ms|seconds|milliseconds)?",
    re.IGNORECASE,
)
_VRAM_UPLOAD_RE = re.compile(
    r"(?:vram\s+upload|upload(?:ing)?\s+(?:to\s+)?vram|gpu\s+upload|vram_upload(?:_s)?)(?:[:\s=]+|\s+in\s+)([0-9.]+)\s*(s|ms|seconds|milliseconds)?",
    re.IGNORECASE,
)
_ENGINE_INIT_RE = re.compile(
    r"(?:engine\s+init(?:ializ(?:ation|ed))?|engine_init(?:_s)?)(?:[:\s=]+|\s+in\s+)([0-9.]+)\s*(s|ms|seconds|milliseconds)?",
    re.IGNORECASE,
)


def _to_seconds(value_str: str, unit: str | None) -> float:
    val = float(value_str)
    if unit and unit.lower().startswith("m"):
        return round(val / 1000.0, 3)
    return round(val, 3)


def parse_strata_subphases(log_data: str | list[str] | dict[str, Any]) -> dict[str, float]:
    """Extract weight_read_s, vram_upload_s, engine_init_s from log text or api-monitor dict."""
    res: dict[str, float] = {}
    if isinstance(log_data, dict):
        if "weight_read_s" in log_data:
            res["weight_read_s"] = float(log_data["weight_read_s"])
        elif "weights_read_ms" in log_data:
            res["weight_read_s"] = round(float(log_data["weights_read_ms"]) / 1000.0, 3)

        if "vram_upload_s" in log_data:
            res["vram_upload_s"] = float(log_data["vram_upload_s"])
        elif "vram_upload_ms" in log_data:
            res["vram_upload_s"] = round(float(log_data["vram_upload_ms"]) / 1000.0, 3)

        if "engine_init_s" in log_data:
            res["engine_init_s"] = float(log_data["engine_init_s"])
        elif "engine_init_ms" in log_data:
            res["engine_init_s"] = round(float(log_data["engine_init_ms"]) / 1000.0, 3)
        return res

    text = "\n".join(log_data) if isinstance(log_data, list) else str(log_data)
    for line in text.splitlines():
        if "weight_read_s" not in res:
            m = _WEIGHT_READ_RE.search(line)
            if m:
                res["weight_read_s"] = _to_seconds(m.group(1), m.group(2))
        if "vram_upload_s" not in res:
            m = _VRAM_UPLOAD_RE.search(line)
            if m:
                res["vram_upload_s"] = _to_seconds(m.group(1), m.group(2))
        if "engine_init_s" not in res:
            m = _ENGINE_INIT_RE.search(line)
            if m:
                res["engine_init_s"] = _to_seconds(m.group(1), m.group(2))
    return res


# --- HTTP Loopback Helpers ---

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise urllib.error.HTTPError(req.full_url, code, f"Redirects refused ({msg})", headers, fp)


_LOOPBACK_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
) -> tuple[int, Any]:
    """Single fail-closed loopback-only HTTP request chokepoint for the benchmark."""
    validate_loopback_url(url)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": "JarvisOS-StrataBench/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _LOOPBACK_OPENER.open(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
        parsed = json.loads(body) if body.strip() else {}
        return resp.status, parsed


def is_model_loaded_response(models_data: Any, target_model: str) -> bool:
    """Return True if /v1/models payload reports target_model as loaded (not unloaded)."""
    if not isinstance(models_data, dict):
        return False
    for item in models_data.get("data", []):
        if not isinstance(item, dict) or item.get("id") != target_model:
            continue
        status = item.get("status")
        if isinstance(status, dict) and status.get("value") is not None:
            return status.get("value") == "loaded"
        return True
    return False


def http_get_json(url: str, *, timeout_s: float = 5.0) -> Any:
    _, body = _http_request(url, method="GET", timeout_s=timeout_s)
    return body


def http_post_json(url: str, payload: dict[str, Any], *, timeout_s: float = 10.0) -> tuple[int, Any]:
    return _http_request(url, method="POST", payload=payload, timeout_s=timeout_s)


def post_load_with_deadline(
    url: str,
    payload: dict[str, Any],
    deadline_s: float = 180.0,
    http_poster: Callable[[str, dict[str, Any], float], tuple[int, Any]] | None = None,
) -> tuple[bool, int, Any]:
    """Execute POST /load in a daemon thread with an absolute wall-clock deadline.

    Returns (completed_in_time, status_code, body).
    On expiry, returns (False, 0, None).
    """
    result: list[tuple[int, Any]] = []
    exc_list: list[Exception] = []
    poster = http_poster or (lambda u, p, t: _http_request(u, method="POST", payload=p, timeout_s=t))

    def _worker() -> None:
        try:
            status, body = poster(url, payload, deadline_s)
            result.append((status, body))
        except Exception as exc:
            exc_list.append(exc)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=deadline_s)

    if th.is_alive():
        return False, 0, None
    if exc_list or not result:
        return True, 0, None
    status, body = result[0]
    return True, status, body


# --- Idle Check (/slots) ---

def check_host_idle(
    slots_url: str,
    *,
    timeout_s: float = 30.0,
    poll_s: float = 0.25,
    getter: Callable[[str, float], Any] | None = None,
    clock_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Check loopback /slots to verify all slots are idle (not processing). Waits until idle or timeout."""
    slots_endpoint = slots_url.rstrip("/") + "/slots"
    fetch = (lambda u, t: http_get_json(u, timeout_s=t)) if getter is None else getter
    deadline = clock_fn() + timeout_s
    while True:
        try:
            slots = fetch(slots_endpoint, 2.0)
            if isinstance(slots, list) and slots:
                busy = any(
                    isinstance(s, dict) and s.get("is_processing") is True
                    for s in slots
                )
                if not busy:
                    return True
            else:
                # If /slots returned empty list or non-list, consider idle
                return True
        except (OSError, ValueError, urllib.error.URLError):
            # If /slots not available or network error, assume idle (or offline test)
            return True

        if clock_fn() >= deadline:
            return False
        sleep_fn(poll_s)


# --- Decode Speed Probe ---

def measure_decode_speed(
    base_url: str,
    model_id: str,
    *,
    prompt: str = "Explain in one sentence what gravity is.",
    max_tokens: int = 16,
    timeout_s: float = 30.0,
    poster: Callable[[str, dict[str, Any], float], tuple[int, Any]] | None = None,
    clock_fn: Callable[[], float] = time.perf_counter,
) -> float | None:
    """Measure decode speed (tokens/sec) with a short completion probe."""
    endpoint = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    do_post = (lambda u, p, t: http_post_json(u, p, timeout_s=t)) if poster is None else poster
    try:
        t0 = clock_fn()
        status, body = do_post(endpoint, payload, timeout_s)
        duration = clock_fn() - t0
        if status != 200 or duration <= 0:
            return None
        tokens = 0
        if isinstance(body, dict):
            usage = body.get("usage", {})
            tokens = usage.get("completion_tokens", 0)
            if not tokens:
                choices = body.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message", {}).get("content", "")
                    tokens = max(1, len(msg.split()))
        if tokens > 0:
            return round(tokens / duration, 2)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return None


# --- Switch Phase Timing ---

@dataclass
class SwitchPhaseTimings:
    direction: str  # "default_to_specialist" or "specialist_to_default"
    run_index: int
    stop_old_s: float = 0.0
    start_dispatch_s: float = 0.0
    model_ready_s: float = 0.0
    first_token_s: float = 0.0
    total_switch_s: float = 0.0
    subphases: dict[str, float] = field(default_factory=dict)
    switch_mode: str = "hostops"
    unload_released_ram: bool | None = None
    unload_ram_delta_bytes: int | None = None
    unload_ram_delta_gib: float | None = None
    load_timed_out: bool | None = None
    skipped: bool = False
    skip_reason: str | None = None


def measure_single_switch(
    source_model: str,
    target_model: str,
    direction: str,
    run_index: int,
    *,
    base_url: str,
    stop_fn: Callable[[], float],
    start_fn: Callable[[], tuple[float, str]],  # returns (start_dispatch_s, mode)
    wait_ready_fn: Callable[[], float],  # returns model_ready_s
    first_token_fn: Callable[[], float],  # returns first_token_s
    subphase_fn: Callable[[], dict[str, float]] | None = None,
    unload_released_ram: bool | None = None,
    unload_ram_delta_bytes: int | None = None,
    unload_ram_delta_gib: float | None = None,
    load_timed_out: bool | None = None,
) -> SwitchPhaseTimings:
    """Measure a single switch breakdown in one direction."""
    stop_old_s = stop_fn()
    start_dispatch_s, mode = start_fn()
    model_ready_s = wait_ready_fn()
    first_token_s = first_token_fn()
    total_s = round(stop_old_s + start_dispatch_s + model_ready_s + first_token_s, 3)
    subphases = subphase_fn() if subphase_fn else {}

    return SwitchPhaseTimings(
        direction=direction,
        run_index=run_index,
        stop_old_s=round(stop_old_s, 3),
        start_dispatch_s=round(start_dispatch_s, 3),
        model_ready_s=round(model_ready_s, 3),
        first_token_s=round(first_token_s, 3),
        total_switch_s=total_s,
        subphases=subphases,
        switch_mode=mode,
        unload_released_ram=unload_released_ram,
        unload_ram_delta_bytes=unload_ram_delta_bytes,
        unload_ram_delta_gib=unload_ram_delta_gib,
        load_timed_out=load_timed_out,
    )


def summarize_timings(timings: list[SwitchPhaseTimings]) -> dict[str, Any]:
    """Compute mean metrics for a list of runs."""
    if not timings:
        return {}
    completed = [t for t in timings if not t.skipped]
    n_completed = len(completed)
    total_runs = len(timings)
    if n_completed == 0:
        return {
            "runs": total_runs,
            "completed_runs": 0,
            "skipped_runs": total_runs,
            "stop_old_s_avg": 0.0,
            "start_dispatch_s_avg": 0.0,
            "model_ready_s_avg": 0.0,
            "first_token_s_avg": 0.0,
            "total_switch_s_avg": 0.0,
            "switch_mode": "skipped",
        }
    res = {
        "runs": total_runs,
        "completed_runs": n_completed,
        "skipped_runs": total_runs - n_completed,
        "stop_old_s_avg": round(sum(t.stop_old_s for t in completed) / n_completed, 3),
        "start_dispatch_s_avg": round(sum(t.start_dispatch_s for t in completed) / n_completed, 3),
        "model_ready_s_avg": round(sum(t.model_ready_s for t in completed) / n_completed, 3),
        "first_token_s_avg": round(sum(t.first_token_s for t in completed) / n_completed, 3),
        "total_switch_s_avg": round(sum(t.total_switch_s for t in completed) / n_completed, 3),
        "switch_mode": completed[0].switch_mode if completed else "hostops",
    }
    fast_released = [t.unload_released_ram for t in completed if t.unload_released_ram is not None]
    if fast_released:
        res["all_unloads_released_ram"] = all(fast_released)
    return res


def format_italian_summary(data: dict[str, Any]) -> str:
    """Produce plain-Italian formatted summary table for console and proof reporting."""
    runs = data.get("runs", 3)
    d2s = data.get("default_to_specialist_summary", {})
    s2d = data.get("specialist_to_default_summary", {})
    ram = data.get("host_ram_snapshot", {})
    decode = data.get("decode_speed_check", {})

    lines = [
        "=" * 80,
        f"Riepilogo Switch Strata ({runs} corse per direzione)",
        "=" * 80,
        f"{'Direzione':<24} {'stop_old_s':<11} {'dispatch_s':<11} {'ready_s':<11} {'1st_tok_s':<11} {'Totale_s':<10}",
        "-" * 80,
        (
            f"{'default -> specialist':<24} "
            f"{d2s.get('stop_old_s_avg', 0.0):<11.3f} "
            f"{d2s.get('start_dispatch_s_avg', 0.0):<11.3f} "
            f"{d2s.get('model_ready_s_avg', 0.0):<11.3f} "
            f"{d2s.get('first_token_s_avg', 0.0):<11.3f} "
            f"{d2s.get('total_switch_s_avg', 0.0):<10.3f}"
        ),
        (
            f"{'specialist -> default':<24} "
            f"{s2d.get('stop_old_s_avg', 0.0):<11.3f} "
            f"{s2d.get('start_dispatch_s_avg', 0.0):<11.3f} "
            f"{s2d.get('model_ready_s_avg', 0.0):<11.3f} "
            f"{s2d.get('first_token_s_avg', 0.0):<11.3f} "
            f"{s2d.get('total_switch_s_avg', 0.0):<10.3f}"
        ),
        "-" * 80,
        f"RAM Host: MemTotal = {ram.get('mem_total_gib', 0.0):.2f} GiB, MemAvailable = {ram.get('mem_available_gib', 0.0):.2f} GiB",
    ]

    if decode:
        before = decode.get("before_tok_s")
        after = decode.get("after_tok_s")
        b_str = f"{before:.2f}" if before is not None else "n/d"
        a_str = f"{after:.2f}" if after is not None else "n/d"
        lines.append(f"Velocità di decodifica modello predefinito: prima = {b_str} tok/s, dopo = {a_str} tok/s")

    cache_info = data.get("page_cache_residency")
    if cache_info:
        for model_k, info in cache_info.items():
            if isinstance(info, dict) and "residency_ratio" in info:
                pct = info["residency_ratio"] * 100.0
                lines.append(f"Residenza cache pagina ({model_k}): {pct:.1f}%")

    subphases = data.get("subphases")
    if subphases:
        parts = [f"{k} = {v:.3f}s" for k, v in subphases.items()]
        lines.append(f"Sotto-fasi motore Strata: {', '.join(parts)}")

    skipped = data.get("skipped_runs_count", 0)
    if skipped:
        lines.append(f"Avviso: {skipped} corse saltate per superamento tempo massimo (--max-wall-s)")

    final_st = data.get("final_state")
    if isinstance(final_st, dict):
        sp_st = "fermato" if final_st.get("specialist_unit_stopped") else "non fermato"
        df_st = "caricato" if final_st.get("default_model_loaded") else "non caricato"
        lines.append(f"Stato finale ripristinato: specialista {sp_st}, predefinito {df_st}")

    lines.append("=" * 80)
    return "\n".join(lines)


# --- Benchmark Suite Runner ---

def _hostops_control_wait(
    spool: Path,
    unit: str,
    action: str,
    *,
    timeout_s: float = 120.0,
    poll_s: float = 0.1,
) -> tuple[bool, float]:
    """Dispatch a hostops service.control request and wait for its ok receipt."""
    from app.modules.local_ai.runtime.host_start import (
        check_receipt,
        write_service_control_request,
    )

    t0 = time.monotonic()
    req_id = write_service_control_request(spool, unit, action, requester="strata-switch-bench")
    deadline = t0 + timeout_s
    while time.monotonic() < deadline:
        receipt = check_receipt(spool, req_id)
        if receipt is not None:
            outcome, _err = receipt
            return outcome == "ok", round(time.monotonic() - t0, 3)
        time.sleep(poll_s)
    return False, round(time.monotonic() - t0, 3)


def _wait_port_silent(host: str, port: int, *, timeout_s: float = 60.0, poll_s: float = 0.1) -> bool:
    from app.modules.local_ai.runtime.host_start import is_loopback_port_silent

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_loopback_port_silent(host, port):
            return True
        time.sleep(poll_s)
    return False


def _probe_api_monitor(url: str) -> dict[str, Any] | None:
    try:
        status, body = _http_request(f"{url.rstrip('/')}/api-monitor", method="GET", timeout_s=2.0)
        if status == 200 and isinstance(body, dict):
            return body
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


def run_benchmark(
    *,
    base_url: str = "http://127.0.0.1:18091",
    default_url: str | None = None,
    specialist_url: str | None = None,
    default_model: str = "inkling-2-mini-sq-10",
    specialist_model: str = "sage-a1.5",
    default_unit: str = "jarvisos-strata-inkling.service",
    specialist_unit: str = "jarvisos-strata-sage.service",
    spool_path: str | Path | None = None,
    mode: str = "auto",
    prewarm: bool = False,
    runs: int = 3,
    strata_log_path: str | Path | None = None,
    default_model_path: str | Path | None = None,
    specialist_model_path: str | Path | None = None,
    check_decode: bool = True,
    min_ram_release_ratio: float = 0.5,
    load_deadline_s: float = 180.0,
    max_wall_s: float = 3600.0,
    cleanup_restore_timeout_s: float = 360.0,
    output_path: str | Path | None = None,
    meminfo_path: str | Path = "/proc/meminfo",
    # Injectable hooks for testing / offline execution
    slots_checker: Callable[[], bool] | None = None,
    switch_runner: Callable[[str, str, str, int], SwitchPhaseTimings] | None = None,
    decode_checker: Callable[[str], float | None] | None = None,
    hostops_control_fn: Callable[[Path, str, str, float], tuple[bool, float]] | None = None,
    port_silent_fn: Callable[[str, int], bool] | None = None,
    models_probe_fn: Callable[[str, str], bool] | None = None,
    unload_poster_fn: Callable[[str, dict[str, Any], float], tuple[int, Any]] | None = None,
    load_poster_fn: Callable[[str, dict[str, Any], float], tuple[int, Any]] | None = None,
    first_token_poster_fn: Callable[[str, dict[str, Any], float], tuple[int, Any]] | None = None,
) -> dict[str, Any]:
    """Execute full 2-directional switch latency benchmark across runs."""
    d_url = (default_url or base_url).rstrip("/")
    s_url = (specialist_url or base_url).rstrip("/")
    d_parsed = validate_loopback_url(d_url)
    s_parsed = validate_loopback_url(s_url)

    bench_start_time = time.monotonic()
    bench_deadline = bench_start_time + max_wall_s

    resolved_spool: Path | None = None
    if spool_path:
        resolved_spool = Path(spool_path)
    elif os.getenv("JARVISOS_HOSTOPS_SPOOL"):
        resolved_spool = Path(os.environ["JARVISOS_HOSTOPS_SPOOL"])

    def _do_hostops_control(unit: str, action: str, timeout: float = 120.0) -> tuple[bool, float]:
        if hostops_control_fn:
            return hostops_control_fn(resolved_spool or Path("/fake/spool"), unit, action, timeout)
        if resolved_spool is None or not resolved_spool.is_dir():
            if switch_runner is not None:
                return True, 0.0
            raise RuntimeError(f"Hostops spool is required for hostops {action} on {unit}")
        return _hostops_control_wait(resolved_spool, unit, action, timeout_s=timeout)

    def _do_wait_silent(host: str, port: int, timeout: float = 60.0) -> bool:
        if port_silent_fn:
            return port_silent_fn(host, port)
        if switch_runner is not None and resolved_spool is None:
            return True
        return _wait_port_silent(host, port, timeout_s=timeout)

    def _do_probe_loaded(url: str, model: str) -> bool:
        if models_probe_fn:
            return models_probe_fn(url, model)
        if switch_runner is not None and resolved_spool is None:
            return True
        try:
            m_data = http_get_json(f"{url}/v1/models", timeout_s=2.0)
            return is_model_loaded_response(m_data, model)
        except (OSError, ValueError, urllib.error.URLError):
            return False

    final_state: dict[str, Any] = {
        "specialist_unit_stopped": False,
        "default_model_loaded": False,
        "cleanup_attempted": False,
    }

    d2s_runs: list[SwitchPhaseTimings] = []
    s2d_runs: list[SwitchPhaseTimings] = []
    prewarm_results: list[dict[str, Any]] = []
    decode_check: dict[str, float | None] = {}
    cache_residency: dict[str, Any] = {}
    api_monitor_probe: dict[str, Any] = {}
    ram_snap = HostRamSnapshot()
    subphases: dict[str, float] = {}

    result_data: dict[str, Any] = {
        "runs": runs,
        "mode": mode,
        "prewarm": prewarm,
        "prewarm_results": prewarm_results,
        "api_monitor_probe": api_monitor_probe,
        "default_model": default_model,
        "specialist_model": specialist_model,
        "host_ram_snapshot": asdict(ram_snap),
        "decode_speed_check": decode_check,
        "page_cache_residency": cache_residency,
        "subphases": subphases,
        "default_to_specialist_runs": [],
        "specialist_to_default_runs": [],
        "default_to_specialist_summary": {},
        "specialist_to_default_summary": {},
        "max_wall_s": max_wall_s,
        "load_deadline_s": load_deadline_s,
        "min_ram_release_ratio": min_ram_release_ratio,
        "final_state": final_state,
    }

    touched_services = False
    try:
        # 1. Host Idle Check
        is_idle = slots_checker() if slots_checker else check_host_idle(d_url)
        if not is_idle:
            raise RuntimeError("Host is not idle: active processing found in /slots")

        # 2. Host RAM snapshot
        ram_snap = get_host_ram_snapshot(meminfo_path)
        result_data["host_ram_snapshot"] = asdict(ram_snap)

        # 3. Initial decode speed check
        if check_decode:
            meas_fn = decode_checker if decode_checker else (lambda m: measure_decode_speed(d_url, m))
            decode_check["before_tok_s"] = meas_fn(default_model)

        # 4. Page-cache residency checks where file paths provided
        if default_model_path:
            res = get_file_page_cache_residency(default_model_path)
            if res:
                cache_residency["default_model"] = res
        if specialist_model_path:
            res = get_file_page_cache_residency(specialist_model_path)
            if res:
                cache_residency["specialist_model"] = res

        if switch_runner is None:
            mon = _probe_api_monitor(d_url)
            api_monitor_probe["default_api_monitor_available"] = mon is not None

        # 5. Measure runs in both directions
        def _real_switch(source: str, target: str, direction: str, run_idx: int) -> SwitchPhaseTimings:
            if direction == "default_to_specialist":
                src_url, src_parsed, src_unit = d_url, d_parsed, default_unit
                dst_url, dst_parsed, dst_unit, dst_path = s_url, s_parsed, specialist_unit, specialist_model_path
                src_path = default_model_path
            else:
                src_url, src_parsed, src_unit = s_url, s_parsed, specialist_unit
                dst_url, dst_parsed, dst_unit, dst_path = d_url, d_parsed, default_unit, default_model_path
                src_path = specialist_model_path

            if prewarm and dst_path:
                from app.modules.local_ai.runtime.page_cache import (
                    can_prewarm,
                    prewarm_model_file,
                )

                allowed, reason = can_prewarm(dst_path, meminfo_path=meminfo_path)
                did_prewarm = prewarm_model_file(dst_path, meminfo_path=meminfo_path) if allowed else False
                prewarm_results.append({
                    "direction": direction,
                    "run_index": run_idx,
                    "path": str(dst_path),
                    "allowed": allowed,
                    "reason": reason,
                    "prewarmed": did_prewarm,
                })

            # Phase 1: Stop / Unload source model
            used_mode = "hostops"
            t_stop = time.monotonic()
            unloaded_fast = False
            unload_released_ram: bool | None = None
            delta_bytes: int | None = None
            delta_gib: float | None = None

            if mode in ("auto", "fast"):
                ram_before = get_host_ram_snapshot(meminfo_path)
                try:
                    do_unload = unload_poster_fn or (lambda u, p, t: _http_request(u, method="POST", payload=p, timeout_s=t))
                    status, _ = do_unload(
                        f"{src_url}/unload",
                        {"model": source},
                        30.0,
                    )
                    if status in (200, 204):
                        deadline_u = time.monotonic() + 30.0
                        while time.monotonic() < deadline_u:
                            if not _do_probe_loaded(src_url, source):
                                unloaded_fast = True
                                used_mode = "loopback_unload"
                                break
                            time.sleep(0.01)
                except (OSError, ValueError, urllib.error.URLError):
                    pass

                if unloaded_fast:
                    ram_after = get_host_ram_snapshot(meminfo_path)
                    delta_kb = ram_after.mem_available_kb - ram_before.mem_available_kb
                    delta_bytes = delta_kb * 1024
                    delta_gib = round(delta_kb / (1024.0 * 1024.0), 3)

                    model_size_bytes = 0
                    if src_path and Path(src_path).is_file():
                        try:
                            model_size_bytes = Path(src_path).stat().st_size
                        except OSError:
                            pass
                    if model_size_bytes == 0:
                        try:
                            from app.modules.local_ai.runtime.model_registry import read_registry

                            entries, _ = read_registry()
                            entry = next((e for e in entries if e.id == source), None)
                            if entry and entry.model_path and Path(entry.model_path).is_file():
                                model_size_bytes = Path(entry.model_path).stat().st_size
                        except Exception:
                            pass

                    required_bytes = int(model_size_bytes * min_ram_release_ratio) if model_size_bytes > 0 else 0
                    if (model_size_bytes > 0 and delta_bytes < required_bytes) or (model_size_bytes == 0 and delta_bytes <= 0):
                        # Memory not released! Fall back to hostops stop below for this switch
                        unload_released_ram = False
                        unloaded_fast = False
                        used_mode = "hostops"
                    else:
                        unload_released_ram = True
                else:
                    unload_released_ram = False

            if not unloaded_fast:
                ok_stop, _ = _do_hostops_control(src_unit, "stop", timeout=120.0)
                if not ok_stop:
                    raise RuntimeError(f"hostops stop failed for {src_unit}")
                _do_wait_silent(src_parsed.hostname or "127.0.0.1", src_parsed.port or 8080, timeout=60.0)
                used_mode = "hostops"
            stop_old_s = round(time.monotonic() - t_stop, 3)

            # Phase 2 + 3: Start / Load target model
            t_start = time.monotonic()
            loaded_fast = False
            load_timed_out = False

            if mode in ("auto", "fast"):
                completed_in_time, status, _ = post_load_with_deadline(
                    f"{dst_url}/load",
                    payload={"model": target},
                    deadline_s=load_deadline_s,
                    http_poster=load_poster_fn,
                )
                if not completed_in_time:
                    load_timed_out = True
                elif status in (200, 204):
                    loaded_fast = True
                    used_mode = "loopback_unload_load" if unloaded_fast else "hostops_stop_loopback_load"

            if load_timed_out:
                # Expiry! Fall back to hostops stop+start of target unit
                _do_hostops_control(dst_unit, "stop", timeout=120.0)
                _do_wait_silent(dst_parsed.hostname or "127.0.0.1", dst_parsed.port or 8080, timeout=60.0)
                ok_start, start_dispatch_s = _do_hostops_control(dst_unit, "start", timeout=120.0)
                if not ok_start:
                    raise RuntimeError(f"hostops start failed for {dst_unit} after load timeout")
                used_mode = "hostops"
                t_ready = time.monotonic()
                deadline_r = t_ready + 360.0
                ready_ok = False
                while time.monotonic() < deadline_r:
                    if _do_probe_loaded(dst_url, target):
                        ready_ok = True
                        break
                    time.sleep(0.25)
                if not ready_ok:
                    raise RuntimeError(f"Timed out waiting for {target} ready at {dst_url} after load timeout")
                model_ready_s = round(time.monotonic() - t_ready, 3)
            elif loaded_fast:
                start_dispatch_s = 0.0
                model_ready_s = round(time.monotonic() - t_start, 3)
                t_confirm = time.monotonic()
                deadline_c = t_confirm + 60.0
                while time.monotonic() < deadline_c:
                    if _do_probe_loaded(dst_url, target):
                        break
                    time.sleep(0.1)
                model_ready_s = round(model_ready_s + (time.monotonic() - t_confirm), 3)
            else:
                ok_start, start_dispatch_s = _do_hostops_control(dst_unit, "start", timeout=120.0)
                if not ok_start:
                    raise RuntimeError(f"hostops start failed for {dst_unit}")
                if unloaded_fast:
                    used_mode = "loopback_unload_hostops_start"
                t_ready = time.monotonic()
                deadline_r = t_ready + 360.0
                ready_ok = False
                while time.monotonic() < deadline_r:
                    if _do_probe_loaded(dst_url, target):
                        ready_ok = True
                        break
                    time.sleep(0.25)
                if not ready_ok:
                    raise RuntimeError(f"Timed out waiting for {target} ready at {dst_url}")
                model_ready_s = round(time.monotonic() - t_ready, 3)

            # Phase 4: First token probe
            t_tok = time.monotonic()
            do_first_tok = first_token_poster_fn or (
                lambda u, p, t: http_post_json(u, p, timeout_s=t)
            )
            try:
                do_first_tok(
                    f"{dst_url}/v1/chat/completions",
                    {"model": target, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "temperature": 0.0},
                    60.0,
                )
            except Exception:
                pass
            first_token_s = round(time.monotonic() - t_tok, 3)

            return SwitchPhaseTimings(
                direction=direction,
                run_index=run_idx,
                stop_old_s=stop_old_s,
                start_dispatch_s=start_dispatch_s,
                model_ready_s=model_ready_s,
                first_token_s=first_token_s,
                total_switch_s=round(stop_old_s + start_dispatch_s + model_ready_s + first_token_s, 3),
                subphases={},
                switch_mode=used_mode,
                unload_released_ram=unload_released_ram,
                unload_ram_delta_bytes=delta_bytes,
                unload_ram_delta_gib=delta_gib,
                load_timed_out=load_timed_out,
            )

        do_switch = switch_runner or _real_switch
        touched_services = True

        for i in range(1, runs + 1):
            # Direction 1: default -> specialist
            if time.monotonic() >= bench_deadline:
                d2s_runs.append(SwitchPhaseTimings(
                    direction="default_to_specialist",
                    run_index=i,
                    skipped=True,
                    skip_reason=f"max_wall_s ({max_wall_s}s) exceeded",
                ))
            else:
                d2s = do_switch(default_model, specialist_model, "default_to_specialist", i)
                d2s_runs.append(d2s)

            # Direction 2: specialist -> default
            if time.monotonic() >= bench_deadline:
                s2d_runs.append(SwitchPhaseTimings(
                    direction="specialist_to_default",
                    run_index=i,
                    skipped=True,
                    skip_reason=f"max_wall_s ({max_wall_s}s) exceeded",
                ))
            else:
                s2d = do_switch(specialist_model, default_model, "specialist_to_default", i)
                s2d_runs.append(s2d)

        # 6. Final decode speed check
        if check_decode:
            meas_fn = decode_checker if decode_checker else (lambda m: measure_decode_speed(d_url, m))
            decode_check["after_tok_s"] = meas_fn(default_model)

        # 7. Subphases parsing
        if strata_log_path and Path(strata_log_path).is_file():
            subphases = parse_strata_subphases(Path(strata_log_path).read_text(encoding="utf-8"))
            result_data["subphases"] = subphases

    finally:
        result_data["default_to_specialist_runs"] = [asdict(t) for t in d2s_runs]
        result_data["specialist_to_default_runs"] = [asdict(t) for t in s2d_runs]
        result_data["default_to_specialist_summary"] = summarize_timings(d2s_runs)
        result_data["specialist_to_default_summary"] = summarize_timings(s2d_runs)
        result_data["skipped_runs_count"] = sum(1 for t in d2s_runs + s2d_runs if t.skipped)
        result_data["overall_duration_s"] = round(time.monotonic() - bench_start_time, 3)

        if touched_services:
            final_state["cleanup_attempted"] = True
            try:
                # 1. hostops stop specialist
                try:
                    _do_hostops_control(specialist_unit, "stop", timeout=120.0)
                    _do_wait_silent(s_parsed.hostname or "127.0.0.1", s_parsed.port or 8080, timeout=60.0)
                    final_state["specialist_unit_stopped"] = True
                except Exception as e_sp:
                    final_state["specialist_stop_error"] = str(e_sp)

                # 2. Check if default is loaded
                default_loaded = _do_probe_loaded(d_url, default_model)
                if not default_loaded:
                    try:
                        # hostops stop default
                        _do_hostops_control(default_unit, "stop", timeout=120.0)
                        _do_wait_silent(d_parsed.hostname or "127.0.0.1", d_parsed.port or 8080, timeout=60.0)
                        # hostops start default
                        _do_hostops_control(default_unit, "start", timeout=120.0)
                        # wait for /v1/models loaded up to cleanup_restore_timeout_s (default 360 s)
                        deadline_restore = time.monotonic() + cleanup_restore_timeout_s
                        poll_clean_s = 0.05 if cleanup_restore_timeout_s < 5.0 else 0.5
                        while time.monotonic() < deadline_restore:
                            if _do_probe_loaded(d_url, default_model):
                                default_loaded = True
                                break
                            time.sleep(poll_clean_s)
                    except Exception as e_df:
                        final_state["default_start_error"] = str(e_df)

                final_state["default_model_loaded"] = default_loaded
            except Exception as e_cl:
                final_state["cleanup_error"] = str(e_cl)

        result_data["final_state"] = final_state

        if output_path:
            try:
                Path(output_path).write_text(json.dumps(result_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    return result_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strata_switch_bench.py",
        description="Strata local model switch latency breakdown benchmark (spec 384)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18091", help="Default loopback URL of Strata server")
    parser.add_argument("--default-url", default=None, help="Loopback URL for default Strata model (overrides --base-url)")
    parser.add_argument("--specialist-url", default=None, help="Loopback URL for specialist Strata model (overrides --base-url)")
    parser.add_argument("--default-model", default="inkling-2-mini-sq-10", help="Default local served model id")
    parser.add_argument("--specialist-model", default="sage-a1.5", help="Specialist local served model id")
    parser.add_argument("--default-unit", default="jarvisos-strata-inkling.service", help="Default Strata systemd unit")
    parser.add_argument("--specialist-unit", default="jarvisos-strata-sage.service", help="Specialist Strata systemd unit")
    parser.add_argument("--spool", default=None, help="Path to hostops spool directory")
    parser.add_argument("--mode", choices=("auto", "hostops", "fast"), default="auto", help="Switch mechanism to benchmark")
    parser.add_argument("--prewarm", action="store_true", help="Attempt safe bounded page-cache prewarm before each switch")
    parser.add_argument("--runs", type=int, default=3, help="Number of benchmark runs (default 3)")
    parser.add_argument("--strata-log", type=str, default=None, help="Path to Strata server log for sub-phase parsing")
    parser.add_argument("--default-model-path", type=str, default=None, help="Path to default model file for mincore residency")
    parser.add_argument("--specialist-model-path", type=str, default=None, help="Path to specialist model file for mincore residency")
    parser.add_argument("--output", type=str, default="result.json", help="Path to write output result.json")
    parser.add_argument("--no-decode-check", action="store_true", help="Skip decode speed measurement")
    parser.add_argument("--mock", action="store_true", help="Simulate switch measurements offline")
    parser.add_argument("--min-ram-release-ratio", type=float, default=0.5, help="Fraction of model file size MemAvailable must rise after /unload")
    parser.add_argument("--load-deadline-s", type=float, default=180.0, help="Wall-clock deadline in seconds for /load before falling back to hostops")
    parser.add_argument("--max-wall-s", type=float, default=3600.0, help="Maximum overall wall-clock seconds before remaining runs are skipped")

    args = parser.parse_args(argv)

    # Check vault guard
    vault = vault_guard.vault_root()
    paths_to_check = [args.output]
    for opt_path in (args.strata_log, args.default_model_path, args.specialist_model_path, args.spool):
        if opt_path:
            paths_to_check.append(opt_path)

    if any(vault_guard.touches_vault(p, vault) for p in paths_to_check):
        print(vault_guard.REFUSED_MESSAGE, file=sys.stderr)
        return vault_guard.REFUSED_EXIT

    try:
        validate_loopback_url(args.default_url or args.base_url)
        validate_loopback_url(args.specialist_url or args.base_url)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    mock_runner = None
    mock_decode = None
    if args.mock:
        def _mock_switch(source: str, target: str, direction: str, run_idx: int) -> SwitchPhaseTimings:
            return SwitchPhaseTimings(
                direction=direction,
                run_index=run_idx,
                stop_old_s=1.1,
                start_dispatch_s=0.15,
                model_ready_s=10.5,
                first_token_s=0.45,
                total_switch_s=12.2,
                switch_mode="loopback_unload",
                unload_released_ram=True,
            )
        mock_runner = _mock_switch

        def _mock_decode(_m: str) -> float:
            return 34.5

        mock_decode = _mock_decode

    try:
        results = run_benchmark(
            base_url=args.base_url,
            default_url=args.default_url,
            specialist_url=args.specialist_url,
            default_model=args.default_model,
            specialist_model=args.specialist_model,
            default_unit=args.default_unit,
            specialist_unit=args.specialist_unit,
            spool_path=args.spool,
            mode=args.mode,
            prewarm=args.prewarm,
            runs=args.runs,
            strata_log_path=args.strata_log,
            default_model_path=args.default_model_path,
            specialist_model_path=args.specialist_model_path,
            check_decode=not args.no_decode_check,
            min_ram_release_ratio=args.min_ram_release_ratio,
            load_deadline_s=args.load_deadline_s,
            max_wall_s=args.max_wall_s,
            output_path=args.output,
            slots_checker=(lambda: True) if args.mock else None,
            switch_runner=mock_runner,
            decode_checker=mock_decode,
        )
    except Exception as err:  # noqa: BLE001
        print(f"Benchmark error: {err}", file=sys.stderr)
        return 1

    # Write result.json
    out_p = Path(args.output)
    out_p.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    # Print plain-Italian summary table
    summary_table = format_italian_summary(results)
    print(summary_table)

    return 0


if __name__ == "__main__":
    sys.exit(main())
