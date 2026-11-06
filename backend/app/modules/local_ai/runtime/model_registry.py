"""Read-only host model registry and serialized llama.cpp selection."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.paths import build_paths
from app.modules.local_ai.runtime.llama_cpp import (
    LlamaCppRuntimeConfig,
    get_llama_cpp_runtime_owner,
    llama_cpp_runtime_config,
    peek_model_sha256,
)

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SHA = re.compile(r"^[a-fA-F0-9]{64}$")


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str
    runtime: str
    qualification: str
    model_path: str
    model_sha256: str
    ctx_size: int | None = None
    n_gpu_layers: int | None = None
    extra_args: tuple[str, ...] | None = None
    thinking: str | None = None


def read_registry() -> tuple[list[ModelEntry], bool]:
    path = build_paths().local_models_file
    if not path.exists():
        return [], False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw["models"] if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or not rows:
            raise ValueError("models must be a nonempty list")
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid entry")
            if "extra_args" in row and (not isinstance(row["extra_args"], list)
                                        or not all(isinstance(arg, str) for arg in row["extra_args"])):
                raise ValueError("invalid extra arguments")
            entry = ModelEntry(
                id=row["id"], label=row["label"], runtime=row["runtime"],
                qualification=row["qualification"], model_path=row["model_path"],
                model_sha256=row["model_sha256"], ctx_size=row.get("ctx_size"),
                n_gpu_layers=row.get("n_gpu_layers"),
                extra_args=tuple(row["extra_args"]) if "extra_args" in row else None,
                thinking=row.get("thinking"),
            )
            if (not isinstance(entry.id, str) or not _ID.fullmatch(entry.id)
                    or not isinstance(entry.label, str) or not entry.label.strip()
                    or len(entry.label) > 120 or any(char in entry.label for char in "/\\\r\n")
                    or entry.runtime not in {"llama_cpp", "strata"}
                    or entry.qualification not in {"default", "qualified", "under_qualification"}
                    or not isinstance(entry.model_path, str) or not Path(entry.model_path).is_absolute()
                    or not isinstance(entry.model_sha256, str) or not _SHA.fullmatch(entry.model_sha256)
                    or entry.ctx_size is not None and (type(entry.ctx_size) is not int or entry.ctx_size <= 0)
                    or entry.n_gpu_layers is not None and (type(entry.n_gpu_layers) is not int or entry.n_gpu_layers < 0)
                    or entry.extra_args is not None and not all(isinstance(arg, str) for arg in entry.extra_args)
                    or entry.thinking is not None and entry.thinking not in {"default", "on", "off"}):
                raise ValueError("invalid entry fields")
            entries.append(entry)
        if len({entry.id for entry in entries}) != len(entries):
            raise ValueError("duplicate model id")
        if sum(entry.qualification == "default" for entry in entries) != 1 or any(
            entry.qualification == "default" and entry.runtime != "llama_cpp" for entry in entries
        ):
            raise ValueError("registry requires exactly one default")
        return entries, True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Local model registry is invalid.") from exc


def list_models() -> dict[str, Any]:
    entries, present = read_registry()
    owner = get_llama_cpp_runtime_owner()
    if not present:
        return {"registry_present": False, "models": [], "active_id": None}
    default = next(entry for entry in entries if entry.qualification == "default")
    models = []
    for entry in entries:
        installed = Path(entry.model_path).is_file()
        digest, pending = peek_model_sha256(entry.model_path) if installed else (None, False)
        models.append({
            "id": entry.id, "label": entry.label, "runtime": entry.runtime,
            "qualification": entry.qualification, "model_file": Path(entry.model_path).name,
            "installed": installed,
            "digest_state": "missing" if not installed else "hashing" if pending else "not_verified" if digest is None else "verified" if digest.lower() == entry.model_sha256.lower() else "mismatch",
        })
    return {"registry_present": True, "models": models, "active_id": owner.active_model_id or default.id}


def activate_model(model_id: str) -> dict[str, Any]:
    entries, present = read_registry()
    if not present:
        raise HTTPException(status_code=404, detail="Local model registry is not configured.")
    entry = next((item for item in entries if item.id == model_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown local model id.")
    if entry.runtime != "llama_cpp":
        raise HTTPException(status_code=409, detail="This model requires host start.")
    if not Path(entry.model_path).is_file():
        raise HTTPException(status_code=409, detail="Local model file is missing.")
    digest, _ = peek_model_sha256(entry.model_path)
    if digest is not None and digest.lower() != entry.model_sha256.lower():
        raise HTTPException(status_code=409, detail="Local model digest differs from its pinned digest.")
    default = next(item for item in entries if item.qualification == "default")
    owner = get_llama_cpp_runtime_owner()
    current = owner.status()
    if current.get("runtime_reachable") and not current.get("spawned_by_jarvis"):
        raise HTTPException(status_code=409, detail="Externally started llama-server cannot be switched here.")
    return owner.switch_model(config_for_entry(entry), entry.id, default.id)


def config_for_entry(entry: ModelEntry) -> LlamaCppRuntimeConfig:
    shared = llama_cpp_runtime_config()
    overrides: dict[str, Any] = {"model_path": entry.model_path, "model_sha256": entry.model_sha256}
    # The default keeps the alias product calls and job records already use; candidates are served under their own id.
    if entry.qualification != "default":
        overrides["model_id"] = entry.id
    for field in ("ctx_size", "n_gpu_layers", "extra_args", "thinking"):
        value = getattr(entry, field)
        if value is not None:
            overrides[field] = value
    return replace(shared, **overrides)


def restore_default() -> dict[str, Any]:
    entries, present = read_registry()
    if not present:
        raise HTTPException(status_code=404, detail="Local model registry is not configured.")
    return activate_model(next(entry.id for entry in entries if entry.qualification == "default"))
