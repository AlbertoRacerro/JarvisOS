#!/usr/bin/env python3
"""Quota-aware worker router for coordinators (difficulty x risk x capability).

Coordinators plan, review and integrate; bounded implementation goes to the cheapest worker that is adequate for
the task and whose subscription pool still has headroom. Required tier = max(difficulty, risk), low=1, medium=2,
high=3. A model qualifies when its capability is at least that tier and its pool's free fraction (the minimum over
the pool's windows) is at least the pool's reserve (RESERVES); a window that resets within SPEND_DOWN may be spent to 0
(use it or lose it, spec 255). A pool that is disabled or unreadable is skipped (fail-closed per pool). Capability is
per effort, never per brand: Flash and Argon at medium take tier 2, at high tier 3, until measured results say
otherwise. Candidates are ordered by another family than `--review-of` first (never required), then smallest capability
surplus (effort follows the task), then the gemini family, then the quota that would expire unused soonest (binding
window's free fraction per hour to its reset), then most free quota, then table order. One `agy /quota` reading feeds
the gemini, argon (plus its own weekly bucket) and agy3p (third-party Sonnet/Opus) pools. Nothing qualifying exits 3
with reason `no_worker`: the coordinator then does the work itself or waits.

Quota is read without a model turn: Codex through `codex app-server` `account/rateLimits/read`, Gemini through
`agy -p /quota`, Copilot through `gh api copilot_internal/user`, Claude through `agent-relay --json limits --refresh`. This tool only reads quota and prints a decision; it launches nothing, writes nothing and holds no
credentials. Launch lines require exported absolute WORKTREE and TASK_FILE, LABEL, AUTHORITY and SPEC (WALL_HOURS optional). Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import queue
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ARGON_SPEC = importlib.util.spec_from_file_location("relay_argon", Path(__file__).resolve().parent / "relay_argon.py")
assert _ARGON_SPEC and _ARGON_SPEC.loader
relay_argon = sys.modules.get("relay_argon") or importlib.util.module_from_spec(_ARGON_SPEC)
if "relay_argon" not in sys.modules:
    sys.modules["relay_argon"] = relay_argon
    _ARGON_SPEC.loader.exec_module(relay_argon)

RESERVE = 0.05
# Per-pool reserve. The Claude pool is also what the coordinator spends: if it is skipped the coordinator would do
# the work itself at higher cost, so delegating to a cheaper Claude worker beats `no_worker`; it is skipped only
# when practically exhausted. Below CLAUDE_LOW_FREE a chosen Claude worker carries a warning note.
RESERVES = {"claude": 0.03, "codex": RESERVE, "copilot": RESERVE, "gemini": RESERVE, "argon": RESERVE, "agy3p": RESERVE}
# Use it or lose it (spec 255): a window that resets within this horizon may be spent down to 0.
SPEND_DOWN = timedelta(hours=24)
UNKNOWN_RESET_HOURS = 168.0
CLAUDE_LOW_FREE = 0.40
CLAUDE_LOW_NOTE = ("claude_pool_low — keep worker task small; coordinator should prefer waiting for a cheaper "
                   "pool reset if the task is not urgent")
# Among equally adequate models the gemini family goes first; the others follow the use-it-or-lose-it score.
FIRST_POOLS = ("gemini", "argon")
FRESHNESS = 900  # seconds a Claude (agent-relay) reading may be old, as the supervisor's reading_freshness
READ_TIMEOUT = 15  # timeout for readers other than agy; all readers run in parallel within TOTAL_TIMEOUT
AGY_READ_TIMEOUT = 40  # agy /quota is slow, typically ~8 s with spikes over 15 s
TOTAL_TIMEOUT = AGY_READ_TIMEOUT + 5
CACHE_TTL = 900  # seconds a summary stays valid inside a long-lived supervisor
FAILED_CACHE_TTL = 60
LEVELS = {"low": 1, "medium": 2, "high": 3}
REVIEW = {"high": "coordinator_full_diff_review", "medium": "coordinator_spot_check", "low": "tests_only"}
NO_WORKER_EXIT = 3
GEMINI_BUCKETS = {"gemini-5h": "5h", "gemini-weekly": "weekly"}
# One `agy /quota` document feeds three pools: the general Gemini pool, Argon (which also has its own weekly
# bucket) and the third-party buckets agy Sonnet and Opus run on.
AGY_POOLS = {"gemini": GEMINI_BUCKETS, "argon": {**GEMINI_BUCKETS, "gemini-argon-wtu-weekly": "argon-weekly"},
             "agy3p": {"3p-5h": "5h", "3p-weekly": "weekly"}}
AGY_DOC_TTL = 60
CODEX_WINDOWS = {"primary": "5h", "secondary": "weekly"}
CLAUDE_FIVE_HOUR = {"session", "five_hour", "5h"}
CLAUDE_WEEKLY = {"weekly", "seven_day", "week", "7d"}
UNREADABLE = "worker quota: unreadable"
COORDINATOR_AGENT_POOLS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "neon": "gemini",
    "argon": "argon",
    "hydrogen": "gemini",
    "agy_opus": "agy3p",
}
# The supervisor persists its last good reading here so a coordinator whose sandbox cannot read the quotas live
# still routes on a recent value. Per-pool entries, each aged on its own `as_of`.
SNAPSHOT_SCHEMA = "worker-quota.v1"
SNAPSHOT_NAME = Path(".agent-relay") / "worker-quota.json"
SNAPSHOT_MAX_AGE = 1800
RECORD_NAME = Path(".agent-relay") / "worker-outcomes.jsonl"
VALID_KINDS = ("docs", "tests", "ui", "feature", "review", "fix")
VALID_FAILURES = ("none", "harness", "model")
VALID_SALVAGES = ("salvaged", "restarted", "n/a")
VALID_FIRST_PASS = ("pass", "fail", "n/a")
RISK_GUIDE = ("risk = harm if the worker is wrong: low docs/tests/UI copy; medium feature code covered by tests; "
              "high security, auth, egress, migrations/data loss, supervisor/relay/sandbox code, irreversible ops")


@dataclass(frozen=True)
class Model:
    key: str
    kind: str  # launch kind: codex | agy | copilot | claude
    model: str
    pool: str
    capability: int  # 1..3, the highest required tier it may take


# Capability is provisional and per effort, never per brand (spec 255, directive L20261109084309-afcd): until
# measured, Flash and Argon at high take tier 3. Effort follows the task: the smallest adequate tier is chosen first.
# agy keys are agy_worker.py aliases. Order comes from capability, pools and measured spend.
MODELS: tuple[Model, ...] = (
    Model("luna", "codex", "gpt-6-luna", "codex", 1),
    Model("flash", "agy", "gemini-3.8-flash-low", "gemini", 1),
    Model("hydrogen-low", "agy", "gemini-4-hydrogen-low", "gemini", 1),
    Model("copilot-auto", "copilot", "auto", "copilot", 1),
    Model("haiku", "claude", "claude-haiku-5-5", "claude", 1),
    Model("flash-medium", "agy", "gemini-3.8-flash-medium", "gemini", 2),
    Model("hydrogen-medium", "agy", "gemini-4-hydrogen-medium", "gemini", 2),
    Model("argon-medium", "agy", "gemini-4-argon-medium", "argon", 2),
    Model("terra", "codex", "gpt-6.1-terra", "codex", 2),
    Model("sonnet", "claude", "claude-sonnet-5-5", "claude", 2),
    Model("flash-high", "agy", "gemini-3.8-flash-high", "gemini", 3),
    Model("hydrogen-high", "agy", "gemini-4-hydrogen-high", "gemini", 3),
    Model("argon-high", "agy", "gemini-4-argon-high", "argon", 3),
    Model("agy-sonnet", "agy", "claude-sonnet-5-5-high", "agy3p", 3),
    Model("agy-opus", "agy", "claude-opus-5-5-high", "agy3p", 3),
    Model("sol", "codex", "gpt-6-sol", "codex", 3),
)


def family(model: Model) -> str:
    """The model family an independent review should differ from (agy Sonnet/Opus are Claude models)."""
    name = model.model
    return ("claude" if name.startswith("claude") else "gemini" if name.startswith("gemini")
            else "codex" if name.startswith("gpt") else model.pool)


@dataclass(frozen=True)
class Pool:
    """A subscription pool reading: windows maps a window label to its free fraction. free is None when the pool
    is unreadable or disabled (reason says why); otherwise it is the minimum over the windows."""

    name: str
    windows: dict[str, float]
    free: float | None
    reason: str | None = None
    source: str = "live"  # live | snapshot
    resets: dict[str, datetime] = field(default_factory=dict, compare=False)  # window label -> reset, when known


def _reset_ok(pool: Pool, label: str, now: datetime, horizon: timedelta) -> bool:
    reset = pool.resets.get(label)
    return reset is not None and now < reset <= now + horizon


def below_reserve(pool: Pool, now: datetime | None = None) -> bool:
    """A window under the reserve blocks the pool unless it resets within SPEND_DOWN (use it or lose it)."""
    now = now or datetime.now(UTC)
    reserve = RESERVES.get(pool.name, RESERVE)
    return any(free < reserve and not (free > 0 and _reset_ok(pool, label, now, SPEND_DOWN))
               for label, free in pool.windows.items())


def expiry_score(pool: Pool, now: datetime | None = None) -> float:
    """Quota that would expire unused: the binding (least free) window's free fraction per hour to its reset.
    An unknown reset counts as a week away."""
    now = now or datetime.now(UTC)
    if not pool.windows:
        return 0.0
    label = min(pool.windows, key=lambda k: pool.windows[k])
    reset = pool.resets.get(label)
    hours = (reset - now).total_seconds() / 3600 if reset is not None and reset > now else UNKNOWN_RESET_HOURS
    return pool.windows[label] / max(hours, 1.0)


def tier(difficulty: str, risk: str) -> int:
    return max(LEVELS[difficulty], LEVELS[risk])


def unreadable(name: str, reason: str) -> Pool:
    return Pool(name, {}, None, reason)


def temp_pools_file(control_dir: Path | None = None) -> Path:
    override = os.environ.get("TEMP_POOLS_FILE")
    if override:
        return Path(override).expanduser()
    base = control_dir if control_dir is not None else default_control_dir()
    return base.parent / "temp-pools.json"


def _parse_iso_zone(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


FREE_LISTS = ("openai_models", "openrouter_public", "opencode_zen")
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
LIST_TTL = timedelta(hours=1)
LIST_TIMEOUT = 10
SMOKE_PREFIX = "tempsmoke-"
_lists_lock = threading.Lock()


def temp_pool_lists_file() -> Path:
    return temp_pools_file().parent / "temp-pool-lists.json"


def _admission(document: Any) -> tuple[int | None, int | None, dict[str, int]]:
    """(reference score, coding reference score, worker key -> index score) from top level of temp-pools.json."""
    if not isinstance(document, dict):
        return None, None, {}
    raw = document.get("scores")
    scores = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)} \
        if isinstance(raw, dict) else {}
    coding_raw = document.get("coding_scores")
    coding_scores = {k: v for k, v in coding_raw.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)} \
        if isinstance(coding_raw, dict) else {}

    ref = document.get("reference")
    ref_score = document.get("reference_score")
    if ref_score is None and isinstance(ref, str):
        ref_score = scores.get(ref)

    coding_ref = document.get("coding_reference") or document.get("reference")
    ref_coding = document.get("reference_coding_index") or document.get("coding_reference_score")
    if ref_coding is None and isinstance(coding_ref, str):
        ref_coding = coding_scores.get(coding_ref)

    ref_score_int = ref_score if isinstance(ref_score, int) and not isinstance(ref_score, bool) else None
    ref_coding_int = ref_coding if isinstance(ref_coding, int) and not isinstance(ref_coding, bool) else None
    return ref_score_int, ref_coding_int, scores


def start_tier(score: int, scores: dict[str, int]) -> int:
    """The highest tier of an existing worker whose index score is at most `score`; none: tier 1."""
    capability = {m.key: m.capability for m in MODELS}
    return max((capability[k] for k, v in scores.items() if k in capability and v <= score), default=1)


def load_temp_pools(path: Path | None = None) -> list[dict[str, Any]]:
    target = path if path is not None else temp_pools_file()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    entries = document if isinstance(document, list) else document.get("pools") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return []
    reference, ref_coding, scores = _admission(document)
    valid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            valid.append({"name": f"invalid#{index}", "model": "", "invalid": "not an object"})
            continue
        name = raw.get("name")
        provider = raw.get("provider")
        base_url = raw.get("base_url")
        model = raw.get("model")
        key_file = raw.get("key_file")
        free_list = raw.get("free_list")
        score = raw.get("index_score")
        coding = raw.get("coding_index") if raw.get("coding_index") is not None else raw.get("coding_score")
        expires = _parse_iso_zone(raw.get("expires_at"))
        # Zen needs neither URL nor key; OpenRouter reads its list without a key but calls with one.
        needs_url = free_list != "opencode_zen"
        needs_key = free_list != "opencode_zen"
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                or not isinstance(provider, str) or not provider.strip()
                or free_list not in FREE_LISTS
                or (needs_url and (not isinstance(base_url, str) or not base_url.startswith("https://")))
                or not isinstance(model, str) or not model.strip()
                or (needs_key and (not isinstance(key_file, str) or not Path(key_file).is_absolute()))
                or (raw.get("expires_at") is not None and expires is None)
                or (score is not None and (not isinstance(score, int) or isinstance(score, bool)))
                or (coding is not None and (not isinstance(coding, int) or isinstance(coding, bool)))
                or "rate_limits" not in raw or "data_policy" not in raw
                or name in seen):
            # Kept, so pick shows why it is skipped; never raised, never routed.
            # A synthetic name per bad entry, so it can never shadow or duplicate a usable pool.
            valid.append({"name": f"invalid#{index}", "model": str(raw.get("model") or ""), "invalid": "malformed entry"})
            continue
        seen.add(name)
        admission = None
        if score is None or coding is None:
            admission = "unindexed"
        elif reference is None or ref_coding is None:
            admission = "no_reference"
        elif score < reference:
            admission = "below_index"
        elif coding < ref_coding:
            admission = "below_coding_index"

        valid.append({
            "name": name, "provider": provider.strip(), "free_list": free_list,
            "base_url": base_url if needs_url else None, "model": model.strip(),
            "key_file": Path(key_file).expanduser() if needs_key else None, "expires_at": expires,
            "index_score": score, "coding_index": coding, "admission": admission,
            "start_tier": start_tier(score, scores) if admission is None and score is not None else 0,
            "pricing": raw.get("pricing"),
            "live_probe": raw.get("live_probe") or raw.get("probe"),
            "live_status": raw.get("live_status"),
            "live_ok_at": raw.get("live_ok_at"),
        })
    return valid


def temp_capability(entry: dict[str, Any], records: list[dict[str, Any]]) -> int:
    """The index start tier moved by real outcomes (the Copilot rule; smoke runs and harness failures excluded)."""
    base = entry.get("start_tier", 0)
    if not base:
        return 0
    key = f"temp-{entry['name']}"
    own = [r for r in records if r.get("worker") == key and not str(r.get("task", "")).startswith(SMOKE_PREFIX)]
    return copilot_effective_capability(own, base, worker=key, model=entry["model"], sparse=base)


def temp_models(outcomes: list[dict[str, Any]] | None = None) -> tuple[Model, ...]:
    records = load_records() if outcomes is None else outcomes
    return tuple(Model(f"temp-{entry['name']}", "opencode", entry["model"], f"temp-{entry['name']}",
                       temp_capability(entry, records)) for entry in load_temp_pools())


def all_models(outcomes: list[dict[str, Any]] | None = None) -> tuple[Model, ...]:
    return MODELS + temp_models(outcomes)


def _read_key(entry: dict[str, Any]) -> str | None:
    try:
        key = entry["key_file"].read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, AttributeError):
        return None
    return key or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another URL; refuse it."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    if not url.startswith("https://"):
        raise ValueError("free-list URL must be https")
    # The Zen endpoint refuses urllib's default User-Agent (403); a named one is accepted.
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "jarvisos-worker-routing/1", **headers})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def list_source(entry: dict[str, Any]) -> str:
    return entry["base_url"] if entry["free_list"] == "openai_models" else entry["free_list"]


def _is_zero(val: Any) -> bool:
    if isinstance(val, bool) or val is None:
        return False
    try:
        return float(val) == 0.0
    except (ValueError, TypeError):
        return False


_default_prober: Callable[..., Any] | None = None


def pool_base_url(entry: dict[str, Any]) -> str | None:
    if entry.get("base_url"):
        return str(entry["base_url"]).rstrip("/")
    if entry.get("free_list") == "opencode_zen":
        return "https://opencode.ai/zen/v1"
    if entry.get("free_list") == "openrouter_public":
        return "https://openrouter.ai/api/v1"
    return None


def _http_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
    if not url.startswith("https://"):
        raise ValueError("probe URL must be https")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        code = getattr(response, "status", 200) or 200
        return code, json.loads(response.read().decode("utf-8"))


def probe_pool_model(
    entry: dict[str, Any],
    model_id: str,
    now: datetime,
    prober: Callable[..., Any] | None = None,
    opener: Callable[..., Any] | None = None,
    timeout: float = LIST_TIMEOUT,
) -> dict[str, Any]:
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    base_url = pool_base_url(entry)
    if not base_url:
        return {"status": "error", "code": None, "at": now_str}
    key = _read_key(entry) if entry.get("key_file") is not None else None
    if entry.get("free_list") == "openai_models" and key is None:
        return {"status": "error", "code": None, "at": now_str}

    url = f"{base_url}/chat/completions"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "jarvisos-worker-routing/1",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
    }

    custom_prober = prober or getattr(sys.modules.get(__name__), "_default_prober", None)
    if custom_prober is not None:
        try:
            try:
                res = custom_prober(entry, model_id, url, headers, payload, timeout)
            except TypeError:
                res = custom_prober(url, headers, payload, timeout)
            if isinstance(res, dict) and "status" in res:
                if "at" not in res:
                    res["at"] = now_str
                return res
            if isinstance(res, dict) and "choices" in res:
                choices = res.get("choices")
                msg = choices[0].get("message", {}).get("content", "") if choices and isinstance(choices, list) and isinstance(choices[0], dict) else ""
                if str(msg).strip():
                    return {"status": "ok", "code": 200, "at": now_str}
                return {"status": "error", "code": 200, "at": now_str}
            if isinstance(res, tuple) and len(res) == 2:
                code, data = res
                if code == 200 and isinstance(data, dict):
                    choices = data.get("choices")
                    msg = choices[0].get("message", {}).get("content", "") if choices and isinstance(choices, list) and isinstance(choices[0], dict) else ""
                    if str(msg).strip():
                        return {"status": "ok", "code": 200, "at": now_str}
                    return {"status": "error", "code": 200, "at": now_str}
                if code == 404:
                    return {"status": "404", "code": 404, "at": now_str}
                if code == 402:
                    return {"status": "402", "code": 402, "at": now_str}
                if code in (408, 504):
                    return {"status": "timeout", "code": code, "at": now_str}
                return {"status": "error", "code": code, "at": now_str}
            return {"status": "error", "code": 200, "at": now_str}
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return {"status": "404", "code": 404, "at": now_str}
            if err.code == 402:
                return {"status": "402", "code": 402, "at": now_str}
            if err.code in (408, 504):
                return {"status": "timeout", "code": err.code, "at": now_str}
            return {"status": "error", "code": err.code, "at": now_str}
        except TimeoutError:
            return {"status": "timeout", "code": 408, "at": now_str}
        except Exception:
            return {"status": "error", "code": None, "at": now_str}

    if opener is not None:
        try:
            try:
                raw_resp = opener(url, headers, payload, timeout)
            except TypeError:
                raw_resp = opener(url, headers, timeout)
            if isinstance(raw_resp, dict) and "choices" in raw_resp:
                choices = raw_resp.get("choices")
                msg = choices[0].get("message", {}).get("content", "") if choices and isinstance(choices, list) and isinstance(choices[0], dict) else ""
                if str(msg).strip():
                    return {"status": "ok", "code": 200, "at": now_str}
                return {"status": "error", "code": 200, "at": now_str}
            if isinstance(raw_resp, dict) and "status" in raw_resp:
                if "at" not in raw_resp:
                    raw_resp["at"] = now_str
                return raw_resp
            return {"status": "error", "code": 200, "at": now_str}
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return {"status": "404", "code": 404, "at": now_str}
            if err.code == 402:
                return {"status": "402", "code": 402, "at": now_str}
            if err.code in (408, 504):
                return {"status": "timeout", "code": err.code, "at": now_str}
            return {"status": "error", "code": err.code, "at": now_str}
        except TimeoutError:
            return {"status": "timeout", "code": 408, "at": now_str}
        except Exception:
            return {"status": "error", "code": None, "at": now_str}

    try:
        code, resp = _http_post_json(url, headers, payload, timeout)
        if code == 200 and isinstance(resp, dict):
            choices = resp.get("choices")
            msg = choices[0].get("message", {}).get("content", "") if choices and isinstance(choices, list) and isinstance(choices[0], dict) else ""
            if str(msg).strip():
                return {"status": "ok", "code": 200, "at": now_str}
            return {"status": "error", "code": 200, "at": now_str}
        return {"status": "error", "code": code, "at": now_str}
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return {"status": "404", "code": 404, "at": now_str}
        if err.code == 402:
            return {"status": "402", "code": 402, "at": now_str}
        if err.code in (408, 504):
            return {"status": "timeout", "code": err.code, "at": now_str}
        return {"status": "error", "code": err.code, "at": now_str}
    except TimeoutError:
        return {"status": "timeout", "code": 408, "at": now_str}
    except urllib.error.URLError as err:
        if isinstance(getattr(err, "reason", None), TimeoutError):
            return {"status": "timeout", "code": 408, "at": now_str}
        return {"status": "error", "code": None, "at": now_str}
    except Exception:
        return {"status": "error", "code": None, "at": now_str}


class FreeIds(list[str]):
    """List of free model ids carrying extracted per-id pricing."""

    def __init__(self, ids: list[str], pricing: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__(ids)
        self.pricing: dict[str, dict[str, Any]] = pricing or {}


def fetch_free_ids(entry: dict[str, Any], opener: Callable[..., Any] | None = None) -> FreeIds:
    """Free model ids its source lists now. When a source document publishes pricing,
    per-id pricing is extracted and ids whose published price is nonzero are never listed as free."""
    get = opener or _http_get_json
    kind = entry["free_list"]
    if kind == "openai_models":
        key = _read_key(entry)
        if key is None:
            raise OSError("no key")
        document = get(entry["base_url"].rstrip("/") + "/models", {"Authorization": f"Bearer {key}"}, LIST_TIMEOUT)
    else:
        document = get(OPENROUTER_MODELS_URL if kind == "openrouter_public" else ZEN_MODELS_URL, {}, LIST_TIMEOUT)
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, list):
        raise ValueError("no model list")

    suffix = "-free" if kind == "opencode_zen" else ":free"
    free_ids: list[str] = []
    pricing_map: dict[str, dict[str, Any]] = {}

    for item in data:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            continue

        p_obj = item.get("pricing") if isinstance(item.get("pricing"), dict) else (
            item.get("prices") if isinstance(item.get("prices"), dict) else None
        )
        prompt_p = None
        comp_p = None
        if p_obj is not None:
            prompt_p = p_obj.get("prompt") if "prompt" in p_obj else p_obj.get("prompt_price")
            comp_p = p_obj.get("completion") if "completion" in p_obj else p_obj.get("completion_price")
        elif "prompt_price" in item or "completion_price" in item:
            prompt_p = item.get("prompt_price")
            comp_p = item.get("completion_price")

        is_nonzero = False
        if prompt_p is not None or comp_p is not None:
            pricing_map[mid] = {"prompt": prompt_p, "completion": comp_p}
            if (prompt_p is not None and not _is_zero(prompt_p)) or (comp_p is not None and not _is_zero(comp_p)):
                is_nonzero = True

        if mid.endswith(suffix) and not is_nonzero:
            free_ids.append(mid)

    return FreeIds(sorted(free_ids), pricing_map)


def _load_lists() -> dict[str, Any]:
    try:
        document = json.loads(temp_pool_lists_file().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _write_lists(document: dict[str, Any]) -> None:
    target = temp_pool_lists_file()
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=".temp-pool-lists.",
                                         delete=False) as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            name = handle.name
        os.replace(name, target)
    except OSError:
        pass


def listed_ids(entry: dict[str, Any], now: datetime, opener: Callable[..., Any] | None = None,
               prober: Callable[..., Any] | None = None) -> list[str] | None:
    """The source's free ids from a cache younger than LIST_TTL, refreshed when older; None when unreadable."""
    source = list_source(entry)
    with _lists_lock:
        document = _load_lists()
        cached = (document.get("sources") or {}).get(source) if isinstance(document.get("sources"), dict) else None
        read_at = _parse_iso_zone(cached.get("read_at")) if isinstance(cached, dict) else None
        if read_at is not None and timedelta(0) <= now - read_at < LIST_TTL and isinstance(cached.get("ids"), list):
            return [i for i in cached["ids"] if isinstance(i, str)]
        try:
            fetched = fetch_free_ids(entry, opener)
            ids = list(fetched)
            pricing = getattr(fetched, "pricing", {})
        except Exception:  # noqa: BLE001 - an unreadable list only takes its pools out of routing
            return None
        sources = document.get("sources") if isinstance(document.get("sources"), dict) else {}
        sources[source] = {"ids": ids, "pricing": pricing, "read_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        document["sources"] = sources

        probes = document.get("probes") if isinstance(document.get("probes"), dict) else {}
        pools_to_probe: list[dict[str, Any]] = [entry]
        try:
            for p in load_temp_pools():
                if list_source(p) == source and p.get("model") not in [cp.get("model") for cp in pools_to_probe]:
                    pools_to_probe.append(p)
        except Exception:
            pass

        probed_models: set[str] = set()
        for pool_entry in pools_to_probe:
            mid = pool_entry.get("model")
            if not isinstance(mid, str) or not mid:
                continue
            if mid not in ids:
                continue
            if not (mid.endswith(":free") or mid.endswith("-free")):
                continue
            pr = pricing.get(mid)
            if pr is not None:
                p_prompt = pr.get("prompt")
                p_comp = pr.get("completion")
                if not _is_zero(p_prompt) or not _is_zero(p_comp):
                    continue
            pool_pr = pool_entry.get("pricing")
            if isinstance(pool_pr, dict):
                p_prompt = pool_pr.get("prompt")
                p_comp = pool_pr.get("completion")
                if not _is_zero(p_prompt) or not _is_zero(p_comp):
                    continue
            if mid in probed_models:
                continue
            probed_models.add(mid)

            probe_result = probe_pool_model(pool_entry, mid, now=now, prober=prober, opener=opener)
            probes[mid] = probe_result

        document["probes"] = probes
        _write_lists(document)
        return ids


def bridge_state(name: str) -> str | None:
    bridge = _load_lists().get("bridge")
    state = bridge.get(name) if isinstance(bridge, dict) else None
    return state.get("state") if isinstance(state, dict) else None


def set_bridge_state(name: str, state: str, now: datetime | None = None) -> None:
    with _lists_lock:
        document = _load_lists()
        bridge = document.get("bridge") if isinstance(document.get("bridge"), dict) else {}
        bridge[name] = {"state": state, "at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        document["bridge"] = bridge
        _write_lists(document)


def temp_pool_read(entry: dict[str, Any], now: datetime | None = None,
                   opener: Callable[..., Any] | None = None,
                   prober: Callable[..., Any] | None = None) -> Pool:
    moment = now or datetime.now(UTC)
    name = f"temp-{entry['name']}"
    if entry.get("invalid"):
        return unreadable(name, "invalid")
    expires = entry.get("expires_at")
    if expires is not None and moment >= expires:
        return unreadable(name, "expired")
    if entry.get("admission"):
        return unreadable(name, entry["admission"])
    if entry.get("key_file") is not None:
        try:
            exists = entry["key_file"].is_file()
        except OSError:
            exists = False
        if not exists:
            return unreadable(name, "no_key")
    ids = listed_ids(entry, moment, opener=opener, prober=prober)
    if ids is None:
        return unreadable(name, "list_unreadable")
    if entry["model"] not in ids:
        return unreadable(name, "not_listed")

    doc = _load_lists()
    source = list_source(entry)
    source_data = (doc.get("sources") or {}).get(source) or {}

    # Zero price check: source's published pricing is authoritative when present.
    # If the source publishes a nonzero price, reject immediately regardless of any local claim.
    source_pricing = None
    for pricing_dict in (source_data.get("pricing"), source_data.get("prices"), doc.get("pricing"), doc.get("prices")):
        if isinstance(pricing_dict, dict) and entry["model"] in pricing_dict:
            source_pricing = pricing_dict[entry["model"]]
            break

    if source_pricing is not None:
        if not isinstance(source_pricing, dict):
            return unreadable(name, "nonzero_price")
        p = source_pricing.get("prompt") if "prompt" in source_pricing else source_pricing.get("prompt_price")
        c = source_pricing.get("completion") if "completion" in source_pricing else source_pricing.get("completion_price")
        if not _is_zero(p) or not _is_zero(c):
            return unreadable(name, "nonzero_price")

    pool_pricing = entry.get("pricing")
    if pool_pricing is not None:
        if not isinstance(pool_pricing, dict):
            return unreadable(name, "nonzero_price")
        p = pool_pricing.get("prompt") if "prompt" in pool_pricing else pool_pricing.get("prompt_price")
        c = pool_pricing.get("completion") if "completion" in pool_pricing else pool_pricing.get("completion_price")
        if not _is_zero(p) or not _is_zero(c):
            return unreadable(name, "nonzero_price")

    # Response proof check
    candidates: list[dict[str, Any]] = []
    for d in (doc.get("probes"), doc.get("live_probes"), source_data.get("probes")):
        if isinstance(d, dict):
            for k in (entry["model"], entry["name"], name):
                val = d.get(k)
                if isinstance(val, dict):
                    candidates.append(val)

    if not candidates:
        return unreadable(name, "missing_proof")

    # If ANY fresh/recent candidate proof reports 404 or 402, fail closed immediately.
    for proof in candidates:
        p_status = str(proof.get("status") or proof.get("live_status") or proof.get("state") or "").lower()
        p_code = proof.get("code") or proof.get("status_code")
        ts_raw = proof.get("at") or proof.get("live_ok_at") or proof.get("timestamp") or proof.get("read_at")
        proof_dt = _parse_iso_zone(ts_raw)
        is_fresh = proof_dt is not None and (-60 <= (moment - proof_dt).total_seconds() <= 3600)
        if (p_code in (404, "404") or p_status in ("404", "not_found")) and (proof_dt is None or is_fresh):
            return unreadable(name, "probe_404")
        if (p_code in (402, "402") or p_status in ("402", "payment_required", "payment_response")) and (proof_dt is None or is_fresh):
            return unreadable(name, "payment_required")

    # Find the newest fresh OK proof
    newest_ok_dt: datetime | None = None
    has_fresh_ok = False
    has_stale_ok = False
    has_error_proof: str | None = None

    for proof in candidates:
        p_status = str(proof.get("status") or proof.get("live_status") or proof.get("state") or "").lower()
        p_code = proof.get("code") or proof.get("status_code")
        if p_code in (500, 502, 503, 504) or p_status in ("error", "timeout", "failed", "unusable"):
            has_error_proof = p_status or "unusable"
            continue
        if p_code in (404, 402, "404", "402") or p_status in ("404", "not_found", "402", "payment_required", "payment_response"):
            continue
        # Reject active, throttled, exhausted as live-response proof
        if p_status in ("active", "throttled", "exhausted"):
            continue
        is_ok = (p_code in (200, "200") and p_status not in ("error", "failed", "timeout", "unusable")) or (
            p_status in ("ok", "success", "succeeded") and (p_code in (200, "200") or p_code is None)
        )
        if not is_ok:
            continue
        ts_raw = proof.get("at") or proof.get("live_ok_at") or proof.get("timestamp") or proof.get("read_at")
        proof_dt = _parse_iso_zone(ts_raw)
        if proof_dt is None or not (-60 <= (moment - proof_dt).total_seconds() <= 3600):
            has_stale_ok = True
            continue
        has_fresh_ok = True
        if newest_ok_dt is None or proof_dt > newest_ok_dt:
            newest_ok_dt = proof_dt

    if not has_fresh_ok:
        if has_error_proof:
            return unreadable(name, has_error_proof if has_error_proof in ("timeout", "error", "unusable") else "unusable")
        if has_stale_ok:
            return unreadable(name, "stale_proof")
        return unreadable(name, "missing_proof")

    # Check if any error proof beats newest_ok_dt
    for proof in candidates:
        p_status = str(proof.get("status") or proof.get("live_status") or proof.get("state") or "").lower()
        p_code = proof.get("code") or proof.get("status_code")
        if p_code in (500, 502, 503, 504) or p_status in ("error", "timeout", "failed", "unusable"):
            ts_raw = proof.get("at") or proof.get("live_ok_at") or proof.get("timestamp") or proof.get("read_at")
            proof_dt = _parse_iso_zone(ts_raw)
            if proof_dt is None or newest_ok_dt is None or proof_dt >= newest_ok_dt:
                err_reason = p_status if p_status in ("timeout", "error", "unusable") else "unusable"
                return unreadable(name, err_reason)

    b_state = bridge_state(name)
    if b_state == "suspect":
        return unreadable(name, "bridge_suspect")
    if b_state == "exhausted":
        return unreadable(name, "exhausted")
    if b_state == "throttled":
        return unreadable(name, "throttled")
    return Pool(name, {"free": 1.0}, 1.0, None, "live", {"expires": expires} if expires is not None else {})


def _fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _json_document(text: str) -> dict[str, Any] | None:
    for candidate in [text, *reversed(text.strip().splitlines())]:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _host_which(command: str) -> str | None:
    """Resolve a CLI as the Relay gateway does: services do not inherit a login PATH."""
    entries = [str(Path.home() / ".local" / "bin")]
    entries += sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin")), reverse=True)
    entries.append(os.environ.get("PATH", "/usr/bin:/bin"))
    return shutil.which(command, path=os.pathsep.join(entries))


def _buckets(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(node, list):
        return [b for item in node for b in _buckets(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    own = node.get("buckets")
    found = [b for b in own if isinstance(b, dict)] if isinstance(own, list) else []
    return found + [b for key, value in node.items() if key != "buckets" for b in _buckets(value, depth + 1)]


def parse_gemini(reading: dict[str, Any] | None, name: str = "gemini") -> Pool:
    """An agy pool (AGY_POOLS) from an `agy /quota` document: every one of its buckets must be present and enabled."""
    if reading is None:
        return unreadable(name, "no_reading")
    wanted = AGY_POOLS[name]
    found = {b.get("id"): b for b in _buckets(reading) if b.get("id") in wanted}
    if set(found) != set(wanted):
        return unreadable(name, "bucket_missing")
    windows: dict[str, float] = {}
    resets: dict[str, datetime] = {}
    for bucket_id, label in wanted.items():
        bucket = found[bucket_id]
        fraction = _fraction(bucket.get("remaining_fraction"))
        if fraction is None or not isinstance(bucket.get("disabled", False), bool):
            return unreadable(name, "invalid_reading")
        if bucket.get("disabled"):
            return Pool(name, {}, None, f"{label}_disabled")
        windows[label] = fraction
        if (reset := _parse_instant(bucket.get("reset_time"))) is not None:
            resets[label] = reset
    return Pool(name, windows, min(windows.values()), resets=resets)


def parse_codex(limits: dict[str, Any] | None) -> Pool:
    """The codex pool from the `rateLimits` object: every window present must be valid, and at least one present."""
    if not isinstance(limits, dict):
        return unreadable("codex", "no_reading")
    windows: dict[str, float] = {}
    for key, label in CODEX_WINDOWS.items():
        window = limits.get(key)
        if window is None:
            continue
        used = window.get("usedPercent") if isinstance(window, dict) else None
        if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used) or not 0 <= used <= 100:
            return unreadable("codex", "invalid_reading")
        windows[label] = 1 - used / 100
    if not windows:
        return unreadable("codex", "no_windows")
    return Pool("codex", windows, min(windows.values()))


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_claude(entry: Any, now: datetime | None = None) -> Pool:
    """The claude pool from the `claude` entry of `agent-relay --json limits`: status ok, an `as_of` no older than
    FRESHNESS, and every window with a valid used_percent. Anything else (e.g. an expired session) is unreadable."""
    if not isinstance(entry, dict):
        return unreadable("claude", "no_reading")
    if entry.get("status") not in (None, "ok"):
        return unreadable("claude", "unavailable")
    as_of = _parse_instant(entry.get("as_of"))
    if as_of is None:
        return unreadable("claude", "no_as_of")
    if abs(((now or datetime.now(UTC)) - as_of).total_seconds()) > FRESHNESS:
        return unreadable("claude", "stale")
    raw = entry.get("windows")
    if not isinstance(raw, list) or not raw:
        return unreadable("claude", "no_windows")
    windows: dict[str, float] = {}
    for window in raw:
        used = window.get("used_percent") if isinstance(window, dict) else None
        if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used) or not 0 <= used <= 100:
            return unreadable("claude", "invalid_reading")
        ident = str(window.get("id", ""))
        label = "5h" if ident in CLAUDE_FIVE_HOUR else "weekly" if ident in CLAUDE_WEEKLY else ident or "window"
        windows[label] = min(windows.get(label, 1.0), 1 - used / 100)
    return Pool("claude", windows, min(windows.values()))


def _month_shift(moment: datetime, months: int) -> datetime:
    """The same day-of-month instant `months` calendar months away, clamped to the month's first day if absent."""
    index = moment.year * 12 + moment.month - 1 + months
    try:
        return moment.replace(year=index // 12, month=index % 12 + 1)
    except ValueError:
        return moment.replace(year=index // 12, month=index % 12 + 1, day=1)


def behind_pace(pool: Pool, now: datetime) -> bool:
    """A monthly pool whose used fraction is below the elapsed share of its month (spec 290)."""
    reset = pool.resets.get("monthly")
    free = pool.windows.get("monthly")
    if reset is None or free is None:
        return False
    start = _month_shift(reset, -1)
    if not start <= now < reset:
        return False
    return 1 - free < (now - start) / (reset - start)


def parse_copilot(reading: dict[str, Any] | None, now: datetime | None = None) -> Pool:
    """Premium interactions from GitHub's no-model-turn entitlement endpoint."""
    if not isinstance(reading, dict):
        return unreadable("copilot", "no_reading")
    if reading.get("cli_enabled") is False:
        return unreadable("copilot", "cli_disabled")
    snapshots = reading.get("quota_snapshots")
    prem = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(prem, dict):
        return unreadable("copilot", "bucket_missing")
    reset = _parse_instant(reading.get("quota_reset_date_utc"))
    if reset is None:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        reset = _month_shift(moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 1)
    resets = {"monthly": reset}
    if prem.get("unlimited") is True:
        return Pool("copilot", {"monthly": 1.0}, 1.0, resets=resets)
    if prem.get("has_quota") is False:
        if prem.get("entitlement") in (0, 0.0):
            return unreadable("copilot", "no_entitlement")
        return Pool("copilot", {"monthly": 0.0}, 0.0, resets=resets)
    pct_rem = prem.get("percent_remaining")
    if (isinstance(pct_rem, bool) or not isinstance(pct_rem, int | float)
            or not math.isfinite(pct_rem) or not 0 <= pct_rem <= 100):
        return unreadable("copilot", "invalid_reading")
    remaining = prem.get("remaining")
    if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, int | float)
                                  or not math.isfinite(remaining)):
        return unreadable("copilot", "invalid_reading")
    fraction = float(pct_rem) / 100.0
    if remaining is not None and remaining <= 0:
        fraction = 0.0
    return Pool("copilot", {"monthly": fraction}, fraction, resets=resets)


def read_copilot() -> Pool:
    """Read entitlement using coordinator credentials; never start a model turn."""
    binary = os.environ.get("JARVISOS_GH_BIN") or _host_which("gh")
    if not binary:
        return unreadable("copilot", "gh_missing")
    env = os.environ.copy()
    token = env.get("JARVISOS_COPILOT_GH_TOKEN") or env.get("COPILOT_GITHUB_TOKEN")
    if not token:
        try:
            token = (Path.home() / ".local/share/jarvisos-sandbox/state/copilot/token").read_text().strip()
        except OSError:
            pass
    if token:
        env["GH_TOKEN"] = token
    try:
        done = subprocess.run([binary, "api", "copilot_internal/user"], capture_output=True, text=True,
                              timeout=READ_TIMEOUT, check=False, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                              env=env)
    except subprocess.TimeoutExpired:
        return unreadable("copilot", "timeout")
    except OSError:
        return unreadable("copilot", "gh_missing")
    if done.returncode != 0:
        output = (done.stdout + done.stderr).lower()
        if any(marker in output for marker in ("401", "bad credentials", "gh auth login", "not logged into", "notsignedin")):
            return unreadable("copilot", "auth_required")
        return unreadable("copilot", "no_reading")
    pool = parse_copilot(_json_document(done.stdout))
    if pool.free is not None and not (token or
            (Path.home() / ".local/share/jarvisos-sandbox/state/copilot/copilot-home").is_dir()):
        return unreadable("copilot", "auth_missing")
    configured = os.environ.get("JARVISOS_COPILOT_BIN")
    if pool.free is not None and not (shutil.which(configured) if configured else _host_which("copilot")):
        return unreadable("copilot", "copilot_missing")
    return pool


def read_claude() -> Pool:
    """Use a fresh cached Relay reading before requesting a refresh."""
    binary = os.environ.get("JARVISOS_AGENT_RELAY_BIN") or _host_which("agent-relay")
    if not binary:
        return unreadable("claude", "relay_missing")

    def read(refresh: bool) -> Pool:
        try:
            done = subprocess.run([binary, "--json", "limits", *(["--refresh"] if refresh else [])],
                                  capture_output=True, text=True, timeout=READ_TIMEOUT, check=False,
                                  cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return unreadable("claude", "timeout")
        except OSError:
            return unreadable("claude", "relay_missing")
        data = _json_document(done.stdout)
        agents = data.get("agents") if data else None
        entry = next((a for a in agents if isinstance(a, dict) and a.get("agent_key") == "claude"), None) \
            if isinstance(agents, list) else None
        return parse_claude(entry)

    cached = read(False)
    if cached.free is not None:
        return cached
    return read(True)


_agy_doc: dict[str, Any] = {"at": None, "reading": None}
_agy_lock = threading.Lock()


def read_agy(name: str) -> Pool:
    """One `agy /quota` read feeds every agy pool: concurrent readers share it for AGY_DOC_TTL seconds."""
    with _agy_lock:
        now = time.monotonic()
        if _agy_doc["at"] is None or now - _agy_doc["at"] > AGY_DOC_TTL:
            _agy_doc.update(at=now, reading=read_agy_document())
        reading = _agy_doc["reading"]
    return unreadable(name, reading) if isinstance(reading, str) else parse_gemini(reading, name)


def read_gemini() -> Pool:
    """A fresh reading of the general Gemini pool (read_pools shares one reading across the agy pools)."""
    reading = read_agy_document()
    return unreadable("gemini", reading) if isinstance(reading, str) else parse_gemini(reading, "gemini")


def read_agy_document() -> dict[str, Any] | str:
    """The `agy /quota` document, or the reason it could not be read."""
    if relay_argon.sandbox():
        try:
            relay_argon.prepare_login()
        except relay_argon.Refused as exc:
            if str(exc) != "login_state_missing":
                return str(exc)
        except OSError:
            return "login_path_conflict"
    binary = os.environ.get("JARVISOS_AGY_BIN") or _host_which("agy")
    if not binary:
        return "agy_missing"
    try:
        done = subprocess.run([binary, "-p", "/quota", "--output-format", "json"], capture_output=True, text=True,
                              timeout=AGY_READ_TIMEOUT, check=False, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return "auth_required" if _agy_auth_required(exc.stdout, exc.stderr) else "timeout"
    except OSError:
        return "agy_missing"
    if _agy_auth_required(done.stdout, done.stderr):
        return "auth_required"
    reading = _json_document(done.stdout)
    if reading is None or reading.get("status") != "SUCCESS":
        return "no_reading"
    return reading


def _agy_auth_required(*outputs: str | bytes | None) -> bool:
    return any("Authentication required" in (output.decode(errors="replace") if isinstance(output, bytes) else output or "")
               for output in outputs)


def read_codex() -> Pool:
    """Ask the Codex CLI for current account limits; never creates a thread, runs a turn or touches credentials."""
    binary = os.environ.get("JARVISOS_CODEX_BIN") or _host_which("codex")
    if not binary:
        return unreadable("codex", "codex_missing")
    try:
        proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                cwd=tempfile.gettempdir())
    except OSError:
        return unreadable("codex", "codex_missing")
    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def collect() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                data = _json_document(line)
                if data is not None:
                    messages.put(data)
        finally:
            messages.put(None)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    deadline = time.monotonic() + READ_TIMEOUT

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def response(request_id: int) -> dict[str, Any] | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            message = messages.get(timeout=remaining)
            if message is None or (message.get("id") == request_id and "error" in message):
                return None
            if message.get("id") == request_id:
                result = message.get("result")
                return result if isinstance(result, dict) else None

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "worker_routing", "version": "0.1.0"}}})
        if response(1) is None:
            return unreadable("codex", "no_reading")
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = response(2)
        if result is None:
            return unreadable("codex", "no_reading")
        by_id = result.get("rateLimitsByLimitId")
        limits = by_id.get("codex") if isinstance(by_id, dict) else None
        return parse_codex(limits if isinstance(limits, dict) else result.get("rateLimits"))
    except queue.Empty:
        return unreadable("codex", "timeout")
    except (OSError, ValueError):
        return unreadable("codex", "no_reading")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=1)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


READERS: dict[str, Callable[[], Pool]] = {
    "codex": read_codex, "gemini": lambda: read_agy("gemini"), "copilot": read_copilot, "claude": read_claude,
    "argon": lambda: read_agy("argon"), "agy3p": lambda: read_agy("agy3p"),
}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _repo_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists():
        return root
    cwd = Path.cwd()
    if (cwd / ".git").exists():
        return cwd
    return root


def default_snapshot_paths() -> list[Path]:
    """Where the supervisor's snapshot may be: this checkout's `.agent-relay`, and, when this is a linked
    worktree, the main checkout's (the supervisor runs against the main checkout)."""
    root = _repo_root()
    paths = [root / SNAPSHOT_NAME]
    marker = root / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.startswith("gitdir:"):
            gitdir = (root / text[len("gitdir:"):].strip()).resolve()
            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                paths.append(gitdir.parent.parent.parent / SNAPSHOT_NAME)
    return paths


def default_record_path() -> Path:
    """Where worker outcomes are recorded: in a linked worktree, the main checkout's .agent-relay/worker-outcomes.jsonl."""
    root = _repo_root()
    marker = root / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.startswith("gitdir:"):
            gitdir = (root / text[len("gitdir:"):].strip()).resolve()
            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                return gitdir.parent.parent.parent / RECORD_NAME
    return root / RECORD_NAME


def resolve_record_path(path: Path | None = None) -> Path:
    return path if path is not None else default_record_path()


def _parse_findings(findings: Any) -> tuple[int, int, int, int]:
    if isinstance(findings, dict):
        try:
            return (int(findings.get("P0", 0)), int(findings.get("P1", 0)),
                    int(findings.get("P2", 0)), int(findings.get("P3", 0)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid findings: {findings}") from exc
    if isinstance(findings, (list, tuple)) and len(findings) == 4:
        try:
            return int(findings[0]), int(findings[1]), int(findings[2]), int(findings[3])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid findings: {findings}") from exc
    if isinstance(findings, str):
        parts = [p.strip() for p in findings.split(",") if p.strip()]
        if len(parts) == 4:
            try:
                nums = [int(p.split("=")[-1]) for p in parts]
                return nums[0], nums[1], nums[2], nums[3]
            except ValueError as exc:
                raise ValueError(f"invalid findings: {findings}") from exc
    raise ValueError(f"invalid findings: {findings}")


def _record_findings_all(r: dict[str, Any]) -> tuple[int, int, int, int]:
    f = r.get("findings")
    if isinstance(f, dict):
        try:
            return int(f.get("P0", 0)), int(f.get("P1", 0)), int(f.get("P2", 0)), int(f.get("P3", 0))
        except (TypeError, ValueError):
            return 0, 0, 0, 0
    if isinstance(f, (list, tuple)) and len(f) >= 4:
        try:
            return int(f[0]), int(f[1]), int(f[2]), int(f[3])
        except (TypeError, ValueError):
            return 0, 0, 0, 0
    return 0, 0, 0, 0


def _rework_score(record: dict[str, Any]) -> float:
    p0, p1, _, _ = _record_findings_all(record)
    rework = record.get("rework_rounds", 0)
    try:
        rework_int = int(rework)
    except (TypeError, ValueError):
        rework_int = 0
    return float(rework_int + p0 + p1)


def family_of_record(r: dict[str, Any], by_key: dict[str, Model]) -> str:
    worker = str(r.get("worker", ""))
    if worker in by_key:
        return family(by_key[worker])
    model_name = str(r.get("model", ""))
    if model_name.startswith("claude"):
        return "claude"
    if model_name.startswith("gemini"):
        return "gemini"
    if model_name.startswith("gpt"):
        return "codex"
    return str(r.get("pool", ""))


def pool_of_record(r: dict[str, Any], by_key: dict[str, Model]) -> str:
    worker = str(r.get("worker", ""))
    if worker in by_key:
        return by_key[worker].pool
    model_name = str(r.get("model", ""))
    for m in MODELS:
        if m.model == model_name:
            return m.pool
    fam = family_of_record(r, by_key)
    if fam:
        return fam
    return str(r.get("pool") or "sconosciuto")


def load_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Valid outcome records from the record file; malformed or unreadable lines are skipped."""
    target = resolve_record_path(path)
    if not target.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(entry, dict):
                    records.append(entry)
    except OSError:
        return []
    return records


def default_control_dir() -> Path:
    return Path(os.environ.get("RELAY_CONTINUITY_DIR", Path.home() / "jarvis-control" / "work" / "continuity")).expanduser()


_classify_turn_fn: Any = None


def _get_classify_turn() -> Any:
    global _classify_turn_fn
    if _classify_turn_fn is not None:
        return _classify_turn_fn
    if "relay_continuity_turns" in sys.modules:
        _classify_turn_fn = getattr(sys.modules["relay_continuity_turns"], "classify_turn", None)
        if _classify_turn_fn is not None:
            return _classify_turn_fn
    turns_path = Path(__file__).resolve().parent / "relay_continuity_turns.py"
    if turns_path.is_file():
        try:
            spec = importlib.util.spec_from_file_location("relay_continuity_turns", turns_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["relay_continuity_turns"] = mod
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    sys.modules.pop("relay_continuity_turns", None)
                    raise
                _classify_turn_fn = getattr(mod, "classify_turn", None)
                return _classify_turn_fn
        except Exception:  # noqa: BLE001 - report must never raise on import failure
            pass
    return None


def _resolve_session_turns_dir(repo: Path, session: str) -> Path | None:
    if not session or "/" in session or "\\" in session or session in (".", ".."):
        return None
    candidate = repo / ".agent-relay" / "sessions" / session / "turns"
    if candidate.is_dir():
        return candidate
    marker = repo / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                main_git = Path(text.split(":", 1)[1].strip()).resolve().parent.parent
                main_cand = main_git.parent / ".agent-relay" / "sessions" / session / "turns"
                if main_cand.is_dir():
                    return main_cand
        except OSError:
            pass
    return None


def load_coordinator_turns(control_dir: Path | None = None, repo: Path | None = None, *,
                           days: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
    try:
        c_dir = default_control_dir() if control_dir is None else Path(control_dir).expanduser()
        r_dir = _repo_root() if repo is None else Path(repo).expanduser()
    except OSError:
        return []
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=days)

    attempt_files: list[Path] = []
    root_attempts = c_dir / "attempts.jsonl"
    if root_attempts.is_file():
        attempt_files.append(root_attempts)
    missions_dir = c_dir / "missions"
    if missions_dir.is_dir():
        try:
            attempt_files.extend(sorted(missions_dir.glob("m-*/attempts.jsonl")))
        except OSError:
            pass

    seen: set[tuple[str | None, str | None]] = set()
    turns: list[dict[str, Any]] = []

    for af in attempt_files:
        try:
            fh = open(af, encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            continue
        with fh:
            for raw_line in fh:
                try:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(rec, dict) or rec.get("event") != "settle":
                        continue

                    raw_agent = rec.get("agent")
                    agent = raw_agent if isinstance(raw_agent, str) and raw_agent else None
                    raw_session = rec.get("session")
                    session = raw_session if isinstance(raw_session, str) and raw_session else None
                    if agent is None and session is None:
                        continue

                    raw_nonce = rec.get("nonce")
                    nonce = raw_nonce if isinstance(raw_nonce, str) and raw_nonce else None

                    at_val = rec.get("at")
                    ts = (_parse_instant(at_val) or _parse_ts(at_val)) if isinstance(at_val, str) and at_val else None
                    if ts is None and nonce is not None:
                        m = re.search(r"rc-(\d{8}T\d{6}Z)-", nonce)
                        if m:
                            try:
                                ts = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                            except ValueError:
                                ts = None
                    if ts is None or ts < cutoff:
                        continue

                    if nonce is not None or session is not None:
                        key = (nonce, session)
                        if key in seen:
                            continue
                        seen.add(key)

                    usage: dict[str, int] | None = None
                    total_tokens = 0
                    rec_usage = rec.get("usage")
                    rec_coord = rec.get("coordinator_tokens")

                    if (isinstance(rec_usage, dict)
                            and isinstance(rec_usage.get("total_tokens"), int)
                            and not isinstance(rec_usage["total_tokens"], bool)
                            and rec_usage["total_tokens"] >= 0):
                        usage = rec_usage
                        total_tokens = rec_usage["total_tokens"]
                    elif (isinstance(rec_coord, int)
                          and not isinstance(rec_coord, bool)
                          and rec_coord >= 0):
                        total_tokens = rec_coord
                        usage = {
                            "input_tokens": rec_coord,
                            "output_tokens": 0,
                            "cache_tokens": 0,
                            "total_tokens": rec_coord,
                        }
                    elif r_dir is not None and session is not None:
                        sess_turns = _resolve_session_turns_dir(r_dir, session)
                        if sess_turns is not None and sess_turns.is_dir():
                            try:
                                t_dirs = sorted(
                                    p for p in sess_turns.iterdir() if p.is_dir() and p.name.startswith("turn-")
                                )
                            except OSError:
                                t_dirs = []
                            if t_dirs:
                                last_td = t_dirs[-1]
                                fn = _get_classify_turn()
                                if fn is not None:
                                    try:
                                        rep = fn(last_td)
                                        if (rep and isinstance(rep.usage, dict)
                                                and isinstance(rep.usage.get("total_tokens"), int)
                                                and not isinstance(rep.usage["total_tokens"], bool)
                                                and rep.usage["total_tokens"] >= 0):
                                            usage = {
                                                "input_tokens": rep.usage.get("input_tokens", 0),
                                                "output_tokens": rep.usage.get("output_tokens", 0),
                                                "cache_tokens": rep.usage.get("cache_tokens", 0),
                                                "total_tokens": rep.usage.get("total_tokens", 0),
                                            }
                                            total_tokens = usage["total_tokens"]
                                        if agent is None and rep and isinstance(rep.agent, str) and rep.agent:
                                            agent = rep.agent
                                    except Exception:  # noqa: BLE001 - report must never raise on malformed turn dir
                                        pass

                    if agent is None and r_dir is not None and session is not None:
                        sess_turns = _resolve_session_turns_dir(r_dir, session)
                        if sess_turns is not None and sess_turns.is_dir():
                            try:
                                t_dirs = sorted(
                                    p for p in sess_turns.iterdir() if p.is_dir() and p.name.startswith("turn-")
                                )
                            except OSError:
                                t_dirs = []
                            if t_dirs:
                                try:
                                    st = json.loads((t_dirs[-1] / "state.json").read_text(encoding="utf-8"))
                                    if isinstance(st, dict) and isinstance(st.get("agent_key"), str) and st["agent_key"]:
                                        agent = st["agent_key"]
                                except (OSError, json.JSONDecodeError):
                                    pass

                    pool = COORDINATOR_AGENT_POOLS.get(agent or "", agent or "sconosciuto")
                    at_iso = at_val if isinstance(at_val, str) and at_val else _iso(ts)
                    turns.append({
                        "at": at_iso,
                        "agent": agent,
                        "pool": pool,
                        "session": session,
                        "tokens": total_tokens,
                        "usage": usage,
                    })
                except Exception:  # noqa: BLE001 - malformed journal record must never abort report
                    continue

    return turns


def record(task: str, kind: str, worker: str, *, tier: int | None = None, ci_first_pass: str = "n/a",
           findings: Any = "0,0,0,0", rework_rounds: int = 0, escalated: bool = False,
           salvage: str = "n/a", reason: str | None = None, failure: str = "none",
           coordinator_tokens: int | None = None, evidence: list[str | Path] | str | Path | None = None,
           record_file: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Append one worker outcome record atomically. Refuses invalid input."""
    by_key = {m.key: m for m in all_models(load_records(record_file))}
    if worker not in by_key:
        raise ValueError(f"unknown worker: {worker}")
    model_obj = by_key[worker]
    model = model_obj.model

    if tier is None:
        if worker == "copilot-auto":
            tier_val = copilot_effective_capability(load_records(record_file))
        else:
            tier_val = model_obj.capability
    else:
        try:
            tier_val = int(tier)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid tier: {tier}") from exc
        if tier_val not in (1, 2, 3):
            raise ValueError(f"invalid tier: {tier}")

    if kind not in VALID_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if failure not in VALID_FAILURES:
        raise ValueError(f"unknown failure: {failure}")
    if salvage not in VALID_SALVAGES:
        raise ValueError(f"unknown salvage: {salvage}")
    if salvage == "restarted" and not (reason and str(reason).strip()):
        raise ValueError("salvage 'restarted' requires a non-empty reason")
    if ci_first_pass not in VALID_FIRST_PASS:
        raise ValueError(f"unknown ci_first_pass: {ci_first_pass}")

    p0, p1, p2, p3 = _parse_findings(findings)

    try:
        rework_int = int(rework_rounds)
        if rework_int < 0:
            raise ValueError(f"invalid rework_rounds: {rework_rounds}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid rework_rounds: {rework_rounds}") from exc

    tokens_val: int | None = None
    if coordinator_tokens is not None:
        try:
            tokens_val = int(coordinator_tokens)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid coordinator_tokens: {coordinator_tokens}") from exc

    spend_data: dict[str, Any] | None = None
    actual_models: list[str] = []
    if evidence is not None:
        raw_list = [evidence] if isinstance(evidence, (str, Path)) else list(evidence)
        if raw_list:
            seen_evidence: set[Path] = set()
            total_tokens = 0
            clean_tokens = 0
            sum_quota = 0.0
            quota_runs = 0
            overlap_runs = 0
            for item in raw_list:
                item_path = Path(item)
                res_path = item_path / "result.json" if item_path.is_dir() else item_path
                if not res_path.is_file():
                    raise ValueError(f"evidence result missing or unreadable: {item}")
                resolved = res_path.resolve()
                if resolved in seen_evidence:
                    raise ValueError(f"duplicate evidence path: {item}")
                seen_evidence.add(resolved)
                try:
                    res_dict = json.loads(res_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise ValueError(f"evidence result missing or unreadable: {item}") from exc
                if not isinstance(res_dict, dict):
                    raise ValueError(f"evidence result missing or unreadable: {item}")
                act = res_dict.get("actual_model")
                if isinstance(act, str) and act.strip():
                    cleaned = act.strip()
                    if cleaned not in actual_models:
                        actual_models.append(cleaned)
                usage = res_dict.get("usage")
                run_tokens = 0
                if isinstance(usage, dict):
                    tot = usage.get("total_tokens", 0)
                    if isinstance(tot, int) and not isinstance(tot, bool) and tot >= 0:
                        run_tokens = tot
                        total_tokens += tot
                overlap = res_dict.get("overlap")
                if overlap is True:
                    overlap_runs += 1
                elif overlap is False:
                    qb = res_dict.get("quota")
                    qa = res_dict.get("quota_after")
                    if isinstance(qb, list) and isinstance(qa, list):
                        bb = next((b for b in qb if isinstance(b, dict) and b.get("id") == "gemini-weekly"), None)
                        ba = next((b for b in qa if isinstance(b, dict) and b.get("id") == "gemini-weekly"), None)
                        if bb and ba:
                            fb = bb.get("remaining_fraction")
                            fa = ba.get("remaining_fraction")
                            if (isinstance(fb, (int, float)) and not isinstance(fb, bool) and math.isfinite(fb)
                                    and 0.0 <= float(fb) <= 1.0
                                    and isinstance(fa, (int, float)) and not isinstance(fa, bool) and math.isfinite(fa)
                                    and 0.0 <= float(fa) <= 1.0):
                                delta = float(fb) - float(fa)
                                if delta >= 0.0:
                                    sum_quota += delta
                                    quota_runs += 1
                                    clean_tokens += run_tokens
            spend_data = {
                "runs": len(raw_list),
                "tokens": total_tokens,
                "quota": round(sum_quota, 6),
                "quota_runs": quota_runs,
                "overlap_runs": overlap_runs,
            }
            if 0 < quota_runs < len(raw_list):
                spend_data["clean_tokens"] = clean_tokens

    moment = now or datetime.now(UTC)
    entry: dict[str, Any] = {
        "at": _iso(moment),
        "task": str(task),
        "kind": kind,
        "worker": worker,
        "model": model,
        "tier": tier_val,
        "ci_first_pass": ci_first_pass,
        "findings": {"P0": p0, "P1": p1, "P2": p2, "P3": p3},
        "rework_rounds": rework_int,
        "escalated": bool(escalated),
        "salvage": salvage,
        "reason": str(reason).strip() if salvage == "restarted" else None,
        "failure": failure,
    }
    if tokens_val is not None:
        entry["coordinator_tokens"] = tokens_val
    if spend_data is not None:
        entry["spend"] = spend_data
    if actual_models:
        entry["actual_model"] = ",".join(actual_models)

    target = resolve_record_path(record_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(target, flags, 0o644)
    try:
        written = 0
        while written < len(line):
            n = os.write(fd, line[written:])
            if n == 0:
                raise OSError("os.write returned 0 bytes")
            written += n
    finally:
        os.close(fd)

    return entry


def cmd_record(args: argparse.Namespace) -> int:
    try:
        record(
            task=args.task,
            kind=args.kind,
            worker=args.worker,
            tier=args.tier,
            ci_first_pass=args.ci_first_pass,
            findings=args.findings,
            rework_rounds=args.rework_rounds,
            escalated=args.escalated,
            salvage=args.salvage,
            reason=args.reason,
            failure=args.failure,
            coordinator_tokens=args.coordinator_tokens,
            evidence=args.evidence,
            record_file=args.record_file,
        )
        return 0
    except (ValueError, TypeError, OSError) as exc:
        sys.stderr.write(f"record: {exc}\n")
        return 2


def load_snapshot(paths: list[Path]) -> dict[str, tuple[datetime, dict[str, float]]]:
    """Valid per-pool entries (as_of, windows) from the given snapshot files, the freshest per pool. Anything
    malformed is ignored."""
    best: dict[str, tuple[datetime, dict[str, float]]] = {}
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA or not isinstance(data.get("pools"), dict):
            continue
        for name, entry in data["pools"].items():
            if not isinstance(entry, dict) or not isinstance(entry.get("windows"), dict) or not entry["windows"]:
                continue
            at = _parse_ts(entry.get("as_of"))
            windows = {str(k): _fraction(v) for k, v in entry["windows"].items()}
            if at is None or any(v is None for v in windows.values()):
                continue
            if name not in best or at > best[name][0]:
                best[name] = (at, {k: v for k, v in windows.items() if v is not None})
    return best


def apply_snapshot(pools: dict[str, Pool], snapshot: dict[str, tuple[datetime, dict[str, float]]],
                   now: datetime, max_age: float = SNAPSHOT_MAX_AGE) -> dict[str, Pool]:
    """Pools that could not be read live fall back to a snapshot entry no older than `max_age`. A live reading
    always wins, and a pool the provider reported as disabled never falls back. Older or missing: unchanged."""
    out = dict(pools)
    for name, pool in pools.items():
        if (name == "copilot" or pool.free is not None or name not in snapshot
                or (pool.reason or "").endswith("_disabled")):
            continue
        at, windows = snapshot[name]
        if -60 <= (now - at).total_seconds() <= max_age:
            out[name] = Pool(name, windows, min(windows.values()), None, "snapshot")
    return out


def write_snapshot(path: Path, pools: dict[str, Pool], now: datetime | None = None) -> bool:
    """Persist the live-read pools atomically, keeping other pools' older entries. Writes nothing when no pool
    was read live. Never raises."""
    live = {n: p for n, p in pools.items() if p.free is not None and p.source == "live"}
    if not live:
        return False
    moment = now or datetime.now(UTC)
    entries: dict[str, Any] = {n: {"as_of": _iso(at), "windows": w} for n, (at, w) in load_snapshot([path]).items()}
    for name, pool in live.items():
        entries[name] = {"as_of": _iso(moment), "windows": {k: round(v, 4) for k, v in pool.windows.items()}}
    document = {"schema": SNAPSHOT_SCHEMA, "as_of": _iso(moment), "pools": entries}
    temp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".worker-quota.",
                                         suffix=".tmp", delete=False) as handle:
            temp = handle.name
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp, 0o644)
        os.replace(temp, path)
        return True
    except OSError:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
        return False


def read_pools(readers: dict[str, Callable[[], Pool]] | None = None, timeout: float = TOTAL_TIMEOUT) -> dict[str, Pool]:
    """Every pool that MODELS uses, read in parallel within one bounded wait. A reader that raises or is still
    running at the deadline leaves its pool unreadable; nothing here ever hangs or raises."""
    chosen = dict(READERS if readers is None else readers)
    temp_entries = {f"temp-{entry['name']}": entry for entry in load_temp_pools()}
    for name, entry in temp_entries.items():
        chosen[name] = lambda entry=entry: temp_pool_read(entry)
    names = list(dict.fromkeys(m.pool for m in all_models()))
    with _agy_lock:
        _agy_doc["at"] = None  # the agy pools share one fresh reading per call
    out: dict[str, Pool] = {name: unreadable(name, "timeout") for name in names}

    def run(name: str) -> None:
        try:
            reader = chosen.get(name)
            out[name] = reader() if reader else unreadable(name, "no_reader")
        except Exception:  # noqa: BLE001 - a broken reader makes only its pool unavailable
            out[name] = unreadable(name, "reader_error")

    threads = [threading.Thread(target=run, args=(name,), daemon=True) for name in names]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return dict(out)


def resolve_pools(quota_files: list[Path] | None = None, now: datetime | None = None) -> dict[str, Pool]:
    """Live readings first; a pool unreadable live falls back to the supervisor's snapshot when fresh enough."""
    pools = read_pools()
    if all(p.free is not None for p in pools.values()):
        return pools
    return apply_snapshot(pools, load_snapshot(quota_files or default_snapshot_paths()), now or datetime.now(UTC))


def pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _is_clean_spend(spend: dict[str, Any]) -> bool:
    runs = spend.get("runs", 0)
    quota_runs = spend.get("quota_runs", 0)
    overlap_runs = spend.get("overlap_runs", 0)
    return (isinstance(runs, int) and isinstance(quota_runs, int) and isinstance(overlap_runs, int)
            and overlap_runs == 0 and quota_runs == runs and runs > 0)


def _clean_quota_and_tokens(spend: dict[str, Any]) -> tuple[float, int] | None:
    if _is_clean_spend(spend):
        tokens = spend.get("tokens")
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and math.isfinite(tokens) and int(tokens) > 0:
            quota = spend.get("quota", 0.0)
            if isinstance(quota, (int, float)) and not isinstance(quota, bool) and math.isfinite(quota) and float(quota) >= 0.0:
                return float(quota), int(tokens)
        return None
    quota_runs = spend.get("quota_runs", 0)
    clean_tok = spend.get("clean_tokens")
    if (isinstance(quota_runs, int) and not isinstance(quota_runs, bool) and quota_runs > 0
            and isinstance(clean_tok, (int, float)) and not isinstance(clean_tok, bool) and math.isfinite(clean_tok) and int(clean_tok) > 0):
        quota = spend.get("quota", 0.0)
        if isinstance(quota, (int, float)) and not isinstance(quota, bool) and math.isfinite(quota) and float(quota) >= 0.0:
            return float(quota), int(clean_tok)
    return None


def candidate_measured_cost(candidate: Model, kind: str | None, outcomes: list[dict[str, Any]] | None) -> tuple[float | None, int]:
    """Median measured quota per accepted task of the requested kind (or across all records when kind is None;
    failure != harness, with spend). Returns (median_cost or None, count of costed records). Measured when count >= 5."""
    if candidate.kind != "agy" or not outcomes:
        return None, 0

    c_records = [
        r for r in outcomes
        if (r.get("worker") == candidate.key or r.get("model") == candidate.model)
        and (kind is None or r.get("kind") == kind)
        and r.get("failure") != "harness"
        and isinstance(r.get("spend"), dict)
    ]
    if not c_records:
        return None, 0

    all_spend_records = [
        r for r in outcomes
        if (r.get("worker") == candidate.key or r.get("model") == candidate.model)
        and isinstance(r.get("spend"), dict)
    ]
    clean_pairs = [
        pair for r in all_spend_records
        if (pair := _clean_quota_and_tokens(r["spend"])) is not None
    ]
    clean_quota = sum(q for q, _ in clean_pairs)
    clean_tokens = sum(t for _, t in clean_pairs)
    ratio = (clean_quota / clean_tokens) if (clean_pairs and clean_tokens > 0) else None

    costs: list[float] = []
    for r in c_records:
        if r.get("failure") == "model" or bool(r.get("escalated")):
            costs.append(math.inf)
        elif _is_clean_spend(r["spend"]):
            quota_val = r["spend"].get("quota", 0.0)
            if isinstance(quota_val, (int, float)) and not isinstance(quota_val, bool) and math.isfinite(quota_val) and float(quota_val) >= 0.0:
                costs.append(float(quota_val))
        elif ratio is not None:
            raw_tok = r["spend"].get("tokens")
            tokens = int(raw_tok) if isinstance(raw_tok, (int, float)) and not isinstance(raw_tok, bool) and math.isfinite(raw_tok) else 0
            if tokens > 0:
                costs.append(tokens * ratio)
        else:
            # no clean run -> that record has no cost
            pass

    count = len(costs)
    if count < 5:
        return None, count

    return float(statistics.median(costs)), count


def copilot_effective_capability(outcomes: list[dict[str, Any]] | None, base_capability: int = 1, *,
                                 worker: str = "copilot-auto", model: str = "auto", sparse: int | None = None) -> int:
    """A tier from recorded outcomes. Built for Copilot; temporary pools reuse it with their own worker and model,
    and `sparse` (their index start tier) is kept while fewer than two tier-2 records exist."""
    if not outcomes:
        return base_capability
    copilot_records = [
        r for r in outcomes
        if (r.get("worker") == worker or r.get("model") == model) and r.get("failure") != "harness"
    ]
    if not copilot_records:
        return base_capability

    def _rework_val(r: dict[str, Any]) -> int:
        val = r.get("rework_rounds", 0) or 0
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    def is_clean(r: dict[str, Any]) -> bool:
        return (
            r.get("failure", "none") == "none"
            and not bool(r.get("escalated"))
            and r.get("ci_first_pass") != "fail"
            and (r.get("ci_first_pass") == "pass" or _rework_val(r) == 0)
        )

    tier2_records = [
        r for r in copilot_records
        if isinstance(r.get("tier"), int) and not isinstance(r.get("tier"), bool) and r["tier"] >= 2
    ]
    if len(tier2_records) < 2:
        if sparse is not None:
            return sparse
        return 2 if (any(is_clean(r) for r in copilot_records) or len(tier2_records) == 1) else 1

    clean_count = sum(1 for r in tier2_records if is_clean(r))
    zero_rework_records = [r for r in tier2_records if is_clean(r) and _rework_val(r) == 0]
    zero_rework_count = len(zero_rework_records)
    if clean_count / len(tier2_records) < 0.50 or zero_rework_count == 0:
        return 1

    def _is_strong_actual_model(r: dict[str, Any]) -> bool:
        act = r.get("actual_model") or (model if worker != "copilot-auto" else None)
        if not isinstance(act, str) or not act.strip():
            return False
        for model_name in act.split(","):
            tokens = {t for t in re.split(r"[^a-z0-9]+", model_name.strip().lower()) if t}
            if tokens and not (tokens & {"flash", "mini", "haiku", "nano", "lite"}):
                return True
        return False

    if (
        len(tier2_records) >= 3
        and zero_rework_count >= 3
        and all(not bool(r.get("escalated")) and r.get("failure", "none") == "none" for r in tier2_records)
        and any(_is_strong_actual_model(r) for r in zero_rework_records)
    ):
        return 3

    return 2


def rank(required: int, pools: dict[str, Pool], models: tuple[Model, ...] | None = None, review_of: str | None = None,
         now: datetime | None = None, task: str | None = None, kind: str | None = None,
         outcomes: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(eligible, skipped) for a required tier. Eligible: another family than `review_of` first (never required),
    then smallest capability surplus (effort by task), the gemini family, the quota that would expire unused first,
    then most free quota."""
    now = now or datetime.now(UTC)
    if models is None:
        models = all_models(outcomes)
    by_key = {m.key: m for m in models}
    eligible: list[tuple[tuple[int, int, int, int, float, float, int], dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []

    failed_families: set[str] = set()
    if task is not None and outcomes:
        for r in outcomes:
            if r.get("task") == task and r.get("failure") == "model":
                fam = family_of_record(r, by_key)
                if fam:
                    failed_families.add(fam)

    def check_measured_inadequate(candidate: Model) -> bool:
        if kind is None or not outcomes:
            return False
        c_records = [
            r for r in outcomes
            if (r.get("worker") == candidate.key or r.get("model") == candidate.model)
            and r.get("kind") == kind
            and r.get("failure") != "harness"
        ]
        judged = [r for r in c_records if r.get("ci_first_pass") in ("pass", "fail")]  # n/a has no first pass
        if len(judged) < 5:
            return False
        passes = sum(1 for r in judged if r.get("ci_first_pass") == "pass")
        if passes / len(judged) >= 0.50:
            return False
        c_mean = sum(_rework_score(r) for r in c_records) / len(c_records)

        c_fam = family(candidate)
        next_family: str | None = None
        for m in models:
            m_cap = copilot_effective_capability(outcomes, m.capability) if m.key == "copilot-auto" else m.capability
            if m_cap < required:
                continue
            p = pools.get(m.pool) or unreadable(m.pool, "no_reading")
            if p.free is None or below_reserve(p, now):
                continue
            m_fam = family(m)
            if m_fam in failed_families:
                continue
            if m_fam != c_fam:
                next_family = m_fam
                break
        if next_family is None:
            return False
        next_records = [
            r for r in outcomes
            if family_of_record(r, by_key) == next_family
            and r.get("kind") == kind
            and r.get("failure") != "harness"
        ]
        if not next_records:
            return False
        next_mean = sum(_rework_score(r) for r in next_records) / len(next_records)
        return c_mean > next_mean

    for index, model in enumerate(models):
        eff_cap = copilot_effective_capability(outcomes, model.capability) if model.key == "copilot-auto" else model.capability
        if eff_cap == 0 and model.key.startswith("temp-"):
            pool = pools.get(model.pool) or unreadable(model.pool, "no_reading")
            reason = pool.reason or "unadmitted"  # expired / invalid / unindexed / below_index ...
            skipped.append({"worker": model.key, "kind": model.kind, "model": model.model, "pool": model.pool,
                            "family": family(model), "capability": 0,
                            "free": None if pool.free is None else round(pool.free, 4),
                            "source": pool.source, "measured_cost": None, "record_count": 0,
                            "skipped": reason})
            continue
        if eff_cap < required:
            continue
        pool = pools.get(model.pool) or unreadable(model.pool, "no_reading")
        cost, count = candidate_measured_cost(model, kind, outcomes)
        entry: dict[str, Any] = {"worker": model.key, "kind": model.kind, "model": model.model, "pool": model.pool,
                                 "family": family(model), "capability": eff_cap,
                                 "free": None if pool.free is None else round(pool.free, 4), "source": pool.source,
                                 "measured_cost": cost, "record_count": count}
        if pool.free is None:
            skipped.append({**entry, "skipped": pool.reason or "unreadable"})
        elif below_reserve(pool, now):
            skipped.append({**entry, "skipped": "below_reserve"})
        elif family(model) in failed_families:
            skipped.append({**entry, "skipped": "escalated_from_family"})
        elif check_measured_inadequate(model):
            skipped.append({**entry, "skipped": "measured_inadequate"})
        else:
            same = int(review_of is not None and family(model) == review_of)
            entry["same"] = same
            first = 0 if model.pool in FIRST_POOLS else 1
            paced = 0 if behind_pace(pool, now) else 1
            eligible.append(((same, eff_cap - required, paced, first, -expiry_score(pool, now), -pool.free,
                              index), entry))

    ordered = sorted(eligible, key=lambda item: item[0])
    today_sorted = [entry for _, entry in ordered]
    if ordered:
        unpaced = min(eligible, key=lambda item: item[0][:2] + item[0][3:])[1]
        if unpaced is not ordered[0][1]:
            ordered[0][1]["pacing"] = "behind_pace"
    result_list = list(today_sorted)

    for same_val in (0, 1):
        pools_in_partition = dict.fromkeys(e["pool"] for e in result_list if e.get("same", 0) == same_val)
        for p in pools_in_partition:
            measured_indices = [
                i for i, e in enumerate(result_list)
                if e.get("same", 0) == same_val and e["pool"] == p and e.get("measured_cost") is not None
            ]
            if len(measured_indices) <= 1:
                continue
            measured_entries = [result_list[i] for i in measured_indices]
            sorted_entries = sorted(
                measured_entries,
                key=lambda item: (item["measured_cost"], measured_indices[measured_entries.index(item)])
            )
            for idx, entry in zip(measured_indices, sorted_entries, strict=True):
                result_list[idx] = entry

    for e in result_list:
        e.pop("same", None)

    return result_list, skipped


def bare_command(model: Model) -> str:
    if model.kind == "codex":
        return f'codex exec --model {model.model} --cd "$WORKTREE" - < "$TASK_FILE"'
    if model.kind == "claude":
        # Headless, edits only: acceptEdits plus no permission prompts (anything that would prompt is denied).
        # Never bypassPermissions or --dangerously-skip-permissions for a worker.
        return (f'cd "$WORKTREE" && claude -p --model {model.model} --permission-mode acceptEdits '
                '--permission-prompts none --output-format json < "$TASK_FILE"')
    if model.kind == "copilot":
        return (f'python3 scripts/copilot_worker.py run --task-file "$TASK_FILE" '
                f'--worktree "$WORKTREE" --model {model.key}')
    if model.kind == "opencode":
        pool_name = model.pool[len("temp-"):] if model.pool.startswith("temp-") else model.pool
        return (f'python3 scripts/opencode_worker.py run --pool {pool_name} '
                f'--task-file "$TASK_FILE" --worktree "$WORKTREE"')
    return f'python3 scripts/agy_worker.py run --task-file "$TASK_FILE" --worktree "$WORKTREE" --model {model.key}'


def launch_command(model: Model) -> str:
    # --input digests the task file, so a new task for the same worker and worktree is not refused as a duplicate.
    return ('python3 scripts/relay_jobs.py start --label "$LABEL" --authority "$AUTHORITY" --spec "$SPEC" '
            '--wall-hours "${WALL_HOURS:-1}" --cwd "$WORKTREE" --input "$TASK_FILE" -- bash -c '
            + shlex.quote(bare_command(model)))


def decide(difficulty: str, risk: str, pools: dict[str, Pool], review_of: str | None = None,
           task: str | None = None, kind: str | None = None, outcomes: list[dict[str, Any]] | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    required = tier(difficulty, risk)
    models = all_models(outcomes)
    eligible, skipped = rank(required, pools, models=models, review_of=review_of, now=now,
                             task=task, kind=kind, outcomes=outcomes)
    decision: dict[str, Any] = {"difficulty": difficulty, "risk": risk, "required_tier": required,
                                "kind": kind, "review": REVIEW[risk], "reserves": RESERVES}
    by_key = {m.key: m for m in models}
    if not eligible:
        reason = "wait_for_escalation_pool" if any(s.get("skipped") == "escalated_from_family" for s in skipped) else "no_worker"
        return {**decision, "status": "no_worker", "reason": reason, "skipped": skipped}
    chosen = eligible[0]
    low = chosen["pool"] == "claude" and chosen["free"] < CLAUDE_LOW_FREE
    return {**decision, "status": "ok", "worker": chosen["worker"], "model": chosen["model"], "pool": chosen["pool"],
            "source": chosen["source"], "command": launch_command(by_key[chosen["worker"]]),
            "claude_pool_low": low, **({"note": CLAUDE_LOW_NOTE} if low else {}),
            "measured_cost": chosen.get("measured_cost"), "record_count": chosen.get("record_count", 0),
            **({"pacing": chosen["pacing"]} if chosen.get("pacing") else {}),
            "alternatives": eligible[1:], "skipped": skipped}


def summary_line(pools: dict[str, Pool], outcomes: list[dict[str, Any]] | None = None) -> str:
    parts = []
    models = all_models(outcomes)
    for name in dict.fromkeys(m.pool for m in models):
        pool = pools.get(name) or unreadable(name, "no_reading")
        if name.startswith("temp-") and pool.reason == "expired":
            continue  # an expired offer leaves the summary by itself
        if pool.free is None:
            parts.append(f"{name} unavailable ({pool.reason or 'unreadable'})")
            continue
        if name.startswith("temp-"):
            entry = next((e for e in load_temp_pools() if f"temp-{e['name']}" == name), None)
            score = f", index {entry['index_score']}" if entry and entry.get("index_score") is not None else ""
            expires = pool.resets.get("expires")
            left = (f", {max(0.0, (expires - datetime.now(UTC)).total_seconds() / 3600):.1f}h left"
                    if expires is not None else "")
            parts.append(f"{name} 100% free (listed{score}{left})")
            continue
        short = f" (5h {pct(pool.windows['5h'])})" if "5h" in pool.windows else ""
        low = " [below reserve]" if below_reserve(pool) else ""
        stale = " [snapshot]" if pool.source == "snapshot" else ""
        parts.append(f"{name} {pct(pool.free)} free{short}{low}{stale}")
    tiers = ", ".join(
        f"{m.key} t{copilot_effective_capability(outcomes, m.capability) if m.key == 'copilot-auto' else m.capability}"
        for m in models
        if not (m.pool.startswith("temp-") and (pools.get(m.pool) or unreadable(m.pool, "")).reason == "expired")
    )
    return ("worker quota: " + " · ".join(parts) + " — effort by task tier, gemini first, then quota expiring "
            "unused; tiers: " + tiers)


_cache: dict[str, Any] = {"at": None, "line": None, "ttl": 0.0}


def cached_summary(ttl: float = CACHE_TTL, clock: Callable[[], float] = time.monotonic,
                   read: Callable[[], dict[str, Pool]] | None = None, snapshot_file: Path | None = None) -> str:
    """One compact quota line for a supervisor prompt, cached for `ttl` seconds. Never raises: any failure is the
    line `worker quota: unreadable`, itself cached briefly so a broken reader is not retried on every turn.
    With `snapshot_file` (the supervisor), a successful live reading is also persisted there for coordinators."""
    now = clock()
    if _cache["at"] is not None and now - _cache["at"] < _cache["ttl"]:
        return str(_cache["line"])
    try:
        pools = (read or read_pools)()
        if snapshot_file is not None:
            write_snapshot(snapshot_file, pools)
        line = summary_line(pools)
        lifetime = ttl if any(p.free is not None for p in pools.values()) else min(ttl, FAILED_CACHE_TTL)
    except Exception:  # noqa: BLE001 - the prompt must never depend on a reading
        line, lifetime = UNREADABLE, min(ttl, FAILED_CACHE_TTL)
    _cache.update(at=now, line=line, ttl=lifetime)
    return line


def _format_cost(cost: float | None, count: int) -> str:
    if cost is None:
        return f"cost unmeasured, {count} records"
    if math.isinf(cost):
        return f"cost inf, {count} records"
    return f"cost {round(cost, 4)}, {count} records"


def render(decision: dict[str, Any]) -> str:
    if decision["status"] != "ok":
        skipped = ", ".join(f"{s['worker']}({s['skipped']})" for s in decision["skipped"]) or "none adequate"
        prefix = f"no_worker ({decision['reason']})" if decision.get("reason") and decision["reason"] != "no_worker" else "no_worker"
        return (f"{prefix}: nothing adequate for tier {decision['required_tier']} "
                f"(difficulty {decision['difficulty']}, risk {decision['risk']}); skipped: {skipped}\n"
                "The coordinator does the work itself or waits.")
    show_measured = decision.get("kind") is not None or decision.get("measured_cost") is not None or bool(decision.get("record_count"))
    worker_desc = f"{decision['model']}, pool {decision['pool']}"
    if decision["source"] == "snapshot":
        worker_desc += ", quota from snapshot"
    if show_measured:
        worker_desc += f", {_format_cost(decision.get('measured_cost'), decision.get('record_count', 0))}"
    lines = [f"worker: {decision['worker']} ({worker_desc})",
             f"tier: {decision['required_tier']} (difficulty {decision['difficulty']}, risk {decision['risk']})",
             "launch requires exported absolute WORKTREE and TASK_FILE, LABEL (1-80 letters, digits, spaces or _.:/+-), AUTHORITY, SPEC; optional WALL_HOURS (default 1):",
             f"launch: {decision['command']}",
             f"review: {decision['review']}"]
    if decision.get("note"):
        lines.append(f"note: {decision['note']}")
    if decision.get("pacing"):
        lines.append("pacing: monthly pool behind the elapsed share of its month, spent before the reset")
    if decision["alternatives"]:
        alt_strs = []
        for a in decision["alternatives"]:
            entry_desc = f"{a['pool']} {pct(a['free'])} free" + (" snapshot" if a.get("source") == "snapshot" else "")
            if show_measured:
                entry_desc += f", {_format_cost(a.get('measured_cost'), a.get('record_count', 0))}"
            alt_strs.append(f"{a['worker']} ({entry_desc})")
        lines.append("alternatives: " + ", ".join(alt_strs))
    if decision["skipped"]:
        lines.append("skipped: " + ", ".join(f"{s['worker']}({s['skipped']})" for s in decision["skipped"]))
    return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "inf" if value > 0 else ("-inf" if value < 0 else None)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def cmd_pick(args: argparse.Namespace) -> int:
    outcomes = load_records(args.record_file)
    decision = decide(args.difficulty, args.risk, resolve_pools(args.quota_file), args.review_of,
                      task=args.task, kind=args.kind, outcomes=outcomes)
    print(json.dumps(_json_safe(decision), indent=2, sort_keys=True, allow_nan=False) if args.json else render(decision))
    return 0 if decision["status"] == "ok" else NO_WORKER_EXIT


def report(days: int = 7, record_file: Path | None = None, records: list[dict[str, Any]] | None = None,
           now: datetime | None = None, *, control_dir: Path | None = None, repo: Path | None = None,
           coordinator_turns: list[dict[str, Any]] | None = None) -> str:
    """A short summary in plain Italian of worker outcomes and net savings over the last N days."""
    moment = now or datetime.now(UTC)
    all_records = records if records is not None else load_records(record_file)
    cutoff = moment - timedelta(days=days)

    filtered: list[dict[str, Any]] = []
    for r in all_records:
        at = _parse_instant(r.get("at")) or _parse_ts(r.get("at"))
        if at is not None and at >= cutoff:
            filtered.append(r)

    if coordinator_turns is not None:
        coord_turns = coordinator_turns
    elif control_dir is not None:
        coord_turns = load_coordinator_turns(control_dir, repo, days=days, now=moment)
    elif records is None and record_file is None:
        coord_turns = load_coordinator_turns(None, repo, days=days, now=moment)
    else:
        coord_turns = []

    if not filtered and not coord_turns:
        return f"Nessun esito registrato negli ultimi {days} giorni."

    by_key = {m.key: m for m in MODELS}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in filtered:
        m_name = str(r.get("model") or r.get("worker") or "sconosciuto")
        grouped.setdefault(m_name, []).append(r)

    lines: list[str] = []
    for m_name, group in grouped.items():
        total = len(group)
        judged = [r for r in group if r.get("ci_first_pass") in ("pass", "fail")]
        passes = sum(1 for r in judged if r.get("ci_first_pass") == "pass")
        pass_rate = pct(passes / len(judged)) if judged else "n/d"
        p0 = sum(_record_findings_all(r)[0] for r in group)
        p1 = sum(_record_findings_all(r)[1] for r in group)
        p2 = sum(_record_findings_all(r)[2] for r in group)
        p3 = sum(_record_findings_all(r)[3] for r in group)
        rework = round(sum(_rework_score(r) - sum(_record_findings_all(r)[:2]) for r in group))
        escalations = sum(1 for r in group if r.get("escalated"))
        w_key = group[0].get("worker")
        actual_counts: dict[str, int] = {}
        for r in group:
            act = r.get("actual_model")
            if isinstance(act, str) and act.strip():
                for tok in act.split(","):
                    tok = tok.strip()
                    if tok:
                        actual_counts[tok] = actual_counts.get(tok, 0) + 1

        if actual_counts:
            mod_str = ", ".join(f"{m}×{c}" if c > 1 else m for m, c in actual_counts.items())
            if w_key and w_key != m_name:
                w_tag = f" ({w_key}, modelli: {mod_str})"
            else:
                w_tag = f" (modelli: {mod_str})"
        else:
            w_tag = f" ({w_key})" if w_key and w_key != m_name else ""
        lines.append(
            f"- {m_name}{w_tag}: {total} compiti, prima prova {pass_rate}, "
            f"rilievi (P0: {p0}, P1: {p1}, P2: {p2}, P3: {p3}), rilavorazioni {rework}, escalation {escalations}"
        )

    if coord_turns:
        coord_by_pool: dict[str, list[dict[str, Any]]] = {}
        for t in coord_turns:
            coord_by_pool.setdefault(t["pool"], []).append(t)

        workers_by_pool: dict[str, list[dict[str, Any]]] = {}
        for r in filtered:
            p = pool_of_record(r, by_key)
            workers_by_pool.setdefault(p, []).append(r)

        all_pools = set(coord_by_pool.keys()) | set(workers_by_pool.keys())
        pool_order = [p for p in dict.fromkeys(COORDINATOR_AGENT_POOLS.values()) if p in all_pools]
        pool_order.extend(sorted(p for p in all_pools if p not in pool_order))

        for pool in pool_order:
            c_turns = len(coord_by_pool.get(pool, []))
            c_tokens = sum(t["tokens"] for t in coord_by_pool.get(pool, []))
            p_workers = workers_by_pool.get(pool, [])
            w_tasks = len(p_workers)
            w_tokens = sum(
                r["spend"]["tokens"]
                for r in p_workers
                if isinstance(r.get("spend"), dict)
                and isinstance(r["spend"].get("tokens"), int)
                and not isinstance(r["spend"].get("tokens"), bool)
            )
            if c_tokens + w_tokens > 0:
                coord_share = pct(c_tokens / (c_tokens + w_tokens))
                lines.append(
                    f"- Pool {pool}: coordinatore {c_turns} turni ({c_tokens} token, {coord_share}), "
                    f"worker {w_tasks} compiti ({w_tokens} token)"
                )
            else:
                lines.append(
                    f"- Pool {pool}: coordinatore {c_turns} turni ({c_tokens} token), "
                    f"worker {w_tasks} compiti (0 token)"
                )

    gemini_avoided = [
        r for r in filtered
        if family_of_record(r, by_key) == "gemini" and not r.get("escalated")
    ]
    avoided_count = len(gemini_avoided)
    record_coord_tokens = sum(
        r["coordinator_tokens"]
        for r in gemini_avoided
        if isinstance(r.get("coordinator_tokens"), int) and not isinstance(r["coordinator_tokens"], bool)
    )
    total_coord_tokens = sum(t["tokens"] for t in coord_turns) if coord_turns else record_coord_tokens

    lines.append(
        f"Risparmio netto: {avoided_count} compiti Claude e GPT evitati ({avoided_count} compiti Gemini non escalati), "
        f"meno {total_coord_tokens} token coordinatore registrati."
    )
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    print(report(days=args.days, record_file=args.record_file, control_dir=args.control_dir))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    print(cached_summary(read=lambda: resolve_pools(args.quota_file)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    quota_file = {"type": Path, "action": "append", "help": "snapshot file to fall back on (default: the supervisor's "
                                                           ".agent-relay/worker-quota.json)"}
    pick = sub.add_parser("pick", help="choose the cheapest adequate worker with quota headroom",
                          description=RISK_GUIDE)
    pick.add_argument("--difficulty", required=True, choices=sorted(LEVELS, key=LEVELS.get))
    pick.add_argument("--risk", required=True, choices=sorted(LEVELS, key=LEVELS.get), help=RISK_GUIDE)
    pick.add_argument("--json", action="store_true")
    pick.add_argument("--task", default=None, help="task id to route, checking previous worker outcomes for escalation")
    pick.add_argument("--kind", default=None, choices=VALID_KINDS, help="task kind to route")
    pick.add_argument("--review-of", choices=("claude", "codex", "gemini", "copilot"),
                      help="the author's model family: prefer another family for an independent review, never block on it")
    pick.add_argument("--quota-file", **quota_file)
    pick.add_argument("--record-file", type=Path, default=None,
                      help="record file of worker outcomes (default: main checkout .agent-relay/worker-outcomes.jsonl)")
    pick.set_defaults(func=cmd_pick)

    rec = sub.add_parser("record", help="append one worker outcome record to worker-outcomes.jsonl")
    rec.add_argument("--task", required=True, help="task id chosen by coordinator")
    rec.add_argument("--kind", required=True, help="kind of task (docs, tests, ui, feature, review, fix)")
    rec.add_argument("--worker", required=True, help="worker key from MODELS")
    rec.add_argument("--tier", type=int, choices=(1, 2, 3), default=None, help="tier of task (1..3)")
    rec.add_argument("--ci-first-pass", default="n/a", help="pass, fail, or n/a")
    rec.add_argument("--findings", default="0,0,0,0", help="counts P0,P1,P2,P3 (e.g. 0,1,0,0)")
    rec.add_argument("--rework-rounds", type=int, default=0, help="rework rounds count")
    rec.add_argument("--escalated", action=argparse.BooleanOptionalAction, default=False, help="whether task was escalated")
    rec.add_argument("--salvage", default="n/a", help="salvaged, restarted, or n/a")
    rec.add_argument("--reason", default=None, help="reason when salvage is restarted")
    rec.add_argument("--failure", default="none", help="none, harness, or model")
    rec.add_argument("--coordinator-tokens", type=int, default=None, help="coordinator tokens recorded on task")
    rec.add_argument("--evidence", action="append", default=None,
                     help="result.json directory of an agy worker run contributing to the task")
    rec.add_argument("--record-file", type=Path, default=None,
                     help="record file of worker outcomes (default: main checkout .agent-relay/worker-outcomes.jsonl)")
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("report", help="summary report of worker outcomes and net savings in Italian")
    rep.add_argument("--days", type=int, default=7, help="number of days to report (default: 7)")
    rep.add_argument("--record-file", type=Path, default=None,
                     help="record file of worker outcomes (default: main checkout .agent-relay/worker-outcomes.jsonl)")
    rep.add_argument("--control-dir", type=Path, default=None,
                     help="control directory of continuity missions (default: ~/jarvis-control/work/continuity or RELAY_CONTINUITY_DIR)")
    rep.set_defaults(func=cmd_report)
    summary = sub.add_parser("summary", help="one compact line of pool headroom")
    summary.add_argument("--quota-file", **quota_file)
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
