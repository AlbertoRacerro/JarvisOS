from __future__ import annotations

import pytest

from app.modules.bluecad.assembly import _assert_ports_conform
from app.modules.bluecad.models import BluecadError, PortFrame
from app.modules.bluecad.spec import SpecValidationError, canonicalize_geometry_spec


def test_new_stub_kinds_validate_and_generate_stable_spec_id() -> None:
    spec = {
        "spec_version": "bluecad_geometry_spec_v0_1",
        "parts": [
            {"part_id": "m", "kind": "manifold", "params": {"outer_d_main": 100.0, "wall_t": 5.0, "length": 400.0, "n_out": 3, "out_d": 40.0, "out_wall_t": 2.0, "spacing": 100.0}},
            {"part_id": "f", "kind": "float", "params": {"outer_d": 200.0, "length": 500.0, "n_mounts": 2, "pad_d": 80.0}},
            {"part_id": "a", "kind": "anchor_mount", "params": {"base_w": 80.0, "base_l": 120.0, "base_t": 12.0, "eye_d": 20.0}},
            {"part_id": "h", "kind": "harvest_module", "params": {"outer_d": 160.0, "height": 240.0, "wall_t": 4.0, "port_d": 40.0}},
        ],
    }

    canonical = canonicalize_geometry_spec(spec)

    assert canonical["spec_id"].startswith("sha256:")


@pytest.mark.parametrize("field", ["n_out", "n_mounts"])
def test_parametric_port_count_bounds_are_enforced(field: str) -> None:
    if field == "n_out":
        part = {"part_id": "m", "kind": "manifold", "params": {"outer_d_main": 100.0, "wall_t": 5.0, "length": 400.0, "n_out": 13, "out_d": 40.0, "out_wall_t": 2.0, "spacing": 100.0}}
    else:
        part = {"part_id": "f", "kind": "float", "params": {"outer_d": 200.0, "length": 500.0, "n_mounts": 0, "pad_d": 80.0}}

    with pytest.raises(SpecValidationError) as exc_info:
        canonicalize_geometry_spec({"spec_version": "bluecad_geometry_spec_v0_1", "parts": [part]})

    assert exc_info.value.code == "SPEC_INVALID"
    assert exc_info.value.detail["path"] == f"$.parts[0].params.{field}"


def test_mixed_tube_pad_connection_fails_with_port_mismatch() -> None:
    tube = PortFrame((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), 40.0, 2.0)
    pad = PortFrame((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), interface="pad", pad_d=40.0)

    with pytest.raises(BluecadError) as exc_info:
        _assert_ports_conform(tube, pad, {"from": "tube.port_b", "to": "float.mount_1"})

    assert exc_info.value.code == "PORT_MISMATCH"
