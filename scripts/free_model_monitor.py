#!/usr/bin/env python3
"""Daily, free-only model discovery and temporary-pool maintenance."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

OPENROUTER = "https://openrouter.ai/api/v1/models"
ZEN = "https://opencode.ai/zen/v1/models"
DEFAULT_WATCHLIST = ("glm-5.4", "glm5.4")
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,150}\Z")
SAFE_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,80}\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _http_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("invalid endpoint")
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "jarvisos-free-model-monitor/1", **headers})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(16_000_001))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            name = handle.name
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def _zero_price(item: dict[str, Any]) -> bool:
    pricing = item.get("pricing")
    if pricing is None:
        return True
    if not isinstance(pricing, dict) or not pricing:
        return False
    for value in pricing.values():
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return False
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return False
        if not number.is_finite() or number != 0:
            return False
    return True


def _source_items(source: dict[str, Any], opener: Callable[..., Any]) -> set[str]:
    kind = source["free_list"]
    if kind == "openrouter_public":
        url, headers, suffix = OPENROUTER, {}, ":free"
    elif kind == "opencode_zen":
        url, headers, suffix = ZEN, {}, "-free"
    else:
        key_file = Path(source["key_file"])
        if not key_file.is_absolute() or not key_file.is_file():
            raise OSError("source key unavailable")
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise OSError("source key unavailable")
        if any(key in json.dumps(source.get(field), ensure_ascii=False) for field in
               ("source_id", "provider", "base_url", "rate_limits", "data_policy", "watchlist")):
            raise ValueError("unsafe source label")
        base = source["base_url"]
        url = base.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {key}"}
        suffix = source["free_suffix"]
    document = opener(url, headers, 10)
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, list):
        raise ValueError("model list unavailable")
    return {model for item in data if isinstance(item, dict)
            if isinstance(model := item.get("id"), str) and SAFE_LABEL.fullmatch(model)
            and (model.endswith(suffix) if suffix else item.get("pricing") is not None)
            and _zero_price(item) and (kind != "openai_models" or key not in model)}


def _sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [
        {"source_id": "openrouter_public", "provider": "OpenRouter", "free_list": "openrouter_public",
         "base_url": "https://openrouter.ai/api/v1", "key_file": "", "free_suffix": ":free",
         "rate_limits": "", "data_policy": ""},
        {"source_id": "opencode_zen", "provider": "OpenCode Zen", "free_list": "opencode_zen",
         "base_url": "", "key_file": "", "free_suffix": "-free", "rate_limits": "", "data_policy": ""},
    ]
    entries = config.get("sources") if isinstance(config, dict) else None
    if not isinstance(entries, list):
        return sources
    for item in entries:
        if not isinstance(item, dict):
            continue
        sid, provider = item.get("source_id"), item.get("provider")
        built_in = next((source for source in sources if source["source_id"] == sid), None)
        if built_in is not None:
            if (item.get("free_list") == built_in["free_list"] and isinstance(provider, str)
                    and SAFE_PROVIDER.fullmatch(provider)
                    and isinstance(item.get("key_file"), str)
                    and (not item["key_file"] or Path(item["key_file"]).is_absolute())
                    and isinstance(item.get("base_url"), str) and item["base_url"].startswith("https://")
                    and "rate_limits" in item and "data_policy" in item):
                built_in.update({key: item[key] for key in ("provider", "key_file", "rate_limits", "data_policy")})
                built_in["base_url"] = item["base_url"]
                if isinstance(item.get("watchlist"), list):
                    built_in["watchlist"] = item["watchlist"]
            continue
        if not (isinstance(sid, str) and SAFE_LABEL.fullmatch(sid) and isinstance(provider, str)
                and SAFE_PROVIDER.fullmatch(provider) and item.get("free_list") == "openai_models"
                and isinstance(item.get("base_url"), str) and item["base_url"].startswith("https://")
                and isinstance(item.get("key_file"), str) and isinstance(item.get("free_suffix"), str)
                and item["free_suffix"] in (":free", "-free", "")
                and "rate_limits" in item and "data_policy" in item):
            continue
        if sid in {s["source_id"] for s in sources}:
            continue
        sources.append(item)
    return sources


def _score(model: str, pool: dict[str, Any], config: dict[str, Any]) -> int | None:
    for document in (config, pool):
        scores = document.get("catalog_scores")
        if isinstance(scores, dict):
            value = scores.get(model)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _watchlist(pool: dict[str, Any], config: dict[str, Any]) -> set[str]:
    words = set(DEFAULT_WATCHLIST)
    for document in (pool, config):
        raw = document.get("watchlist")
        if isinstance(raw, list):
            words.update(word.lower() for word in raw if isinstance(word, str) and SAFE_LABEL.fullmatch(word))
    return words


def _watched(model: str, words: set[str]) -> bool:
    lowered = model.lower()
    return any(word in lowered for word in words)


def _pool_name(source_id: str, model: str) -> str:
    import hashlib
    label = re.sub(r"[^A-Za-z0-9._-]", "-", model).strip("-")[:48]
    return f"auto-{label}-{hashlib.sha256((source_id + ':' + model).encode()).hexdigest()[:10]}"


def _reference(pool: dict[str, Any]) -> int | None:
    scores, name = pool.get("scores"), pool.get("reference")
    value = scores.get(name) if isinstance(scores, dict) and isinstance(name, str) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _notify(callback: Callable[[str, str], Any] | None, kind: str, message: str) -> bool:
    if callback is None:
        return False
    try:
        return callback(kind, message) is not False
    except Exception:  # noqa: BLE001 - delivery errors may contain credentials
        return False


def check(*, control_dir: Path | str | None = None, notify_fn: Callable[[str, str], Any] | None = None,
          now: datetime | None = None, opener: Callable[..., Any] | None = None,
          log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Check available sources; retain last known state for any source whose fetch fails."""
    root = Path(control_dir) if control_dir is not None else Path(os.environ.get(
        "RELAY_CONTINUITY_DIR", Path.home() / "jarvis-control" / "work" / "continuity"))
    pool_path = Path(os.environ["TEMP_POOLS_FILE"]) if "TEMP_POOLS_FILE" in os.environ else root.parent / "temp-pools.json"
    state_path = pool_path.parent / "free-model-monitor-state.json"
    if not pool_path.is_file():
        return {"checked": False, "reason": "no_pool_file"}
    pool = _read_json(pool_path)
    if not isinstance(pool.get("pools"), list):
        return {"checked": False, "reason": "invalid_pool_file"}
    config = _read_json(pool_path.parent / "free-monitor-sources.json")
    state = _read_json(state_path)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    previous = state.get("seen_free_by_source")
    previous = previous if isinstance(previous, dict) else {}
    seen = dict(previous)
    available: dict[str, set[str]] = {}
    source_by_id = {source["source_id"]: source for source in _sources(config)}
    for source in source_by_id.values():
        if source["free_list"] != "openrouter_public" or source["key_file"]:
            continue
        for entry in pool["pools"]:
            if (isinstance(entry, dict) and entry.get("free_list") == "openrouter_public"
                    and entry.get("auto_managed") is not True and isinstance(entry.get("key_file"), str)
                    and Path(entry["key_file"]).is_absolute()
                    and isinstance(entry.get("base_url"), str) and entry["base_url"].startswith("https://")):
                source["key_file"] = entry["key_file"]
                source["base_url"] = entry["base_url"]
                break
    for sid, source in source_by_id.items():
        try:
            available[sid] = _source_items(source, opener or _http_json)
            seen[sid] = sorted(available[sid])
        except Exception:  # noqa: BLE001 - source errors must never reveal a key or abort other sources
            if log:
                log("free model monitor: source unavailable")
    reference = _reference(pool)
    words = _watchlist(pool, config)
    old_pools = pool["pools"]
    new_pools: list[Any] = []
    old_expired = state.get("expired_auto")
    expired_by_source = {sid: {model for model in ids if isinstance(model, str)} for sid, ids in old_expired.items()
                         if isinstance(sid, str) and isinstance(ids, list)} if isinstance(old_expired, dict) else {}
    for entry in old_pools:
        if not isinstance(entry, dict) or entry.get("auto_managed") is not True:
            new_pools.append(entry)
            continue
        sid, model = entry.get("source_id"), entry.get("model")
        expiry = _time(entry.get("expires_at"))
        if expiry and moment >= expiry:
            if isinstance(sid, str) and isinstance(model, str):
                expired_by_source.setdefault(sid, set()).add(model)
            continue
        if sid in available and model not in available[sid]:
            continue
        new_pools.append(dict(entry))
    manual_models = {(entry["free_list"], entry["model"]) for entry in new_pools
                     if isinstance(entry, dict) and entry.get("auto_managed") is not True
                     and isinstance(entry.get("free_list"), str) and isinstance(entry.get("model"), str)}
    existing = {(entry.get("source_id"), entry.get("model")): entry for entry in new_pools
                if isinstance(entry, dict) and entry.get("auto_managed") is True
                and isinstance(entry.get("source_id"), str) and isinstance(entry.get("model"), str)}
    for sid, ids in available.items():
        source = source_by_id[sid]
        expired_by_source.setdefault(sid, set()).intersection_update(ids)
        for model in sorted(ids):
            if model in expired_by_source[sid]:
                continue
            score = _score(model, pool, config)
            if reference is None or score is None or score < reference:
                continue
            if (source["free_list"], model) in manual_models:
                continue
            key = (sid, model)
            if key in existing:
                existing[key]["index_score"] = score
                continue
            item = {"name": _pool_name(sid, model), "provider": source["provider"], "model": model,
                    "free_list": source["free_list"], "index_score": score,
                    "rate_limits": source["rate_limits"], "data_policy": source["data_policy"],
                    "auto_managed": True, "source_id": sid}
            if source["free_list"] != "opencode_zen":
                item["base_url"] = source["base_url"]
                item["key_file"] = source["key_file"]
            new_pools.append(item)
    old_hits = state.get("alert_hits")
    old_hits = old_hits if isinstance(old_hits, dict) else {}
    hits = dict(old_hits)
    admitted = state.get("admitted_strong")
    admitted = dict(admitted) if isinstance(admitted, dict) else {}
    watched_hits = state.get("watched_hits")
    watched_hits = dict(watched_hits) if isinstance(watched_hits, dict) else {}
    alerts = 0
    for sid, ids in available.items():
        source = source_by_id[sid]
        source_words = set(words)
        raw_words = source.get("watchlist")
        if isinstance(raw_words, list):
            source_words.update(word.lower() for word in raw_words
                                if isinstance(word, str) and SAFE_LABEL.fullmatch(word))
        qualifying = {model for model in ids if model not in expired_by_source.get(sid, set()) and
                      (_watched(model, source_words) or
                      (reference is not None and (score := _score(model, pool, config)) is not None
                       and score >= reference))}
        before = {model for model in old_hits.get(sid, []) if isinstance(model, str)} \
            if isinstance(old_hits.get(sid), list) else set()
        delivered = set(before)
        for model in sorted(qualifying - before):
            if _notify(notify_fn, f"free-model|added|{sid}|{model}",
                       f"Nuovo modello gratuito disponibile: {model} su {source['provider']}."):
                delivered.add(model)
                alerts += 1
        for model in sorted(before - qualifying):
            if _notify(notify_fn, f"free-model|removed|{sid}|{model}",
                       f"Il modello gratuito {model} su {source['provider']} non è più disponibile."):
                delivered.discard(model)
                alerts += 1
        hits[sid] = sorted(delivered)
        admitted[sid] = sorted(model for model in qualifying if
                               reference is not None and (score := _score(model, pool, config)) is not None
                               and score >= reference)
        watched_hits[sid] = sorted(model for model in qualifying if _watched(model, source_words))
    manual_delisted: list[str] = []
    for entry in new_pools:
        if not isinstance(entry, dict) or entry.get("auto_managed") is True:
            continue
        model = entry.get("model")
        if not isinstance(model, str):
            continue
        for sid, ids in available.items():
            source = source_by_id[sid]
            if entry.get("free_list") == source["free_list"] and (
                source["free_list"] != "openai_models" or entry.get("base_url") == source["base_url"]
            ) and model not in ids:
                manual_delisted.append(model)
                break
    pool["pools"] = new_pools
    state.update({"seen_free_by_source": seen,
                  "admitted_strong": admitted, "watched_hits": watched_hits,
                  "alert_hits": hits,
                  "manual_delisted": sorted(set(manual_delisted)),
                  "expired_auto": {sid: sorted(ids) for sid, ids in expired_by_source.items() if ids}})
    state["last_attempt_at"] = moment.isoformat()
    if available:
        state["last_checked_at"] = moment.isoformat()
    if pool["pools"] != old_pools:
        _atomic_json(pool_path, pool)
    _atomic_json(state_path, state)
    return {"checked": True, "sources": sorted(available), "alerts": alerts}


def maybe_run_daily(*, control_dir: Path | str | None = None,
                    notify_fn: Callable[[str, str], Any] | None = None, now: datetime | None = None,
                    interval_hours: float = 24, force: bool = False,
                    opener: Callable[..., Any] | None = None,
                    log: Callable[[str], None] | None = None) -> dict[str, Any]:
    try:
        root = Path(control_dir) if control_dir is not None else Path(os.environ.get(
            "RELAY_CONTINUITY_DIR", Path.home() / "jarvis-control" / "work" / "continuity"))
        pool_path = Path(os.environ["TEMP_POOLS_FILE"]) if "TEMP_POOLS_FILE" in os.environ else root.parent / "temp-pools.json"
        state = _read_json(pool_path.parent / "free-model-monitor-state.json")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        last = _time(state.get("last_checked_at"))
        if not force and last and timedelta(0) <= moment - last < timedelta(hours=interval_hours):
            return {"checked": False, "reason": "cadence"}
        attempt = _time(state.get("last_attempt_at"))  # offline host: retry hourly, not every owner loop
        if not force and attempt and timedelta(0) <= moment - attempt < timedelta(hours=min(1, interval_hours)):
            return {"checked": False, "reason": "retry_backoff"}
        return check(control_dir=root, notify_fn=notify_fn, now=moment, opener=opener, log=log)
    except Exception:  # noqa: BLE001 - the supervisor must keep running, and exceptions may contain secrets
        if log:
            try:
                log("free model monitor: unexpected failure")
            except Exception:  # noqa: BLE001
                pass
        return {"checked": False, "reason": "error"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("check")
    command.add_argument("--force", action="store_true")
    command.add_argument("--notify", action="store_true")
    command.add_argument("--control-dir", type=Path)
    args = parser.parse_args(argv)
    callback = None
    if args.notify:
        try:
            from relay_continuity_missions import webhook_sender
            from relay_continuity_notify import Notifier
            from relay_continuity_state import Store
            root = args.control_dir or Path(os.environ.get(
                "RELAY_CONTINUITY_DIR", Path.home() / "jarvis-control" / "work" / "continuity"))
            store = Store(root / "serve")
            state = store.load()
            sender = webhook_sender()
            notifier = Notifier(state, sender=sender, log=lambda _: None)
            def callback(kind, message):
                return notifier.send_text(kind, message, datetime.now(UTC))
        except Exception:  # noqa: BLE001
            callback = None
    result = maybe_run_daily(control_dir=args.control_dir, notify_fn=callback, force=args.force)
    if args.notify and callback and result.get("alerts"):
        store.save(state, datetime.now(UTC))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
