"""Read the typed pipe case's OpenFOAM probes without inferring missing values."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.modules.bluecad.cfd_pipe.casegen import probe_locations
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract
from app.modules.bluecad.cfd_pipe.references import (
    darcy_blasius,
    darcy_colebrook_smooth,
    poiseuille_velocity,
    pressure_gradient_pa_m,
)

_PROBE = re.compile(r"^# Probe (\d+) \(([^)]+)\)$")
_VECTOR = re.compile(r"\(([^)]+)\)")


class ProbeResultError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeSample:
    iteration: int
    locations: tuple[tuple[float, float, float], ...]
    values: tuple[float | tuple[float, float, float], ...]


def _numbers(text: str, count: int) -> tuple[float, ...]:
    try:
        values = tuple(float(part) for part in text.split())
    except ValueError as exc:
        raise ProbeResultError("probe contains a non-numeric value") from exc
    if len(values) != count or not all(math.isfinite(value) for value in values):
        raise ProbeResultError("probe has missing or non-finite values")
    return values


def _vector(text: str) -> tuple[float, float, float]:
    x, y, z = _numbers(text, 3)
    return x, y, z


def parse_probes(text: str, *, vectors: bool) -> ProbeSample:
    locations: list[tuple[float, float, float]] = []
    rows: list[tuple[int, tuple[float | tuple[float, float, float], ...]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        match = _PROBE.fullmatch(line)
        if match:
            if rows or int(match.group(1)) != len(locations):
                raise ProbeResultError("probe header has missing or reordered locations")
            locations.append(_vector(match.group(2)))
            continue
        if not line or line.startswith("#"):
            continue
        if not locations:
            raise ProbeResultError("probe data has no locations")
        values: tuple[float | tuple[float, float, float], ...]
        if vectors:
            values = tuple(_vector(group) for group in _VECTOR.findall(line))
            prefix = _VECTOR.sub("", line).strip()
            parts = prefix.split()
            if len(parts) != 1:
                raise ProbeResultError("vector probe row is malformed")
        else:
            parts = line.split()
            values = _numbers(" ".join(parts[1:]), len(locations))
        try:
            iteration = int(parts[0])
        except (ValueError, IndexError) as exc:
            raise ProbeResultError("probe iteration is invalid") from exc
        if iteration < 0 or len(values) != len(locations) or (rows and iteration <= rows[-1][0]):
            raise ProbeResultError("probe row is incomplete or out of order")
        rows.append((iteration, values))
    if not locations or not rows:
        raise ProbeResultError("probe output has no complete sample")
    iteration, values = rows[-1]
    return ProbeSample(iteration, tuple(locations), values)


def _developed_samples(contract: PipeStudyContract, pressure_text: str, velocity_text: str
                       ) -> tuple[int, float, ProbeSample]:
    """Return the final iteration, the density-scaled gradient and the velocity sample.

    The upstream pressure station must lie past the contract's development length;
    otherwise the gradient includes entrance-region losses and is refused.
    """
    pressure = parse_probes(pressure_text, vectors=False)
    velocity = parse_probes(velocity_text, vectors=True)
    expected = probe_locations(contract)
    if pressure.locations != velocity.locations or len(pressure.locations) != len(expected):
        raise ProbeResultError("pressure and velocity probes do not match")
    for actual, planned in zip(pressure.locations, expected, strict=True):
        if any(not math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-10) for a, b in zip(actual, planned, strict=True)):
            raise ProbeResultError("probe location differs from the typed case")
    if pressure.iteration != velocity.iteration:
        raise ProbeResultError("pressure and velocity samples are from different iterations")
    if expected[0][0] < contract.required_length_m:
        raise ProbeResultError(
            f"upstream pressure station at {expected[0][0]:.4g} m lies before the "
            f"{contract.required_length_m:.4g} m development length"
        )
    p0, p1 = pressure.values[:2]
    if not isinstance(p0, float) or not isinstance(p1, float):
        raise ProbeResultError("pressure samples are not scalar")
    gradient = (p0 - p1) * contract.fluid.density_kg_m3 / (expected[1][0] - expected[0][0])
    return pressure.iteration, gradient, velocity


def laminar_comparison(contract: PipeStudyContract, pressure_text: str, velocity_text: str) -> dict[str, float | int]:
    """Compare final developed-section probes to Hagen–Poiseuille and its profile.

    Pressure probes contain kinematic pressure; multiplying by density is required
    before reporting Pa/m. Errors are reported, never converted to a pass claim.
    """
    if contract.regime != "laminar":
        raise ProbeResultError("laminar comparison requires a laminar contract")
    iteration, gradient, velocity = _developed_samples(contract, pressure_text, velocity_text)
    expected = probe_locations(contract)
    reference = pressure_gradient_pa_m(64.0 / contract.reynolds, contract.fluid.density_kg_m3,
                                       contract.mean_velocity_m_s, contract.diameter_m)
    errors = []
    for location, value in zip(expected[1:], velocity.values[1:], strict=True):
        if not isinstance(value, tuple):
            raise ProbeResultError("velocity samples are not vectors")
        radius = math.hypot(location[1], location[2])
        profile = poiseuille_velocity(radius, contract.diameter_m / 2.0, contract.mean_velocity_m_s)
        errors.append(abs(value[0] - profile) / contract.mean_velocity_m_s)
    return {
        "iteration": iteration,
        "pressure_gradient_pa_m": gradient,
        "hagen_poiseuille_pa_m": reference,
        "pressure_gradient_relative_error": abs(gradient - reference) / reference,
        "profile_max_error_over_mean_velocity": max(errors),
    }


def turbulent_comparison(contract: PipeStudyContract, pressure_text: str, velocity_text: str) -> dict[str, float | int]:
    """Compare the final developed-section RANS gradient to smooth-pipe correlations.

    Errors are signed (measured minus reference, over reference). Colebrook and
    Blasius are correlations with their own scatter of a few percent; the report
    states the difference and makes no qualification claim.
    """
    if contract.regime != "turbulent_rans":
        raise ProbeResultError("turbulent comparison requires a turbulent_rans contract")
    iteration, gradient, _ = _developed_samples(contract, pressure_text, velocity_text)
    rho, speed, diameter = contract.fluid.density_kg_m3, contract.mean_velocity_m_s, contract.diameter_m
    colebrook = pressure_gradient_pa_m(darcy_colebrook_smooth(contract.reynolds), rho, speed, diameter)
    blasius = pressure_gradient_pa_m(darcy_blasius(contract.reynolds), rho, speed, diameter)
    return {
        "iteration": iteration,
        "reynolds": contract.reynolds,
        "pressure_gradient_pa_m": gradient,
        "darcy_friction_factor": 2.0 * diameter * gradient / (rho * speed**2),
        "colebrook_smooth_pa_m": colebrook,
        "blasius_pa_m": blasius,
        "signed_error_vs_colebrook": (gradient - colebrook) / colebrook,
        "signed_error_vs_blasius": (gradient - blasius) / blasius,
    }
