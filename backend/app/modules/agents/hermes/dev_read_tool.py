"""Strictly read-only JarvisOS development, supervisor, quota, and runtime health tool."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.modules.ai.agent_contracts import StructuredToolCall
from app.modules.coding import control_room, provider_quota, relay_monitor

_SPEC_ID_RE = re.compile(r"^[0-9]{1,4}[a-z]?$", re.ASCII)

_ALLOWED_OPS = frozenset({
    "specs",
    "spec_text",
    "recent_work",
    "plan",
    "supervisor",
    "directives",
    "quotas",
    "runtime_health",
})

_OP_ALLOWED_KEYS = {
    "specs": {"op", "spec_id", "status", "limit"},
    "spec_text": {"op", "spec_id"},
    "recent_work": {"op", "limit"},
    "plan": {"op"},
    "supervisor": {"op"},
    "directives": {"op", "limit"},
    "quotas": {"op"},
    "runtime_health": {"op"},
}

_OP_LIMIT_BOUNDS = {
    "specs": (1, 50),
    "recent_work": (1, 30),
    "directives": (1, 30),
}


def _repo_root() -> Path:
    return relay_monitor.relay_repo()


def _control_root() -> Path:
    return relay_monitor.control_root()


def _scrub_value(val: Any, home_str: str) -> Any:
    if isinstance(val, str):
        if home_str and home_str in val:
            val = val.replace(home_str, "~")
        val = re.sub(r"/home/[a-zA-Z0-9._-]+", "~", val)
        val = re.sub(
            r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+[A-Za-z0-9_.-]+)",
            "[REDACTED_SECRET]",
            val,
        )
        return val
    if isinstance(val, list):
        return [_scrub_value(item, home_str) for item in val]
    if isinstance(val, dict):
        scrubbed: dict[str, Any] = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(secret_key in k_lower for secret_key in ("token", "api_key", "secret", "password", "credential")):
                continue
            scrubbed[str(k)] = _scrub_value(v, home_str)
        return scrubbed
    return val


def _handle_specs(args: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    limit = args.get("limit", 20)
    spec_id_filter = args.get("spec_id")
    status_filter = args.get("status")

    status_path = repo_root / "docs" / "specs" / "STATUS.md"
    registry: dict[str, control_room.RegistryRow] = {}
    if status_path.is_file():
        try:
            registry, _ = control_room.parse_registry(
                status_path.read_text(encoding="utf-8", errors="replace"),
            )
        except Exception:
            registry = {}

    rows = list(registry.values())
    if spec_id_filter is not None:
        rows = [r for r in rows if r.spec_id == spec_id_filter]
    if status_filter is not None:
        rows = [r for r in rows if r.status == status_filter]

    total = len(rows)
    sliced = rows[:limit]
    specs_list = [
        {
            "spec_id": r.spec_id,
            "status": r.status,
            "name": r.name,
            "depends_on": list(r.depends_on),
            "pr_numbers": list(r.implementation_prs),
            "description": r.description[:300],
        }
        for r in sliced
    ]
    return {
        "op": "specs",
        "total": total,
        "count": len(specs_list),
        "specs": specs_list,
    }


def _handle_spec_text(spec_id: str, repo_root: Path, default_error: str) -> tuple[dict[str, Any] | None, str]:
    specs_dir = (repo_root / "docs" / "specs").resolve()
    if not specs_dir.is_dir():
        return {
            "op": "spec_text",
            "spec_id": spec_id,
            "filename": None,
            "truncated": False,
            "content": "",
        }, default_error

    candidates = sorted(specs_dir.glob(f"{spec_id}-*.md"))
    if not candidates:
        return {
            "op": "spec_text",
            "spec_id": spec_id,
            "filename": None,
            "truncated": False,
            "content": "",
        }, default_error

    target_file = candidates[0]
    resolved = target_file.resolve()
    if not (resolved.is_relative_to(specs_dir) and resolved.is_file()):
        return None, "invalid_arguments"

    raw_text = target_file.read_text(encoding="utf-8", errors="replace")
    truncated = len(raw_text) > 16_000
    content = raw_text[:16_000]

    return {
        "op": "spec_text",
        "spec_id": spec_id,
        "filename": target_file.name,
        "truncated": truncated,
        "content": content,
    }, default_error


def _handle_recent_work(args: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    limit = args.get("limit", 15)
    items: list[dict[str, Any]] = []

    for branch in ("main", "master"):
        try:
            proj = control_room.ControlRoomProjection(root=repo_root)
            res = proj.project(branch)
            recent = res.get("recent_work")
            if isinstance(recent, dict) and recent.get("status") == "available":
                raw_items = recent.get("items")
                if isinstance(raw_items, list):
                    items = [dict(it) for it in raw_items if isinstance(it, dict)]
                    break
        except Exception:
            pass

    if not items:
        try:
            registry: dict[str, control_room.RegistryRow] = {}
            status_path = repo_root / "docs" / "specs" / "STATUS.md"
            if status_path.is_file():
                registry, _ = control_room.parse_registry(
                    status_path.read_text(encoding="utf-8", errors="replace"),
                )
            proc = subprocess.run(
                [
                    "git", "log", "--first-parent", f"--max-count={3 * limit}",
                    "--format=%H%x00%cI%x00%s", "HEAD", "--",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                parsed_items, _ = control_room.recent_work_items(proc.stdout, registry)
                items = [dict(it) for it in parsed_items if isinstance(it, dict)]
        except Exception:
            pass

    sliced = items[:limit]
    return {
        "op": "recent_work",
        "count": len(sliced),
        "items": sliced,
    }


def _handle_plan(repo_root: Path) -> dict[str, Any]:
    control_dir = _control_root()
    plan_path = control_dir / "plan.json"

    registry: dict[str, control_room.RegistryRow] = {}
    status_path = repo_root / "docs" / "specs" / "STATUS.md"
    if status_path.is_file():
        try:
            registry, _ = control_room.parse_registry(
                status_path.read_text(encoding="utf-8", errors="replace"),
            )
        except Exception:
            registry = {}

    plan_text: str | None = None
    if plan_path.is_symlink() and not plan_path.resolve().is_relative_to(control_dir.resolve()):
        plan_data: dict[str, Any] = {"status": "invalid"}
        warnings = ["plan_invalid"]
    elif plan_path.is_file():
        try:
            raw = plan_path.read_bytes()[:control_room.PLAN_MAX_BYTES + 1]
            plan_text = raw.decode("utf-8", errors="replace")
            res, warnings = control_room.plan_projection(plan_text, registry)
            plan_data = dict(res)
        except Exception:
            plan_data = {"status": "invalid"}
            warnings = ["plan_invalid"]
    else:
        res, warnings = control_room.plan_projection(None, registry)
        plan_data = dict(res)

    return {
        "op": "plan",
        "plan": plan_data,
        "warnings": warnings,
        **plan_data,
    }


def _handle_supervisor() -> dict[str, Any]:
    home_str = str(Path.home())
    try:
        snap = relay_monitor.snapshot()
    except Exception as exc:
        snap = {"state": "unknown", "reason": f"snapshot_error:{type(exc).__name__}"}
    scrubbed = _scrub_value(snap, home_str)
    return {
        "op": "supervisor",
        "snapshot": scrubbed,
        **scrubbed,
    }


def _handle_directives(args: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    limit = args.get("limit", 15)
    home_str = str(Path.home())

    candidates = [
        repo_root / ".agent-relay" / "devctx" / "ledger.jsonl",
        _control_root() / "ledger.jsonl",
    ]
    ledger_path = next((p for p in candidates if p.is_file()), None)

    directives: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    if ledger_path is not None:
        try:
            lines = ledger_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []

        entries: list[dict[str, Any]] = []
        superseded: set[str] = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and "id" in entry and "kind" in entry:
                    entries.append(entry)
                    for sup in entry.get("supersedes") or []:
                        if isinstance(sup, str):
                            superseded.add(sup)
            except Exception:
                continue

        entries.reverse()
        for entry in entries:
            kind = entry.get("kind")
            entry_id = str(entry.get("id"))
            ts = entry.get("ts")
            status = entry.get("status")
            raw_text = entry.get("text", "")
            scrubbed_text = _scrub_value(raw_text, home_str)
            if isinstance(scrubbed_text, str):
                scrubbed_text = scrubbed_text[:500]

            if kind == "directive":
                if entry_id not in superseded:
                    directive_dict = entry.get("directive")
                    source = directive_dict.get("source") if isinstance(directive_dict, dict) else None
                    scrubbed_source = _scrub_value(source, home_str)
                    summary = {
                        "id": entry_id,
                        "kind": "directive",
                        "ts": ts,
                        "status": status or "accepted",
                        "text": scrubbed_text,
                        "source": scrubbed_source,
                    }
                    directives.append(summary)
                    items.append(summary)
            elif kind == "note":
                note_summary = {
                    "id": entry_id,
                    "kind": "note",
                    "ts": ts,
                    "status": status,
                    "text": scrubbed_text,
                }
                notes.append(note_summary)
                items.append(note_summary)

    return {
        "op": "directives",
        "count": len(items[:limit]),
        "directives": directives[:limit],
        "notes": notes[:limit],
        "items": items[:limit],
    }


def _handle_quotas() -> dict[str, Any]:
    home_str = str(Path.home())
    readings = provider_quota.read_all(refresh=False)
    normalized = provider_quota.normalize(readings, datetime.now(UTC))
    scrubbed_quotas = _scrub_value(normalized, home_str)

    control_dir = _control_root()
    candidates = [
        control_dir / "temp-pools.json",
        control_dir.parent / "temp-pools.json",
    ]
    pools_file = next((p for p in candidates if p.is_file()), None)
    free_pools: list[dict[str, Any]] = []
    if pools_file is not None:
        try:
            data = json.loads(pools_file.read_text(encoding="utf-8", errors="replace"))
            raw_pools = data.get("pools", []) if isinstance(data, dict) else []
            if isinstance(raw_pools, list):
                for pool in raw_pools:
                    if isinstance(pool, dict):
                        p_id = pool.get("id") or pool.get("name")
                        p_label = pool.get("label") or pool.get("name")
                        p_tier = pool.get("tier")
                        p_status = pool.get("status") or "admitted"
                        p_score = pool.get("index_score")
                        free_pools.append({
                            "id": str(p_id) if p_id is not None else "unknown",
                            "label": str(p_label) if p_label is not None else "unknown",
                            "tier": p_tier,
                            "status": str(p_status),
                            "index_score": p_score,
                        })
        except Exception:
            pass

    return {
        "op": "quotas",
        "quotas": scrubbed_quotas,
        "free_pools": free_pools,
    }


def _handle_runtime_health(call: StructuredToolCall, repo_root: Path) -> dict[str, Any]:
    local_ai: Any
    try:
        from app.modules.local_ai.runtime.status import get_local_ai_runtime_status

        status = get_local_ai_runtime_status()
        if not status or not status.get("ollama_reachable"):
            local_ai = "unavailable"
        else:
            local_ai = {
                "status": "running",
                "installed_models": status.get("installed_models", []),
                "loaded_models": status.get("loaded_models", []),
            }
    except Exception:
        local_ai = "unavailable"

    hermes: Any
    if call.session_ref is not None:
        hermes = {
            "status": "running",
            "generation": call.session_ref.generation,
            "upstream_revision": call.session_ref.upstream_revision,
            "profile_id": call.session_ref.profile_id,
        }
    else:
        hermes = "unavailable"

    repo_summary: Any
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            head_sha = proc.stdout.strip()
            branch_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "unknown"
            repo_summary = {
                "status": "ok",
                "head_sha": head_sha,
                "branch": branch,
            }
        else:
            repo_summary = "unavailable"
    except Exception:
        repo_summary = "unavailable"

    return {
        "op": "runtime_health",
        "local_ai": local_ai,
        "hermes_supervisor": hermes,
        "repository": repo_summary,
    }


def dispatch_dev_read_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
) -> tuple[dict[str, Any] | None, str]:
    """Execute a strictly read-only dev_read operation for Hermes."""
    if call.capability_id != "jarvis.dev_read":
        return None, "capability_denied"

    raw_args = dict(call.arguments)
    raw_args.pop("tool_name", None)
    raw_args.pop("grant_id", None)

    op = raw_args.get("op")
    if not isinstance(op, str) or op not in _ALLOWED_OPS:
        return None, "invalid_arguments"

    allowed_keys = _OP_ALLOWED_KEYS[op]
    if set(raw_args.keys()) - allowed_keys:
        return None, "invalid_arguments"

    if "limit" in raw_args:
        limit = raw_args["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int):
            return None, "invalid_arguments"
        min_limit, max_limit = _OP_LIMIT_BOUNDS.get(op, (1, 30))
        if not (min_limit <= limit <= max_limit):
            return None, "invalid_arguments"

    repo_root = _repo_root()

    if op == "specs":
        spec_id = raw_args.get("spec_id")
        if spec_id is not None and not isinstance(spec_id, str):
            return None, "invalid_arguments"
        status = raw_args.get("status")
        if status is not None and not isinstance(status, str):
            return None, "invalid_arguments"
        return _handle_specs(raw_args, repo_root), default_error

    if op == "spec_text":
        spec_id = raw_args.get("spec_id")
        if not isinstance(spec_id, str) or not _SPEC_ID_RE.fullmatch(spec_id):
            return None, "invalid_arguments"
        return _handle_spec_text(spec_id, repo_root, default_error)

    if op == "recent_work":
        return _handle_recent_work(raw_args, repo_root), default_error

    if op == "plan":
        return _handle_plan(repo_root), default_error

    if op == "supervisor":
        return _handle_supervisor(), default_error

    if op == "directives":
        return _handle_directives(raw_args, repo_root), default_error

    if op == "quotas":
        return _handle_quotas(), default_error

    if op == "runtime_health":
        return _handle_runtime_health(call, repo_root), default_error

    return None, "invalid_arguments"
