"""Layered diameter checks for the explicit v2 optics kernel."""

import math

import numpy as np
import pytest

from app.modules.process_stack.optics_t2 import ArrayModel, Optics, OpticsError


def _layout(**changes: float) -> Optics:
    values = dict(diameter=0.046, tubes_across=1, layers=1, pitch_h=0.08, pitch_v=0.08,
                  stagger=0.0, axis_azimuth=0.0, centre_height=0.08, n_wall=1.0,
                  n_water=1.0, lower_albedo=0.0, outer_diameter=0.05,
                  wall_absorption_m_inv=0.0)
    values.update(changes)
    return Optics(**values)


def _beam(optics: Optics) -> dict[str, np.ndarray]:
    return ArrayModel(optics).evaluate(np.array([math.pi / 2]), np.array([0.0]),
                                       np.array([1000.0]), np.array([0.0]), np.array([10.0]))


def test_v2_outer_interception_inner_culture_and_versioned_digest() -> None:
    optics = _layout()
    result = _beam(optics)
    assert optics.version == "optics_t2.v2"
    assert result["beam_intercepted"][0] == pytest.approx(50.0)
    assert result["transmitted"][0] == pytest.approx(46.0, rel=1e-8)
    assert result["i0_eq"][0] == pytest.approx(1000.0, rel=2e-5)
    assert 0 < result["absorbed"][0] < result["transmitted"][0]
    assert optics.digest() != _layout(wall_absorption_m_inv=1.0).digest()


def test_v2_wall_loss_is_monotone_and_absorption_is_bounded() -> None:
    clear = _beam(_layout())
    lossy = _beam(_layout(wall_absorption_m_inv=500.0))
    assert 0 < lossy["transmitted"][0] < clear["transmitted"][0]
    assert 0 <= lossy["absorbed"][0] < lossy["transmitted"][0]
    assert lossy["transmitted"][0] <= lossy["intercepted"][0]


@pytest.mark.parametrize("change", [
    {"outer_diameter": None},
    {"outer_diameter": 0.046},
    {"outer_diameter": 0.05, "wall_absorption_m_inv": None},
    {"outer_diameter": 0.05, "wall_absorption_m_inv": -1.0},
])
def test_v2_rejects_incomplete_or_invalid_wall(change: dict[str, float | None]) -> None:
    with pytest.raises(OpticsError, match="V2 needs outer diameter"):
        _layout(**change)


def test_v2_pitch_checks_outer_diameter() -> None:
    with pytest.raises(OpticsError, match="pitch"):
        _layout(pitch_h=0.048)


def test_v2_converges_to_v1_as_outer_diameter_approaches_inner() -> None:
    base = dict(diameter=0.046, tubes_across=3, layers=2, pitch_h=0.07, pitch_v=0.06, stagger=0.5,
                axis_azimuth=0.3, centre_height=0.06, n_wall=1.49, n_water=1.33, lower_albedo=0.2)
    sun = (np.array([0.25, 1.2]), np.array([0.4, 3.5]), np.array([800.0, 1500.0]),
           np.array([300.0, 150.0]), np.array([30.0, 30.0]))
    v1 = ArrayModel(Optics(**base)).evaluate(*sun)
    v2 = ArrayModel(Optics(**base, outer_diameter=0.046 * (1 + 1e-7),
                           wall_absorption_m_inv=0.0)).evaluate(*sun)
    for key in ("intercepted", "transmitted", "absorbed", "i0_eq"):
        assert np.asarray(v2[key]) == pytest.approx(np.asarray(v1[key]), rel=1e-6)
    opaque = ArrayModel(Optics(**base, outer_diameter=0.05, wall_absorption_m_inv=1e6)).evaluate(*sun)
    assert np.all(np.asarray(opaque["transmitted"]) == 0.0)
