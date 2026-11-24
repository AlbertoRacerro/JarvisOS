"""Strictly read-only JarvisOS repository and private pack code/doc read tool."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core import packs
from app.modules.agents.hermes.ip_boundary import (
    CLOUD_IP_MODE_FULL,
    CLOUD_IP_MODE_NO_CLOUD,
    CLOUD_IP_MODE_SANITIZED,
    check_sensitivity_floor,
    is_local_route,
    log_cloud_boundary_event,
    resolve_cloud_ip_mode,
    sanitize_text,
)
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall
from app.modules.coding import relay_monitor

_ALLOWED_OPS = frozenset({"list", "search", "read"})

_OP_ALLOWED_KEYS = {
    "list": {"op", "scope", "pack_id", "path", "limit"},
    "search": {"op", "scope", "pack_id", "path", "query", "limit"},
    "read": {"op", "scope", "pack_id", "path", "start_line", "max_lines"},
}

_SKIP_DIR_NAMES = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".turbo",
    ".next",
})


def _repo_root() -> Path:
    return relay_monitor.relay_repo().resolve()


def _is_secret_like(name: str) -> bool:
    lower = name.lower()
    if lower.startswith(".env"):
        return True
    if lower.endswith(".pem") or lower.endswith(".key"):
        return True
    if lower.startswith("id_rsa"):
        return True
    if "secret" in lower:
        return True
    if "credential" in lower:
        return True
    if ".sqlite" in lower:
        return True
    return False


_LOCAL_PROVIDER_MODES = frozenset(
    {"fake", "local", "local_llamacpp", "local_ollama", "ollama", "llamacpp"}
)


def _is_binary_file(file_path: Path) -> bool:
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(4096)
            return b"\x00" in chunk
    except OSError:
        return True


def _path_has_forbidden_segment(rel_path: Path) -> bool:
    for part in rel_path.parts:
        if part in {".", ""}:
            continue
        if part == ".git" or _is_secret_like(part):
            return True
    return False


def _safe_relative_walk_path(entry: Path, scope_root: Path) -> Path | None:
    try:
        resolved = entry.resolve()
        if not resolved.is_relative_to(scope_root):
            return None
        rel = resolved.relative_to(scope_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if _path_has_forbidden_segment(rel):
        return None
    return rel


def _is_local_route(
    route_class: str | None,
    grant: CapabilityGrantRef | None,
) -> bool:
    candidate_route: Any = route_class
    if candidate_route is None and grant is not None:
        candidate_route = grant.constraints.get("route_class")
        if candidate_route is None:
            is_local = grant.constraints.get("is_local")
            if is_local is not None:
                return bool(is_local)
    if candidate_route is not None:
        if isinstance(candidate_route, str):
            return candidate_route.startswith("local:") or candidate_route == "fake"
        return False
    try:
        from app.modules.ai.settings import get_ai_settings

        settings = get_ai_settings()
        provider_mode = str(getattr(settings, "provider_mode", "") or "").lower()
        return provider_mode in _LOCAL_PROVIDER_MODES
    except Exception:
        return False


def _resolve_relative_path(
    raw_path: Any,
    scope_root: Path,
) -> tuple[Path | None, str]:
    if raw_path is None or raw_path == "" or raw_path == ".":
        return scope_root, ""
    if not isinstance(raw_path, str):
        return None, ""
    if "\x00" in raw_path or "\\" in raw_path:
        return None, ""
    p = Path(raw_path)
    if p.is_absolute():
        return None, ""
    for part in p.parts:
        if part in {"..", ".", ""}:
            if part == "..":
                return None, ""
            continue
        if part == ".git" or _is_secret_like(part):
            return None, ""
    try:
        resolved = (scope_root / p).resolve()
    except (OSError, RuntimeError):
        return None, ""
    if not resolved.is_relative_to(scope_root):
        return None, ""
    try:
        rel = resolved.relative_to(scope_root)
    except ValueError:
        return None, ""
    if _path_has_forbidden_segment(rel):
        return None, ""
    rel_posix = rel.as_posix()
    if rel_posix == ".":
        rel_posix = ""
    return resolved, rel_posix


def _handle_list(
    scope_root: Path,
    target_path: Path,
    clean_rel: str,
    limit: int,
    scope: str,
    pack_id: str | None,
    confidentiality: str,
) -> tuple[dict[str, Any] | None, str]:
    if not target_path.exists():
        return None, "invalid_arguments"

    all_files: list[str] = []
    if target_path.is_file():
        if _is_binary_file(target_path) or _safe_relative_walk_path(target_path, scope_root) is None:
            return None, "invalid_arguments"
        all_files.append(clean_rel)
    else:
        for root, dirs, files in os.walk(target_path, followlinks=False):
            dirs[:] = [
                d for d in dirs
                if d not in _SKIP_DIR_NAMES
                and not d.startswith(".")
                and not _is_secret_like(d)
                and (
                    not (Path(root) / d).is_symlink()
                    or _safe_relative_walk_path(Path(root) / d, scope_root) is not None
                )
            ]
            for file_name in files:
                if file_name == ".git" or _is_secret_like(file_name):
                    continue
                file_full = Path(root) / file_name
                rel = _safe_relative_walk_path(file_full, scope_root)
                if rel is None:
                    continue
                if _is_binary_file(file_full):
                    continue
                all_files.append(rel.as_posix())

    all_files.sort()
    sliced = all_files[:limit]
    return {
        "op": "list",
        "scope": scope,
        "pack_id": pack_id,
        "confidentiality": confidentiality,
        "path": clean_rel,
        "total": len(all_files),
        "count": len(sliced),
        "truncated": len(all_files) > len(sliced),
        "files": sliced,
    }, "ok"


def _handle_search(
    scope_root: Path,
    target_path: Path,
    clean_rel: str,
    query: str,
    limit: int,
    scope: str,
    pack_id: str | None,
    confidentiality: str,
) -> tuple[dict[str, Any] | None, str]:
    if not target_path.exists():
        return None, "invalid_arguments"

    candidate_files: list[tuple[Path, str]] = []
    if target_path.is_file():
        if not _is_binary_file(target_path) and _safe_relative_walk_path(target_path, scope_root) is not None:
            candidate_files.append((target_path, clean_rel))
    else:
        for root, dirs, files in os.walk(target_path, followlinks=False):
            dirs[:] = [
                d for d in dirs
                if d not in _SKIP_DIR_NAMES
                and not d.startswith(".")
                and not _is_secret_like(d)
                and (
                    not (Path(root) / d).is_symlink()
                    or _safe_relative_walk_path(Path(root) / d, scope_root) is not None
                )
            ]
            for file_name in files:
                if file_name == ".git" or _is_secret_like(file_name):
                    continue
                file_full = Path(root) / file_name
                rel = _safe_relative_walk_path(file_full, scope_root)
                if rel is None:
                    continue
                if _is_binary_file(file_full):
                    continue
                candidate_files.append((file_full, rel.as_posix()))

    candidate_files.sort(key=lambda item: item[1])
    query_lower = query.lower()
    matches: list[dict[str, Any]] = []

    for file_path, rel_path in candidate_files:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if query_lower in line.lower():
                snippet = line.strip()[:240]
                matches.append({
                    "path": rel_path,
                    "line": line_no,
                    "snippet": snippet,
                })
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break

    return {
        "op": "search",
        "scope": scope,
        "pack_id": pack_id,
        "confidentiality": confidentiality,
        "query": query,
        "path": clean_rel,
        "count": len(matches),
        "matches": matches,
    }, "ok"


def _slice_text_content(
    raw_text: str,
    clean_rel: str,
    start_line: int,
    max_lines: int,
    scope: str,
    pack_id: str | None,
    confidentiality: str,
) -> tuple[dict[str, Any] | None, str]:
    lines = raw_text.splitlines()
    total_lines = len(lines)

    if total_lines == 0:
        content = ""
        end_line = 0
        truncated = False
    elif start_line > total_lines:
        content = ""
        end_line = total_lines
        truncated = False
    else:
        start_idx = start_line - 1
        end_idx = min(total_lines, start_idx + max_lines)
        selected_lines = lines[start_idx:end_idx]
        end_line = end_idx
        raw_slice = "\n".join(selected_lines)
        truncated = end_idx < total_lines
        if len(raw_slice) > 16_000:
            content = raw_slice[:16_000]
            truncated = True
        else:
            content = raw_slice

    return {
        "op": "read",
        "scope": scope,
        "pack_id": pack_id,
        "confidentiality": confidentiality,
        "path": clean_rel,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": truncated,
        "content": content,
    }, "ok"


def _handle_read(
    scope_root: Path,
    target_path: Path,
    clean_rel: str,
    start_line: int,
    max_lines: int,
    scope: str,
    pack_id: str | None,
    confidentiality: str,
) -> tuple[dict[str, Any] | None, str]:
    if not clean_rel or not target_path.is_file():
        return None, "invalid_arguments"
    if _is_secret_like(target_path.name) or _is_binary_file(target_path):
        return None, "invalid_arguments"

    try:
        raw_text = target_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "invalid_arguments"

    return _slice_text_content(raw_text, clean_rel, start_line, max_lines, scope, pack_id, confidentiality)


def dispatch_repo_read_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
    *,
    grant: CapabilityGrantRef | None = None,
    route_class: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Execute a strictly read-only repository and private pack read operation for Hermes."""
    if call.capability_id != "jarvis.repo_read":
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

    scope = raw_args.get("scope", "public")
    if not isinstance(scope, str) or scope not in {"public", "pack"}:
        return None, "invalid_arguments"

    pack_id = raw_args.get("pack_id")
    if pack_id is not None and (not isinstance(pack_id, str) or not pack_id.strip() or len(pack_id) > 64):
        return None, "invalid_arguments"

    is_local = is_local_route(route_class, grant)
    egress_mode = resolve_cloud_ip_mode(workspace_id, grant) if not is_local else None

    if scope == "pack":
        if not is_local:
            if egress_mode == CLOUD_IP_MODE_NO_CLOUD:
                log_cloud_boundary_event(
                    tool_name="jarvis_repo_read",
                    op=op,
                    egress_mode="no_cloud",
                    withheld_count=1,
                    reason_code="egress_pack_blocked",
                    workspace_id=workspace_id,
                )
                return None, "egress_pack_blocked"

        loaded_packs = packs.load_packs()
        if not loaded_packs:
            return {
                "op": op,
                "scope": "pack",
                "pack_id": None,
                "confidentiality": "private_pack",
                "error_code": "pack_not_configured",
                "error": "No pack configured",
            }, default_error

        if pack_id is not None:
            matching = [p for p in loaded_packs if p.id == pack_id]
            if not matching:
                return {
                    "op": op,
                    "scope": "pack",
                    "pack_id": pack_id,
                    "confidentiality": "private_pack",
                    "error_code": "pack_not_found",
                    "error": f"Pack {pack_id!r} not found",
                }, default_error
            selected_pack = matching[0]
        else:
            if len(loaded_packs) == 1:
                selected_pack = loaded_packs[0]
                pack_id = selected_pack.id
            else:
                return {
                    "op": op,
                    "scope": "pack",
                    "pack_id": None,
                    "confidentiality": "private_pack",
                    "error_code": "ambiguous_pack_id",
                    "error": "Multiple packs loaded; pack_id is required",
                }, default_error

        scope_root = selected_pack.root.resolve()
        confidentiality = "private_pack"
    else:
        pack_id = None
        scope_root = _repo_root()
        confidentiality = "public"

    raw_path = raw_args.get("path")
    target_path, clean_rel = _resolve_relative_path(raw_path, scope_root)
    if target_path is None:
        return None, "invalid_arguments"

    if op == "list":
        limit = raw_args.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
            return None, "invalid_arguments"
        res, err = _handle_list(scope_root, target_path, clean_rel, limit, scope, pack_id, confidentiality)
        if res is not None and not is_local and scope == "pack":
            files = res.get("files", [])
            kept_files = []
            withheld_count = 0
            if egress_mode == CLOUD_IP_MODE_SANITIZED:
                for f in files:
                    if check_sensitivity_floor(f) in {"S2", "S3", "S4"}:
                        withheld_count += 1
                    else:
                        kept_files.append(f)
                res["files"] = kept_files
                res["count"] = len(kept_files)
                res["withheld_count"] = withheld_count
                res["sanitized"] = True
                res["egress_mode"] = "sanitized"
                if withheld_count > 0:
                    log_cloud_boundary_event(
                        tool_name="jarvis_repo_read",
                        op="list",
                        egress_mode="sanitized",
                        withheld_count=withheld_count,
                        reason_code="egress_sanitization_required",
                        workspace_id=workspace_id,
                    )
            elif egress_mode == CLOUD_IP_MODE_FULL:
                res["withheld_count"] = 0
                res["egress_mode"] = "full"
        return (res, default_error) if res is not None else (None, err)

    if op == "search":
        query = raw_args.get("query")
        if not isinstance(query, str) or not (1 <= len(query) <= 200) or "\x00" in query:
            return None, "invalid_arguments"
        limit = raw_args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 50):
            return None, "invalid_arguments"
        res, err = _handle_search(scope_root, target_path, clean_rel, query, limit, scope, pack_id, confidentiality)
        if res is not None and not is_local and scope == "pack":
            matches = res.get("matches", [])
            kept_matches = []
            withheld_count = 0
            if egress_mode == CLOUD_IP_MODE_SANITIZED:
                for match in matches:
                    snippet = match.get("snippet", "")
                    redacted = sanitize_text(snippet)
                    if check_sensitivity_floor(redacted) in {"S0", "S1", None}:
                        m = dict(match)
                        m["snippet"] = redacted
                        m["sanitized"] = True
                        kept_matches.append(m)
                    else:
                        withheld_count += 1
                res["matches"] = kept_matches
                res["count"] = len(kept_matches)
                res["withheld_count"] = withheld_count
                res["sanitized"] = True
                res["egress_mode"] = "sanitized"
                if withheld_count > 0:
                    log_cloud_boundary_event(
                        tool_name="jarvis_repo_read",
                        op="search",
                        egress_mode="sanitized",
                        withheld_count=withheld_count,
                        reason_code="egress_sanitization_required",
                        workspace_id=workspace_id,
                    )
            elif egress_mode == CLOUD_IP_MODE_FULL:
                for match in matches:
                    snippet = match.get("snippet", "")
                    if check_sensitivity_floor(snippet) == "S4":
                        withheld_count += 1
                    else:
                        kept_matches.append(match)
                res["matches"] = kept_matches
                res["count"] = len(kept_matches)
                res["withheld_count"] = withheld_count
                res["egress_mode"] = "full"
                if withheld_count > 0:
                    log_cloud_boundary_event(
                        tool_name="jarvis_repo_read",
                        op="search",
                        egress_mode="full",
                        withheld_count=withheld_count,
                        reason_code="egress_s4_blocked",
                        workspace_id=workspace_id,
                    )
        return (res, default_error) if res is not None else (None, err)

    if op == "read":
        if raw_path is None or not isinstance(raw_path, str) or not raw_path.strip():
            return None, "invalid_arguments"
        start_line = raw_args.get("start_line", 1)
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            return None, "invalid_arguments"
        max_lines = raw_args.get("max_lines", 150)
        if isinstance(max_lines, bool) or not isinstance(max_lines, int) or not (1 <= max_lines <= 300):
            return None, "invalid_arguments"

        if not is_local and scope == "pack":
            if not clean_rel or not target_path.is_file():
                return None, "invalid_arguments"
            if _is_secret_like(target_path.name) or _is_binary_file(target_path):
                return None, "invalid_arguments"
            try:
                raw_text = target_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None, "invalid_arguments"

            if egress_mode == CLOUD_IP_MODE_SANITIZED:
                if check_sensitivity_floor(raw_text) == "S4":
                    log_cloud_boundary_event(
                        tool_name="jarvis_repo_read",
                        op="read",
                        egress_mode="sanitized",
                        withheld_count=1,
                        reason_code="egress_sanitization_required",
                        workspace_id=workspace_id,
                    )
                    return None, "egress_sanitization_required"
                redacted_text = sanitize_text(raw_text)
                post_floor = check_sensitivity_floor(redacted_text)
                if post_floor in {"S2", "S3", "S4"}:
                    log_cloud_boundary_event(
                        tool_name="jarvis_repo_read",
                        op="read",
                        egress_mode="sanitized",
                        withheld_count=1,
                        reason_code="egress_sanitization_required",
                        workspace_id=workspace_id,
                    )
                    return None, "egress_sanitization_required"

                res, err = _slice_text_content(
                    redacted_text, clean_rel, start_line, max_lines, scope, pack_id, confidentiality
                )
                if res is not None:
                    res["sanitized"] = True
                    res["egress_mode"] = "sanitized"
                    res["withheld_count"] = 0
                return (res, default_error) if res is not None else (None, err)

            if egress_mode == CLOUD_IP_MODE_FULL:
                if check_sensitivity_floor(raw_text) == "S4":
                    log_cloud_boundary_event(
                        tool_name="jarvis_repo_read",
                        op="read",
                        egress_mode="full",
                        withheld_count=1,
                        reason_code="egress_s4_blocked",
                        workspace_id=workspace_id,
                    )
                    return None, "egress_s4_blocked"

                res, err = _slice_text_content(
                    raw_text, clean_rel, start_line, max_lines, scope, pack_id, confidentiality
                )
                if res is not None:
                    res["egress_mode"] = "full"
                    res["withheld_count"] = 0
                return (res, default_error) if res is not None else (None, err)

        res, err = _handle_read(scope_root, target_path, clean_rel, start_line, max_lines, scope, pack_id, confidentiality)
        return (res, default_error) if res is not None else (None, err)

    return None, "invalid_arguments"
