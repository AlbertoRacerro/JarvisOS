from __future__ import annotations

import queue
from typing import Any

from app.modules.bluecad.builders import build_part
from app.modules.bluecad.service import build_geometry_spec


def _tube_spec() -> dict[str, object]:
    return {
        "spec_version": "bluecad_geometry_spec_v0_1",
        "name": "spawn_isolation_probe",
        "parts": [
            {
                "part_id": "probe_tube",
                "kind": "tube_run",
                "params": {
                    "outer_d": 20.0,
                    "wall_t": 2.0,
                    "length": 100.0,
                },
            }
        ],
        "connections": [],
    }


def test_build_worker_survives_parent_kernel_initialization(tmp_path) -> None:
    spec = _tube_spec()
    build_part(spec["parts"][0])

    result = build_geometry_spec(spec, tmp_path / "build", timeout_s=20.0)

    assert result.verdict == "pass", result.report
    assert result.manifest_path is not None and result.manifest_path.is_file()
    assert result.report_path is not None and result.report_path.is_file()
    assert (tmp_path / "build" / "model.step").is_file()
    assert (tmp_path / "build" / "model.stl").is_file()
    assert (tmp_path / "build" / "model.glb").is_file()


def test_dead_worker_is_detected_without_waiting_full_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    import app.modules.bluecad.service as service

    observed_timeouts: list[float] = []

    class EmptyQueue:
        def get(self, *, timeout: float) -> dict[str, Any]:
            observed_timeouts.append(timeout)
            raise queue.Empty

        def close(self) -> None:
            return None

    class DeadProcess:
        exitcode = 17

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout: float | None = None) -> None:
            return None

        def kill(self) -> None:
            raise AssertionError("dead worker must not be killed")

    class SpawnContext:
        def Queue(self, *, maxsize: int) -> EmptyQueue:
            assert maxsize == 1
            return EmptyQueue()

        def Process(self, **kwargs: Any) -> DeadProcess:
            assert kwargs["daemon"] is True
            return DeadProcess()

    def get_context(method: str) -> SpawnContext:
        assert method == "spawn"
        return SpawnContext()

    monkeypatch.setattr(service.mp, "get_context", get_context)

    result = service.build_geometry_spec(
        _tube_spec(),
        tmp_path / "dead-worker",
        timeout_s=10.0,
    )

    assert result.verdict == "error"
    assert result.errors[0]["code"] == "KERNEL_ERROR"
    assert observed_timeouts
    assert max(observed_timeouts) <= service._WORKER_POLL_SECONDS


def test_worker_start_failure_returns_bounded_error_result(
    tmp_path,
    monkeypatch,
) -> None:
    import app.modules.bluecad.service as service

    class StartQueue:
        def close(self) -> None:
            return None

    class StartFailProcess:
        def start(self) -> None:
            raise RuntimeError("process capacity exhausted")

    class StartFailContext:
        def Queue(self, *, maxsize: int) -> StartQueue:
            assert maxsize == 1
            return StartQueue()

        def Process(self, **kwargs: Any) -> StartFailProcess:
            assert kwargs["daemon"] is True
            return StartFailProcess()

    monkeypatch.setattr(
        service.mp,
        "get_context",
        lambda method: StartFailContext() if method == "spawn" else None,
    )

    result = service.build_geometry_spec(
        _tube_spec(),
        tmp_path / "start-failure",
        timeout_s=10.0,
    )

    assert result.verdict == "error"
    assert result.errors[0]["code"] == "KERNEL_ERROR"
    assert result.report_path.is_file()
    assert result.report["errors"][0]["detail"]["message"] == ("build worker could not start")
