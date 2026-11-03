"""Single-process atomic resource observation and lease admission."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from threading import Lock
from uuid import uuid4

from app.modules.local_ai.resource_contracts import (
    ReleaseReason,
    ResourceAmounts,
    ResourceLease,
    ResourceLeaseError,
    ResourceReservationRequest,
    RuntimeResourceSnapshot,
    end_lease,
    grant_lease,
)
from app.modules.local_ai.runtime.resource_observation import observe_resources


class ResourceCapacityError(ValueError):
    pass


class InProcessResourceArbiter:
    """Unknown numeric capacity refuses a positive request; known capacity is never assumed free.

    Loaded-model identity alone can be reserved without a numeric claim. A cold
    model requires an explicit known RAM or VRAM footprint and observed capacity.
    One active lease per model; worker requests require an observed idle worker.
    """

    def __init__(self, observe: Callable[[], RuntimeResourceSnapshot] = observe_resources) -> None:
        self._observe = observe
        self._lock = Lock()
        self._snapshot: RuntimeResourceSnapshot | None = None
        self._leases: dict[str, ResourceLease] = {}
        self._generation = 0

    @staticmethod
    def _identity(snapshot: RuntimeResourceSnapshot) -> tuple[object, ...]:
        """Only changes to admission identity invalidate a ranked snapshot."""
        return (
            snapshot.cpu.logical_cores,
            snapshot.memory.total_bytes,
            frozenset((gpu.index, gpu.name, gpu.vram_total_bytes) for gpu in snapshot.gpus),
            frozenset(model.name for model in snapshot.loaded_models),
            frozenset(
                (worker.worker_id, worker.worker_kind, worker.state, worker.lease_ids) for worker in snapshot.workers
            ),
        )

    def _refresh(self, now: datetime) -> RuntimeResourceSnapshot:
        for lease_id, lease in tuple(self._leases.items()):
            if lease.state == "active" and not lease.is_live(now):
                self._leases[lease_id] = end_lease(lease, expected_version=lease.version, reason="expired", now=now)
                self._generation += 1
        observed = self._observe()
        previous = self._snapshot
        if previous is None or self._identity(previous) != self._identity(observed):
            self._generation += 1
        active = tuple(key for key, lease in self._leases.items() if lease.state == "active")
        self._snapshot = observed.model_copy(update={"generation": self._generation, "active_lease_ids": active})
        return self._snapshot

    def snapshot(self) -> RuntimeResourceSnapshot:
        with self._lock:
            return self._refresh(datetime.now(UTC))

    def _fits(self, request: ResourceAmounts, snapshot: RuntimeResourceSnapshot) -> bool:
        active = [lease.request.resources for lease in self._leases.values() if lease.state == "active"]
        cpu = snapshot.cpu.logical_cores
        utilization = snapshot.cpu.utilization
        if request.cpu_cores and (
            cpu is None
            or utilization is None
            or request.cpu_cores + sum(r.cpu_cores for r in active) > int(cpu * (1 - utilization))
        ):
            return False
        ram = snapshot.memory.available_bytes
        if request.ram_bytes and (ram is None or request.ram_bytes + sum(r.ram_bytes for r in active) > ram):
            return False
        if request.vram_bytes:
            gpu = next((gpu for gpu in snapshot.gpus if gpu.index == request.gpu_index), None)
            if gpu is None or gpu.vram_total_bytes is None or gpu.vram_used_bytes is None:
                return False
            reserved = sum(r.vram_bytes for r in active if r.gpu_index == request.gpu_index)
            if request.vram_bytes + reserved > gpu.vram_total_bytes - gpu.vram_used_bytes:
                return False
        if request.model_name:
            if any(r.model_name == request.model_name for r in active):
                return False
            loaded = any(model.name == request.model_name for model in snapshot.loaded_models)
            if not loaded and not (request.ram_bytes or request.vram_bytes):
                return False
        if request.worker_kind:
            idle = sum(
                1 for worker in snapshot.workers if worker.worker_kind == request.worker_kind and worker.state == "idle"
            )
            reserved = sum(1 for r in active if r.worker_kind == request.worker_kind)
            if reserved >= idle:
                return False
        return True

    def reserve(self, request: ResourceReservationRequest) -> ResourceLease:
        with self._lock:
            now = datetime.now(UTC)
            snapshot = self._refresh(now)
            lease = grant_lease(request, lease_id=str(uuid4()), current_generation=snapshot.generation, now=now)
            if not self._fits(request.resources, snapshot):
                raise ResourceCapacityError("observed capacity does not fit reservation")
            self._leases[lease.lease_id] = lease
            self._generation += 1
            return lease

    def end(self, lease_id: str, *, expected_version: int, reason: ReleaseReason) -> ResourceLease:
        with self._lock:
            now = datetime.now(UTC)
            self._refresh(now)
            lease = self._leases.get(lease_id)
            if lease is None:
                raise ResourceLeaseError("lease_not_active", "unknown lease")
            ended = end_lease(lease, expected_version=expected_version, reason=reason, now=now)
            self._leases[lease_id] = ended
            self._generation += 1
            return ended


@lru_cache(maxsize=1)
def get_resource_arbiter() -> InProcessResourceArbiter:
    """Return the process's sole production lease owner."""
    return InProcessResourceArbiter()
