"""Independent analytic and geometry checks for the T2 optics kernel."""

import math

import numpy as np
import pytest

from app.modules.process_stack.optics_t2 import (
    ArrayModel,
    Optics,
    fresnel_interface,
    lit_gaps,
)


def _optics(*, across: int = 1, layers: int = 1, pitch: float = 0.09) -> Optics:
    return Optics(0.06, across, layers, pitch, pitch, 0.0, 0.0, 0.1, 1.0, 1.0, 0.0)


def test_isolated_tube_beam_and_isotropic_sky_have_analytic_intercepts() -> None:
    model = ArrayModel(_optics())
    result = model.evaluate(np.array([math.pi / 2]), np.array([0.0]), np.array([1000.0]), np.array([500.0]))
    assert result["beam_intercepted"][0] == pytest.approx(60.0, rel=1e-12)
    assert result["sky_intercepted"][0] == pytest.approx(math.pi * 0.06 * 500.0 / 2.0, rel=1e-12)
    assert result["transmitted"][0] == pytest.approx(result["intercepted"][0], rel=1e-12)


def test_zero_extinction_equivalent_irradiance_matches_normal_beam() -> None:
    model = ArrayModel(_optics())
    result = model.evaluate(np.array([math.pi / 2]), np.array([0.0]), np.array([750.0]), np.array([0.0]))
    assert result["i0_eq"][0] == pytest.approx(750.0, rel=1e-8)
    assert result["diffuse_share"][0] == 0.0


def test_shadow_widths_sum_to_projected_silhouette_union() -> None:
    optics = _optics(across=4, layers=3)
    centres = optics.centres()
    directions = np.random.default_rng(174).uniform(-math.pi, math.pi, 10_000)
    low, high = lit_gaps(centres, optics.diameter, directions)
    assert np.all(high >= low)
    assert np.all(low >= -optics.diameter / 2 - 1e-12)
    assert np.all(high <= optics.diameter / 2 + 1e-12)
    for direction, visible in zip(directions, (high - low).sum(axis=(1, 2)), strict=True):
        normal = np.array([math.cos(direction), -math.sin(direction)])
        projection = centres @ normal
        intervals = sorted((p - optics.diameter / 2, p + optics.diameter / 2) for p in projection)
        start, end = intervals[0]
        union = 0.0
        for left, right in intervals[1:]:
            if left > end:
                union += end - start
                start, end = left, right
            else:
                end = max(end, right)
        union += end - start
        assert visible == pytest.approx(union, abs=1e-12)


def test_fresnel_interface_conserves_power() -> None:
    angles = np.linspace(0.001, 1.0, 1000)
    rs, rp, ts, tp = fresnel_interface(angles, 1.0, 1.5)
    np.testing.assert_allclose(rs + ts, 1.0, atol=1e-12)
    np.testing.assert_allclose(rp + tp, 1.0, atol=1e-12)
