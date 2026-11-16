"""Tests for studio runtime worker geometry and port validation (Spec 286)."""

from __future__ import annotations

import math
from pathlib import Path

import build123d as bd
import pytest

from app.modules.studio_runtime.worker import _geometry, _validate_port


def test_geometry_bare_shape_compatibility(tmp_path: Path) -> None:
    box = bd.Box(10.0, 10.0, 10.0)
    facts = _geometry(box, artifact_dir=str(tmp_path))
    assert facts["valid"] is True
    assert facts["solids"] == 1
    assert pytest.approx(facts["volume"], rel=1e-3) == 1000.0
    assert "ports" not in facts
    assert (tmp_path / "model.step").is_file()
    assert (tmp_path / "model.stl").is_file()
    assert (tmp_path / "model.glb").is_file()


def test_geometry_dict_without_ports(tmp_path: Path) -> None:
    box = bd.Box(10.0, 10.0, 10.0)
    facts = _geometry({"shape": box}, artifact_dir=str(tmp_path))
    assert facts["valid"] is True
    assert "ports" not in facts


def test_geometry_dict_with_empty_ports(tmp_path: Path) -> None:
    box = bd.Box(10.0, 10.0, 10.0)
    facts = _geometry({"shape": box, "ports": {}}, artifact_dir=str(tmp_path))
    assert facts["valid"] is True
    assert facts["ports"] == {}


def test_geometry_dict_with_valid_tube_and_pad_ports(tmp_path: Path) -> None:
    box = bd.Box(20.0, 20.0, 20.0)
    ports = {
        "inlet": {
            "origin": [-10, 0, 0],
            "direction": [-1.0, 0.0, 0.0],
            "interface": "tube",
            "outer_d": 12.0,
            "wall_t": 1.5,
        },
        "mounting_pad": {
            "origin": [0.0, 0.0, -10.0],
            "direction": [0.0, 0.0, -1.0],
            "interface": "pad",
            "pad_d": 16.0,
        },
    }
    facts = _geometry({"shape": box, "ports": ports}, artifact_dir=str(tmp_path))
    assert facts["valid"] is True
    assert "ports" in facts
    norm_ports = facts["ports"]
    assert set(norm_ports.keys()) == {"inlet", "mounting_pad"}

    inlet = norm_ports["inlet"]
    assert inlet["origin"] == [-10.0, 0.0, 0.0]
    assert inlet["direction"] == [-1.0, 0.0, 0.0]
    assert inlet["interface"] == "tube"
    assert inlet["outer_d"] == 12.0
    assert inlet["wall_t"] == 1.5

    pad = norm_ports["mounting_pad"]
    assert pad["origin"] == [0.0, 0.0, -10.0]
    assert pad["direction"] == [0.0, 0.0, -1.0]
    assert pad["interface"] == "pad"
    assert pad["pad_d"] == 16.0


def test_geometry_rejects_unknown_keys_in_result_dict(tmp_path: Path) -> None:
    box = bd.Box(10.0, 10.0, 10.0)
    with pytest.raises(ValueError, match="unknown key"):
        _geometry({"shape": box, "ports": {}, "extra": 123}, artifact_dir=str(tmp_path))


def test_geometry_rejects_missing_shape_in_dict(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must contain 'shape'"):
        _geometry({"ports": {}}, artifact_dir=str(tmp_path))


def test_geometry_rejects_invalid_shape_types(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="must return one build123d shape"):
        _geometry("not_a_shape", artifact_dir=str(tmp_path))

    with pytest.raises(TypeError, match="build123d Shape"):
        _geometry({"shape": "not_a_shape"}, artifact_dir=str(tmp_path))


def test_geometry_rejects_non_dict_ports(tmp_path: Path) -> None:
    box = bd.Box(10.0, 10.0, 10.0)
    with pytest.raises(TypeError, match="ports must be a dict"):
        _geometry({"shape": box, "ports": ["inlet"]}, artifact_dir=str(tmp_path))


@pytest.mark.parametrize(
    "bad_name",
    [
        123,
        "",
        "in-let",
        "in.let",
        "in let",
        "inlet/1",
        "inlet@port",
    ],
)
def test_port_name_validation(bad_name: object) -> None:
    valid_tube = {
        "origin": [0.0, 0.0, 0.0],
        "direction": [1.0, 0.0, 0.0],
        "interface": "tube",
        "outer_d": 10.0,
        "wall_t": 1.0,
    }
    with pytest.raises(ValueError, match="port name must match"):
        _validate_port(bad_name, valid_tube)


def test_port_value_must_be_dict() -> None:
    with pytest.raises(TypeError, match="must be a dict"):
        _validate_port("inlet", "not_a_dict")


def test_port_interface_validation() -> None:
    with pytest.raises(ValueError, match="interface must be 'tube' or 'pad'"):
        _validate_port("inlet", {"origin": [0, 0, 0], "direction": [1, 0, 0]})

    with pytest.raises(ValueError, match="interface must be 'tube' or 'pad'"):
        _validate_port("inlet", {"origin": [0, 0, 0], "direction": [1, 0, 0], "interface": "flange"})


def test_port_unknown_keys_rejected() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        _validate_port(
            "inlet",
            {
                "origin": [0, 0, 0],
                "direction": [1, 0, 0],
                "interface": "tube",
                "outer_d": 10.0,
                "wall_t": 1.0,
                "pad_d": 10.0,  # invalid for tube
            },
        )

    with pytest.raises(ValueError, match="unknown keys"):
        _validate_port(
            "pad1",
            {
                "origin": [0, 0, 0],
                "direction": [1, 0, 0],
                "interface": "pad",
                "pad_d": 10.0,
                "outer_d": 10.0,  # invalid for pad
            },
        )


@pytest.mark.parametrize(
    "bad_origin",
    [
        None,
        [0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],
        [True, 0.0, 0.0],
        ["0", 0.0, 0.0],
        [math.nan, 0.0, 0.0],
        [math.inf, 0.0, 0.0],
    ],
)
def test_port_origin_validation(bad_origin: object) -> None:
    port: dict[str, object] = {
        "direction": [1.0, 0.0, 0.0],
        "interface": "tube",
        "outer_d": 10.0,
        "wall_t": 1.0,
    }
    if bad_origin is not None:
        port["origin"] = bad_origin
    with pytest.raises(ValueError):
        _validate_port("inlet", port)


@pytest.mark.parametrize(
    "bad_dir",
    [
        None,
        [0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],
        [True, 0.0, 0.0],
        ["1", 0.0, 0.0],
        [math.nan, 0.0, 0.0],
        [math.inf, 0.0, 0.0],
        [0.0, 0.0, 0.0],  # zero norm
    ],
)
def test_port_direction_validation(bad_dir: object) -> None:
    port: dict[str, object] = {
        "origin": [0.0, 0.0, 0.0],
        "interface": "tube",
        "outer_d": 10.0,
        "wall_t": 1.0,
    }
    if bad_dir is not None:
        port["direction"] = bad_dir
    with pytest.raises(ValueError):
        _validate_port("inlet", port)


@pytest.mark.parametrize(
    "bad_outer_d",
    [
        None,
        0.0,
        -1.0,
        True,
        "10",
        math.nan,
        math.inf,
    ],
)
def test_tube_outer_d_validation(bad_outer_d: object) -> None:
    port: dict[str, object] = {
        "origin": [0.0, 0.0, 0.0],
        "direction": [1.0, 0.0, 0.0],
        "interface": "tube",
        "wall_t": 1.0,
    }
    if bad_outer_d is not None:
        port["outer_d"] = bad_outer_d
    with pytest.raises(ValueError):
        _validate_port("inlet", port)


@pytest.mark.parametrize(
    "bad_wall_t",
    [
        None,
        -0.5,
        5.0,   # wall_t == outer_d / 2
        6.0,   # wall_t > outer_d / 2
        True,
        "1",
        math.nan,
        math.inf,
    ],
)
def test_tube_wall_t_validation(bad_wall_t: object) -> None:
    port: dict[str, object] = {
        "origin": [0.0, 0.0, 0.0],
        "direction": [1.0, 0.0, 0.0],
        "interface": "tube",
        "outer_d": 10.0,
    }
    if bad_wall_t is not None:
        port["wall_t"] = bad_wall_t
    with pytest.raises(ValueError):
        _validate_port("inlet", port)


def test_tube_wall_t_zero_allowed() -> None:
    port = {
        "origin": [0.0, 0.0, 0.0],
        "direction": [1.0, 0.0, 0.0],
        "interface": "tube",
        "outer_d": 10.0,
        "wall_t": 0.0,
    }
    norm = _validate_port("inlet", port)
    assert norm["wall_t"] == 0.0


@pytest.mark.parametrize(
    "bad_pad_d",
    [
        None,
        0.0,
        -1.0,
        True,
        "10",
        math.nan,
        math.inf,
    ],
)
def test_pad_d_validation(bad_pad_d: object) -> None:
    port: dict[str, object] = {
        "origin": [0.0, 0.0, 0.0],
        "direction": [1.0, 0.0, 0.0],
        "interface": "pad",
    }
    if bad_pad_d is not None:
        port["pad_d"] = bad_pad_d
    with pytest.raises(ValueError):
        _validate_port("pad1", port)
