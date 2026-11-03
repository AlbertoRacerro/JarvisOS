from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from app.modules.coding.repository_truth import (
    RepositoryTruthError,
    RepositoryTruthResult,
    RepositoryTruthService,
)

CANONICAL_RUNTIME_REPOSITORY = "AlbertoRacerro/JarvisOS"
SNAPSHOT_DEADLINE_SECONDS = 2.0
MAX_PROBE_OUTPUT_BYTES = 65_536
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
logger = logging.getLogger(__name__)

HeadState = Literal["branch", "detached", "unavailable"]
DirtyState = Literal["clean", "dirty", "unknown"]
Alignment = Literal["aligned", "local_behind", "divergent", "unknown"]
ObserverStatus = Literal["ok", "degraded", "unavailable"]
LocalFailureCode = Literal[
    "git_unavailable",
    "not_git_worktree",
    "root_mismatch",
    "probe_timeout",
    "probe_output_oversized",
    "malformed_probe_output",
    "probe_failed",
    "dirty_state_unavailable",
    "startup_snapshot_unavailable",
    "repository_mismatch",
    "remote_target_unavailable",
    "remote_target_moved",
    "remote_relation_unavailable",
]


@dataclass(frozen=True)
class RuntimeSnapshot:
    root_identity: str
    observed_at: str
    git_available: bool
    git_sha: str | None
    head_state: HeadState
    branch: str | None
    dirty_state: DirtyState
    provenance: Literal["process_start_observation", "live_worktree_observation"]
    failure_code: LocalFailureCode | None


@dataclass(frozen=True)
class ProbeResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass
class _PipeCapture:
    data: bytearray
    oversized: bool = False


def trusted_repository_root() -> Path:
    """Derive the JarvisOS root from this module; callers cannot override it."""
    return Path(__file__).resolve().parents[4]


def _root_identity(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _minimal_git_environment() -> dict[str, str]:
    env: dict[str, str] = {
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key in (
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _drain_pipe(
    stream: BinaryIO,
    capture: _PipeCapture,
    limit: int = MAX_PROBE_OUTPUT_BYTES,
) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            room = limit + 1 - len(capture.data)
            if room > 0:
                capture.data.extend(chunk[:room])
            if len(capture.data) > limit or len(chunk) > room:
                capture.oversized = True
    finally:
        stream.close()


def _run_git_probe(
    root: Path,
    args: tuple[str, ...],
    timeout: float,
    max_output_bytes: int = MAX_PROBE_OUTPUT_BYTES,
) -> ProbeResult:
    if timeout <= 0:
        raise TimeoutError("runtime truth snapshot deadline exhausted")
    try:
        process = subprocess.Popen(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=str(root),
            env=_minimal_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError("git probe failed to start") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _PipeCapture(bytearray())
    stderr_capture = _PipeCapture(bytearray())
    stdout_thread = threading.Thread(
        target=_drain_pipe,
        args=(process.stdout, stdout_capture, max_output_bytes),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_pipe,
        args=(process.stderr, stderr_capture, max_output_bytes),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise TimeoutError("git probe timed out") from exc
    stdout_thread.join()
    stderr_thread.join()
    if stdout_capture.oversized or stderr_capture.oversized:
        raise OverflowError("git probe output exceeded bound")
    return ProbeResult(
        returncode=returncode,
        stdout=bytes(stdout_capture.data),
        stderr=bytes(stderr_capture.data),
    )


def _decode_probe(raw: bytes) -> str:
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("git probe output is not UTF-8") from exc


def _failed_snapshot(
    root: Path,
    provenance: Literal[
        "process_start_observation",
        "live_worktree_observation",
    ],
    code: LocalFailureCode,
    *,
    git_available: bool = True,
    git_sha: str | None = None,
    head_state: HeadState = "unavailable",
    branch: str | None = None,
    dirty_state: DirtyState = "unknown",
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        root_identity=_root_identity(root),
        observed_at=datetime.now(UTC).isoformat(),
        git_available=git_available,
        git_sha=git_sha,
        head_state=head_state,
        branch=branch,
        dirty_state=dirty_state,
        provenance=provenance,
        failure_code=code,
    )


def capture_runtime_snapshot(
    *,
    provenance: Literal[
        "process_start_observation",
        "live_worktree_observation",
    ],
    root: Path | None = None,
) -> RuntimeSnapshot:
    expected_root = (root or trusted_repository_root()).resolve()
    deadline = time.monotonic() + SNAPSHOT_DEADLINE_SECONDS

    def probe(args: tuple[str, ...]) -> ProbeResult:
        remaining = max(0.0, deadline - time.monotonic())
        return _run_git_probe(expected_root, args, remaining)

    try:
        top = probe(("rev-parse", "--show-toplevel"))
    except FileNotFoundError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "git_unavailable",
            git_available=False,
        )
    except TimeoutError:
        return _failed_snapshot(expected_root, provenance, "probe_timeout")
    except OverflowError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_output_oversized",
        )
    except (OSError, RuntimeError):
        return _failed_snapshot(expected_root, provenance, "probe_failed")
    if top.returncode != 0:
        return _failed_snapshot(expected_root, provenance, "not_git_worktree")
    try:
        observed_root = Path(_decode_probe(top.stdout)).resolve()
    except (ValueError, OSError):
        return _failed_snapshot(
            expected_root,
            provenance,
            "malformed_probe_output",
        )
    if os.path.normcase(str(observed_root)) != os.path.normcase(str(expected_root)):
        return _failed_snapshot(expected_root, provenance, "root_mismatch")

    try:
        head = probe(("rev-parse", "--verify", "HEAD"))
    except TimeoutError:
        return _failed_snapshot(expected_root, provenance, "probe_timeout")
    except OverflowError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_output_oversized",
        )
    except (OSError, RuntimeError):
        return _failed_snapshot(expected_root, provenance, "probe_failed")
    if head.returncode != 0:
        return _failed_snapshot(expected_root, provenance, "probe_failed")
    try:
        git_sha = _decode_probe(head.stdout).lower()
    except ValueError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "malformed_probe_output",
        )
    if not _SHA_RE.fullmatch(git_sha):
        return _failed_snapshot(
            expected_root,
            provenance,
            "malformed_probe_output",
        )

    head_state: HeadState = "unavailable"
    branch: str | None = None
    try:
        symbolic = probe(("symbolic-ref", "--quiet", "--short", "HEAD"))
    except TimeoutError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_timeout",
            git_sha=git_sha,
        )
    except OverflowError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_output_oversized",
            git_sha=git_sha,
        )
    except (OSError, RuntimeError):
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_failed",
            git_sha=git_sha,
        )
    if symbolic.returncode == 0:
        try:
            candidate = _decode_probe(symbolic.stdout)
        except ValueError:
            return _failed_snapshot(
                expected_root,
                provenance,
                "malformed_probe_output",
                git_sha=git_sha,
            )
        if (
            not candidate
            or len(candidate) > 255
            or _CONTROL_RE.search(candidate)
        ):
            return _failed_snapshot(
                expected_root,
                provenance,
                "malformed_probe_output",
                git_sha=git_sha,
            )
        head_state = "branch"
        branch = candidate
    elif symbolic.returncode == 1:
        head_state = "detached"
    else:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_failed",
            git_sha=git_sha,
        )

    try:
        status = probe(
            ("status", "--porcelain=v1", "--untracked-files=normal")
        )
    except TimeoutError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_timeout",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    except OverflowError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_output_oversized",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    except (OSError, RuntimeError):
        return _failed_snapshot(
            expected_root,
            provenance,
            "dirty_state_unavailable",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    if status.returncode != 0:
        return _failed_snapshot(
            expected_root,
            provenance,
            "dirty_state_unavailable",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    try:
        _decode_probe(status.stdout)
    except ValueError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "malformed_probe_output",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )

    dirty_state: DirtyState = "dirty" if status.stdout.strip() else "clean"

    # A checkout/commit can move HEAD while the status probe is running. Re-read
    # the exact SHA/ref identity before accepting the sampled dirty state so one
    # snapshot cannot combine metadata from two different worktrees.
    try:
        final_head = probe(("rev-parse", "--verify", "HEAD"))
        final_symbolic = probe(
            ("symbolic-ref", "--quiet", "--short", "HEAD")
        )
    except TimeoutError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_timeout",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    except OverflowError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_output_oversized",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    except (OSError, RuntimeError):
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_failed",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    if final_head.returncode != 0:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_failed",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    try:
        final_git_sha = _decode_probe(final_head.stdout).lower()
    except ValueError:
        return _failed_snapshot(
            expected_root,
            provenance,
            "malformed_probe_output",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )
    if not _SHA_RE.fullmatch(final_git_sha):
        return _failed_snapshot(
            expected_root,
            provenance,
            "malformed_probe_output",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )

    final_head_state: HeadState
    final_branch: str | None = None
    if final_symbolic.returncode == 0:
        try:
            final_branch = _decode_probe(final_symbolic.stdout)
        except ValueError:
            return _failed_snapshot(
                expected_root,
                provenance,
                "malformed_probe_output",
                git_sha=git_sha,
                head_state=head_state,
                branch=branch,
            )
        if (
            not final_branch
            or len(final_branch) > 255
            or _CONTROL_RE.search(final_branch)
        ):
            return _failed_snapshot(
                expected_root,
                provenance,
                "malformed_probe_output",
                git_sha=git_sha,
                head_state=head_state,
                branch=branch,
            )
        final_head_state = "branch"
    elif final_symbolic.returncode == 1:
        final_head_state = "detached"
    else:
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_failed",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )

    if (
        final_git_sha != git_sha
        or final_head_state != head_state
        or final_branch != branch
    ):
        return _failed_snapshot(
            expected_root,
            provenance,
            "probe_failed",
            git_sha=git_sha,
            head_state=head_state,
            branch=branch,
        )

    return RuntimeSnapshot(
        root_identity=_root_identity(expected_root),
        observed_at=datetime.now(UTC).isoformat(),
        git_available=True,
        git_sha=git_sha,
        head_state=head_state,
        branch=branch,
        dirty_state=dirty_state,
        provenance=provenance,
        failure_code=None,
    )


def startup_snapshot_unavailable() -> RuntimeSnapshot:
    root = trusted_repository_root()
    return _failed_snapshot(
        root,
        "process_start_observation",
        "startup_snapshot_unavailable",
    )


def worktree_changed_since_start(
    startup: RuntimeSnapshot,
    live: RuntimeSnapshot,
) -> bool:
    return (
        startup.git_sha != live.git_sha
        or startup.dirty_state != live.dirty_state
        or startup.head_state != live.head_state
        or startup.branch != live.branch
    )


def _snapshot_public(snapshot: RuntimeSnapshot) -> dict[str, object]:
    return asdict(snapshot)


def _unknown(
    startup: RuntimeSnapshot,
    live: RuntimeSnapshot,
    *,
    reason: str,
    target: RepositoryTruthResult | None = None,
    compare: RepositoryTruthResult | None = None,
    remote_status: str = "unavailable",
) -> dict[str, object]:
    return {
        "startup": _snapshot_public(startup),
        "live": _snapshot_public(live),
        "remote": _remote_public(target),
        "alignment": "unknown",
        "reason": reason,
        "worktree_changed_since_start": worktree_changed_since_start(
            startup,
            live,
        ),
        "semantic_delta": _compare_public(compare, reason=reason),
        "observer_status": _observer_status(startup, live),
        "remote_status": remote_status,
    }


def _remote_public(
    result: RepositoryTruthResult | None,
) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "provider": result.provider,
        "repository": result.repository,
        "requested_ref": result.requested_ref,
        "resolved_sha": result.resolved_sha,
        "observed_at": result.observed_at,
    }


def _compare_public(
    result: RepositoryTruthResult | None,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    if result is None:
        return {"status": "unavailable", "reason": reason}
    payload = result.payload
    return {
        "status": "available",
        "partial": result.partial,
        "relation": payload.get("status"),
        "ahead_by": payload.get("ahead_by"),
        "behind_by": payload.get("behind_by"),
        "files": payload.get("files", []),
    }


def _observer_status(
    startup: RuntimeSnapshot,
    live: RuntimeSnapshot,
) -> ObserverStatus:
    if not startup.git_available or not live.git_available:
        return "unavailable"
    if startup.failure_code is not None or live.failure_code is not None:
        return "degraded"
    return "ok"


def _valid_relation(
    result: RepositoryTruthResult,
    local_sha: str,
    target_sha: str,
) -> tuple[str, int, int] | None:
    payload = result.payload
    if (
        payload.get("base_sha") != local_sha
        or payload.get("head_sha") != target_sha
    ):
        return None
    status = payload.get("status")
    ahead = payload.get("ahead_by")
    behind = payload.get("behind_by")
    if (
        not isinstance(status, str)
        or status not in {"ahead", "behind", "diverged", "identical"}
        or isinstance(ahead, bool)
        or isinstance(behind, bool)
        or not isinstance(ahead, int)
        or not isinstance(behind, int)
        or ahead < 0
        or behind < 0
    ):
        return None
    if status == "ahead" and not (ahead > 0 and behind == 0):
        return None
    if status == "behind" and not (ahead == 0 and behind > 0):
        return None
    if status == "identical" and not (ahead == 0 and behind == 0):
        return None
    if status == "diverged" and not (ahead > 0 and behind > 0):
        return None
    return status, ahead, behind


class RuntimeTruthService:
    def __init__(self, repository_truth: RepositoryTruthService) -> None:
        self._repository_truth = repository_truth

    def inspect(
        self,
        *,
        repository: str,
        target_ref: str,
        startup: RuntimeSnapshot,
    ) -> dict[str, object]:
        live = capture_runtime_snapshot(
            provenance="live_worktree_observation"
        )
        if repository != CANONICAL_RUNTIME_REPOSITORY:
            return _unknown(startup, live, reason="repository_mismatch")

        try:
            initial_target = self._repository_truth.repository_ref_truth(
                repository,
                target_ref,
            )
        except RepositoryTruthError as exc:
            return _unknown(
                startup,
                live,
                reason="remote_target_unavailable",
                remote_status=exc.code,
            )

        changed = worktree_changed_since_start(startup, live)
        if startup.failure_code is not None:
            return _unknown(
                startup,
                live,
                reason=startup.failure_code,
                target=initial_target,
                remote_status="ok",
            )
        if live.failure_code is not None:
            return _unknown(
                startup,
                live,
                reason=live.failure_code,
                target=initial_target,
                remote_status="ok",
            )
        if changed:
            return _unknown(
                startup,
                live,
                reason="worktree_changed_since_start",
                target=initial_target,
                remote_status="ok",
            )
        if (
            startup.dirty_state != "clean"
            or live.dirty_state != "clean"
        ):
            return _unknown(
                startup,
                live,
                reason="dirty_local_state",
                target=initial_target,
                remote_status="ok",
            )

        local_sha = live.git_sha
        target_sha = initial_target.resolved_sha
        if local_sha is None or target_sha is None:
            return _unknown(
                startup,
                live,
                reason="remote_relation_unavailable",
                target=initial_target,
                remote_status="ok",
            )

        compare: RepositoryTruthResult | None = None
        relation: tuple[str, int, int] | None = None
        if local_sha != target_sha:
            try:
                compare = self._repository_truth.compare_truth(
                    repository,
                    local_sha,
                    target_sha,
                )
            except RepositoryTruthError as exc:
                return _unknown(
                    startup,
                    live,
                    reason="remote_relation_unavailable",
                    target=initial_target,
                    remote_status=exc.code,
                )
            relation = _valid_relation(compare, local_sha, target_sha)
            if relation is None:
                return _unknown(
                    startup,
                    live,
                    reason="remote_relation_unavailable",
                    target=initial_target,
                    compare=compare,
                    remote_status="malformed_relation",
                )

        try:
            final_target = self._repository_truth.repository_ref_truth(
                repository,
                target_ref,
            )
        except RepositoryTruthError as exc:
            return _unknown(
                startup,
                live,
                reason="remote_target_unavailable",
                target=initial_target,
                compare=compare,
                remote_status=exc.code,
            )
        if final_target.resolved_sha != target_sha:
            return _unknown(
                startup,
                live,
                reason="remote_target_moved",
                target=final_target,
                compare=compare,
                remote_status="ok",
            )

        alignment: Alignment
        reason: str
        if local_sha == target_sha:
            alignment = "aligned"
            reason = "exact_clean_match"
        elif relation is not None and relation[0] == "ahead":
            alignment = "local_behind"
            reason = "remote_target_ahead"
        elif relation is not None and relation[0] in {"behind", "diverged"}:
            alignment = "divergent"
            reason = "non_ancestor_relation"
        else:
            return _unknown(
                startup,
                live,
                reason="remote_relation_unavailable",
                target=final_target,
                compare=compare,
                remote_status="unknown_relation",
            )

        result: dict[str, object] = {
            "startup": _snapshot_public(startup),
            "live": _snapshot_public(live),
            "remote": _remote_public(final_target),
            "alignment": alignment,
            "reason": reason,
            "worktree_changed_since_start": False,
            "semantic_delta": _compare_public(compare),
            "observer_status": _observer_status(startup, live),
            "remote_status": "ok",
        }
        logger.info(
            "coding_runtime_truth repository_digest=%s target_sha=%s "
            "root_identity=%s observer_status=%s alignment=%s reason=%s "
            "dirty_state=%s compare=%s",
            hashlib.sha256(repository.encode("utf-8")).hexdigest(),
            target_sha[:12],
            live.root_identity,
            result["observer_status"],
            alignment,
            reason,
            live.dirty_state,
            (
                "partial"
                if compare is not None and compare.partial
                else "complete"
                if compare is not None
                else "not_needed"
            ),
        )
        return result