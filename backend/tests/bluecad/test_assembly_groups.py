"""Placement of connected groups and their frame roots."""

import pytest

from app.modules.bluecad.assembly import assemble_parts
from app.modules.bluecad.template import SerpentineTemplateParams, _serpentine_parts


def tube(name, origin=None, direction=None):
    part = {"part_id": name, "kind": "tube_run", "params": {"outer_d": 20, "wall_t": 2, "length": 100}}
    if origin is not None:
        part["frame"] = {"origin": origin, "direction": direction}
    return part


def test_detached_serpentine_uses_first_run_frame():
    parts, connections = _serpentine_parts(SerpentineTemplateParams(
        outer_d_mm=20, wall_t_mm=2, segment_length_mm=200, segment_count=3, bend_radius_mm=50,
    ))
    parts[0]["frame"] = {"origin": [0, 300, 0], "direction": [0, 1, 0]}
    built = assemble_parts({"parts": [tube("feed"), *parts], "connections": connections})
    # The runs alternate along Y, separated by twice the bend radius along -X.
    for name, a, b in [
        ("run_1", (0, 300, 0), (0, 500, 0)),
        ("run_2", (-100, 500, 0), (-100, 300, 0)),
        ("run_3", (-200, 300, 0), (-200, 500, 0)),
    ]:
        assert built[name].ports["port_a"].origin == pytest.approx(a, abs=1e-6)
        assert built[name].ports["port_b"].origin == pytest.approx(b, abs=1e-6)
    assert built["run_1"].ports["port_a"].direction == pytest.approx((0, -1, 0), abs=1e-6)
    for connection in connections:
        left_id, left_port = connection["from"].split(".")
        right_id, right_port = connection["to"].split(".")
        left, right = built[left_id].ports[left_port], built[right_id].ports[right_port]
        assert left.origin == pytest.approx(right.origin, abs=1e-6)
        assert left.direction == pytest.approx(tuple(-v for v in right.direction), abs=1e-6)


@pytest.mark.parametrize("reverse", [False, True])
def test_detached_groups_are_rooted_in_spec_order(reverse):
    groups = [
        [tube("a", [10, 20, 0], [1, 0, 0]), tube("b", [999, 999, 0], [0, 1, 0])],
        [tube("c", [0, 300, 0], [0, 1, 0]), tube("d", [999, 999, 0], [1, 0, 0])],
    ]
    if reverse:
        groups.reverse()
    connections = [{"from": "a.port_b", "to": "b.port_a"}, {"from": "c.port_b", "to": "d.port_a"}]
    built = assemble_parts({"parts": [tube("feed"), *groups[0], *groups[1]], "connections": connections})
    for name, origin in [("a", (10, 20, 0)), ("b", (110, 20, 0)), ("c", (0, 300, 0)), ("d", (0, 400, 0))]:
        assert built[name].ports["port_a"].origin == pytest.approx(origin, abs=1e-6)


def test_connected_chain_ignores_non_root_frames():
    parts = [tube("a", [10, 20, 0], [0, 1, 0]), tube("b", [900, 900, 0], [1, 0, 0]), tube("c", [-900, 0, 0], [1, 0, 0])]
    connections = [{"from": "a.port_b", "to": "b.port_a"}, {"from": "b.port_b", "to": "c.port_a"}]
    built = assemble_parts({"parts": parts, "connections": connections})
    for name, y in [("a", 20), ("b", 120), ("c", 220)]:
        assert built[name].ports["port_a"].origin == pytest.approx((10, y, 0), abs=1e-6)
        assert built[name].ports["port_b"].origin == pytest.approx((10, y + 100, 0), abs=1e-6)
