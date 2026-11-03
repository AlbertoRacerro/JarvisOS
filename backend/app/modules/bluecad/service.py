"""BLUECAD CAD adapter public build API."""

from __future__ import annotations

import json
import multiprocessing as mp
import queue
import time
from pathlib import Path
from typing import Any

from app.modules.bluecad.export import ARTIFACT_NAMES, build_artifacts
from app.modules.bluecad.models import BluecadError, BuildResult
from app.modules.bluecad.spec import (
    SpecValidationError,
    canonicalize_geometry_spec,
    load_geometry_spec,
)
from app.modules.bluecad.validate import write_validation_report

DEFAULT_TIMEOUT_SECONDS = 30.0
_WORKER_JOIN_GRACE_SECONDS = 1.0
_WORKER_POLL_SECONDS = 0.1
_BUILD_PHASE_FILENAME = ".bluecad_build_phase"


def build_geometry_spec(
    spec: dict[str, Any],
    out_dir: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
) -> BuildResult:
    canonical = canonicalize_geometry_spec(spec)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    context = mp.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_worker,
        args=(canonical, str(out_path), result_queue),
        daemon=True,
    )
    try:
        process.start()
    except Exception as exc:
        result_queue.close()
        error = BluecadError(
            "KERNEL_ERROR",
            {
                "message": "build worker could not start",
                "type": type(exc).__name__,
            },
        )
        report = write_validation_report(canonical, out_path, error=error)
        return BuildResult(
            canonical["spec_id"],
            out_path,
            None,
            out_path / "validation_report.json",
            None,
            report,
            "error",
            [error.as_report_error()],
        )
    payload: dict[str, Any] | None = None
    worker_exited = False
    deadline = time.monotonic() + timeout_s
    try:
        while payload is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            try:
                payload = result_queue.get(timeout=min(_WORKER_POLL_SECONDS, remaining))
            except queue.Empty:
                if process.is_alive():
                    continue
                process.join()
                try:
                    payload = result_queue.get(timeout=_WORKER_POLL_SECONDS)
                except queue.Empty:
                    worker_exited = True
                break

        if payload is None:
            if process.is_alive():
                process.kill()
                process.join()
                error = BluecadError(
                    "TIMEOUT",
                    {
                        "timeout_s": timeout_s,
                        "phase": _read_and_remove_build_phase(out_path),
                        "partial_artifacts": _partial_artifact_sizes(out_path),
                    },
                )
            else:
                if not worker_exited:
                    process.join()
                error = BluecadError(
                    "KERNEL_ERROR",
                    {
                        "message": "worker exited without returning a result",
                        "exitcode": process.exitcode,
                        "phase": _read_and_remove_build_phase(out_path),
                        "partial_artifacts": _partial_artifact_sizes(out_path),
                    },
                )
            report = write_validation_report(canonical, out_path, error=error)
            return BuildResult(
                canonical["spec_id"],
                out_path,
                None,
                out_path / "validation_report.json",
                None,
                report,
                "error",
                [error.as_report_error()],
            )
    finally:
        result_queue.close()

    process.join(_WORKER_JOIN_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()
    _read_and_remove_build_phase(out_path)
    if payload["ok"]:
        manifest = payload["manifest"]
        report = write_validation_report(canonical, out_path)
        return BuildResult(
            canonical["spec_id"],
            out_path,
            out_path / "manifest.json",
            out_path / "validation_report.json",
            manifest,
            report,
            report["verdict"],
        )
    error = BluecadError(payload["code"], payload["detail"])
    report = write_validation_report(canonical, out_path, error=error)
    return BuildResult(
        canonical["spec_id"],
        out_path,
        None,
        out_path / "validation_report.json",
        None,
        report,
        "error",
        [error.as_report_error()],
    )


def build_geometry_spec_file(
    spec_path: str | Path,
    out_dir: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
) -> BuildResult:
    try:
        spec = load_geometry_spec(spec_path)
    except SpecValidationError as exc:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        report = {
            "report_version": "bluecad_validation_report_v0_1",
            "spec_id": "sha256:" + "0" * 64,
            "manifest_sha256": None,
            "verdict": "error",
            "checks": [],
            "errors": [{"code": exc.code, "detail": exc.detail}],
        }
        report_path = out_path / "validation_report.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return BuildResult(
            report["spec_id"],
            out_path,
            None,
            report_path,
            None,
            report,
            "error",
            report["errors"],
        )
    return build_geometry_spec(spec, out_dir, timeout_s=timeout_s)


def _partial_artifact_sizes(out_path: Path) -> dict[str, int | None]:
    evidence: dict[str, int | None] = {}
    for name in ARTIFACT_NAMES:
        path = out_path / name
        try:
            evidence[name] = path.stat().st_size if path.is_file() else None
        except OSError:
            evidence[name] = None
    return evidence


def _read_and_remove_build_phase(out_path: Path) -> str | None:
    path = out_path / _BUILD_PHASE_FILENAME
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return value[:64] or None


def _worker(
    spec: dict[str, Any],
    out_dir: str,
    result_queue: Any,
) -> None:
    try:
        manifest = build_artifacts(spec, out_dir)
    except BluecadError as exc:
        result_queue.put({"ok": False, "code": exc.code, "detail": exc.detail})
    except Exception as exc:  # pragma: no cover - kernel crash/error protection
        result_queue.put(
            {
                "ok": False,
                "code": "KERNEL_ERROR",
                "detail": {"message": str(exc), "type": type(exc).__name__},
            }
        )
    else:
        result_queue.put({"ok": True, "manifest": manifest})
