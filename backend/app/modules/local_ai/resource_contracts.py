"""145 frozen local resource arbitration contracts.

Decision recommends; deterministic reservation/revalidation admits execution.
This module freezes the snapshot/request/lease shapes and the pure lease state
machine (generation revalidation, version CAS, expiry). It owns no store.

Store ownership: the single arbiter is the L-147 local resource supervisor in
the backend process, which already dispatches every local model call through
``run_ai_task`` (Hermes inference is relayed, never direct). A lease therefore
does not need to outlive the backend: after a restart the arbiter re-observes
runtime truth (the local model runtime status probe) into a new snapshot generation. If a
later accepted requirement needs cross-process or restart-durable leases, that
persistence is a K-owned additive migration storing exactly these fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from pydantic import Field, model_validator

from app.modules.ai.jarvis_context_models import (
    ContractId,
    FrozenContract,
    OwnerToken,
    SourceRef,
    UtcDatetime,
)

RESOURCE_CONTRACTS_VERSION: Final = "resource_contracts.v1"
ResourceContractsVersion = Literal["resource_contracts.v1"]

MAX_SNAPSHOT_ITEMS = 64
MAX_LEASE_HOLD_SECONDS = 86_400

LeaseState = Literal["active", "released", "expired"]
ReleaseReason = Literal["completed", "cancelled", "failed", "deadline_exceeded", "revoked", "owner_lost", "expired"]
WorkerState = Literal["idle", "busy", "unavailable", "unknown"]
LeaseRefusalCode = Literal["stale_snapshot", "deadline_exceeded", "stale_version", "lease_not_active", "invalid_transition"]


class ResourceLeaseError(ValueError):
    def __init__(self, code: LeaseRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- Snapshot ---------------------------------------------------------------


class CpuState(FrozenContract):
    logical_cores: int | None = Field(default=None, ge=1)
    utilization: float | None = Field(default=None, ge=0.0, le=1.0)


class MemoryState(FrozenContract):
    total_bytes: int | None = Field(default=None, ge=0)
    available_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def available_within_total(self) -> MemoryState:
        if self.total_bytes is not None and self.available_bytes is not None and self.available_bytes > self.total_bytes:
            raise ValueError("available_bytes cannot exceed total_bytes")
        return self


class GpuState(FrozenContract):
    index: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    vram_total_bytes: int | None = Field(default=None, ge=0)
    vram_used_bytes: int | None = Field(default=None, ge=0)


class LoadedModelState(FrozenContract):
    """One loaded local model, typable from ``get_local_ai_runtime_status()['loaded_models']``."""

    name: str = Field(min_length=1, max_length=256)
    size_bytes: int | None = Field(default=None, ge=0)
    vram_bytes: int | None = Field(default=None, ge=0)
    processor: str | None = Field(default=None, max_length=64)
    keep_alive_until: UtcDatetime | None = None


class WorkerOccupancy(FrozenContract):
    worker_id: ContractId
    worker_kind: OwnerToken
    state: WorkerState
    lease_ids: tuple[ContractId, ...] = Field(default=(), max_length=MAX_SNAPSHOT_ITEMS)


class RuntimeResourceSnapshot(FrozenContract):
    """Observed resource truth. ``None`` means unobserved, never zero.

    ``generation`` is issued by the arbiter and increases whenever observed
    state or the active lease set changes; requests ranked against an older
    generation are refused at reservation time.
    """

    schema_version: ResourceContractsVersion = RESOURCE_CONTRACTS_VERSION
    generation: int = Field(ge=0)
    observed_at: UtcDatetime
    cpu: CpuState = CpuState()
    memory: MemoryState = MemoryState()
    gpus: tuple[GpuState, ...] = Field(default=(), max_length=MAX_SNAPSHOT_ITEMS)
    loaded_models: tuple[LoadedModelState, ...] = Field(default=(), max_length=MAX_SNAPSHOT_ITEMS)
    workers: tuple[WorkerOccupancy, ...] = Field(default=(), max_length=MAX_SNAPSHOT_ITEMS)
    active_lease_ids: tuple[ContractId, ...] = Field(default=(), max_length=MAX_SNAPSHOT_ITEMS)


# --- Reservation / lease ----------------------------------------------------


class ResourceAmounts(FrozenContract):
    cpu_cores: int = Field(default=0, ge=0)
    ram_bytes: int = Field(default=0, ge=0)
    vram_bytes: int = Field(default=0, ge=0)
    gpu_index: int | None = Field(default=None, ge=0)
    model_name: str | None = Field(default=None, min_length=1, max_length=256)
    worker_kind: OwnerToken | None = None

    @model_validator(mode="after")
    def require_some_resource(self) -> ResourceAmounts:
        if not (self.cpu_cores or self.ram_bytes or self.vram_bytes or self.model_name or self.worker_kind):
            raise ValueError("reservation must request at least one resource")
        return self


class ResourceReservationRequest(FrozenContract):
    """``snapshot_generation`` is the generation the requester ranked against."""

    schema_version: ResourceContractsVersion = RESOURCE_CONTRACTS_VERSION
    request_id: ContractId
    owner_kind: OwnerToken
    owner_id: ContractId
    correlation_id: ContractId
    resources: ResourceAmounts
    snapshot_generation: int = Field(ge=0)
    requested_at: UtcDatetime
    deadline_at: UtcDatetime
    max_hold_seconds: int = Field(ge=1, le=MAX_LEASE_HOLD_SECONDS)
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def deadline_after_request(self) -> ResourceReservationRequest:
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self


class ResourceLease(FrozenContract):
    """Granted reservation. ``version`` is the CAS token for every transition."""

    schema_version: ResourceContractsVersion = RESOURCE_CONTRACTS_VERSION
    lease_id: ContractId
    request: ResourceReservationRequest
    state: LeaseState
    version: int = Field(ge=1)
    granted_generation: int = Field(ge=0)
    granted_at: UtcDatetime
    expires_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    release_reason: ReleaseReason | None = None

    @model_validator(mode="after")
    def validate_state(self) -> ResourceLease:
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be after granted_at")
        if self.expires_at > self.granted_at + timedelta(seconds=self.request.max_hold_seconds):
            raise ValueError("expires_at exceeds the requested max_hold_seconds")
        if self.state == "active":
            if self.ended_at is not None or self.release_reason is not None:
                raise ValueError("active leases have no ended_at or release_reason")
        else:
            if self.ended_at is None or self.release_reason is None:
                raise ValueError("ended leases require ended_at and release_reason")
            if (self.state == "expired") != (self.release_reason == "expired"):
                raise ValueError("state expired and release_reason expired must coincide")
        return self

    def is_live(self, now: datetime) -> bool:
        return self.state == "active" and now < self.expires_at


def grant_lease(
    request: ResourceReservationRequest,
    *,
    lease_id: str,
    current_generation: int,
    now: datetime,
) -> ResourceLease:
    """Admit a request only against the arbiter's current generation and before its deadline.

    Capacity arithmetic is arbiter policy (L); this enforces the frozen
    revalidation rule every arbiter must apply first.
    """
    if now >= request.deadline_at:
        raise ResourceLeaseError("deadline_exceeded", "reservation deadline has passed")
    if request.snapshot_generation != current_generation:
        raise ResourceLeaseError(
            "stale_snapshot",
            f"request ranked against generation {request.snapshot_generation}, current is {current_generation}",
        )
    return ResourceLease(
        lease_id=lease_id,
        request=request,
        state="active",
        version=1,
        granted_generation=current_generation,
        granted_at=now,
        expires_at=now + timedelta(seconds=request.max_hold_seconds),
    )


def end_lease(lease: ResourceLease, *, expected_version: int, reason: ReleaseReason, now: datetime) -> ResourceLease:
    """CAS transition active -> released|expired. Ended leases are terminal.

    A lease past ``expires_at`` always ends as ``expired`` whatever reason the
    holder reports, so late releases cannot rewrite expiry history.
    """
    if lease.version != expected_version:
        raise ResourceLeaseError(
            "stale_version", f"expected lease version {expected_version}, current is {lease.version}"
        )
    if lease.state != "active":
        raise ResourceLeaseError("lease_not_active", f"lease is already {lease.state}")
    expired = reason == "expired" or now >= lease.expires_at
    if reason == "expired" and now < lease.expires_at:
        raise ResourceLeaseError("invalid_transition", "lease cannot expire before expires_at")
    return ResourceLease.model_validate(
        {
            **lease.model_dump(),
            "state": "expired" if expired else "released",
            "version": lease.version + 1,
            "ended_at": max(now, lease.granted_at),
            "release_reason": "expired" if expired else reason,
        }
    )


class ResourceArbiter(Protocol):
    """The one deterministic admission owner (implemented by L-147)."""

    def snapshot(self) -> RuntimeResourceSnapshot: ...

    def reserve(self, request: ResourceReservationRequest) -> ResourceLease:
        """Atomically revalidate (``grant_lease``) and apply capacity policy, or raise ResourceLeaseError."""
        ...

    def end(self, lease_id: str, *, expected_version: int, reason: ReleaseReason) -> ResourceLease:
        """Atomic ``end_lease`` against the arbiter's current copy of the lease."""
        ...
