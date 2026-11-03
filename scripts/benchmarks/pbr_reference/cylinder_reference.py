"""Independent cylinder optics reference for spec 170.

This deliberately uses a reduced integral derived by exchanging the optical-path
and elevation integrals, rather than the production area/direction quadrature.
The nonlinear reference independently integrates the original area/direction/
elevation equations on a much denser rule than the production kernel. Its area
map is a simple cubic wall-distance map and its angular rules are ungraded.
It supplies reference values for the exact-head 170 optics acceptance.

At I₀=800 µmol/(m²·s), K_I=150 µmol/(m²·s), f_d=0.5, doubling the
independent rule from (160 radial, 256 area-angle, 160 direction, 96 elevation)
to (240, 384, 240, 128) changes the reference response by 2.70e-10 at τ=1,
3.17e-7 at τ=100 and 3.52e-6 at τ=1000, relative. Production differs from
the lower rule by 3.51e-10, 5.06e-7 and 5.58e-6 respectively. These measured
errors are below the accepted 1e-4 relative response bound, including τ=1000.
"""

from __future__ import annotations

import json
import math

from scipy.integrate import quad


def nonlinear_reference(tau: float, surface_par: float, diffuse_fraction: float,
                        saturation: float, *, radial_nodes: int = 160,
                        angle_nodes: int = 256, direction_nodes: int = 160,
                        elevation_nodes: int = 96) -> float:
    """Direct 3-D field and polar-area Monod response, with independent dense quadrature."""
    import numpy as np
    from numpy.polynomial.legendre import leggauss

    if tau == 0:
        return surface_par / (saturation + surface_par)

    def gauss_interval(count: int, start: float, stop: float) -> tuple[np.ndarray, np.ndarray]:
        node, weight = leggauss(count)
        return start + (stop - start) * (node + 1.0) / 2.0, (stop - start) * weight / 2.0

    radial_u, radial_w = gauss_interval(radial_nodes, 0.0, 1.0)
    wall_distance = radial_u**3
    radius = 1.0 - wall_distance
    area_ring_w = 6.0 * radius * radial_u**2 * radial_w
    phi, phi_w = gauss_interval(angle_nodes, -math.pi / 2, math.pi / 2)
    x = radius[:, None] * np.cos(phi)[None, :]
    y = radius[:, None] * np.sin(phi)[None, :]
    beam_path = y + np.sqrt(np.maximum(0.0, 1.0 - x * x))
    beam = np.exp(-0.5 * tau * beam_path)

    theta, theta_w = gauss_interval(direction_nodes, 0.0, math.pi)
    projection = radius[:, None] * np.cos(theta)[None, :]
    path = projection + np.sqrt(np.maximum(0.0, 1.0 - radius[:, None] ** 2
                                            * np.sin(theta)[None, :] ** 2))
    elevation, elevation_w = gauss_interval(elevation_nodes, 0.0, math.pi / 2)
    cosine = np.cos(elevation)
    ki2 = np.sum(elevation_w[None, None, :] * cosine[None, None, :]
                 * np.exp(-0.5 * tau * path[:, :, None] / cosine[None, None, :]), axis=2)
    diffuse = np.sum(ki2 * theta_w[None, :], axis=1) / math.pi
    light = surface_par * ((1.0 - diffuse_fraction) * beam + diffuse_fraction * diffuse[:, None])
    factor = light / (saturation + light)
    return float(np.sum(area_ring_w[:, None] * phi_w[None, :] * factor) / math.pi)


def reference(tau: float) -> dict[str, float]:
    if tau == 0:
        return {"tau": tau, "beam": 1.0, "diffuse_3d": 1.0}

    def beam(theta: float) -> float:
        c = math.cos(theta)
        return c * -math.expm1(-tau * c)

    def diffuse(theta: float) -> float:
        c = math.cos(theta)

        def elevation(phi: float) -> float:
            z = math.cos(phi)
            if z == 0:
                return 0.0
            return z * z * -math.expm1(-tau * c / z)

        return c * quad(elevation, 0, math.pi / 2, epsabs=2e-13, epsrel=2e-12)[0]

    factor = 4 / (math.pi * tau)
    return {
        "tau": tau,
        "beam": factor * quad(beam, 0, math.pi / 2, epsabs=2e-13, epsrel=2e-12)[0],
        "diffuse_3d": factor * quad(diffuse, 0, math.pi / 2, epsabs=2e-13, epsrel=2e-12)[0],
    }


if __name__ == "__main__":
    print(json.dumps([reference(tau) for tau in (0, 1e-6, 0.01, 0.1, 1, 10, 100, 316, 1000)], indent=2))
