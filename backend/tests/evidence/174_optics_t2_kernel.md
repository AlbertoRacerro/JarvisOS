# Spec 174: optics kernel measurements

Measurements below concern the pure kernel; they do not by themselves establish engine integration, browser behavior, or biological qualification. Integration is checked by `test_optics_t2_dynamic_174.py`, and the same-origin Chromium run is recorded separately under `/home/user/jarvis-control/work/evidence/174/`. `cylinder.py` is used only as an independent T1 field reference in the evidence calculation, not imported by the kernel.

## Reproduction inputs and units

Q1 source and raw results were present and read at `/home/user/jarvis-control/work/evidence/pbr/probes/probe_q1_pvlib.py` and `q1_raw.txt`. Inputs: Cádiz (36.5° N, 6.3° W), altitude 0 m; 2026-06-21 in fixed UTC+2 and 2026-12-21 in fixed UTC+1; 144 samples per day, every 10 minutes starting at local midnight; NREL SPA (`pvlib.solarposition.spa_python`, temperature 12 °C), apparent elevation; packaged offline Linke turbidity and Ineichen clear sky. Diameter 0.060 m, row pitch 0.090 m. No wall loss or refraction (`n_wall = n_water = 1`), lower albedo zero.

For the Q1 comparison only, input irradiance is kept in Q1's W/m²; the linear kernel then returns W/m and the 1/6 h rectangular sum gives Wh/m. Normal product usage supplies PAR in µmol m⁻² s⁻¹ and gets µmol s⁻¹ m⁻¹. No PAR-to-energy conversion is assumed. All kernel angles are radians.

The spec 170 qualification README stores raw environment-dependent evidence outside the repository. This kernel-only report follows the user's explicit `backend/tests/evidence/174_optics_t2_kernel.md` location and spec 174's request for evidence next to the test.

## Analytic interpretation

Acceptance 1's closed-form Fresnel value applies at a surface-normal ray: an air-to-index-1.5 interface transmits 0.96. A cylinder illuminated perpendicular to its axis has varying local surface incidence across its silhouette. Its integrated transmission is therefore the Fresnel-weighted width, not diameter multiplied by the surface-normal value. Tests check the closed-form interface/slab values separately and integrate the actual curved-surface incidence prescribed by Decision 4.

## Q1 isolated reproduction and single-row comparison

| Season | Axis | Q1 raw isolated Wh/m | Kernel isolated Wh/m | Difference |
|---|---|---:|---:|---:|
| summer | N-S | 739.07 | 738.83719 | -0.03150% |
| summer | E-W | 582.34 | 582.11184 | -0.03918% |
| winter | N-S | 293.87 | 293.81766 | -0.01781% |
| winter | E-W | 389.71 | 389.65487 | -0.01415% |

All four also meet the requested 0.5% tolerance against rounded 739/582/294/390 Wh/m references.

Single physical row: eight tubes, no stagger, module mean per tube. Q1 instead uses an infinite periodic-row rule.

| Season | Axis | Q1 row Wh/m | Kernel 8-tube row Wh/m | Difference |
|---|---|---:|---:|---:|
| summer | N-S | 632.76 | 645.59048 | +2.028% |
| summer | E-W | 551.27 | 554.69798 | +0.622% |
| winter | N-S | 220.39 | 229.46699 | +4.119% |
| winter | E-W | 255.62 | 272.26793 | +6.513% |

The finite module has an unshadowed upstream edge tube; Q1 assigns the periodic interior shade fraction to every tube. Kernel sky interception / isolated sky interception is 0.80630207 for either axis and solstice; Q1 reports 0.7814. Independently, integrating the periodic single-row `min(D, p*cos(psi))` rule gives 0.77860806, and a finite eight-tube row gives `(1 + 7*0.77860806)/8 = 0.80628205`. The kernel differs from this analytic finite-row sky ratio by 0.0025% relative. Q1's approximate angular rule (`linspace(0.5, 89.5, 180)` with nominal 0.5° weights, plus azimuth sampling) explains its remaining diffuse discrepancy and the small isolated differences. Array agreement is diagnostic, not a pass criterion.

## Pitch and layer sweep

Same Q1 meteorology; eight tubes across; aligned layers; `pitch_h = pitch_v`; zero stagger, lowest centre 1 m above the reflecting plane; no wall or lower reflection. Values are daily intercepted energy in Wh/m **per tube**, averaged over the representative module. Module totals equal these values times `8*layers`; these are not footprint-normalized energies.

| Season | Axis | Layers | 60 mm pitch | 90 mm pitch | 120 mm pitch | 180 mm pitch |
|---|---|---:|---:|---:|---:|---:|
| summer | N-S | 1 | 546.097 | 645.590 | 682.328 | 711.329 |
| summer | N-S | 2 | 300.469 | 409.743 | 481.063 | 563.062 |
| summer | N-S | 3 | 218.594 | 306.049 | 377.390 | 480.426 |
| summer | E-W | 1 | 526.506 | 554.698 | 562.934 | 570.218 |
| summer | E-W | 2 | 274.140 | 348.150 | 382.204 | 434.649 |
| summer | E-W | 3 | 190.018 | 261.425 | 311.579 | 380.774 |
| winter | N-S | 1 | 188.638 | 229.467 | 249.336 | 268.722 |
| winter | N-S | 2 | 107.093 | 146.743 | 174.722 | 210.174 |
| winter | N-S | 3 | 79.912 | 112.033 | 139.042 | 179.869 |
| winter | E-W | 1 | 200.618 | 272.268 | 341.819 | 375.774 |
| winter | E-W | 2 | 121.711 | 169.753 | 216.516 | 272.401 |
| winter | E-W | 3 | 95.409 | 134.746 | 173.523 | 228.379 |

The measured cases show increased per-tube capture with wider pitch and reduced per-tube capture as aligned layers are added. Opaque upstream tubes ignore transmitted light reaching downstream culture; the sweep describes that declared conservative model.

## Midpoint-sun interval bias

The table compares 5-, 15- and 60-minute midpoint solar/clear-sky samples on the Cádiz 2026 solstices. It uses an eight-across, three-layer module with 60 mm tubes, 90 mm pitch, no wall loss or lower reflection, and reports module-mean intercepted Wh per metre per tube. Each interval's DNI/DHI comes from pvlib Ineichen at that same midpoint. The 5-minute result is the comparison baseline; this is a numerical discretization check, not a comparison with measured weather.

| Season | Axis | 5 min (Wh/m) | 15 min (Wh/m; bias) | 60 min (Wh/m; bias) |
| --- | --- | ---: | ---: | ---: |
| Summer | N–S | 306.029 | 306.056; +0.009% | 303.472; −0.836% |
| Summer | E–W | 261.425 | 261.465; +0.016% | 261.583; +0.061% |
| Winter | N–S | 111.996 | 111.926; −0.063% | 110.693; −1.164% |
| Winter | E–W | 134.741 | 134.743; +0.002% | 134.917; +0.131% |

The 15-minute cap in `OPTICS_RESOLUTION_MAX_MIN` corresponds to a maximum observed daily integration bias of 0.063% on these cases; the 60-minute option reaches 1.164%. The calculation was run from `/tmp/jarvis174_bias.py`. Its timezone-aware timestamps were explicitly converted to nanoseconds before epoch-second conversion (`as_unit('ns')`); pandas on this host stores the original index in microseconds, so dividing its raw integer representation by 1e9 would silently produce a false 1970 solar date.

## Refracted absorption versus the T1 field shape

Diameter 0.060 m; isolated tube; axis north–south; `n_wall=1.5`, `n_water=1.333`, lower albedo zero. Cases: normal beam (zenith, DNI=1000, DHI=0); sky only (DNI=0, DHI=250); oblique mixed (elevation 30°, azimuth 45°, DNI=800, DHI=100). Irradiances are PAR in µmol m⁻² s⁻¹. `tau=kappa*D`.

T2 is the directly integrated refracted-chord absorption. The T1 surrogate uses the same kernel `I0_eq` and `f_d`, with `A_T1 = kappa*pi*R²*cylinder.response_average_tau(identity, I0_eq, tau, f_d)`. Residual is `(A_T1 - A_T2)/A_T2`; positive means the T1 field shape predicts more absorption. This comparison is not an engine hand-off test.

| Case | I0_eq (µmol m⁻² s⁻¹) | f_d |
|---|---:|---:|
| normal beam | 1066.17065961 | 0.00000000 |
| sky only | 452.56621775 | 1.00000000 |
| oblique mixed | 943.40001752 | 0.19188731 |

| tau | Normal T2 absorption | Normal residual | Sky T2 absorption | Sky residual | Mixed T2 absorption | Mixed residual |
|---:|---:|---:|---:|---:|---:|---:|
| 0.0001 | 0.005023982 | +0.000312% | 0.0021325592 | -0.001560% | 0.0044454361 | +0.000505% |
| 0.001 | 0.050219227 | +0.003116% | 0.0213158 | -0.015535% | 0.044433509 | +0.005064% |
| 0.01 | 0.50013921 | +0.031153% | 0.21218212 | -0.150496% | 0.44225708 | +0.051516% |
| 0.1 | 4.8021804 | +0.311046% | 2.0275046 | -1.252012% | 4.2217992 | +0.558190% |
| 1 | 33.005871 | +3.022062% | 13.400981 | -5.158403% | 27.63792 | +6.369159% |
| 3 | 51.843741 | +7.911422% | 20.16737 | -4.327480% | 40.987224 | +16.431361% |
| 10 | 55.720888 | +13.614333% | 21.324359 | -0.761644% | 43.008056 | +24.936766% |
| 100 | 55.732136 | +14.770122% | 21.326682 | -0.007506% | 43.010461 | +26.173651% |
| 1000 | 55.732136 | +14.781488% | 21.326682 | -0.000080% | 43.010461 | +26.185656% |

Absorption columns are µmol s⁻¹ m⁻¹. The largest absolute residual on this declared grid is **26.1857%**, in the oblique mixed case at tau=1000. This is a measured limitation of preserving the T1 field shape, not a numerical integration error. A universal 15% bound would not be supported. The observed maximum absolute residual on this grid is the frozen surrogate comparison for this implementation. The 15-minute profile-resolution cap is justified by the midpoint bias table above. The refracted kernel absorption remains bounded by transmitted light; the surrogate is not asserted to obey that bound.

## Timings and validation

On this host, the current integration hand-off was timed over 34,560 five-minute intervals (120 days). A deterministic performance input supplied 1,000 µmol m⁻² s⁻¹ DNI and 100 µmol m⁻² s⁻¹ DHI at every interval; this deliberately isolates computation cost and is **not** a physical weather series or a biological result. The site was Cádiz (36.5° N, 6.3° W, 0 m), SPA conditions were 101325 Pa, 12 °C and ΔT 67 s; 60 mm tubes, 90 mm pitch, 0.1 m lower centre, PMMA index 1.5, water index 1.333 and lower albedo 0.06. The run included solar-position calculation and both hemisphere integrals.

| Module | Intervals | Wall time | Finite equivalent irradiance |
| --- | ---: | ---: | --- |
| 8 × 3 (24 tubes) | 34,560 | 2.800 s | yes |
| 12 × 4 (48 tubes) | 34,560 | 5.513 s | yes |

Measured with `PYTHONPATH=backend backend/.venv/bin/python /tmp/jarvis174_perf.py` after filtering night intervals before beam geometry. The pre-filter run took 3.858 s and 6.304 s respectively on the same host. These timings meet the spec's 3 s and 10 s prepare budgets for 24 and 48 tubes; they are host measurements, not cross-platform guarantees. The dynamic right-hand side reads prepared arrays and does not call the kernel.

The output-series absorption calculation groups rays that share the same exact `(tube, refracted chord)` pair and sums their weights once per run. On the 24-tube case, this reduced each hemisphere from 2,291,280 ray entries to 88,062 sky and 94,478 lower entries. A four-direction, four-extinction comparison against the ungrouped kernel changed absorbed PAR by at most `3.55e-14` µmol s⁻¹ m⁻¹ (relative `2.05e-14`); all other output differences were roundoff. On this host, 97 output rows took 3.558 s versus 7.534 s ungrouped. A 30-day, 15-minute output (2,881 rows) took 9.924 s with grouping. These measurements use the declared `/tmp/jarvis174_abs_perf24.py`, `/tmp/jarvis174_abs_perf24_month.py`, and `/tmp/jarvis174_aggregate_compare.py` inputs; the month benchmark uses a deterministic synthetic irradiance/extinction sweep, not a measured weather or growth outcome.

Measurements were generated with `/home/user/src/JarvisOS/backend/.venv/bin/python`, pvlib 0.16.1, from the worktree backend directory. The numerical sweep script and raw JSON for this run are `/tmp/174_kernel_evidence.py` and `/tmp/174_kernel_evidence.json`; the input definitions and results needed to reproduce the calculation are recorded above. Quadrature used 10 Gauss–Legendre nodes per graded silhouette panel and fixed hemisphere quadrature with 12 axial-angle nodes and 36×4 in-plane nodes. Diffuse ray geometry is prepared on each caller-owned model instance and reused; there is no process-global cache.
