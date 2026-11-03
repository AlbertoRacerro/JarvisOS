"""Read-only Relay owner monitor for Coding → Runtime (spec 206).

A narrow seam over the spec 205 adapter ``scripts/relay_observe.py``: its snapshot and its cursor-paged
timeline are served unchanged, bounded and checked for secrets here. Nothing in this module takes a lock,
writes owner, mission, Relay or ledger state, signals a process or calls a provider.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "scripts"
MAX_PAYLOAD_BYTES = 512_000
DELIVERED_SHOWN = 5

_observe: ModuleType | None = None


def observe() -> ModuleType:
    """The spec 205 adapter, loaded once under the module names its own tests use."""
    global _observe
    if _observe is None:
        if "relay_continuity" not in sys.modules:
            spec = importlib.util.spec_from_file_location("relay_continuity", SCRIPTS_DIR / "relay_continuity.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules["relay_continuity"] = module
            spec.loader.exec_module(module)
        spec = importlib.util.spec_from_file_location("relay_observe", SCRIPTS_DIR / "relay_observe.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _observe = module
    return _observe


def control_root() -> Path:
    return Path(observe().rc.control_root())


def relay_repo() -> Path:
    """The main checkout, whose ``.agent-relay`` tree Relay writes, even when this backend runs in a worktree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=4, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return REPO_ROOT
    common = out.stdout.strip()
    return Path(common).parent if out.returncode == 0 and common else REPO_ROOT


def _checked(payload: dict[str, Any], *, snapshot_paths: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False)
    if observe().devctx.find_secret(text):
        return {"state": "refused", "reason": "secret_detected"}
    if snapshot_paths:
        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                return observe()._redact(value, len(value) + 1, paths=True, lines=True)
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            return value

        payload = scrub(payload)
        text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode()) > MAX_PAYLOAD_BYTES:
        return {"state": "refused", "reason": "payload_too_large"}
    return payload


def snapshot(now: datetime | None = None) -> dict[str, Any]:
    """The owner's semantic state, or a truthful ``unknown`` with a reason when it cannot be read."""
    ro = observe()
    root = control_root()
    if not root.is_dir():
        return {"state": "unknown", "reason": "owner_state_missing"}
    now = now or datetime.now(UTC)
    try:
        snap = ro.snapshot(root, relay_repo(), now)
        found = ro.incidents(snap, now)
    except Exception as exc:  # noqa: BLE001 - any unreadable owner state is reported, never raised
        return {"state": "unknown", "reason": f"owner_state_unreadable:{type(exc).__name__}"}
    delivered = sorted(snap.get("delivered") or [], key=lambda d: d.get("at") or "")[-DELIVERED_SHOWN:]
    return _checked({**snap, "delivered": delivered, "incidents": sorted(found)}, snapshot_paths=True)


def timeline(cursor: str | None, *, start: str = "turn", limit: int = 200) -> dict[str, Any]:
    """The next bounded page of the live turn's timeline after an opaque cursor. Raises ValueError on a cursor
    that is not one this endpoint issued."""
    ro = observe()
    root = control_root()
    if not root.is_dir():
        return {"state": "unknown", "reason": "owner_state_missing", "items": [], "cursor": cursor, "more": False}
    try:
        page = ro.timeline_page(root, relay_repo(), cursor, start=start, limit=limit)
    except ro.CursorInvalid:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"state": "unknown", "reason": f"owner_state_unreadable:{type(exc).__name__}", "items": [],
                "cursor": cursor, "more": False}
    return _checked({"state": "ok", **page})
