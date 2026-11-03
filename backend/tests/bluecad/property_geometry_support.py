from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sys
from collections.abc import Iterator, Mapping, Sequence
from importlib import metadata
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis.errors import InvalidArgument

from app.modules.bluecad.assembly import ABS_TOL
from app.modules.bluecad.export import ARTIFACT_NAMES, sha256_file
from app.modules.bluecad.models import BuildResult
from app.modules.bluecad.service import build_geometry_spec
from app.modules.bluecad.spec import canonical_json, canonicalize_geometry_spec

PROPERTY_PROFILE = "bluecad_property_ci"
CANARY_PROFILE_ID = "ubuntu24-py311"
CANARY_PROFILE_ENV = "JARVISOS_BLUECAD_CANARY_PROFILE"
EXPECTED_OUTPUT_FILES = frozenset(
    {*ARTIFACT_NAMES, "manifest.json", "validation_report.json"}
)
_BANNED_TIME_KEYS = frozenset(
    {"timestamp", "created_at", "updated_at", "elapsed", "duration"}
)

try:
    settings.get_profile(PROPERTY_PROFILE)
except InvalidArgument:
    settings.register_profile(
        PROPERTY_PROFILE,
        derandomize=True,
        database=None,
        deadline=None,
        print_blob=True,
        suppress_health_check=[HealthCheck.too_slow],
    )
settings.load_profile(PROPERTY_PROFILE)


def build_and_assert(
    spec: Mapping[str, Any],
    out_dir: Path,
    *,
    connection: tuple[str, str] | None = None,
) -> BuildResult:
    canonical = canonicalize_geometry_spec(spec)
    result = build_geometry_spec(dict(spec), out_dir)
    assert result.verdict == "pass", result.errors
    assert result.errors == []
    assert result.spec_id == canonical["spec_id"]
    assert result.manifest is not None
    assert result.manifest_path is not None
    assert result.report_path is not None
    assert result.report["verdict"] == "pass", result.report

    root = out_dir.resolve()
    assert result.out_dir.resolve() == root
    _assert_output_files(root)
    _assert_confined_file(result.manifest_path, root)
    _assert_confined_file(result.report_path, root)

    disk_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    disk_report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert disk_manifest == result.manifest
    assert disk_report == result.report
    assert disk_manifest["spec_id"] == result.spec_id
    assert disk_report["spec_id"] == result.spec_id
    assert disk_report["manifest_sha256"] == sha256_file(result.manifest_path)

    assert_manifest_digest(disk_manifest)
    assert_manifest_invariants(disk_manifest, root, connection=connection)
    return result


def build_twice_and_assert(
    spec: Mapping[str, Any],
    *,
    connection: tuple[str, str] | None = None,
) -> tuple[BuildResult, BuildResult]:
    with TemporaryDirectory(prefix="bluecad-property-repeat-") as directory:
        root = Path(directory)
        first = build_and_assert(spec, root / "first", connection=connection)
        second = build_and_assert(spec, root / "second", connection=connection)
        assert first.spec_id == second.spec_id
        assert first.manifest == second.manifest
        assert first.manifest is not None
        assert second.manifest is not None
        assert first.manifest["manifest_digest"] == second.manifest["manifest_digest"]
        for artifact_name in ARTIFACT_NAMES:
            assert (
                first.manifest["artifacts"][artifact_name]["sha256"]
                == second.manifest["artifacts"][artifact_name]["sha256"]
            )
        assert_manifest_digest(first.manifest)
        assert_manifest_digest(second.manifest)
        return first, second


def assert_manifest_digest(manifest: Mapping[str, Any]) -> None:
    payload = dict(manifest)
    stored = payload.pop("manifest_digest", None)
    assert isinstance(stored, str) and len(stored) == 64
    recomputed = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert stored == recomputed


def assert_manifest_invariants(
    manifest: Mapping[str, Any],
    root: Path,
    *,
    connection: tuple[str, str] | None = None,
) -> None:
    parts = manifest["parts"]
    resolved_ports = manifest["resolved_ports"]
    kernel_checks = manifest["assembly"]["kernel_checks"]
    assert parts
    assert set(parts) == set(resolved_ports) == set(kernel_checks)

    for part_id, part in parts.items():
        volume = float(part["volume_mm3"])
        bbox = part["bbox_mm"]
        mins = [float(value) for value in bbox["min"]]
        maxs = [float(value) for value in bbox["max"]]
        assert math.isfinite(volume) and volume > 0.0
        assert len(mins) == len(maxs) == 3
        assert all(math.isfinite(value) for value in (*mins, *maxs))
        assert all(
            minimum <= maximum
            for minimum, maximum in zip(mins, maxs, strict=True)
        )
        envelope = math.prod(
            maximum - minimum
            for minimum, maximum in zip(mins, maxs, strict=True)
        )
        assert envelope > 0.0
        if part["kind"] in {"tube_run", "float"}:
            assert volume <= envelope * (1.0 + 1.0e-9)
        assert kernel_checks[part_id] == {"brep_valid": True, "manifold": True}
        assert part["ports"] == resolved_ports[part_id]
        for port in resolved_ports[part_id].values():
            origin = [float(value) for value in port["origin"]]
            direction = [float(value) for value in port["direction"]]
            assert len(origin) == len(direction) == 3
            assert all(math.isfinite(value) for value in (*origin, *direction))
            norm = math.sqrt(math.fsum(value * value for value in direction))
            assert abs(norm - 1.0) <= 1.0e-8

    assembly_bbox = manifest["assembly"]["bbox_mm"]
    assembly_mins = [float(value) for value in assembly_bbox["min"]]
    assembly_maxs = [float(value) for value in assembly_bbox["max"]]
    assert all(
        minimum <= maximum
        for minimum, maximum in zip(assembly_mins, assembly_maxs, strict=True)
    )
    assert math.isfinite(float(manifest["assembly"]["total_volume_mm3"]))
    assert float(manifest["assembly"]["total_volume_mm3"]) > 0.0

    if connection is not None:
        from_ref, to_ref = connection
        from_part, from_port = from_ref.split(".", 1)
        to_part, to_port = to_ref.split(".", 1)
        source = resolved_ports[from_part][from_port]
        target = resolved_ports[to_part][to_port]
        assert all(
            abs(float(left) - float(right)) <= ABS_TOL
            for left, right in zip(source["origin"], target["origin"], strict=True)
        )
        assert all(
            abs(float(left) + float(right)) <= ABS_TOL
            for left, right in zip(source["direction"], target["direction"], strict=True)
        )

    root_text = str(root.resolve())
    for key, value in _walk_json(manifest):
        assert key.lower() not in _BANNED_TIME_KEYS
        if isinstance(value, str):
            assert root_text not in value
            assert not Path(value).is_absolute()
            assert not PureWindowsPath(value).is_absolute()


def canonical_profile_metadata() -> dict[str, str]:
    distributions = metadata.packages_distributions().get("OCP", [])
    normalized = sorted(
        distribution
        for distribution in distributions
        if distribution.lower().replace("_", "-").startswith("cadquery-ocp")
    )
    if len(normalized) != 1:
        raise AssertionError(
            "expected one OCP distribution, found " + json.dumps(distributions)
        )
    ocp_distribution = normalized[0]
    return {
        "profile_id": CANARY_PROFILE_ID,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system(),
        "architecture": platform.machine(),
        "build123d": metadata.version("build123d"),
        "ocp_distribution": ocp_distribution,
        "ocp_version": metadata.version(ocp_distribution),
    }


def canonical_canary_enabled() -> bool:
    return os.getenv(CANARY_PROFILE_ENV) == CANARY_PROFILE_ID


def artifact_hashes(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {
        name: str(manifest["artifacts"][name]["sha256"])
        for name in ARTIFACT_NAMES
    }


def _assert_output_files(root: Path) -> None:
    children = {path.name for path in root.iterdir()}
    assert children == EXPECTED_OUTPUT_FILES
    for name in EXPECTED_OUTPUT_FILES:
        _assert_confined_file(root / name, root)


def _assert_confined_file(path: Path, root: Path) -> None:
    assert path.exists() and path.is_file()
    assert not path.is_symlink()
    resolved = path.resolve()
    assert root == resolved.parent or root in resolved.parents
    assert path.stat().st_size > 0


def _walk_json(value: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    yield key, value
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            yield from _walk_json(child, str(child_key))
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for child in value:
            yield from _walk_json(child, key)
