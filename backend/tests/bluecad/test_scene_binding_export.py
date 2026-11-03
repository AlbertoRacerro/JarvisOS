from __future__ import annotations

import json
import struct
from pathlib import Path

from app.modules.bluecad.export import SCENE_BINDING_VERSION, build_artifacts

FIXTURE = Path(__file__).parent / "fixtures" / "chain_tube_bend_joint.json"
_GLTF_JSON_CHUNK = 0x4E4F534A


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _glb_json(path: Path) -> dict:
    data = path.read_bytes()
    assert data[:4] == b"glTF"
    version, total_length = struct.unpack_from("<II", data, 4)
    assert version == 2
    assert total_length == len(data)
    offset = 12
    while offset < len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_length]
        offset += chunk_length
        if chunk_type == _GLTF_JSON_CHUNK:
            return json.loads(chunk.rstrip(b" \t\r\n\x00").decode("utf-8"))
    raise AssertionError("GLB JSON chunk not found")


def _node_names(path: Path) -> set[str]:
    payload = _glb_json(path)
    return {
        node["name"]
        for node in payload.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("name"), str)
    }


def test_scene_binding_semantic_keys_survive_real_glb_export(tmp_path: Path) -> None:
    spec = _fixture()

    manifest = build_artifacts(spec, tmp_path)

    binding = manifest["scene_binding"]
    assert binding["version"] == SCENE_BINDING_VERSION
    assert binding["artifact"] == "model.glb"
    targets = {key: target["part_id"] for key, target in binding["objects"].items()}
    assert set(targets.values()) == {part["part_id"] for part in spec["parts"]}
    assert len(targets) == len(spec["parts"])
    assert set(targets).issubset(_node_names(tmp_path / "model.glb"))


def test_scene_binding_is_stable_when_spec_part_order_changes(tmp_path: Path) -> None:
    spec_a = _fixture()
    spec_b = _fixture()
    spec_b["parts"] = list(reversed(spec_b["parts"]))

    manifest_a = build_artifacts(spec_a, tmp_path / "a")
    manifest_b = build_artifacts(spec_b, tmp_path / "b")

    assert manifest_a["scene_binding"]["objects"] == manifest_b["scene_binding"]["objects"]
    keys = set(manifest_a["scene_binding"]["objects"])
    assert keys.issubset(_node_names(tmp_path / "a" / "model.glb"))
    assert keys.issubset(_node_names(tmp_path / "b" / "model.glb"))
