from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.bluecad.property_geometry_support import (
    build_and_assert,
    build_twice_and_assert,
)

SPEC_VERSION = "bluecad_geometry_spec_v0_1"
CANARY_PROFILE = "ubuntu24-py311"
CAPPED_MANIFOLD_PROPERTY = pytest.mark.skipif(
    os.getenv("JARVISOS_BLUECAD_CANARY_PROFILE") != CANARY_PROFILE,
    reason="capped-manifold property builds run in the pinned BLUECAD canary",
)
CARDINAL_DIRECTIONS = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
)
TUBE_CONNECTION = ("tube_a.port_b", "tube_b.port_a")


@st.composite
def planar_frame(draw: st.DrawFn) -> dict[str, list[int]] | None:
    if not draw(st.booleans()):
        return None
    return {
        "origin": list(
            draw(
                st.tuples(
                    st.integers(-10_000, 10_000),
                    st.integers(-10_000, 10_000),
                    st.integers(-10_000, 10_000),
                )
            )
        ),
        "direction": list(draw(st.sampled_from(CARDINAL_DIRECTIONS))),
    }


@st.composite
def tube_dimensions(draw: st.DrawFn) -> dict[str, int]:
    outer_d = draw(st.integers(20, 500))
    wall_t = draw(st.integers(1, min(25, (outer_d - 1) // 4)))
    length = draw(st.integers(50, 5000))
    return {"outer_d": outer_d, "wall_t": wall_t, "length": length}


@st.composite
def single_tube_specs(draw: st.DrawFn) -> dict[str, Any]:
    part: dict[str, Any] = {
        "part_id": "tube1",
        "kind": "tube_run",
        "params": draw(tube_dimensions()),
    }
    frame = draw(planar_frame())
    if frame is not None:
        part["frame"] = frame
    return {
        "spec_version": SPEC_VERSION,
        "name": "property_single_tube",
        "parts": [part],
        "connections": [],
    }


@st.composite
def connected_tube_specs(draw: st.DrawFn) -> dict[str, Any]:
    outer_d = draw(st.integers(20, 500))
    wall_t = draw(st.integers(1, min(25, (outer_d - 1) // 4)))
    first: dict[str, Any] = {
        "part_id": "tube_a",
        "kind": "tube_run",
        "params": {
            "outer_d": outer_d,
            "wall_t": wall_t,
            "length": draw(st.integers(50, 2500)),
        },
    }
    frame = draw(planar_frame())
    if frame is not None:
        first["frame"] = frame
    second = {
        "part_id": "tube_b",
        "kind": "tube_run",
        "params": {
            "outer_d": outer_d,
            "wall_t": wall_t,
            "length": draw(st.integers(50, 2500)),
        },
    }
    return {
        "spec_version": SPEC_VERSION,
        "name": "property_connected_tubes",
        "parts": [first, second],
        "connections": [{"from": TUBE_CONNECTION[0], "to": TUBE_CONNECTION[1]}],
    }


@st.composite
def single_float_specs(draw: st.DrawFn) -> dict[str, Any]:
    outer_d = draw(st.integers(50, 500))
    part: dict[str, Any] = {
        "part_id": "float1",
        "kind": "float",
        "params": {
            "outer_d": outer_d,
            "length": draw(st.integers(100, 5000)),
            "n_mounts": draw(st.integers(1, 6)),
            "pad_d": draw(st.integers(10, max(10, outer_d // 2))),
        },
    }
    frame = draw(planar_frame())
    if frame is not None:
        part["frame"] = frame
    return {
        "spec_version": SPEC_VERSION,
        "name": "property_single_float",
        "parts": [part],
        "connections": [],
    }


@st.composite
def single_capped_manifold_specs(draw: st.DrawFn) -> dict[str, Any]:
    main_outer_d = draw(st.integers(80, 180))
    branch_outer_d = draw(st.integers(20, min(70, main_outer_d - 10)))
    part: dict[str, Any] = {
        "part_id": "manifold1",
        "kind": "capped_manifold",
        "params": {
            "main_outer_d": main_outer_d,
            "main_wall_t": draw(st.integers(2, min(12, (main_outer_d - 1) // 4))),
            "branch_count": draw(st.sampled_from((1, 2, 12))),
            "branch_outer_d": branch_outer_d,
            "branch_wall_t": draw(st.integers(1, min(8, (branch_outer_d - 1) // 4))),
            "branch_gap": draw(st.integers(2, 30)),
            "end_gap": draw(st.integers(2, 30)),
            "branch_stub_length": draw(st.integers(20, 120)),
            "cap_thickness": draw(st.integers(2, 20)),
        },
    }
    frame = draw(planar_frame())
    if frame is not None:
        part["frame"] = frame
    return {
        "spec_version": SPEC_VERSION,
        "name": "property_single_capped_manifold",
        "parts": [part],
        "connections": [],
    }


@settings(max_examples=8)
@given(single_tube_specs())
def test_single_tube_valid_domain_invariants(spec: dict[str, Any]) -> None:
    with TemporaryDirectory(prefix="bluecad-property-tube-") as directory:
        build_and_assert(spec, Path(directory) / "build")


@settings(max_examples=6)
@given(connected_tube_specs())
def test_connected_tube_valid_domain_invariants(spec: dict[str, Any]) -> None:
    with TemporaryDirectory(prefix="bluecad-property-connected-") as directory:
        build_and_assert(
            spec,
            Path(directory) / "build",
            connection=TUBE_CONNECTION,
        )


@settings(max_examples=6)
@given(single_float_specs())
def test_single_float_valid_domain_invariants(spec: dict[str, Any]) -> None:
    with TemporaryDirectory(prefix="bluecad-property-float-") as directory:
        build_and_assert(spec, Path(directory) / "build")


@CAPPED_MANIFOLD_PROPERTY
@settings(max_examples=3)
@given(single_capped_manifold_specs())
def test_single_capped_manifold_valid_domain_invariants(spec: dict[str, Any]) -> None:
    with TemporaryDirectory(prefix="bluecad-property-capped-manifold-") as directory:
        build_and_assert(spec, Path(directory) / "build")


@settings(max_examples=3)
@given(single_tube_specs())
def test_single_tube_same_environment_repeatability(spec: dict[str, Any]) -> None:
    build_twice_and_assert(spec)


@settings(max_examples=3)
@given(connected_tube_specs())
def test_connected_tube_same_environment_repeatability(spec: dict[str, Any]) -> None:
    build_twice_and_assert(spec, connection=TUBE_CONNECTION)


@settings(max_examples=3)
@given(single_float_specs())
def test_single_float_same_environment_repeatability(spec: dict[str, Any]) -> None:
    build_twice_and_assert(spec)


@CAPPED_MANIFOLD_PROPERTY
@settings(max_examples=1)
@given(single_capped_manifold_specs())
def test_single_capped_manifold_same_environment_repeatability(spec: dict[str, Any]) -> None:
    build_twice_and_assert(spec)
