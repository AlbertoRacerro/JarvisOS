"""PBR solar optics T2 kernel (spec 174): tube-array interception, Fresnel wall, opaque exact shading.

One pure, deterministic numpy kernel. It performs no I/O, keeps no global mutable state and imports nothing
from the dynamic engine. Inputs are sun elevation/azimuth, DNI and DHI already converted to PAR, the tube
diameter D, a representative cross-section module of identical horizontal tubes, the wall and water indices,
the lower-hemisphere albedo (sea or ground) and (optionally) the culture extinction kappa. Everything is per metre of tube length with per-tube and module-mean results. DNI/DHI are in umol m^-2 s^-1; intercepted, transmitted
and absorbed outputs are in umol s^-1 m^-1. All angles are radians, all lengths metres.

Geometry (decisions 2-6 of the spec)
------------------------------------
* Tubes are infinite parallel horizontal cylinders of equal diameter with axis azimuth alpha. For a unit
  vector s towards the source, a = s . axis is the axial component, c = cos(gamma) = sqrt(1 - a^2) and
  psi = atan2(s_h, s_z) the in-plane direction. A beam strip carries DNI*c per unit lateral width, so an
  isolated tube normal to the beam intercepts DNI*D.
* Shading is exact 2D projected-shadow geometry with opaque upstream tubes: the unshadowed part of tube i is its
  silhouette interval minus the union of overlapping upstream silhouettes, evaluated with sorted breakpoints
  (a sort-by-lower-edge sweep with a running maximum), never a grid.
* Wall: unpolarised, thin, incoherent slab air/wall/water, T = T1*T2/(1 - R1*R2) per polarisation, averaged.
  The local incidence angle at silhouette offset s = R sin(u) satisfies cos(theta) = c cos(u).
* Culture: Beer-Lambert along the refracted straight chord l = D cos(phi_w)/cos(gamma_w); internal
  re-reflection is ignored. Absorbed = sum of T (1 - exp(-kappa l)).
* Sources: beam (DNI), isotropic sky (radiance DHI/pi, upper hemisphere) and isotropic lower-hemisphere reflection
  (radiance rho*GHI/pi, lower hemisphere). The diffuse integrals depend only on the layout and axis, so they
  are integrated once per layout.

Integration variable. Silhouette offsets are integrated in u = asin(s/R), which removes the square-root edge
singularity of the wall transmission; thin-limit/transmission beam quantities use one 10-point Gauss-Legendre
panel per unshadowed interval and absorption uses a fixed graded panel set that resolves optical depths to 1000.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
from numpy.polynomial.legendre import leggauss

VERSION: Final = "optics_t2.v1"
VERSION_V2: Final = "optics_t2.v2"
MAX_MODULE_TUBES: Final = 64
# Maximum midpoint interval supported by the current T2 hand-off evidence.
OPTICS_RESOLUTION_MAX_MIN: Final = 15
_GAP_NODES: Final = 10  # thin / transmission panel (smooth integrand)
_FINE_NODES: Final = 10  # per graded panel for kappa-dependent absorption
_FINE_DELTAS: Final = (0.0, 1e-3, 4e-3, 1.6e-2, 6.4e-2, 0.25, 0.7)
_EPS_NODES: Final = 12  # axial-angle Gauss-Legendre nodes of the hemisphere rules
_PSI_PANELS: Final = 36
_PSI_NODES: Final = 4
_C_MIN: Final = 1e-14


class OpticsError(ValueError):
    """Typed kernel input failure with a stable code and optional parameter detail."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message[:600])
        self.code = code
        self.detail = detail or {}


# --------------------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Optics:
    """Validated layout: lengths in m, angles in radians, lower_albedo for sea or ground.

    centre_height is the lowest row centre; stagger shifts alternating rows horizontally.
    Indices are explicit PAR-mean refractive indices. No finite-end/support shading.
    """

    diameter: float
    tubes_across: int
    layers: int
    pitch_h: float
    pitch_v: float
    stagger: float
    axis_azimuth: float
    centre_height: float
    n_wall: float
    n_water: float
    lower_albedo: float
    tube_count: int | None = None
    outer_diameter: float | None = None
    wall_absorption_m_inv: float | None = None

    def __post_init__(self) -> None:
        numbers = (self.diameter, self.pitch_h, self.pitch_v, self.stagger, self.axis_azimuth, self.centre_height,
                   self.n_wall, self.n_water, self.lower_albedo)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in numbers):
            raise OpticsError("OPTICS_PARAMETER_INVALID", "Optics inputs must be finite numbers.")
        for name in ("tubes_across", "layers"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise OpticsError("OPTICS_LAYOUT_INVALID", f"{name} must be a whole number of at least 1.",
                                  {"field": name})
        if not 0.0 < self.diameter:
            raise OpticsError("OPTICS_PARAMETER_INVALID", "Tube diameter must be positive in m.", {"field": "diameter"})
        if self.outer_diameter is not None or self.wall_absorption_m_inv is not None:
            if (self.outer_diameter is None or self.wall_absorption_m_inv is None
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                               for v in (self.outer_diameter, self.wall_absorption_m_inv))
                    or self.outer_diameter <= self.diameter or self.wall_absorption_m_inv < 0):
                raise OpticsError("OPTICS_WALL_INVALID", "V2 needs outer diameter greater than inner diameter "
                                  "and a nonnegative declared wall absorption coefficient.",
                                  {"fields": ["outer_diameter", "wall_absorption_m_inv"]})
        if self.tubes_across * self.layers > MAX_MODULE_TUBES:
            raise OpticsError("OPTICS_LAYOUT_INVALID",
                              f"The cross-section module has {self.tubes_across * self.layers} tubes; the limit is "
                              f"{MAX_MODULE_TUBES}.", {"module_tubes": self.tubes_across * self.layers})
        if self.tube_count is not None and (isinstance(self.tube_count, bool)
                or not isinstance(self.tube_count, int) or self.tube_count < self.module_tubes):
            raise OpticsError("OPTICS_LAYOUT_INVALID", "tube_count must be an integer >= module size.")
        if self.pitch_h < self.silhouette_diameter or self.pitch_v < self.silhouette_diameter:
            raise OpticsError("OPTICS_LAYOUT_INVALID", "Tube pitch must be at least the tube (outer, when set) diameter.",
                              {"pitch_h": self.pitch_h, "pitch_v": self.pitch_v, "diameter": self.silhouette_diameter})
        if self.centre_height < self.silhouette_diameter / 2.0:
            raise OpticsError("OPTICS_LAYOUT_INVALID", "The lowest tube centre must be at least one tube (outer, when set) radius "
                              "above the sea or ground.", {"field": "centre_height_above_sea"})
        if not (1.0 <= self.n_wall and 1.0 <= self.n_water):
            raise OpticsError("OPTICS_PARAMETER_INVALID", "Refractive indices must be >= 1.",
                              {"n_wall": self.n_wall, "n_water": self.n_water})
        if not 0.0 <= self.lower_albedo <= 1.0:
            raise OpticsError("OPTICS_PARAMETER_INVALID", "Lower-hemisphere albedo must lie in [0, 1].",
                              {"field": "lower_albedo"})
        centres = self.centres()
        if len(centres) > 1:
            offsets = centres[:, None, :] - centres[None, :, :]
            distance = np.sqrt((offsets**2).sum(-1)) + np.eye(len(centres)) * 1e9
            if distance.min() < self.silhouette_diameter * (1.0 - 1e-9):
                raise OpticsError("OPTICS_LAYOUT_INVALID", "Tubes in the layout overlap.", {})

    @property
    def module_tubes(self) -> int:
        return self.tubes_across * self.layers

    @property
    def version(self) -> str:
        return VERSION_V2 if self.outer_diameter is not None else VERSION

    @property
    def silhouette_diameter(self) -> float:
        return self.outer_diameter if self.outer_diameter is not None else self.diameter

    def centres(self) -> np.ndarray:
        """Tube centres (h, z) in the cross-section plane, shape (N, 2); h along the horizontal normal of the axis."""
        rows = []
        for layer in range(self.layers):
            shift = self.stagger if layer % 2 else 0.0
            for index in range(self.tubes_across):
                rows.append(((index - (self.tubes_across - 1) / 2.0) * self.pitch_h + shift,
                             self.centre_height + layer * self.pitch_v))
        return np.asarray(rows, dtype=np.float64)

    def digest(self) -> str:
        payload = json.dumps([self.version, self.diameter, self.tubes_across, self.layers, self.pitch_h, self.pitch_v,
                              self.stagger, self.axis_azimuth, self.centre_height, self.n_wall, self.n_water,
                              self.lower_albedo] + ([self.outer_diameter, self.wall_absorption_m_inv]
                                                    if self.version == VERSION_V2 else []), separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


# -------------------------------------------------------------------------------------------- Fresnel


def _fresnel_amplitudes(n1: float, n2: float, cos_i: np.ndarray, cos_t: np.ndarray) -> tuple[np.ndarray, ...]:
    """Amplitude reflection/transmission coefficients (rs, rp, ts, tp); 0/0 (equal media) is no interface."""
    den_s = n1 * cos_i + n2 * cos_t
    den_p = n2 * cos_i + n1 * cos_t
    safe_s = np.where(den_s > 0.0, den_s, 1.0)
    safe_p = np.where(den_p > 0.0, den_p, 1.0)
    rs = np.where(den_s > 0.0, (n1 * cos_i - n2 * cos_t) / safe_s, 0.0)
    rp = np.where(den_p > 0.0, (n2 * cos_i - n1 * cos_t) / safe_p, 0.0)
    ts = np.where(den_s > 0.0, 2.0 * n1 * cos_i / safe_s, 1.0)
    tp = np.where(den_p > 0.0, 2.0 * n1 * cos_i / safe_p, 1.0)
    return rs, rp, ts, tp


def fresnel_interface(cos_i: np.ndarray | float, n1: float, n2: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Power coefficients (Rs, Rp, Ts, Tp) of one interface n1 -> n2 at incidence cosine ``cos_i``.

    Transmittance is computed independently from the amplitude coefficient (T = n2 cos_t/(n1 cos_i) t^2) so
    R + T = 1 is a genuine energy-conservation check. Total internal reflection returns R = 1, T = 0.
    """
    cos_i_arr = np.clip(np.asarray(cos_i, dtype=np.float64), 0.0, 1.0)
    sin2_t = (n1 / n2) ** 2 * (1.0 - cos_i_arr**2)
    total = (sin2_t >= 1.0) & (n1 != n2)
    cos_t = np.sqrt(np.maximum(0.0, 1.0 - sin2_t))
    rs, rp, ts, tp = _fresnel_amplitudes(n1, n2, cos_i_arr, cos_t)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(cos_i_arr > 0.0, n2 * cos_t / (n1 * np.where(cos_i_arr > 0.0, cos_i_arr, 1.0)), 0.0)
    r_s: np.ndarray = rs**2
    r_p: np.ndarray = rp**2
    t_s = np.where(cos_i_arr > 0.0, ratio * ts**2, np.where(n1 == n2, 1.0, 0.0))
    t_p = np.where(cos_i_arr > 0.0, ratio * tp**2, np.where(n1 == n2, 1.0, 0.0))
    r_s = np.where(total, 1.0, r_s)
    r_p = np.where(total, 1.0, r_p)
    t_s = np.where(total, 0.0, t_s)
    t_p = np.where(total, 0.0, t_p)
    return r_s, r_p, t_s, t_p


def wall_transmittance(cos_theta: np.ndarray | float, n_wall: float, n_water: float) -> np.ndarray:
    """Unpolarised thin-slab transmittance air / wall / water with incoherent multiple reflections."""
    return fresnel_slab(cos_theta, n_wall, n_water)[0]


def fresnel_slab(cos_theta: np.ndarray | float, n_wall: float, n_water: float) -> tuple[np.ndarray, np.ndarray]:
    """(T, R) of the slab, polarisation averaged, no absorption. R is computed independently of T."""
    cos0 = np.clip(np.asarray(cos_theta, dtype=np.float64), 0.0, 1.0)
    sin2 = 1.0 - cos0**2
    cos_wall = np.sqrt(np.maximum(0.0, 1.0 - sin2 / n_wall**2))
    r1s, r1p, _, _ = fresnel_interface(cos0, 1.0, n_wall)
    r2s, r2p, _, _ = fresnel_interface(cos_wall, n_wall, n_water)
    transmitted = np.zeros_like(cos0)
    reflected = np.zeros_like(cos0)
    for r1, r2 in ((r1s, r2s), (r1p, r2p)):
        t1, t2 = 1.0 - r1, 1.0 - r2
        denominator = 1.0 - r1 * r2
        safe = np.where(denominator > 0.0, denominator, 1.0)
        transmitted += 0.5 * np.where(denominator > 0.0, t1 * t2 / safe, 0.0)
        reflected += 0.5 * np.where(denominator > 0.0, r1 + t1 * t1 * r2 / safe, 1.0)
    return transmitted, reflected


# ------------------------------------------------------------------------------------------ geometry


def sun_unit_vector(elevation: np.ndarray, azimuth: np.ndarray) -> np.ndarray:
    """Unit vectors towards source (east, north, up); angles in radians, azimuth clockwise from north."""
    el, az = np.broadcast_arrays(np.asarray(elevation, float), np.asarray(azimuth, float))
    return np.stack([np.cos(el) * np.sin(az), np.cos(el) * np.cos(az), np.sin(el)], axis=-1)


def decompose(vectors: np.ndarray, axis_azimuth: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Axial component a, cos(gamma) and in-plane angle psi for (east, north, up) unit vectors."""
    axis = np.array([math.sin(axis_azimuth), math.cos(axis_azimuth), 0.0])
    normal = np.array([math.cos(axis_azimuth), -math.sin(axis_azimuth), 0.0])
    a = np.clip(vectors @ axis, -1.0, 1.0)
    horizontal = vectors @ normal
    up = vectors[..., 2]
    c = np.hypot(horizontal, up)
    return a, c, np.arctan2(horizontal, up)


def source_geometry(elevation: np.ndarray, azimuth: np.ndarray,
                    axis_azimuth: float) -> tuple[np.ndarray, np.ndarray]:
    """Return cos(gamma), in-plane psi from source/axis angles (radians, azimuth from north)."""
    el, az = _sun_inputs(elevation, azimuth)
    if not math.isfinite(axis_azimuth):
        raise OpticsError("OPTICS_INPUT_INVALID", "Axis azimuth must be finite in radians.")
    _, cosine, psi = decompose(sun_unit_vector(el, az), axis_azimuth)
    return cosine, psi


def lit_gaps(centres: np.ndarray, diameter: float, psi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unshadowed intervals of every tube for in-plane source directions ``psi`` (shape (M,)).

    Returns lower/upper local offsets (M, N, N + 1) in [-R, R]; zero-length candidates mean 'none'. The unshadowed
    set of tube i is its silhouette minus the union of overlapping upstream silhouettes, found by sorting the
    upstream intervals by lower edge and sweeping a running maximum (exact; no grid).
    """
    psi = np.atleast_1d(np.asarray(psi, dtype=np.float64))
    radius = diameter / 2.0
    towards = np.stack([np.sin(psi), np.cos(psi)], axis=-1)  # (M, 2) direction towards the source
    across = np.stack([np.cos(psi), -np.sin(psi)], axis=-1)  # silhouette normal
    along = towards @ centres.T  # (M, N)
    offset = across @ centres.T  # (M, N)
    upstream = ((along[:, None, :] - along[:, :, None]) > 1e-12 * diameter) & (
        np.abs(offset[:, None, :] - offset[:, :, None]) < diameter)  # [m, i, j]: j shadows i
    window_low = offset - radius  # (M, N)
    window_high = offset + radius
    low_i = window_low[:, :, None]
    high_i = window_high[:, :, None]
    shadow_low = np.where(upstream, np.clip(offset[:, None, :] - radius, low_i, high_i), low_i)
    shadow_high = np.where(upstream, np.clip(offset[:, None, :] + radius, low_i, high_i), low_i)
    order = np.argsort(shadow_low, axis=-1, kind="stable")
    sorted_low = np.take_along_axis(shadow_low, order, axis=-1)
    sorted_high = np.take_along_axis(shadow_high, order, axis=-1)
    covered = np.maximum.accumulate(np.maximum(sorted_high, low_i), axis=-1)
    gap_low = np.concatenate([window_low[:, :, None], covered], axis=-1)  # (M, N, N + 1)
    gap_high = np.concatenate([sorted_low, window_high[:, :, None]], axis=-1)
    gap_high = np.maximum(gap_high, gap_low)
    shift = offset[:, :, None]
    return gap_low - shift, gap_high - shift


def _gauss(n: int) -> tuple[np.ndarray, np.ndarray]:
    nodes, weights = leggauss(n)
    return nodes, weights


def _panel_edges(fine: bool) -> np.ndarray:
    if not fine:
        return np.array([-math.pi / 2, math.pi / 2])
    half = math.pi / 2
    upper = [half - d for d in _FINE_DELTAS]
    return np.array(sorted(set([-v for v in upper] + upper)))


# ------------------------------------------------------------------------------------- integration


def _node_terms(c: np.ndarray, a: np.ndarray, u: np.ndarray, optics: Optics) -> tuple[np.ndarray, np.ndarray]:
    """Wall transmittance and refracted chord length at nodes u (any shape broadcastable with c, a)."""
    cos_theta = c * np.cos(u)
    if optics.version == VERSION_V2 and optics.wall_absorption_m_inv is not None:
        outer_radius = optics.silhouette_diameter / 2
        inner_radius = optics.diameter / 2
        impact = outer_radius * c * np.sin(u)
        c_wall = np.sqrt(1.0 - (a / optics.n_wall) ** 2)
        c_water = np.sqrt(1.0 - (a / optics.n_water) ** 2)
        wall_impact = np.abs(impact) / (optics.n_wall * c_wall)
        culture_impact = np.abs(impact) / (optics.n_water * c_water)
        reaches_culture = (wall_impact < inner_radius) & (culture_impact < inner_radius)
        wall_path = (np.sqrt(np.maximum(0.0, outer_radius**2 - wall_impact**2))
                     - np.sqrt(np.maximum(0.0, inner_radius**2 - wall_impact**2))) / c_wall
        wall_path = np.maximum(wall_path, 0.0)
        attenuation = np.exp(-optics.wall_absorption_m_inv * wall_path)
        inner_cos = c_wall * np.sqrt(np.maximum(0.0, 1.0 - (wall_impact / inner_radius)**2))
        r1s, r1p, _, _ = fresnel_interface(cos_theta, 1.0, optics.n_wall)
        r2s, r2p, _, _ = fresnel_interface(inner_cos, optics.n_wall, optics.n_water)
        transmittance = np.zeros_like(cos_theta)
        for r1, r2 in ((r1s, r2s), (r1p, r2p)):
            bounce = r1 * r2 * attenuation**2
            transmittance += 0.5 * (1 - r1) * (1 - r2) * attenuation / np.maximum(1 - bounce, 1e-300)
        transmittance = np.where(reaches_culture, transmittance, 0.0)
        chord = np.where(reaches_culture,
                         2 * np.sqrt(np.maximum(0.0, inner_radius**2 - culture_impact**2)) / c_water, 0.0)
        return transmittance, chord
    transmittance = wall_transmittance(cos_theta, optics.n_wall, optics.n_water)
    n = optics.n_water
    cos_gamma_w = np.sqrt(np.maximum(1.0 - (a / n) ** 2, 1e-300))
    sin_phi_w = np.clip(c * np.sin(u) / (n * cos_gamma_w), -1.0, 1.0)
    chord = optics.diameter * np.sqrt(np.maximum(0.0, 1.0 - sin_phi_w**2)) / cos_gamma_w
    return transmittance, chord


def _rays(optics: Optics, gap_low: np.ndarray, gap_high: np.ndarray,
          c: np.ndarray, a: np.ndarray, fine: bool) -> tuple[np.ndarray, ...]:
    """Flattened direction/tube indices, projected weights (m), transmission and chords (m)."""
    radius = optics.silhouette_diameter / 2
    mask = gap_high > gap_low
    m, tube, _ = np.nonzero(mask)
    u0 = np.arcsin(np.clip(gap_low[mask] / radius, -1, 1))
    u1 = np.arcsin(np.clip(gap_high[mask] / radius, -1, 1))
    edges = (np.clip(_panel_edges(True)[None, :], u0[:, None], u1[:, None]) if fine
             else np.stack([u0, u1], axis=1))
    if optics.version == VERSION_V2:
        # The wall/culture boundary is a step in transmission. Split the quadrature
        # exactly there; a smooth Gauss panel across it biases the culture flux.
        c_ray, a_ray = c[m], a[m]
        wall_cos = np.sqrt(1.0 - (a_ray / optics.n_wall) ** 2)
        water_cos = np.sqrt(1.0 - (a_ray / optics.n_water) ** 2)
        limit = (optics.diameter / optics.silhouette_diameter) * np.minimum(
            optics.n_wall * wall_cos, optics.n_water * water_cos) / np.maximum(c_ray, _C_MIN)
        boundary = np.arcsin(np.clip(limit, 0.0, 1.0))
        edges = np.sort(np.concatenate([edges, np.clip(-boundary[:, None], u0[:, None], u1[:, None]),
                                        np.clip(boundary[:, None], u0[:, None], u1[:, None])], axis=1), axis=1)
    x, w = _gauss(_FINE_NODES if fine else _GAP_NODES)
    half = (edges[:, 1:] - edges[:, :-1]) / 2
    u = (edges[:, 1:] + edges[:, :-1])[..., None] / 2 + half[..., None] * x
    weight = half[..., None] * w * radius * np.cos(u)
    trans, chord = _node_terms(c[m, None, None], a[m, None, None], u, optics)
    valid = weight > 0
    mi = np.broadcast_to(m[:, None, None], weight.shape)[valid]
    ti = np.broadcast_to(tube[:, None, None], weight.shape)[valid]
    return mi, ti, weight[valid], trans[valid], chord[valid]


def _integrate(optics: Optics, gap_low: np.ndarray, gap_high: np.ndarray, c: np.ndarray, a: np.ndarray,
               kappa: np.ndarray | None, fine: bool) -> dict[str, np.ndarray]:
    """Per-direction, per-tube integrals over visible intervals (before axial projection)."""
    steps, tubes = gap_low.shape[:2]
    m, tube, weight, trans, chord = _rays(optics, gap_low, gap_high, c, a, fine)
    index = m * tubes + tube

    def reduce(value: np.ndarray) -> np.ndarray:
        return np.bincount(index, weights=value, minlength=steps * tubes).reshape(steps, tubes)

    return {"width": (gap_high - gap_low).sum(axis=2),
            "transmitted": reduce(weight * trans), "thin": reduce(weight * trans * chord),
            "absorbed": (reduce(weight * trans * -np.expm1(-kappa[m] * chord))
                         if kappa is not None else np.zeros((steps, tubes)))}


def _directions(optics: Optics, vectors: np.ndarray, kappa: np.ndarray | None, fine: bool,
                chunk: int) -> dict[str, np.ndarray]:
    a, c, psi = decompose(vectors, optics.axis_azimuth)
    centres = optics.centres()
    count = len(a)
    out = {key: np.zeros((count, optics.module_tubes)) for key in ("width", "transmitted", "thin", "absorbed")}
    for start in range(0, count, chunk):
        sl = slice(start, min(count, start + chunk))
        gap_low, gap_high = lit_gaps(centres, optics.silhouette_diameter, psi[sl])
        part = _integrate(optics, gap_low, gap_high, c[sl], a[sl], None if kappa is None else kappa[sl], fine)
        for key in out:
            out[key][sl] = part[key]
    out["c"] = c
    out["a"] = a
    return out


# ---------------------------------------------------------------------------------------------- model


@dataclass(frozen=True)
class HemisphereRule:
    """Fixed quadrature directions of one hemisphere: unit vectors (east, north, up) and weights (sr)."""

    vectors: np.ndarray
    weights: np.ndarray  # cos(eps) d(eps) d(psi): the solid-angle measure


def _hemisphere_rule(lower: bool) -> HemisphereRule:
    eps_x, eps_w = _gauss(_EPS_NODES)
    eps = eps_x * math.pi / 2
    eps_weight = eps_w * math.pi / 2
    psi_x, psi_w = _gauss(_PSI_NODES)
    start = math.pi / 2 if lower else -math.pi / 2
    width = math.pi / _PSI_PANELS
    psis: list[float] = []
    psi_weights: list[float] = []
    for panel in range(_PSI_PANELS):
        centre = start + (panel + 0.5) * width
        psis.extend(centre + psi_x * width / 2)
        psi_weights.extend(psi_w * width / 2)
    psi_arr, psi_weight = np.asarray(psis), np.asarray(psi_weights)
    # Direction in the (axis, horizontal-normal, up) frame, then rotated by the axis azimuth in the model.
    e_grid, p_grid = np.meshgrid(eps, psi_arr, indexing="ij")
    a = np.sin(e_grid).ravel()
    c = np.cos(e_grid).ravel()
    h = (c * np.sin(p_grid.ravel()))
    z = (c * np.cos(p_grid.ravel()))
    weights = (np.cos(e_grid) * eps_weight[:, None] * psi_weight[None, :]).ravel()
    frame = np.stack([a, h, z], axis=-1)
    return HemisphereRule(frame, weights)


class ArrayModel:
    """Evaluator for one validated layout; diffuse integrals are computed once and cached on the instance."""

    def __init__(self, optics: Optics) -> None:
        self.optics = optics
        self.module_tubes = optics.module_tubes
        self._rules = {name: _hemisphere_rule(lower) for name, lower in (("sky", False), ("lower", True))}
        self._cache: dict[str, Any] = {}

    # -- frame handling
    def _world_vectors(self, frame: np.ndarray) -> np.ndarray:
        """Convert (axis, normal, up) frame vectors to (east, north, up) for ``decompose``."""
        alpha = self.optics.axis_azimuth
        axis = np.array([math.sin(alpha), math.cos(alpha), 0.0])
        normal = np.array([math.cos(alpha), -math.sin(alpha), 0.0])
        east_north = frame[:, 0:1] * axis[None, :2] + frame[:, 1:2] * normal[None, :2]
        return np.concatenate([east_north, frame[:, 2:3]], axis=1)

    def _hemisphere(self, name: str) -> dict[str, np.ndarray]:
        """Prepare fixed projected rays once; caller-owned instance, no global cache."""
        if name not in self._cache:
            rule = self._rules[name]
            a, c, psi = decompose(self._world_vectors(rule.vectors), self.optics.axis_azimuth)
            weights, chords, indices = [], [], []
            intercept = np.zeros(self.module_tubes)
            for start in range(0, len(a), 64):
                sl = slice(start, start + 64)
                low, high = lit_gaps(self.optics.centres(), self.optics.silhouette_diameter, psi[sl])
                intercept += ((high - low).sum(axis=2) * (rule.weights[sl] * c[sl])[:, None]).sum(axis=0)
                m, tube, w, t, chord = _rays(self.optics, low, high, c[sl], a[sl], True)
                weights.append(w * t * c[sl][m] * rule.weights[sl][m])
                chords.append(chord)
                indices.append(tube)
            weight, chord, tube = np.concatenate(weights), np.concatenate(chords), np.concatenate(indices)
            # Many angular rays have exactly the same refracted chord on the same tube.
            # Combine their weights once; the Beer-Lambert factor then needs one
            # evaluation per distinct (tube, chord) rather than per angular ray.
            pairs: np.ndarray = np.empty(len(tube), dtype=[("tube", np.int64), ("chord", np.float64)])
            pairs["tube"] = tube
            pairs["chord"] = chord
            distinct, inverse = np.unique(pairs, return_inverse=True)
            weight = np.bincount(inverse, weights=weight)
            chord = distinct["chord"]
            tube = distinct["tube"]
            self._cache[name] = {
                "intercept": intercept, "weight": weight, "chord": chord, "tube": tube,
                "transmitted": np.bincount(tube, weights=weight, minlength=self.module_tubes),
                "thin": np.bincount(tube, weights=weight * chord, minlength=self.module_tubes),
            }
        return self._cache[name]

    def diffuse_absorption(self, name: str, kappa: np.ndarray) -> np.ndarray:
        """Fixed hemisphere ray integral at extinction kappa (m^-1), shape (steps, tubes)."""
        kappa = _extinction(kappa)
        rays = self._hemisphere(name)
        unique, inverse = np.unique(kappa, return_inverse=True)
        values = np.array([np.bincount(rays["tube"],
                           weights=rays["weight"] * -np.expm1(-k * rays["chord"]),
                           minlength=self.module_tubes) for k in unique])
        return values[inverse]

    def beam(self, elevation: np.ndarray, azimuth: np.ndarray, kappa: np.ndarray | None = None,
             *, fine: bool | None = None) -> dict[str, np.ndarray]:
        """Beam integrals (steps, tubes), before c*DNI; angles radians, extinction m^-1.

        c and a are one-dimensional direction cosines. Below-horizon and axial beams are zero.
        """
        el, az = _sun_inputs(elevation, azimuth)
        k = None if kappa is None else _extinction(kappa, el.shape)
        vectors = sun_unit_vector(el, az)
        a, c, _ = decompose(vectors, self.optics.axis_azimuth)
        up = (el >= 0) & (c > _C_MIN)
        result = {key: np.zeros((len(el), self.module_tubes))
                  for key in ("width", "transmitted", "thin", "absorbed")}
        if np.any(up):
            active = _directions(self.optics, vectors[up], None if k is None else k[up],
                                 k is not None if fine is None else fine, 128)
            for key in result:
                result[key][up] = active[key]
        result["c"] = c
        result["a"] = a
        return result

    def evaluate_per_tube(self, elevation: np.ndarray, azimuth: np.ndarray, dni: np.ndarray, dhi: np.ndarray,
                          kappa: np.ndarray | None = None) -> dict[str, np.ndarray]:
        """Per-tube arrays (steps, tubes), angles radians; DNI/DHI in umol m^-2 s^-1.

        intercepted/transmitted/reflected/absorbed are umol s^-1 m^-1; i0_eq is umol m^-2 s^-1.
        thin is absorbed/kappa at kappa->0; diffuse_share includes sky and sea or ground reflection.
        Beam unshaded fraction is zero below horizon/axial incidence, geometrical even when DNI=0.
        Absorbed is returned only when extinction kappa (m^-1) is supplied.
        """
        el, az = _sun_inputs(elevation, azimuth)
        dni, dhi = np.atleast_1d(np.asarray(dni, float)), np.atleast_1d(np.asarray(dhi, float))
        if dni.shape != el.shape or dhi.shape != el.shape:
            raise OpticsError("OPTICS_INPUT_INVALID", "Sun and irradiance arrays must have equal length.")
        if not (np.all(np.isfinite(dni)) and np.all(np.isfinite(dhi))) or np.any(dni < 0) or np.any(dhi < 0):
            raise OpticsError("OPTICS_INPUT_INVALID", "DNI and DHI must be finite and nonnegative.")
        k = None if kappa is None else _extinction(kappa, el.shape)
        parts = self.beam(el, az, k)
        ghi = dni * np.maximum(np.sin(el), 0) + dhi
        sky, lower = self._hemisphere("sky"), self._hemisphere("lower")
        sky_scale = dhi[:, None] / math.pi
        lower_scale = self.optics.lower_albedo * ghi[:, None] / math.pi
        beam_scale = (dni * parts["c"])[:, None]
        out = {"beam_intercepted": beam_scale * parts["width"],
               "sky_intercepted": sky_scale * sky["intercept"],
               "lower_intercepted": lower_scale * lower["intercept"]}
        out["intercepted"] = out["beam_intercepted"] + out["sky_intercepted"] + out["lower_intercepted"]
        out["transmitted"] = (beam_scale * parts["transmitted"] + sky_scale * sky["transmitted"]
                              + lower_scale * lower["transmitted"])
        out["reflected"] = out["intercepted"] - out["transmitted"]
        diffuse_thin = sky_scale * sky["thin"] + lower_scale * lower["thin"]
        out["thin"] = beam_scale * parts["thin"] + diffuse_thin
        out["i0_eq"] = out["thin"] / (math.pi * (self.optics.diameter / 2)**2)
        out["diffuse_share"] = np.divide(diffuse_thin, out["thin"], out=np.zeros_like(diffuse_thin),
                                         where=out["thin"] > 0)
        out["beam_unshaded_fraction"] = parts["width"] / self.optics.silhouette_diameter
        if k is not None:
            out["absorbed"] = beam_scale * parts["absorbed"]
            if np.any(dhi):
                out["absorbed"] += sky_scale * self.diffuse_absorption("sky", k)
            if np.any(lower_scale):
                out["absorbed"] += lower_scale * self.diffuse_absorption("lower", k)
        return out

    def evaluate(self, elevation: np.ndarray, azimuth: np.ndarray, dni: np.ndarray, dhi: np.ndarray,
                 kappa: np.ndarray | None = None) -> dict[str, np.ndarray]:
        """Module means of evaluate_per_tube; diffuse_share is the ratio of mean thin contributions."""
        parts = self.evaluate_per_tube(elevation, azimuth, dni, dhi, kappa)
        result = {key: value.mean(axis=1) for key, value in parts.items()}
        diffuse = (parts["thin"] * parts["diffuse_share"]).mean(axis=1)
        result["diffuse_share"] = np.divide(diffuse, result["thin"], out=np.zeros_like(diffuse),
                                             where=result["thin"] > 0)
        return result


def _sun_inputs(elevation: np.ndarray, azimuth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    el, az = np.atleast_1d(np.asarray(elevation, float)), np.atleast_1d(np.asarray(azimuth, float))
    if (el.ndim != 1 or el.shape != az.shape or not np.all(np.isfinite(el))
            or not np.all(np.isfinite(az)) or np.any(np.abs(el) > math.pi / 2)):
        raise OpticsError("OPTICS_INPUT_INVALID", "Sun angles must be finite matching 1D arrays; elevation in [-pi/2,pi/2].")
    return el, az


def _extinction(kappa: np.ndarray, shape: tuple[int, ...] | None = None) -> np.ndarray:
    k = np.atleast_1d(np.asarray(kappa, float))
    if not np.all(np.isfinite(k)) or np.any(k < 0):
        raise OpticsError("OPTICS_INPUT_INVALID", "Extinction must be finite and nonnegative in m^-1.")
    try:
        return k if shape is None else np.broadcast_to(k, shape)
    except ValueError as exc:
        raise OpticsError("OPTICS_INPUT_INVALID", "Extinction must be scalar or match sun arrays.") from exc


def build_layout(*, diameter: float, tubes_across: int, layers: int, pitch_h: float, pitch_v: float,
                 axis_azimuth: float, centre_height: float, n_wall: float, n_water: float,
                 lower_albedo: float, stagger: float = 0.0, tube_count: int | None = None) -> Optics:
    """Build a validated module; lengths m, angles radians, lower_albedo for sea or ground."""
    return Optics(diameter, tubes_across, layers, pitch_h, pitch_v, stagger, axis_azimuth,
                  centre_height, n_wall, n_water, lower_albedo, tube_count)


def array_model(optics: Optics) -> ArrayModel:
    """Construct a run-owned array model; its hemisphere cache stays local to the caller."""
    return ArrayModel(optics)


def sun_position(epochs: np.ndarray, site: dict[str, Any], conditions: dict[str, float]) -> tuple[np.ndarray, np.ndarray]:
    """Apparent solar elevation and azimuth in radians at UTC epoch midpoints."""
    import pandas as pd
    import pvlib

    index = pd.DatetimeIndex(pd.to_datetime(np.asarray(epochs, dtype=np.float64), unit="s", utc=True))
    frame = pvlib.solarposition.spa_python(
        index, float(site["latitude"]), float(site["longitude"]), altitude=float(site["elevation_m"]),
        pressure=float(conditions["pressure_pa"]), temperature=float(conditions["temperature_c"]),
        delta_t=float(conditions["delta_t"]),
    )
    return (np.deg2rad(frame["apparent_elevation"].to_numpy(dtype=np.float64)),
            np.deg2rad(frame["azimuth"].to_numpy(dtype=np.float64)))


def build_handoff(optics: Optics, epochs_mid: np.ndarray, site: dict[str, Any], conditions: dict[str, float],
                  dni_par: np.ndarray, dhi_par: np.ndarray) -> dict[str, np.ndarray]:
    """Freeze interval optics for the dynamic engine without sharing a global kernel cache."""
    elevation, azimuth = sun_position(epochs_mid, site, conditions)
    values = array_model(optics).evaluate(elevation, azimuth, dni_par, dhi_par)
    return {"elevation_rad": elevation, "azimuth_rad": azimuth, "i0_eq": values["i0_eq"],
            "diffuse_fraction": np.clip(values["diffuse_share"], 0.0, 1.0),
            "intercepted": values["intercepted"], "beam_unshaded_fraction": values["beam_unshaded_fraction"]}
