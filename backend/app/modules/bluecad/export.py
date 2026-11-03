"""BLUECAD artifact export and deterministic manifest writing."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from app.modules.bluecad.assembly import assemble_parts
from app.modules.bluecad.models import BluecadError, BuiltPart
from app.modules.bluecad.spec import canonical_json, canonicalize_geometry_spec

ARTIFACT_NAMES = ("model.step", "model.stl", "model.glb", "model.3mf")
THREE_MF_ARTIFACT = "model.3mf"
THREE_MF_LINEAR_DEFLECTION = 0.001
THREE_MF_ANGULAR_DEFLECTION = 0.1
_MESH_DIGEST_DECIMALS = 6
_THREE_MF_MODEL_ENTRY = "3D/3dmodel.model"
_THREE_MF_UUID_RE = re.compile(r'(p:UUID=")[0-9a-fA-F-]{36}(")')
_THREE_MF_UUID_NAMESPACE = uuid.UUID("5d0f1c6e-3a7b-4c1e-9a52-165b1ec3a0d1")
SCENE_BINDING_VERSION = "bluecad_scene_binding_v0_1"
SCENE_BINDING_ARTIFACT = "model.glb"
_SCENE_BINDING_KEY_PREFIX = "bluecad-part-sha256-"
_CANONICAL_STEP_TIMESTAMP = "1970-01-01T00:00:00"
_STEP_FILE_NAME_TIMESTAMP_RE = re.compile(r"(FILE_NAME\('[^']*',)'[^']*'")


def build_artifacts(spec_payload: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    spec = canonicalize_geometry_spec(spec_payload)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    parts = assemble_parts(spec)
    try:
        scene_bindings, print_handoff = _export_shapes(parts, out_path)
    except Exception as exc:
        if isinstance(exc, BluecadError):
            raise
        raise BluecadError("EXPORT_ERROR", {"message": str(exc)}) from exc
    manifest = _manifest(spec, parts, out_path, scene_bindings=scene_bindings, print_handoff=print_handoff)
    manifest_path = out_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _export_shapes(parts: dict[str, BuiltPart], out_dir: Path) -> tuple[dict[str, str], dict[str, Any]]:
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc

    shapes = [part.shape for part in parts.values()]
    shape = shapes[0] if len(shapes) == 1 else bd.Compound(children=shapes)
    step_path = out_dir / "model.step"
    bd.export_step(shape, step_path)
    _normalize_step_header_timestamp(step_path)
    bd.export_stl(shape, out_dir / "model.stl")

    gltf_shape, scene_bindings = _scene_bound_gltf_shape(parts, bd)
    bd.export_gltf(
        gltf_shape,
        out_dir / SCENE_BINDING_ARTIFACT,
        binary=True,
        linear_deflection=0.001,
        angular_deflection=0.1,
    )
    print_handoff = _export_3mf(parts, out_dir / THREE_MF_ARTIFACT, bd)
    for name in ARTIFACT_NAMES:
        path = out_dir / name
        if not path.exists() or path.stat().st_size <= 0:
            raise BluecadError("EXPORT_ERROR", {"artifact": name, "message": "artifact was not written"})
    return scene_bindings, print_handoff


def _export_3mf(parts: dict[str, BuiltPart], path: Path, bd: Any) -> dict[str, Any]:
    """Write a millimetre 3MF with one mesh object per part and return the print_handoff block.

    Meshing is from the in-memory kernel shapes. Measured on lib3mf 2.5.0: two
    identical builds differ in raw bytes only through random ``p:UUID`` values
    in 3D/3dmodel.model. Those are rewritten to deterministic UUIDs below so the
    manifest stays reproducible, but ``mesh_digest`` (canonical triangle soup)
    remains the determinism anchor and callers must not rely on file bytes.
    """
    mesher = bd.Mesher(unit=bd.Unit.MM)
    object_names: list[str] = []
    for part_id, part in sorted(parts.items()):
        before = mesher.mesh_count
        mesher.add_shape(
            part.shape,
            linear_deflection=THREE_MF_LINEAR_DEFLECTION,
            angular_deflection=THREE_MF_ANGULAR_DEFLECTION,
            part_number=part_id,
        )
        if mesher.mesh_count - before != 1:
            raise BluecadError(
                "EXPORT_ERROR",
                {"artifact": THREE_MF_ARTIFACT, "part_id": part_id, "message": "part did not mesh to exactly one object"},
            )
        mesher.meshes[-1].SetName(part_id)
        object_names.append(part_id)
    triangle_counts = {name: int(count) for name, count in zip(object_names, mesher.triangle_counts, strict=True)}
    digest = hashlib.sha256()
    for name, mesh in zip(object_names, mesher.meshes, strict=True):
        digest.update(f"object:{name}\n".encode())
        vertices = [tuple(vertex.Coordinates) for vertex in mesh.GetVertices()]
        soup = sorted(
            tuple(sorted(_round_vertex(vertices[index]) for index in triangle.Indices))
            for triangle in mesh.GetTriangleIndices()
        )
        for triangle in soup:
            digest.update((" ".join(f"{c:.{_MESH_DIGEST_DECIMALS}f}" for v in triangle for c in v) + "\n").encode())
    mesher.write(str(path))
    _normalize_3mf_uuids(path)
    return {
        "unit": "mm",
        "object_count": len(object_names),
        "objects": dict(sorted(triangle_counts.items())),
        "linear_deflection": THREE_MF_LINEAR_DEFLECTION,
        "angular_deflection": THREE_MF_ANGULAR_DEFLECTION,
        "mesh_digest": digest.hexdigest(),
    }


def _round_vertex(vertex: tuple[float, float, float]) -> tuple[float, float, float]:
    # "+ 0.0" folds -0.0 into 0.0 so the digest text is stable.
    return tuple(round(float(c), _MESH_DIGEST_DECIMALS) + 0.0 for c in vertex)  # type: ignore[return-value]


def _normalize_3mf_uuids(path: Path) -> None:
    with zipfile.ZipFile(path) as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    counter = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        value = uuid.uuid5(_THREE_MF_UUID_NAMESPACE, f"bluecad-3mf:{counter}")
        return f"{match.group(1)}{value}{match.group(2)}"

    rewritten = False
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in entries:
            if info.filename == _THREE_MF_MODEL_ENTRY:
                data = _THREE_MF_UUID_RE.sub(replace, data.decode("utf-8")).encode("utf-8")
                rewritten = True
            # Fresh ZipInfo: the source one carries a stale zip64 extra field with the old compressed size.
            fresh = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fresh.compress_type = zipfile.ZIP_DEFLATED
            fresh.external_attr = info.external_attr
            target.writestr(fresh, data)
    if not rewritten:
        raise BluecadError("EXPORT_ERROR", {"artifact": path.name, "message": "3MF model part was not found"})


def _scene_bound_gltf_shape(parts: dict[str, BuiltPart], bd: Any) -> tuple[Any, dict[str, str]]:
    """Wrap each selectable part in a deterministic GLTF-visible semantic node.

    The wrapper label is machine identity only. It is derived from canonical
    ``part_id`` using a fixed SHA-256 encoding so arbitrary user part IDs do not
    become unbounded or markup-like scene node names. The manifest owns the
    reversible key -> part_id relationship and collisions fail closed.
    """

    wrappers: list[Any] = []
    scene_bindings: dict[str, str] = {}
    for part_id, part in sorted(parts.items()):
        key = _scene_binding_key(part_id)
        existing = scene_bindings.get(key)
        if existing is not None and existing != part_id:
            raise BluecadError(
                "EXPORT_ERROR",
                {"artifact": SCENE_BINDING_ARTIFACT, "message": "scene binding key collision"},
            )
        wrapper = bd.Compound(children=[part.shape])
        wrapper.label = key
        wrappers.append(wrapper)
        scene_bindings[key] = part_id

    if not wrappers:
        raise BluecadError(
            "EXPORT_ERROR",
            {"artifact": SCENE_BINDING_ARTIFACT, "message": "no selectable parts to export"},
        )
    return (wrappers[0] if len(wrappers) == 1 else bd.Compound(children=wrappers), scene_bindings)


def _scene_binding_key(part_id: str) -> str:
    return _SCENE_BINDING_KEY_PREFIX + hashlib.sha256(part_id.encode("utf-8")).hexdigest()


def _normalize_step_header_timestamp(path: Path) -> None:
    """Remove OCCT wall-clock drift without changing STEP geometry data."""
    text = path.read_text(encoding="utf-8")
    normalized, count = _STEP_FILE_NAME_TIMESTAMP_RE.subn(
        lambda match: f"{match.group(1)}'{_CANONICAL_STEP_TIMESTAMP}'",
        text,
        count=1,
    )
    if count != 1:
        raise BluecadError(
            "EXPORT_ERROR",
            {"artifact": path.name, "message": "STEP FILE_NAME timestamp header was not found exactly once"},
        )
    path.write_text(normalized, encoding="utf-8")


def _manifest(
    spec: dict[str, Any],
    parts: dict[str, BuiltPart],
    out_dir: Path,
    *,
    scene_bindings: dict[str, str],
    print_handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the canonical geometry manifest.

    Runtime duration is excluded. Exported-file hashes remain part of the public
    manifest integrity contract and are expected to be deterministic for an
    equivalent build under one pinned kernel/tool environment.
    """
    total_bbox = _total_bbox(parts)
    payload: dict[str, Any] = {
        "manifest_version": "bluecad_manifest_v0_1",
        "spec_id": spec["spec_id"],
        "tool_versions": _tool_versions(),
        "parts": {part_id: part.manifest_entry() for part_id, part in sorted(parts.items())},
        "resolved_ports": {
            part_id: {name: port.as_dict() for name, port in sorted(part.ports.items())}
            for part_id, part in sorted(parts.items())
        },
        "assembly": {
            "total_volume_mm3": round(sum(part.volume_mm3 for part in parts.values()), 9),
            "bbox_mm": {
                "min": [round(value, 9) for value in total_bbox[0]],
                "max": [round(value, 9) for value in total_bbox[1]],
            },
            "kernel_checks": _kernel_checks(parts),
        },
        "scene_binding": {
            "version": SCENE_BINDING_VERSION,
            "artifact": SCENE_BINDING_ARTIFACT,
            "spec_id": spec["spec_id"],
            "objects": {
                key: {"part_id": part_id}
                for key, part_id in sorted(scene_bindings.items())
            },
        },
        "artifacts": {
            name: {"sha256": sha256_file(out_dir / name), "bytes": (out_dir / name).stat().st_size}
            for name in ARTIFACT_NAMES
        },
    }
    if print_handoff is not None:
        payload["print_handoff"] = print_handoff
    _validate_scene_binding_manifest(payload)
    payload["manifest_digest"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return payload


def _validate_scene_binding_manifest(manifest: dict[str, Any]) -> None:
    binding = manifest["scene_binding"]
    if binding.get("version") != SCENE_BINDING_VERSION or binding.get("artifact") != SCENE_BINDING_ARTIFACT:
        raise BluecadError("EXPORT_ERROR", {"message": "invalid scene binding contract"})
    objects = binding.get("objects")
    if not isinstance(objects, dict) or not objects:
        raise BluecadError("EXPORT_ERROR", {"message": "scene binding objects are missing"})
    parts = manifest["parts"]
    seen_parts: set[str] = set()
    for key, target in objects.items():
        if not isinstance(key, str) or not key.startswith(_SCENE_BINDING_KEY_PREFIX):
            raise BluecadError("EXPORT_ERROR", {"message": "invalid scene binding key"})
        if not isinstance(target, dict) or set(target) != {"part_id"}:
            raise BluecadError("EXPORT_ERROR", {"message": "invalid scene binding target"})
        part_id = target["part_id"]
        if not isinstance(part_id, str) or part_id not in parts:
            raise BluecadError("EXPORT_ERROR", {"message": "scene binding target is not a manifest part"})
        if part_id in seen_parts:
            raise BluecadError("EXPORT_ERROR", {"message": "scene binding is not bijective"})
        if key != _scene_binding_key(part_id):
            raise BluecadError("EXPORT_ERROR", {"message": "scene binding key does not match canonical part identity"})
        seen_parts.add(part_id)
    if seen_parts != set(parts):
        raise BluecadError("EXPORT_ERROR", {"message": "scene binding does not cover every manifest part"})


def _total_bbox(parts: dict[str, BuiltPart]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    mins = [min(part.bbox_mm[0][axis] for part in parts.values()) for axis in range(3)]
    maxs = [max(part.bbox_mm[1][axis] for part in parts.values()) for axis in range(3)]
    return (tuple(mins), tuple(maxs))


def _kernel_checks(parts: dict[str, BuiltPart]) -> dict[str, Any]:
    return {
        part_id: {
            "brep_valid": bool(getattr(part.shape, "is_valid", False)),
            "manifold": bool(getattr(part.shape, "is_manifold", False)),
        }
        for part_id, part in sorted(parts.items())
    }


def _tool_versions() -> dict[str, str | None]:
    try:
        import build123d as bd
    except ImportError:
        return {"build123d": None}
    return {"build123d": getattr(bd, "__version__", "unknown")}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
