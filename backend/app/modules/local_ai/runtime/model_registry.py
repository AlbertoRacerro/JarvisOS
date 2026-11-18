"""Read-only host model registry and serialized llama.cpp selection."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.paths import build_paths
from app.modules.local_ai.runtime.host_start import SYSTEMD_UNIT_RE
from app.modules.local_ai.runtime.llama_cpp import (
    LlamaCppRuntimeConfig,
    get_llama_cpp_runtime_owner,
    llama_cpp_runtime_config,
    peek_model_sha256,
    validate_loopback_host,
)

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SHA = re.compile(r"^[a-fA-F0-9]{64}$")
_UNIT = SYSTEMD_UNIT_RE


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str
    runtime: str
    qualification: str
    model_path: str = ""
    model_sha256: str = ""
    ctx_size: int | None = None
    n_gpu_layers: int | None = None
    extra_args: tuple[str, ...] | None = None
    thinking: str | None = None
    unit: str | None = None
    port: int | None = None
    served_model: str | None = None
    host: str = "127.0.0.1"


def _get_choice_file() -> Path:
    paths = build_paths()
    if hasattr(paths, "local_model_choice_file"):
        return paths.local_model_choice_file
    parent = getattr(paths, "local_models_file", Path(".")).parent
    return parent / "local_model_choice.json"


def read_stored_model_choice() -> str | None:
    try:
        path = _get_choice_file()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            choice = data.get("active_model_id") or data.get("model_id")
            if isinstance(choice, str):
                return choice
    except (OSError, ValueError, AttributeError):
        pass
    return None


def save_model_choice(model_id: str) -> None:
    try:
        path = _get_choice_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f".{path.name}.tmp"
        temp_path.write_text(json.dumps({"active_model_id": model_id}), encoding="utf-8")
        temp_path.replace(path)
    except (OSError, ValueError, AttributeError):
        pass


def determine_initial_model(entries: list[ModelEntry]) -> ModelEntry | None:
    if not entries:
        return None
    default_entry = next((e for e in entries if e.qualification == "default"), None)
    stored_id = read_stored_model_choice()
    stored_entry = next((e for e in entries if e.id == stored_id), None) if stored_id else None
    target = (
        stored_entry
        if stored_entry and stored_entry.qualification in ("default", "qualified") and _host_startable(stored_entry)
        else default_entry
    )

    if target is None:
        return None

    # Without JARVISOS_HOSTOPS_SPOOL behaviour is exactly as today for llama_cpp-only registries;
    # with a strata default and no spool, start the llama_cpp entry.
    from app.modules.local_ai.runtime.host_start import get_spool_path

    if target.runtime == "strata" and get_spool_path() is None:
        llama_entry = next(
            (e for e in entries if e.runtime == "llama_cpp" and e.qualification in ("default", "qualified")),
            None,
        )
        if llama_entry is not None:
            return llama_entry
    return target


def _host_startable(entry: ModelEntry) -> bool:
    return entry.runtime != "strata" or entry.unit is not None or entry.port is not None or entry.served_model is not None


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
            if "extra_args" in row and (
                not isinstance(row["extra_args"], list)
                or not all(isinstance(arg, str) for arg in row["extra_args"])
            ):
                raise ValueError("invalid extra arguments")
            entry = ModelEntry(
                id=row["id"],
                label=row["label"],
                runtime=row["runtime"],
                qualification=row["qualification"],
                model_path=row.get("model_path", ""),
                model_sha256=row.get("model_sha256", ""),
                ctx_size=row.get("ctx_size"),
                n_gpu_layers=row.get("n_gpu_layers"),
                extra_args=tuple(row["extra_args"]) if "extra_args" in row else None,
                thinking=row.get("thinking"),
                unit=row.get("unit"),
                port=row.get("port"),
                served_model=row.get("served_model"),
                host=row.get("host", "127.0.0.1"),
            )
            if (
                not isinstance(entry.id, str)
                or not _ID.fullmatch(entry.id)
                or not isinstance(entry.label, str)
                or not entry.label.strip()
                or len(entry.label) > 120
                or any(char in entry.label for char in "/\\\r\n")
                or entry.runtime not in {"llama_cpp", "strata"}
                or entry.qualification not in {"default", "qualified", "under_qualification"}
            ):
                raise ValueError("invalid entry fields")

            if entry.runtime == "llama_cpp":
                if (
                    not isinstance(entry.model_path, str)
                    or not Path(entry.model_path).is_absolute()
                    or not isinstance(entry.model_sha256, str)
                    or not _SHA.fullmatch(entry.model_sha256)
                    or (entry.ctx_size is not None and (type(entry.ctx_size) is not int or entry.ctx_size <= 0))
                    or (entry.n_gpu_layers is not None and (type(entry.n_gpu_layers) is not int or entry.n_gpu_layers < 0))
                    or (entry.extra_args is not None and not all(isinstance(arg, str) for arg in entry.extra_args))
                    or (entry.thinking is not None and entry.thinking not in {"default", "on", "off"})
                ):
                    raise ValueError("invalid llama_cpp entry fields")
            elif entry.runtime == "strata" and not _host_startable(entry):
                # Registries written before host start (spec 302) list a Strata model by file only; it stays
                # visible and "requires host start", and cannot be the default.
                if (
                    entry.unit is not None
                    or entry.port is not None
                    or entry.served_model is not None
                    or entry.qualification == "default"
                    or not isinstance(entry.model_path, str)
                    or not Path(entry.model_path).is_absolute()
                    or not isinstance(entry.model_sha256, str)
                    or not _SHA.fullmatch(entry.model_sha256)
                ):
                    raise ValueError("invalid strata entry fields")
            elif entry.runtime == "strata":
                if (
                    not isinstance(entry.unit, str)
                    or not _UNIT.fullmatch(entry.unit)
                    or not isinstance(entry.port, int)
                    or isinstance(entry.port, bool)
                    or entry.port < 1
                    or entry.port > 65535
                    or not isinstance(entry.served_model, str)
                    or not entry.served_model.strip()
                    or not isinstance(entry.host, str)
                    or validate_loopback_host(entry.host) is not None
                    or (entry.model_path and (not isinstance(entry.model_path, str) or not Path(entry.model_path).is_absolute()))
                    or (entry.model_sha256 and (not isinstance(entry.model_sha256, str) or not _SHA.fullmatch(entry.model_sha256)))
                ):
                    raise ValueError("invalid strata entry fields")

            entries.append(entry)
        if len({entry.id for entry in entries}) != len(entries):
            raise ValueError("duplicate model id")
        if sum(entry.qualification == "default" for entry in entries) != 1:
            raise ValueError("registry requires exactly one default")
        return entries, True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Local model registry is invalid.") from exc


def get_active_model_entry() -> ModelEntry | None:
    try:
        entries, present = read_registry()
    except HTTPException:
        return None
    if not present or not entries:
        return None
    owner = get_llama_cpp_runtime_owner()
    active_id = owner.active_model_id
    if active_id:
        entry = next((e for e in entries if e.id == active_id), None)
        if entry:
            return entry
    return determine_initial_model(entries)


def list_models() -> dict[str, Any]:
    entries, present = read_registry()
    owner = get_llama_cpp_runtime_owner()
    if not present:
        return {"registry_present": False, "models": [], "active_id": None}
    initial = determine_initial_model(entries)
    active_id = owner.active_model_id or (initial.id if initial else None)
    models = []
    from app.modules.local_ai.runtime.host_start import StrataModelState, get_strata_controller

    strata_ctrl = get_strata_controller()
    for entry in entries:
        if entry.runtime == "strata":
            st = (
                strata_ctrl.get_state(entry.id, entry.host, entry.port or 8080, entry.served_model or "")
                if _host_startable(entry)
                else StrataModelState(state="requires host start", reason="This model requires host start.")
            )
            state = st.state
            installed = Path(entry.model_path).is_file() if entry.model_path else True
            digest, pending = peek_model_sha256(entry.model_path) if (entry.model_path and installed) else (None, False)
            digest_state = (
                "missing" if (entry.model_path and not installed)
                else "hashing" if pending
                else "not_verified" if digest is None or not entry.model_sha256
                else "verified" if digest.lower() == entry.model_sha256.lower()
                else "mismatch"
            )
            model_file = Path(entry.model_path).name if entry.model_path else (entry.served_model or "")
            models.append({
                "id": entry.id,
                "label": entry.label,
                "runtime": entry.runtime,
                "qualification": entry.qualification,
                "model_file": model_file,
                "installed": installed,
                "digest_state": digest_state,
                "state": state,
                "state_reason": st.reason,
                "elapsed_seconds": st.elapsed_seconds,
            })
        else:
            installed = Path(entry.model_path).is_file()
            digest, pending = peek_model_sha256(entry.model_path) if installed else (None, False)
            digest_state = (
                "missing"
                if not installed
                else "hashing"
                if pending
                else "not_verified"
                if digest is None
                else "verified"
                if digest.lower() == entry.model_sha256.lower()
                else "mismatch"
            )
            if active_id == entry.id:
                owner_status = owner.status() if hasattr(owner, "status") else {}
                if owner_status.get("model_loaded"):
                    state = "ready"
                elif owner_status.get("reason_code") in ("LLAMACPP_LOADING", "LLAMACPP_RECOVERING"):
                    state = "loading"
                elif owner_status.get("reason_code") == "LLAMACPP_CRASH_LOOP" or (
                    owner_status.get("last_exit_code") and not owner_status.get("spawned_by_jarvis")
                ):
                    state = "failed"
                elif owner_status.get("runtime_reachable"):
                    state = "ready"
                else:
                    state = "stopped"
            else:
                state = "stopped"
            models.append({
                "id": entry.id,
                "label": entry.label,
                "runtime": entry.runtime,
                "qualification": entry.qualification,
                "model_file": Path(entry.model_path).name,
                "installed": installed,
                "digest_state": digest_state,
                "state": state,
            })
    return {"registry_present": True, "models": models, "active_id": active_id}


def activate_model(model_id: str) -> dict[str, Any]:
    entries, present = read_registry()
    if not present:
        raise HTTPException(status_code=404, detail="Local model registry is not configured.")
    entry = next((item for item in entries if item.id == model_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown local model id.")

    from contextlib import nullcontext

    from app.modules.local_ai.runtime.host_start import get_spool_path, get_strata_controller
    owner = get_llama_cpp_runtime_owner()
    default = next(item for item in entries if item.qualification == "default")
    lock = getattr(owner, "_lock", None) or nullcontext()

    with lock:
        current_id = getattr(owner, "active_model_id", None) or default.id
        current_entry = next((item for item in entries if item.id == current_id), None)

        if entry.runtime == "strata":
            spool = get_spool_path()
            if spool is None or not _host_startable(entry):
                raise HTTPException(status_code=409, detail="This model requires host start.")
            current = owner.status()
            if current.get("runtime_reachable") and not current.get("spawned_by_jarvis") and (
                current_entry is None or current_entry.runtime == "llama_cpp"
            ):
                raise HTTPException(status_code=409, detail="Externally started llama-server cannot be switched here.")
            owner.stop()
            strata_ctrl = get_strata_controller()
            strata_ctrl.start(entry.id, entry.unit or "", entry.host, entry.port or 8080, entry.served_model or "")
            owner.active_model_id = entry.id
            save_model_choice(entry.id)
            return owner.status()

        if not Path(entry.model_path).is_file():
            raise HTTPException(status_code=409, detail="Local model file is missing.")
        digest, _ = peek_model_sha256(entry.model_path)
        if digest is not None and digest.lower() != entry.model_sha256.lower():
            raise HTTPException(status_code=409, detail="Local model digest differs from its pinned digest.")

        if current_entry and current_entry.runtime == "strata":
            spool = get_spool_path()
            if spool is None:
                # Without the spool the backend cannot stop Strata; refuse while its port still answers.
                st = get_strata_controller().get_state(
                    current_entry.id, current_entry.host, current_entry.port or 8080, current_entry.served_model or ""
                )
                if st.state == "ready":
                    raise HTTPException(status_code=409, detail="The previous local model has not stopped yet.")
            else:
                strata_ctrl = get_strata_controller()
                stopped = strata_ctrl.stop(
                    current_entry.id,
                    current_entry.unit or "",
                    current_entry.host,
                    current_entry.port or 8080,
                    current_entry.served_model or "",
                )
                # One hot model on the card: never spawn llama-server while the previous model still answers.
                if stopped.state != "stopped":
                    raise HTTPException(status_code=409, detail="The previous local model has not stopped yet.")

        if current_entry and current_entry.runtime == "llama_cpp":
            current = owner.status()
            if current.get("runtime_reachable") and not current.get("spawned_by_jarvis"):
                raise HTTPException(status_code=409, detail="Externally started llama-server cannot be switched here.")

        if current_entry and current_entry.runtime == "strata":
            # Strata is stopped; the owner refuses to spawn llama-server while a Strata entry is active.
            owner.active_model_id = entry.id
        result = owner.switch_model(config_for_entry(entry), entry.id, default.id)
        save_model_choice(entry.id)
        return result


def config_for_entry(entry: ModelEntry) -> LlamaCppRuntimeConfig:
    shared = llama_cpp_runtime_config()
    overrides: dict[str, Any] = {}
    if entry.model_path:
        overrides["model_path"] = entry.model_path
    if entry.model_sha256:
        overrides["model_sha256"] = entry.model_sha256
    if entry.qualification != "default":
        overrides["model_id"] = entry.id
    for field in ("ctx_size", "n_gpu_layers", "extra_args", "thinking"):
        value = getattr(entry, field, None)
        if value is not None:
            overrides[field] = value
    return replace(shared, **overrides)


def start_configured_model() -> None:
    from app.modules.local_ai.runtime.host_start import get_spool_path, get_strata_controller

    owner = get_llama_cpp_runtime_owner()
    active = get_active_model_entry()
    if active and active.runtime == "strata":
        spool = get_spool_path()
        if spool:
            get_strata_controller().start(
                active.id,
                active.unit or "",
                active.host,
                active.port or 8080,
                active.served_model or "",
            )
            return
    if llama_cpp_runtime_config().manage:
        owner.start()


def restore_default() -> dict[str, Any]:
    entries, present = read_registry()
    if not present:
        raise HTTPException(status_code=404, detail="Local model registry is not configured.")
    default = next(entry for entry in entries if entry.qualification == "default")
    return activate_model(default.id)
