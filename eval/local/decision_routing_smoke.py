"""Manual 147 runtime smoke: real local model selection, reservation and execution.

Requires a reachable local model runtime and --confirm-live-local. Point
JARVISOS_DATA_ROOT at a disposable directory; never at the operator data root.
Writes one JSON evidence file; every number in it comes from this run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _snapshot_summary(snapshot) -> dict:
    return {
        "generation": snapshot.generation,
        "logical_cores": snapshot.cpu.logical_cores,
        "memory_total_bytes": snapshot.memory.total_bytes,
        "memory_available_bytes": snapshot.memory.available_bytes,
        "gpus": [gpu.model_dump(mode="json") for gpu in snapshot.gpus],
        "loaded_models": [model.name for model in snapshot.loaded_models],
        "active_lease_ids": list(snapshot.active_lease_ids),
    }


def _route(run_local_selected_task, arbiter, capability: str, prompt: str) -> dict:
    started = time.perf_counter()
    outcome = run_local_selected_task(
        user_prompt=prompt, task_kind="general", owner_id=f"smoke-{uuid4()}", arbiter=arbiter, capability=capability
    )
    elapsed = time.perf_counter() - started
    response = outcome.outcome.response if outcome.outcome else None
    return {
        "capability": capability,
        "status": outcome.status,
        "elapsed_s": round(elapsed, 3),
        "model_id": response.model_id if response else None,
        "text_head": (response.text or response.content or "")[:120] if response else None,
        "attempts": [
            {
                "decision_id": attempt.decision.decision_id,
                "outcome": attempt.decision.outcome,
                "selected": attempt.decision.selected_candidate,
                "reason_code": attempt.decision.reason_code,
                "model_ref": attempt.decision.model_ref,
                "lease_id": attempt.lease_id,
                "ledger_id": attempt.ledger_id,
                "status": attempt.status,
            }
            for attempt in outcome.attempts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live-local", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.confirm_live_local:
        print("Refusing to call local models without --confirm-live-local.", file=sys.stderr)
        return 2

    import platform

    from app.core.bootstrap import initialize_storage
    from app.core.paths import build_paths
    from app.modules.local_ai.local_router import run_local_selected_task
    from app.modules.local_ai.resource_arbiter import InProcessResourceArbiter, ResourceCapacityError
    from app.modules.local_ai.resource_contracts import (
        ResourceAmounts,
        ResourceLeaseError,
        ResourceReservationRequest,
    )
    from app.modules.local_ai.runtime.status import get_local_ai_runtime_status

    paths = build_paths()
    initialize_storage(seed_default=True)
    status = get_local_ai_runtime_status()
    report: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "data_root": str(paths.data_root),
        "runtime": {key: status.get(key) for key in ("ollama_reachable", "ollama_version", "endpoint", "error_type")},
        "cases": {},
    }
    if not status.get("ollama_reachable"):
        report["cases"]["unreachable"] = status.get("error_message")
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    arbiter = InProcessResourceArbiter()
    before = arbiter.snapshot()
    report["cases"]["snapshot_before"] = _snapshot_summary(before)
    report["cases"]["cold_start_observed"] = not any(
        model.name == "qwen3:8b" for model in before.loaded_models
    )
    prompt = "Reply with exactly one short sentence explaining what a photobioreactor is."
    report["cases"]["cold_route"] = _route(run_local_selected_task, arbiter, "fast", prompt)
    report["cases"]["loaded_route"] = _route(run_local_selected_task, arbiter, "fast", prompt)
    snapshot = arbiter.snapshot()
    report["cases"]["snapshot_after"] = _snapshot_summary(snapshot)

    now = datetime.now(UTC)

    def _request(model: str, vram: int, generation: int) -> ResourceReservationRequest:
        gpu_index = snapshot.gpus[0].index if snapshot.gpus else None
        return ResourceReservationRequest(
            request_id=str(uuid4()), owner_kind="smoke", owner_id="pressure", correlation_id=str(uuid4()),
            resources=ResourceAmounts(model_name=model, vram_bytes=vram, gpu_index=gpu_index if vram else None),
            snapshot_generation=generation, requested_at=now, deadline_at=now + timedelta(minutes=1),
            max_hold_seconds=30,
        )

    # Resource pressure: a VRAM claim larger than the whole GPU must be refused.
    if snapshot.gpus and snapshot.gpus[0].vram_total_bytes:
        try:
            arbiter.reserve(_request("pressure-probe", snapshot.gpus[0].vram_total_bytes + 1, snapshot.generation))
            report["cases"]["pressure"] = "UNEXPECTED_ADMIT"
        except (ResourceCapacityError, ResourceLeaseError) as exc:
            report["cases"]["pressure"] = {"refused": type(exc).__name__, "detail": str(exc)}
    else:
        report["cases"]["pressure"] = "no GPU observed"

    # Competing reservation race on one loaded model: exactly one winner.
    loaded = [model.name for model in snapshot.loaded_models]
    if loaded:
        fresh = arbiter.snapshot()

        def _contend(_: int) -> str:
            try:
                return arbiter.reserve(_request(loaded[0], 0, fresh.generation)).lease_id
            except (ResourceCapacityError, ResourceLeaseError) as exc:
                return type(exc).__name__

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_contend, range(6)))
        winners = [item for item in results if item not in {"ResourceCapacityError", "ResourceLeaseError"}]
        report["cases"]["race"] = {"model": loaded[0], "results": results, "winners": len(winners)}
        for lease_id in winners:
            arbiter.end(lease_id, expected_version=1, reason="completed")
    else:
        report["cases"]["race"] = "no loaded model to contend for"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report["cases"][key] for key in ("cold_route", "loaded_route")}, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
