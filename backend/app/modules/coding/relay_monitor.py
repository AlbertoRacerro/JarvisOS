"""Bounded Relay snapshot and timeline (spec 206), supervisor controls and answers (spec 240)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

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
    override = observe().os.environ.get("JARVIS_RELAY_REPO")
    if override:
        return Path(override)
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


def control_supervisor(action: Literal["pause", "resume", "graceful_restart"], note: str | None = None) -> dict[str, Any]:
    ro = observe()
    note = (note or "").strip()
    if len(note) > 240:
        return {"state": "refused", "reason": "note_too_long"}
    if ro.devctx.find_secret(note):
        return {"state": "refused", "reason": "secret_detected"}
    rc, root, now = ro.rc, control_root(), datetime.now(UTC)
    if not root.is_dir():
        return {"state": "refused", "reason": "owner_state_missing"}
    if action not in ("pause", "resume", "graceful_restart"):
        return {"state": "refused", "reason": "action_invalid"}
    if action == "graceful_restart":
        store = rc.Store(root)
        store.root.mkdir(parents=True, exist_ok=True)
        store.stop_path.write_text(rc.iso(now) or "")
        rc.missions_journal(root, {"event": "owner_stop_requested", "at": rc.iso(now), "source": "jarvis_ui"})
    else:
        lock = rc.roadmap_lock(root, wait=5.0)
        if lock is None:
            return {"state": "refused", "reason": "roadmap_busy"}
        try:
            road = rc.load_roadmap(root)
            if road and road.get("corrupt"):
                return {"state": "refused", "reason": "roadmap_corrupt"}
            at = rc.iso(now)
            if action == "pause":
                if not road or not road.get("enabled"):
                    return {"state": "refused", "reason": "roadmap_not_enabled"}
                clean_note = note or "Paused from Jarvis UI"
                road.update(paused="maintainer_stop", paused_at=at, note=clean_note)
                rc.write_json_atomic(rc.roadmap_path(root), road)
                rc.missions_journal(root, {"event": "roadmap_paused", "at": at, "reason": "maintainer_stop", "note": clean_note})
            else:
                stop_existed = (root / "STOP").exists()
                (root / "STOP").unlink(missing_ok=True)
                resumed_roadmap = False
                if road and road.get("enabled") and road.get("paused") and not road.get("hostops_wait") and not road.get("job_wait"):
                    road.update(paused=None, paused_at=None, no_progress=0)
                    rc.write_json_atomic(rc.roadmap_path(root), road)
                    rc.missions_journal(root, {"event": "roadmap_started", "at": at, "authority": road.get("authority")})
                    resumed_roadmap = True
                if stop_existed and not resumed_roadmap:
                    rc.missions_journal(root, {"event": "owner_stop_cleared", "at": at, "source": "jarvis_ui"})
        finally:
            lock.release()
    return {"state": "ok", "action": action, "snapshot": snapshot(now=now)}


def answer_question(question_id: str, option_index: int | None = None, custom_text: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"L\d{14}-[0-9a-f]{4}", question_id):
        return {"state": "refused", "reason": "question_id_invalid"}
    ro, root, repo = observe(), control_root(), relay_repo()
    if not root.is_dir():
        return {"state": "refused", "reason": "owner_state_missing"}
    question = next((q for q in ro.parse_open_questions(ro._ledger(repo)) if q["id"] == question_id), None)
    if question is None:
        return {"state": "refused", "reason": "question_not_open"}
    custom = (custom_text or "").strip()
    if len(custom) > 400:
        return {"state": "refused", "reason": "answer_too_long"}
    prefix = f"Maintainer answer to question {question_id} (via Jarvis UI): "
    if option_index is not None:
        option = next((o for o in question["options"] if o["index"] == option_index), None)
        if option is None:
            return {"state": "refused", "reason": "option_invalid"}
        answer = f"{prefix}option {option_index} ({option['label']})." + (f" Note: {custom}" if custom else "")
    elif custom:
        answer = prefix + custom
    else:
        return {"state": "refused", "reason": "answer_empty"}
    if ro.devctx.find_secret(answer):
        return {"state": "refused", "reason": "secret_detected"}
    now = datetime.now(UTC)
    result = ro.record_answer(root, repo, question_id, answer[len(prefix):], source="Jarvis UI",
                              author="maintainer (jarvis-ui)")
    return {**result, "snapshot": snapshot(now=now)} if result["state"] == "ok" else result
