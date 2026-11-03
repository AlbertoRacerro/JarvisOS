# Spec 216 CFD pipe qualification — in progress

This is a measured checkpoint for the first typed smooth-tube cases. It is **not**
the spec 216 acceptance run or a qualified reduced model. All solver outputs
below were generated outside the repository by pinned OpenFOAM v2412 tools.

## Reproduction

The case is the BlueCad tube template with outer diameter 14 mm, wall 2 mm,
length 200 mm, so its bore is 10 mm. Water-like properties are 998.2 kg/m³ and
1.002 mPa·s. Mean inlet speed is 0.01 m/s (Re 99.6208). The steady laminar
`simpleFoam` case runs 300 iterations with residual target 1e-6. The generated
quarter-pipe O-grid has two symmetry planes. Pressure is sampled at 0.65 L and
0.85 L at the same radius, and velocity at three radii at 0.85 L. `p` is
kinematic pressure; the comparison multiplies by density. The analytic
Hagen–Poiseuille gradient is 3.2064 Pa/m. The source fixture files in
`backend/tests/fixtures/cfd_pipe/laminar_probe_{p,U}.txt` preserve the values
from the level 0 real run; trailing spaces were removed for repository checks.

Use the operator registry built by `scripts/qualification/216/operator_registry.py`
and `scripts/qualification/216/run_case.py --contract <JSON> --registry <YAML>
--out <external directory> [--processors N]` to repeat one typed case. The command writes a
manifest, registry and tool-binary SHA-256 values, mesh check, full logs,
residual verdict and analytic error to that external directory. It reports
measurement without claiming numerical qualification or publication.

### Current result: casegen v2 with the pinned-build workaround (2026-10-24)

The defect identified in the last section below is now worked around in the
adapter, as spec 216 requirement 1 allows. Case generator
`cfd_pipe_casegen_v2` writes `cache { grad(U) yes; }` into `fvSolution`.
With a cached gradient, `fvc::grad(U)` returns a reference instead of a
reusable temporary. `T()` then allocates its result and transposes correctly.
That covers every `simpleFoam` stress path that calls `fvc::grad(U)`: laminar
`Stokes` and the RAS `linearViscousStress` family. This is a dictionary
entry, not compiled code. conda-forge has no other OpenFOAM release to move
the pin to (only two `openfoam-2412` builds). Probes now use `cellPoint`
interpolation. The default cell-centre value biased the radial profile
comparison by up to half a cell's velocity change: 2.1% at level 0 and 5.1%
at level 1 on the same fields.

Regression controls: `scripts/qualification/216/solver_controls.py` generates
two `blockMesh` cases from typed values. Both reuse the generated pipe
case's `fvSchemes`, `fvSolution` and transport dictionaries. It runs them with
the pinned tools. The tool-dependent test
`test_solver_controls_with_real_openfoam_detect_the_transpose_defect` runs
both controls. It also checks that the square duct **fails** when the cache
entry is removed.

| Control (pinned v2412) | Mesh | Measured | Analytic | Error | Tolerance |
| --- | --- | ---: | ---: | ---: | ---: |
| Square duct, four walls | 40×16×16 | 2.810362 Pa/m | 2.851091 Pa/m (f·Re 56.908) | −1.43% | 2% |
| Same settings without the cache entry (hand-written control above) | 40×16×16 | 3.483047 Pa/m | 2.851091 Pa/m | +22.2% | fails |
| Half channel, one wall | 80×16×16 | 1.200088 Pa/m | 1.2024 Pa/m | −0.19% | 2% |

The −1.4% duct residual equals the `icoFoam` control on the same mesh. It is
discretisation error at 16 cells per side, not the defect.

Typed laminar reruns, from `run_case.py`, with the same contract as below
(Re 99.62, 300 iterations):

| Mesh level | Cells | Measured ΔP/L (Pa/m) | Error vs HP 3.2064 | Profile max error / mean speed | Case manifest digest |
| --- | ---: | ---: | ---: | ---: | --- |
| 0 | 15,360 | 3.220777 | +0.448% | 0.797% | `sha256:39629a90b242538e72abe886e46d36db242b73ed5c46475643aedabdff3fa577` |
| 1 | 51,840 | 3.215386 | +0.280% | 0.400% | `sha256:aa3b56243979ef3c18b736e1185cfcc52b2fe13d2e12997788984849e52b3ad2` |
| 2 | 174,960 | 3.218273 | +0.370% | 0.552% | `sha256:d3977c14b5470e7c3f93407f6b78272fae7f33d8468a279987f303fee35c7eeb` |

Levels 0 and 1 met the 1e-6 residual target. Level 2 did **not**: its final
initial residuals were Ux 9.4e-6 and p 4.0e-7 after 300 iterations
(1230 s). Its value is shown for completeness only. It must not be used for
an observed order or GCI until it is rerun with a larger iteration budget.
All three meshes pass `checkMesh` with zero failed checks. The pressure error
is now within 0.5% on every mesh, against +25% before. No mesh-sensitivity
uncertainty is claimed yet.

The level 0 probe output is preserved in
`backend/tests/fixtures/cfd_pipe/laminar_cached_grad_probe_{p,U}.txt`.

One typed turbulent case ran at level 0: the C1 condition (D = 46 mm,
L = 2 m, v = 0.45 m/s, Re 20,621). It used k-ω SST, low-Re resolved walls,
estimated y⁺ 0.50 and 121,800 cells, with `checkMesh` accepted. It ran 1500
iterations with every residual below the 1e-5 target. Manifest:
`sha256:c4609aeeb56ee558fff1dbf586e9fd9a7ac5aa3be2826d7f94aad88b05960d85`.
The probe gradient between 0.65 L and 0.85 L, density-scaled, is
54.454 Pa/m. That is −3.53% against smooth-pipe Colebrook (56.444 Pa/m) and
−6.01% against Blasius (57.938 Pa/m). This is a single mesh. Its first
pressure station, at 1.3 m, lies before the contract's 1.59 m development
allowance, so it is a measurement and not yet the turbulent qualification.
These numbers were computed by hand from the probe file; the runner now
refuses this probe set (next section). The external run directories are
`~/jarvis-control/work/evidence/216-solver-controls/c81-typed/v2/` and
`c81-controls-v2/`.

### Runner-computed turbulent comparison in the developed section (2026-10-24)

`run_case.py` now computes the turbulent comparison itself
(`probe_results.turbulent_comparison`). It reports the density-scaled probe
gradient, the Darcy factor, smooth-pipe Colebrook and Blasius gradients, and
signed errors (measured minus reference, over reference). Both the laminar and
turbulent comparisons now **refuse** a probe set whose upstream pressure station
lies before the contract's development length. The 2 m run above is refused
(1.3 m station, 1.59 m allowance); its probes are kept as the refusal fixture
`backend/tests/fixtures/cfd_pipe/turbulent_sst_2m_probe_{p,U}.txt`.

The same C1 contract was rerun with a 3 m tube, so the stations are at 1.95 m
and 2.55 m, both past the allowance. Everything else is unchanged: k-ω SST,
low-Re resolved walls, level 0, estimated y⁺ 0.50, 182,700 cells, `checkMesh`
accepted with zero failed checks, 1500 iterations, final initial residuals
≤ 1.0e-7, 1030 s.

| Quantity | Value |
| --- | ---: |
| Re | 20,621 |
| Measured ΔP/L | 55.530 Pa/m |
| Darcy f (measured) | 0.02527 |
| Colebrook, smooth | 56.444 Pa/m |
| Blasius | 57.938 Pa/m |
| Signed error vs Colebrook | −1.62% |
| Signed error vs Blasius | −4.16% |

Case manifest
`sha256:d44d76d4d959b1f0d2fbabe7c72b8c8964453ce999f17e573a3b3e24fc81f42e`,
contract `sha256:87bba56544ffa9671097661acf23773fc1f92f1518750ffa2252e1d277956458`.
Probes are preserved in
`backend/tests/fixtures/cfd_pipe/turbulent_sst_3m_probe_{p,U}.txt`; the run
directory is `~/jarvis-control/work/evidence/216-solver-controls/c82-turb/`.
Moving the stations downstream changed the error against Colebrook from
−3.5% to −1.6%, which is consistent with the 2 m stations including
development effects. The value is still from **one mesh**. Colebrook and Blasius
differ by 2.6% from each other at this Re, so the result is within correlation
scatter, but no turbulent tolerance has been stated or met yet.
Model limitation: k-ω SST with a resolved low-Re wall (first cell y⁺ ≈ 0.5)
on a smooth wall. Wall functions, roughness and transition are not tested.

### Mesh-study tooling (2026-10-24)

`cfd_pipe/mesh_study.py` computes a three-mesh observed order, Richardson
extrapolation and fine-mesh GCI (safety factor 1.25) with a constant
refinement ratio, following Celik et al. (2008). The generated mesh levels
refine by r = 1.5 (cell ratio 3.375). Oscillatory or divergent triplets get no
order and no GCI. Offline tests recover p = 2 from a manufactured
second-order sequence. The current laminar levels 0/1/2
(3.220777, 3.215386, 3.218273 Pa/m) are **oscillatory**. Level 2 is also not
residual-converged, so no GCI is reported for them.

### Laminar three-mesh study, residual-converged (2026-10-24)

The solver timeout is now a typed contract value (`SolverControls.wall_time_limit_s`).
The same laminar contract was rerun on all three levels with 600 iterations and a
4800 s bound. Each level met the 1e-6 residual target (final initial residuals
≤ 3.2e-8), and `checkMesh` accepted every mesh with zero failed checks. The run
directories are `~/jarvis-control/work/evidence/216-solver-controls/c83-lam/`.

| Mesh level | Cells | Measured ΔP/L (Pa/m) | Error vs HP 3.2064 | Profile max error / mean speed | Solver time | Case manifest digest |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 15,360 | 3.220778 | +0.448% | 0.797% | 14 s | `sha256:9e01104677c1a7503aaee59a06da9f079cc7b8951057cc29a625da6413e665ed` |
| 1 | 51,840 | 3.215367 | +0.280% | 0.397% | 128 s | `sha256:99d485b14c22879543c6d459d6edcbab28d794e1146f8cfedff043328a62723b` |
| 2 | 174,960 | 3.212181 | +0.180% | 0.188% | 1540 s | `sha256:741cb01af5fd98f3de6119b6aa3116398c68d1d8758a53a19116db18006cd4da` |

Levels 0 and 1 reproduce their 300-iteration values to 2e-5 Pa/m. Level 2
moved from the unconverged 3.218273 to 3.212181 Pa/m. That removes the earlier
oscillation. `mesh_study.three_mesh_study` with r = 1.5 now gives:

| Quantity | Value |
| --- | ---: |
| Convergence | monotonic |
| Observed order p | 1.31 |
| Richardson extrapolation | 3.207617 Pa/m (+0.038% vs HP) |
| Fine-mesh GCI | 0.178% (±0.0057 Pa/m) |
| Fine-mesh error vs HP | +0.180% |

The analytic value lies 0.002% outside the fine-mesh GCI band, i.e. at its edge.
The observed order is below the formal second order of the schemes. Likely
contributors are the polygonal O-grid wall, whose area deficit shrinks with
refinement (0.16% at level 0), and `cellPoint` probe interpolation. Neither is
separated here. The profile error also converges monotonically (p = 1.61).
No laminar tolerance was stated before this comparison, so it is reported as a
measurement with uncertainty, not as a pass. Mass conservation was checked
above (imbalance ≤ 3.4e-8 of the prescribed rate).

### Turbulent three-mesh study: tolerances stated before the runs (2026-10-24)

These acceptance tolerances are fixed in this commit, before any of the runs
below exist. They are builder-stated for spec 216 contract 5d and are open to
maintainer review on PR #917. If a run misses them, that is reported as a miss.

- **Case.** The C1 condition: D = 46 mm, v = 0.45 m/s, water at 20 °C
  (Re 20,621), k-ω SST with a low-Re resolved wall (estimated y⁺ 0.50 on every
  level). Mesh levels 0, 1 and 2 have 158,200, 533,925 and 1,769,284 cells.
  Each runs 2000 iterations with residual target 1e-5. The tube is **2.6 m**,
  not 3 m: at 3 m, level 2 has 2,040,412 cells and is refused by the
  2,000,000-cell budget. The pressure stations at 0.65 L and 0.85 L are 1.69 m
  and 2.21 m, past the 1.59 m development allowance. The geometry digest is
  `sha256:21d7f06a289e564a0331246a3ca2a0c5e158267f8c152c906ef96fa1dfb0ece2`.
- **T1 (residuals).** Every level meets the 1e-5 residual target, and
  `checkMesh` accepts it.
- **T2 (mesh sensitivity).** The three pressure gradients converge monotonically,
  and the fine-mesh GCI is ≤ 3%. Radial rounding makes the refinement ratio
  non-constant (1.491 and 1.500), so the order is solved with Celik's
  non-constant-ratio form.
- **T3 (correlation).** The fine-mesh gradient is within ±10% of smooth-pipe
  Colebrook (56.444 Pa/m at this Re). The Blasius error is also reported, with
  no separate tolerance. ±10% is about the published scatter of RANS
  smooth-pipe friction predictions and the correlations' own ±5%. It is not a
  plant-accuracy claim.

Model limitation: k-ω SST with a resolved low-Re wall on a smooth wall. Wall
functions, roughness, transition and swirl are not tested.

### MPI-parallel execution (2026-10-24)

Serial level 2 was projected to exceed its 24 h wall-time bound: level 1 alone
took 13,985 s serially. `run_case.py --processors N` now runs
`decomposePar`, then `simpleFoam -parallel` under the pinned MPICH `mpiexec`,
then `reconstructPar -latestTime`. All three are hash-pinned registry tools.
The case generator writes a typed scotch `decomposeParDict` and records
`processors` in the manifest only when N > 1. Serial cases and manifests are
unchanged.

The conda-forge `openfoam-2412` binaries carry RPATH `lib/sys-mpich`, which
the package does not ship. They therefore load `lib/dummy/libPstream.so`,
which aborts under MPI ("The dummy Pstream library cannot be used in parallel
mode"). As spec 216 requirement 1 allows, the adapter works around this by
preloading the shipped `lib/mpich-3.3/libPstream.so` into the ranks only
(`mpiexec -genv LD_PRELOAD`). The report records that library's SHA-256.

The parallel path reproduces the serial result on both regimes:

| Case | Serial | MPI | Relative difference |
| --- | ---: | ---: | ---: |
| Laminar level 0, 600 iterations, 4 ranks | 3.2207776 Pa/m | 3.2207775 Pa/m | 1e-8 |
| Turbulent C1 level 1, 2000 iterations, 6 ranks | 55.88613 Pa/m | 55.88525 Pa/m | 1.6e-5 |

### Turbulent three-mesh study: result against T1–T3 (2026-10-24)

The study ran with the contracts and tolerances fixed at `4f2ccf87`. Levels 0
and 1 ran serially at that runner. Level 2 ran on 16 MPI ranks at `c73b0332`.
The run directories are `~/jarvis-control/work/evidence/216-solver-controls/c84-turb/`
(`run-l0`, `run-l1`, `run-l2-np16`, and the cross-check `run-l1-np6`).

| Mesh level | Cells | ΔP/L (Pa/m) | vs Colebrook 56.444 | vs Blasius 57.938 | Max final initial residual | Solver time | Case manifest digest |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 158,200 | 55.643 | −1.42% | −3.96% | 9.7e-8 | 968 s | `sha256:0f692a4e8bfbc1e74d1d12ccad69cf7c1d8905c22f0a102a6d15fa267c903e1c` |
| 1 | 533,925 | 55.886 | −0.99% | −3.54% | 1.0e-7 | 13,985 s | `sha256:e3cfb85094128c5c17ba35d23de6ec932fe22d5e982795ee5964c4b9cd125fe5` |
| 2 | 1,769,284 | 55.991 | −0.80% | −3.36% | 5.5e-6 (Uy, Uz) | 11,796 s (16 ranks) | `sha256:1eba33fb686875c6aa65b4447332736942e0e85ab3a146ac699945e577fb01fd` |

Mesh study (Celik, r21 = 1.491, r32 = 1.500): **monotonic**, observed order
p = 2.05, Richardson extrapolation 56.074 Pa/m (−0.66% vs Colebrook), fine-mesh
GCI **0.185%** (±0.10 Pa/m).

| Tolerance | Required | Measured | Verdict |
| --- | --- | --- | --- |
| T1 residuals and mesh | every level ≤ 1e-5, `checkMesh` accepted | max 5.5e-6; all accepted, zero failed checks | **met** |
| T2 mesh sensitivity | monotonic, fine GCI ≤ 3% | monotonic, 0.185% | **met** |
| T3 correlation | fine mesh within ±10% of Colebrook | −0.80% | **met** |

Limits of this result: level 2 met the residual target, but its transverse
velocity residuals (5.5e-6) were about 50 times those of levels 0 and 1. The
probes record only the final iteration, so the iterative error is not
quantified separately from the GCI. All three levels under-predict both
correlations and approach Colebrook from below. The Colebrook–Blasius spread
(2.6%) is larger than the mesh uncertainty. The single-condition result
supports SST with a resolved wall at Re ≈ 2×10⁴ only. It is not a range
qualification.

**Still open before publication:** the reduced model needs more than one
Reynolds number for a validated range. Then the rest of spec 216: live
residual console, viewport, publication and Process pinning. No reduced
model is published.

### Reduced-model fitting and multi-Re tolerances stated before the runs (2026-10-24)

`backend/app/modules/bluecad/cfd_pipe/reduced_model.py` fits, guards, versions,
evaluates and ages the reduced model (offline tests in
`backend/tests/test_216_reduced_model.py`). Each regime segment fits a Darcy
factor `f = a Re^b` by least squares in log space. It uses measured,
residual-converged runs of **one** tube geometry and **one** study template
(the contract without inlet, mesh level and run budget). The segment's
relative uncertainty is the linear sum of three terms:

- discretization: `max(GCI_fine, 1.25 |f_point_level − f_extrapolated| / f_extrapolated)`
  from the segment's three-mesh study. Points run on a coarser level than the
  study's fine mesh carry their distance from the Richardson value;
- fit: the largest relative residual of the power law;
- correlation: the largest deviation from Hagen–Poiseuille (laminar) or smooth
  Colebrook (turbulent).

Publication refuses runs that are unmeasured or unconverged, runs that span
more than one tube geometry, segments with fewer than two Reynolds numbers or
without a monotonic mesh study, and points outside the stated tolerance. The
version id is the SHA-256 of the canonical model content, and tampering is
detected. Outside the validated Re range (including the transitional gap),
or at another diameter or another fluid, evaluation refuses, or with `warn`
returns the number with the violation and no uncertainty. Obsolescence
reports a changed geometry digest or a changed study template. Nothing is
published to Studio or pinned in Process yet.

Multi-Re points for the range, with tolerances fixed in this commit before any
result was read (also `~/jarvis-control/work/evidence/216-solver-controls/c85-multire/TOLERANCES.txt`):

- **M1 (completion).** Every run is `measured`, residual-converged and
  `checkMesh`-accepted.
- **M2 (laminar).** Re 100, 500 and 1000 on a 10 mm bore, 1.0 m tube, level 1:
  ΔP/L within ±2% of Hagen–Poiseuille and a profile error ≤ 2% of the mean
  velocity.
- **M3 (turbulent).** Re 1×10⁴ and 4×10⁴ on the C1 tube (46 mm, 2.6 m),
  SST with a resolved wall, level 1, 8 MPI ranks each: ΔP/L within ±10% of
  smooth Colebrook (the T3 band). The level-1 discretization uncertainty comes
  from the Re ≈ 2×10⁴ three-mesh study and is not re-derived at each Re. The
  reduced model states this as an assumption.

Misses are reported, not retuned.

### Multi-Re points and candidate reduced models: result against M1–M3 (2026-10-24)

The first set ran at `f4a498ee`. Re 1000 (laminar) and Re 4×10⁴ (turbulent)
solved and met their residual targets, but the runner's probe guard refused
both after solving. Their upstream pressure station (0.65 L: 0.65 m and 1.69 m)
lay before the development length (0.90 m and 1.775 m). The contract had
accepted them because it compared the development length with the whole tube.
`ff79f48a` makes the contract refuse such a tube before a run, so both
contracts are now refused at validation. Replacement points on the same
tubes, Re 700 and Re 2.8×10⁴, were added to the tolerance record before they
ran. Re 700 and 2.8×10⁴ ran on 8 MPI ranks; the other laminar runs were serial.
Evidence: `~/jarvis-control/work/evidence/216-solver-controls/c85-multire/`.

| Run | Re | Cells | ΔP/L (Pa/m) | Reference (Pa/m) | Error | Profile error / U | Max final initial residual | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| laminar, 10 mm × 1.0 m, L1 | 100 | 259,200 | 3.2274 | 3.2186 (H–P) | +0.27% | 0.40% | 2.2e-8 | M1, M2 met |
| laminar | 500 | 259,200 | 16.138 | 16.093 (H–P) | +0.28% | 0.40% | 9.0e-9 | M1, M2 met |
| laminar | 700 | 259,200 | 22.609 | 22.530 (H–P) | +0.35% | 0.42% | 3.4e-8 | M1, M2 met |
| laminar | 1000 | 259,200 | — | — | — | — | 9.1e-9 | M1 missed: probe guard refusal, replaced |
| SST low-Re, 46 mm × 2.6 m, L1 | 10,000 | 533,925 | 16.202 | 15.956 (Colebrook) | +1.54% | — | 1.0e-7 | M1, M3 met |
| SST (cycle 84 level 1) | 20,621 | 533,925 | 55.886 | 56.444 | −0.99% | — | 1.0e-7 | T1–T3 met |
| SST | 28,000 | 533,925 | 95.008 | 96.683 | −1.73% | — | 9.9e-8 | M1, M3 met |
| SST | 40,000 | 533,925 | — | — | — | — | 9.4e-8 | M1 missed: probe guard refusal, replaced |

`scripts/qualification/216/fit_reduced_model.py` fitted two **candidate**
models from these run reports. It reads each contract back from its case
manifest and checks it against the report's contract digest. Both files are
in the evidence directory. They are not published to Studio or pinned in
Process.

| Candidate | Re range | f = a Re^b | Discretization | Fit | Correlation | Combined | Version id |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `c1_tube.darcy.turbulent_sst` (46 mm, 2.6 m) | 1.0×10⁴–2.8×10⁴ | a = 0.4260, b = −0.2834 | 0.42% | 0.33% | 1.73% | **2.48%** | `sha256:5914b3555c2e10a3dff8ed6aff072b9105a87500a2db34ff9e97952130ec2121` |
| `d10_tube.darcy.laminar` (10 mm, 1.0 m) | 100–700 | a = 64.09, b = −0.9997 | 0.30% | 0.03% | 0.35% | **0.68%** | `sha256:5dacac23de0dbae57f8f5de52dd8c31e4dd1d95cc48186a25415185c120cb4e2` |

Limits:

- SST with a resolved wall gives a steeper Re dependence than both
  correlations (b = −0.283 against Blasius −0.25). Its deviation moves from
  +1.5% to −1.7% of Colebrook across the range. The correlation term dominates
  the uncertainty, and the model must not be extrapolated.
- The turbulent discretization term comes from the Re ≈ 2×10⁴ three-mesh study
  on the same tube. It is assumed to hold at 1×10⁴ and 2.8×10⁴.
- The laminar discretization term comes from the cycle 83 three-mesh study,
  which used a different tube (10 mm × 0.2 m, Re 100). The laminar candidate
  only demonstrates the laminar segment and Hagen–Poiseuille recovery. It is
  not a model of the C1 tube. On the C1 tube, fully developed laminar flow
  fits only below about Re 630.
- Re 2000–4000 is outside both candidates. Evaluation refuses it.

### Publication through the Studio published-version owner (2026-10-25)

`app/modules/bluecad/cfd_pipe/studio_publication.py` publishes a reduced model
as an ordinary Studio calculation (`studio_calc_v1`), so it reuses the Studio
version owner and attaches to the component Physics node and the 109/110
handoff. No new store is introduced. The operation is deterministic:

- The Studio source is generated only from the model's typed values. It
  carries the whole versioned model as a JSON constant, so the Studio source
  digest binds the fit, validity, uncertainty and provenance.
  `reduced_model_of_source` reads the model back. It refuses a source that
  does not regenerate exactly from the model it carries.
- Inputs are mean velocity, bore, density and viscosity. Bore and fluid
  bounds are pinned to the validated values within a 1e-9 relative band. The
  velocity bounds span the validated Re. The source itself refuses Re outside
  every segment, including the gap between the laminar and turbulent
  segments, and refuses a bore or fluid that differs from the validated one.
  Outputs are Re, Darcy factor, ΔP/L, wall shear and the combined relative
  uncertainty.
- The Studio test cases are the measured study points. Each must reproduce
  its run's Darcy factor within the segment's fit uncertainty. Studio's own
  guard (a passing report for the current source and contract) then decides
  publication. A model whose version identity does not verify is refused.
  So is a host where the Studio runtime is unavailable, and nothing is
  created in either case.
- The published version keeps Studio's `Scientifically unqualified` label.
  The provenance note in the model's assumptions states the range and says
  that the model is not a validated plant claim.

Evidence: both cycle 85 candidates were published into a throwaway data root,
and their Studio tests ran in the real bubblewrap runtime
(`~/jarvis-control/work/evidence/216-solver-controls/c86-studio-pub/`).

| Candidate | Studio version | Studio source SHA-256 | Test cases (Re) |
| --- | --- | --- | --- |
| `c1_tube.darcy.turbulent_sst` | v1 | `599a98ec…f4e72a24` | 1.0×10⁴, 2.06×10⁴, 2.8×10⁴ passed |
| `d10_tube.darcy.laminar` | v1 | `4da66a35…f1912c2` | 100, 500, 700 passed |

### Component reduced-model status (2026-10-25)

`GET …/components/{component_id}/reduced-models` (Design component owner) is a
read-only report on each Studio version pinned to the component that carries
a reduced model. It returns the pinned ids, the fidelity label "CFD reduced
model (scientifically unqualified)", the validity segments with their combined
uncertainty, and a status:

- `current`: the fluid-domain digest of the attached tube geometry equals the
  model's digest.
- `obsolete`: the digests differ, and the reasons say to rerun the study.
- `unknown`: there is no usable geometry, and the route says why.
- `invalid`: the source is missing or tampered, or the carried model fails
  its identity check.

Ordinary Studio calculations are skipped. The route never changes the pin.
Tests: `backend/tests/test_216_component_reduced_models.py`.

### Component publish-and-pin operation (2026-10-25)

`POST …/components/{component_id}/reduced-models` is the typed operation over
this publication (contract 9). It takes the model id, the study points, the
three-mesh evidence, the stated tolerances and the expected component revision
and active version. In order, it:

1. Refuses a stale component revision or active version (409).
2. Fits and guards the model on the server with `reduced_model.publish`. A
   refusal returns 422 with every reason, and nothing is created.
3. Requires the model's geometry digest to equal the fluid-domain digest of
   the component's attached tube. Unknown geometry or a different tube
   returns 409, and nothing is published.
4. Publishes through `publish_to_studio`, where Studio's tests and guard
   still decide. A refusal returns 422, and nothing is pinned.
5. Rechecks the revision, pins the Studio version on the Physics node and
   records a `DesignComponentReducedModelPublished` history event with the
   Studio version, reduced-model identity, geometry digest and run ids. If the
   component changed after publication, the route returns 409 and names the
   Studio version that was published but not pinned. Studio versions are
   append-only.

The response is the pinned entry's status from the read-only route, so it is
`current`. Manual `PUT …/models` pinning is unchanged.

**Limit:** until study execution through the evaluator/study owners exists
(contract 4), the study points are supplied by the caller. The route re-fits
and guards them, but it cannot prove that the runs took place. Provenance
records the caller's run ids and manifest digests as given.

**Still open before acceptance:** server-side study execution feeding this
operation, agent-tool exposure, switching the Process unit to the pinned model
and the GUI display of this status, the live residual console, the viewport
fields, and the browser walk.

### Process unit hydraulics pin (2026-10-25)

The Process draft op `set_unit_hydraulics` pins a published CFD reduced-model
Studio version on a `PhotobioreactorT1` unit (contract 8, Process side). The
pin records the Studio version, its source SHA-256, the reduced-model version
identity, the geometry digest, and an out-of-range mode, `refuse` (the
default) or `warn`. Clearing the pin returns the unit to the literature
correlation. Other unit types refuse the op.

A steady Process evaluation of a pinned unit reads the published source from
the Studio owner. It then requires the source digest and the carried model's
version identity to equal the pin. Any mismatch, missing version or tampered
source fails the unit with `PBR_HYDRAULICS_MODEL_UNAVAILABLE`, and the
correlation is never used silently in its place. The operating point uses the
same water properties as the correlation path. Outside the validity range:

- `refuse` reports no pressure drop or pumping power, and adds a warning
  finding and a caveat that list the violations;
- `warn` reports the numbers with a warning, a caveat and no uncertainty.

The unit result carries a `hydraulics_model` block. Unpinned, it holds only the
fidelity "Literature correlation (Colebrook/Clamond, screening)". Pinned, its
fidelity is "CFD reduced model (scientifically unqualified)", and it adds the
pinned ids, the evaluation status, the violations and the relative
uncertainty. Dynamic runs still use the correlation, and on a pinned unit
they add the warning `PBR_HYDRAULICS_PIN_NOT_APPLIED_DYNAMIC`. Tests:
`backend/tests/test_216_process_hydraulics_pin.py`.

### Design-owned Process pin and obsolescence on the unit (2026-10-25)

`POST …/components/{id}/reduced-models/{model_version_id}/process-pin` pins
the component's linked Process unit to one of its reduced models. It checks the
active version, the component revision and the draft revision, any of which
returns 409 when stale. The model must be attached to the component and its
status must be `current`; an obsolete, unknown or invalid model is refused with
its reasons and nothing changes. The published source must still match the
attached digest. The pin is applied through the typed draft op
`set_unit_hydraulics` with Design provenance, and the event
`DesignComponentProcessHydraulicsPinned` is logged. The component status route
now reports `process_unit_pinned` for the entry the linked unit uses.

A steady evaluation of a pinned unit re-checks the geometry of every Design
component that pins the same Studio version in that workspace. If a
component's tube geometry changed since the study, or its geometry cannot be
read, the unit gets the warning `PBR_HYDRAULICS_MODEL_OBSOLETE` ("Reduced
model obsolete, rerun the study: …") and a caveat. The pin is kept and still
used until the engineer re-pins or clears it. Changes to pinned upstream
study inputs are tracked through the component study templates (next
section). Tests: `backend/tests/test_216_process_pin_from_design.py`.

The GUI pin (Design component panel) and the agent-tool pin (agent workspace
action) were delivered in commits 813de354 and 4fa01b76.

### Component study templates and input obsolescence (2026-10-25)

`PUT …/components/{id}/cfd-study-templates/{regime}` records the component's
current CFD study inputs for one regime as a typed `PipeStudyContract`;
`GET …/cfd-study-templates` lists them (regime, template digest, update time,
contract). The table is `design_component_study_templates`, one row per
component and regime. The PUT checks the active version and component revision
(409 when stale), refuses a contract whose regime differs from the path (422)
or whose full fluid domain differs from the component's current derived domain
(422; 409 when that geometry is unknown), stores the canonical
contract and `study_template_digest`, bumps the component revision and logs
`DesignComponentStudyTemplateSet`.

A recorded template is compared with each reduced-model segment's
`study_template_digest`. A mismatch makes the model `obsolete` with "<regime>
study inputs changed since publication; rerun the study" in the component
status route, which also refuses the Process pin. The steady-evaluation check
of an already pinned unit reports the same mismatch in its
`PBR_HYDRAULICS_MODEL_OBSOLETE` warning. Publishing is refused with 409 when the
fitted segment's template digest differs from the recorded one. A regime with no
recorded template is not checked against study inputs: only the geometry digest
applies to it, and that is not a statement that its inputs are current. The
template digest excludes per-point fields (inlet, mesh level, solver controls).
Tests: `backend/tests/test_216_component_study_templates.py`.
The full-domain check also refuses caller-supplied bore dimensions that claim
the correct geometry digest; the attached BlueCad geometry owns those values.

`GET …/components/{id}/fluid-domain` returns the read-only `FluidDomain`
derived from the component's attached GeometrySpec (409 with the same reason
text as the template PUT when it cannot be derived). The Design component
"Reduced model" node shows a CFD study templates panel: recorded templates
(regime, short digest, update time), the derived diameter and length or the 409
reason, and a form that records a laminar or turbulent template (turbulence
model and wall treatment only when turbulent, fluid defaults water at 20 C, named `user_defined` when edited,
inlet mean velocity, mesh level, outlet pressure 0). The domain block of the
contract is copied from the fluid-domain route and is never typed. The panel
sends the component revision and active version id, then reloads the component,
templates and reduced-model status; 409/422 detail text is shown verbatim.
Contract building and client-side checks live in
`frontend/src/pages/studyTemplateForm.ts`; the server remains the authority.
Tests: `backend/tests/test_216_component_fluid_domain.py`,
`frontend/tests/216-study-template-form.mjs`.

**Still open for contract 8:** setting study templates from agent tools, and
server-side study execution.

### 2D-axisymmetric wedge with the transpose workaround: diagnostic (2026-10-25)

The module docstring of `casegen.py` rejects a wedge because an earlier wedge
probe showed a pressure-gradient error on the pinned v2412 build. That probe
predates the diagnosis of the defective tensor transpose below. The wedge was
re-tested with the same pinned v2412 `simpleFoam` and with every generated
solver dictionary unchanged, including `cache { grad(U) yes; }`. The
maintainer asked for this in #918: it would make studies take minutes instead
of hours.

**Method.** A diagnostic script, outside the repository, takes a generated
case. It keeps `0/`, `constant/` and `system/` and replaces only:

- the mesh, with one ±1° wedge block about the x axis;
- the two symmetry planes, with `wedge` patches;
- the inlet flow rate, with the wedge-sector fraction of the quarter-pipe
  value.

The ±1° angle keeps the planar-facet area deficit at 0.02%. The radial line
reproduces the 3D grid along the y axis (z = 0): the same core cell count, and the same
wall-graded outer block with its grading ratio. The axial cell count and the
iteration count are also the same. Probes sit in the wedge mid-plane at the 3D
probes' x and radius. The runner's own `laminar_comparison` and
`turbulent_comparison` evaluated them. The laminar contract is cycle 83's
level 0 (Re 99.62, L = 0.2 m). Its levels refine the radial and axial counts by
2. The turbulent contracts are cycle 84's C1 levels 0–2.

**Results.**

| Case | Wedge cells | Wedge ΔP/L (Pa/m) | 3D ΔP/L (Pa/m) | Wedge vs 3D | Wedge wall time | 3D solver time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Laminar, 16 radial | 1,280 | 3.20197 (−0.138% vs HP) | — | — | 0.3 s | — |
| Laminar, 32 radial | 5,120 | 3.20669 (+0.009%) | — | — | 1.8 s | — |
| Laminar, 64 radial | 20,480 | 3.20783 (+0.045%) | — | — | 20 s | — |
| C1 SST level 0 | 9,040 | 55.399 | 55.643 | −0.44% | 50 s | 968 s |
| C1 SST level 1 | 20,340 | 55.682 | 55.886 | −0.37% | 127 s | 13,985 s |
| C1 SST level 2 | 45,810 | 55.774 | 55.991 | −0.39% | 439 s | 11,796 s (16 ranks) |

- **Laminar wedge.** The three meshes converge monotonically, with observed order
  2.05. The extrapolation is 3.20819 Pa/m, +0.06% vs Hagen–Poiseuille 3.2064,
  and the fine GCI is 0.014%. The profile error is 0.89% / 0.21% / 0.04% of the
  mean speed. The 3D quarter-pipe levels were +0.28% to +0.45% vs
  Hagen–Poiseuille.
- **Turbulent wedge.** The three levels converge monotonically, with observed
  order 2.77. The extrapolation is 55.819 Pa/m, −1.11% vs Colebrook, and the
  fine GCI is 0.10%. Against the 3D extrapolation of 56.074 Pa/m, the wedge is
  0.45% lower. That difference is larger than the two GCIs combined (≈0.29%) and
  is not explained here. On the laminar case, where the exact answer is known,
  the wedge is the closer of the two.
- **Residuals.** On every turbulent level, every solved field reaches ≤ 1.3e-7 except `Uz`, the
  wedge-normal component. Its normalised residual stalls at 3e-4, 1e-3 and 7e-2
  on turbulent levels 0, 1 and 2. In the saved fields, |Uz| ≤ 1.1e-14 m/s
  against |Ux| up to 0.56 m/s, so the residual normalises a machine-zero field.
  Under the runner's current rule (every component ≤ target) these runs would
  therefore read as not converged. The laminar runs stopped on
  `residualControl { p 1e-8; }`. Fixed-iteration controls check that stop.
  At 16 radial cells, 5000 iterations reproduce the early-stopped ΔP/L to 7
  digits. At 64 radial cells, the early stop came at iteration 403, and 3000
  iterations move ΔP/L by −0.003%. The table uses the 3000-iteration value.
- `checkMesh` accepts every wedge mesh: zero non-orthogonality, skewness 0.33,
  and a maximum aspect ratio of 293 (turbulent level 0).

**Status.** This is a diagnostic, not product evidence. The wedge cases were
not produced by the typed case generator. No equivalence tolerance was stated
before the runs. No mesh-check refusal rules for wedges exist. Before a wedge
domain is used, the case generator needs a typed wedge variant (spec 216
requirement 2 allows a reduced domain "when its equivalence to the 3D tube is
stated and tested"). That variant needs the following, in order:

1. a wedge-normal residual rule that excludes `Uz` only when the field's |Uz|
   is below a stated bound;
2. a pre-stated wedge-vs-3D equivalence tolerance that takes the 0.45%
   turbulent offset into account;
3. typed reruns of the study above.

The 3D results above stay the qualification of record. Evidence:
`~/jarvis-control/work/evidence/216-solver-controls/c88-wedge/`
(`wedge.py`, `run.sh`, `evalcase.py`, `lam_eval.jsonl`, `lam_controls.jsonl`, `turb_eval.jsonl` and
the case directories).

### Superseded first runs: casegen v1, defective stress path

| Mesh level | Cells | Final residual target | Measured ΔP/L (Pa/m) | Error vs HP | Profile max error / mean speed |
| --- | ---: | --- | ---: | ---: | ---: |
| 0 | 15,360 | met | 4.016805 | 25.275% | 1.434% |
| 1 | 51,840 | met | 4.013776 | 25.180% | 4.699% |

Both `checkMesh` reports have zero failed checks (non-orthogonality maxima
24.69° and 27.83°). Input geometry digest for both is
`sha256:e2d7f53b600c50f9fea73d81398453f9dfe0c7e509711a25fb8f2c01e3ded988`.
The level 0 and 1 case manifest digests are respectively
`sha256:96c183cf2949609579555404b60f457239c0b01c9d792da11eca6a6d5f8c322d`
and `sha256:0f7670d87fc5f42256f886c42ab482a2d758e6555054f99dff7051ace69782f8`.
The level 2 mesh passed `checkMesh` at 174,960 cells, but its diagnostic
solver run was stopped after about ten minutes while the earlier two levels
already showed the material pressure discrepancy. It has no final fields or
numerical comparison and is excluded from the table.

The pressure error persists after the first refinement, so a small mesh
spread would not establish physical accuracy. No pressure-drop model should
be published from these runs. The next numerical repair must trace the
pressure equation, boundary conditions, sampling and mesh family, then rerun
the three-mesh laminar and turbulent qualification before publication.

### Controlled solver diagnostics (2026-10-24)

The level 0 generated case was copied into separate external `/tmp` directories;
each diagnostic copy changed only the stated solver input. All variants ran
`blockMesh` and `simpleFoam` with the same pinned v2412 installation and used
the same two pressure probe locations. These hand-edited copies are diagnostics,
not typed cases or qualification evidence.

| Variant | Solver input change | Final pressure gradient | Solver verdict |
| --- | --- | ---: | --- |
| Generated baseline | none | 4.016805 Pa/m | residual-converged at 300 iterations |
| Linear convection | `div(phi,U)` from bounded linear-upwind to `Gauss linear` | 4.015921 Pa/m | completed 300 iterations |
| Fixed-speed inlet | `flowRateInletVelocity` to `fixedValue` at 0.01 m/s | 4.010354 Pa/m | residual-converged at 300 iterations |
| Standard SIMPLE trial | `consistent no`, velocity relaxation 0.5 | 3.080548 Pa/m at iteration 300; −25539.4 Pa/m at iteration 1500 | **failed**: continuity and momentum residuals did not converge |
| Relaxed standard SIMPLE | `consistent no`, velocity relaxation 0.7, pressure-field relaxation 0.3 | 4.016811 Pa/m | completed 500 iterations; final Ux and p residuals below 1e-8 |

The inlet, convection and relaxed standard SIMPLE changes do not explain the
25% pressure excess. The first standard SIMPLE trial must not be cited as
agreement with Hagen–Poiseuille:
its iteration-300 pressure was transient and the run subsequently diverged.
The relaxed trial copied the generated level 0 case into an external diagnostic
directory, removed its saved fields, and changed only `fvSolution` and the
iteration/write limit in `controlDict`. Its final probe values were
`p(0.13 m) = 0.000286128986166` and `p(0.17 m) = 0.000125166824916` m²/s²;
at 998.2 kg/m³ they yield 4.016811 Pa/m, within 0.0002% of the baseline.
This is a solver-setting diagnostic, not a typed qualification run. The
saved-field checks below narrow the discrepancy but do not resolve the
discrete axial momentum balance. Only a converged physical balance can
justify changing the case generator or the reported pressure result.

### Developed-section pressure check (2026-10-24)

The saved level 0 `300/` field was copied to a separate external diagnostic
case and resampled with pinned v2412 `postProcess` at ten axial stations, all
at `(y, z) = (0.5, 0.5) mm`. No solver input or field was changed. The outputs
are preserved in `backend/tests/fixtures/cfd_pipe/laminar_axial_probe_{p,U}.txt`.
The sampled axial velocity approaches 0.01952 m/s by x = 0.10 m and stays within 0.02% of
that value through x = 0.18 m. Density-scaled pressure gradients are:

| Axial interval | Measured gradient |
| --- | ---: |
| 0.02–0.06 m | 4.189823 Pa/m |
| 0.06–0.10 m | 4.024052 Pa/m |
| 0.10–0.14 m | 4.017085 Pa/m |
| 0.14–0.18 m | 4.016832 Pa/m |

The 25% excess is already present in the nearly developed interior and is
not explained by the choice of the original 0.13–0.17 m probe interval. This
diagnostic still leaves the discrete wall-stress/pressure balance and mesh
geometry unresolved. The resampled field is not a qualification run.

### Saved-field wall-stress check (2026-10-24)

Pinned v2412 `simpleFoam -postProcess -func wallShearStress -time 300` was
applied to the saved level 0 and 1 fields. For wall faces whose axial centres
lie in the original 0.13–0.17 m pressure interval, the axial kinematic wall
stress was averaged by each face's area from `constant/polyMesh`. For a
developed quarter-pipe, the wall-implied pressure gradient is
`rho * 2 * |mean wallShearStress_x| / R` (R = 5 mm). This is a diagnostic force
balance from the saved fields, not a new solver run or qualification result.

| Mesh level | Wall faces in interval | Mean axial kinematic wall stress (m²/s²) | Wall-implied gradient (Pa/m) | Sampled pressure gradient (Pa/m) |
| --- | ---: | ---: | ---: | ---: |
| 0 | 256 | 8.426385e-6 | 3.364487 | 4.016805 |
| 1 | 576 | 8.418050e-6 | 3.361159 | 4.013776 |

The wall-implied gradient is about 5% above Hagen–Poiseuille, while the
sampled pressure gradient is about 25% above it. Their roughly 19% mismatch
persists across refinement. The profile and near-constant axial velocity in
the developed interval make entrance loss an inadequate explanation. The
discrete pressure/momentum balance remains unresolved; neither gradient is
accepted as a publishable pipe correlation. A separate level 0 trial changing
only `consistent yes` to `consistent no` with the existing U relaxation 0.9
failed with a floating-point exception and supplied no usable result.

### Radial pressure and axial-development check (2026-10-24)

The unchanged saved level 0 `300/` field was resampled with pinned v2412
`simpleFoam -postProcess` at x = 0.13 and 0.17 m. At each station, five points
had z = 0.5 mm and y = 0.5, 1.5, 2.5, 3.5, or 4.0 mm. The temporary case was
outside the repository; only its `controlDict` probe locations changed.
The raw output is preserved in
`backend/tests/fixtures/cfd_pipe/laminar_radial_probe_{p,U}.txt`.

| y (mm) | Density-scaled pressure gradient (Pa/m) | Ux at 0.13 m (m/s) | Ux change to 0.17 m (m/s) |
| ---: | ---: | ---: | ---: |
| 0.5 | 4.016805 | 0.019521201 | +2.418e-7 |
| 1.5 | 4.016805 | 0.018275295 | +1.335e-7 |
| 2.5 | 4.016805 | 0.014518725 | −4.235e-8 |
| 3.5 | 4.016805 | 0.009856709 | −9.448e-8 |
| 4.0 | 4.016805 | 0.007924333 | −8.997e-8 |

The pressure gradient is independent of these radial sample points to the
reported precision. The sampled axial velocities change by at most 0.0025%
of the 0.01 m/s mean speed between stations. This excludes radial probe
placement and a still-developing axial profile in that interval as explanations
for the 25% pressure excess. It does not identify the discrete balance error;
the case remains unqualified and publication remains refused.

The maintainer-owned cloud prototype PR #916
was read as input. It reports laminar agreement within 0.037% using ESI
OpenFOAM v1912, a wedge mesh and a longer entrance allowance. Those are
prototype claims from a different tool release and mesh; they do not validate
this v2412 case. The next repair can use its field and mesh analysis as a
comparison without modifying or merging that lane.

### Saved-field flow balance (2026-10-24)

The unchanged level 0 and 1 `300/phi` surface-scalar fields were read from the
external pinned-v2412 cases. Summing each inlet and outlet patch's face fluxes
gives the following volumetric rates. The prescribed quarter-pipe rate is
`π(0.005 m)²(0.01 m/s)/4 = 1.96349540849e-7 m³/s`.

| Mesh level | Inlet flux (m³/s) | Outlet flux (m³/s) | Absolute imbalance / prescribed rate |
| --- | ---: | ---: | ---: |
| 0 | −1.96349540849e-7 | 1.96349534126e-7 | 3.42e-8 |
| 1 | −1.96349540849e-7 | 1.96349543474e-7 | 1.34e-8 |

This excludes an incorrect imposed quarter-flow or gross mass imbalance as the
source of the 25% pressure excess. It is a saved-field diagnostic, not an
independent solver run or numerical qualification. The discrete axial momentum
balance remains unresolved, so publication is still refused.

### Solver-path controls outside the case generator (2026-10-24)

Hand-written `blockMesh` cases, independent of the spec 216 generator, were run
with the same pinned v2412 installation, the generated level 0 `fvSchemes`,
`fvSolution` (SIMPLEC), transport properties and probe method. Each control has
an exact analytic laminar answer. These are external diagnostics, not typed
cases or qualification evidence. Inputs, logs, probe output and the two duct
fields are preserved outside the repository in
`~/jarvis-control/work/evidence/216-solver-controls/`.

| Control | Mesh | Solver | Measured gradient | Analytic | Error |
| --- | --- | --- | ---: | ---: | ---: |
| Plane half-channel, one wall, z one cell | 200×20×1 | `simpleFoam` | 1.200936 Pa/m | 1.2024 Pa/m | −0.12% |
| Plane half-channel, one wall, z resolved | 80×16×16 | `simpleFoam` | 1.200091 Pa/m | 1.2024 Pa/m | −0.19% |
| Square-duct quarter, two walls, two `symmetryPlane` | 80×16×16 | `simpleFoam` | 3.533717 Pa/m | 2.851106 Pa/m | +23.94% |
| Same quarter, `symmetry` patch type | 80×16×16 | `simpleFoam` | 3.533717 Pa/m | 2.851106 Pa/m | +23.94% |
| Full square duct, four walls | 80×32×32 | `simpleFoam` | 3.530449 Pa/m | 2.851106 Pa/m | +23.83% |
| Full square duct, four walls | 40×16×16 | `simpleFoam` | 3.483047 Pa/m | 2.851106 Pa/m | +22.16% |
| Same, `grad(U) leastSquares` | 40×16×16 | `simpleFoam` | 3.495584 Pa/m | 2.851106 Pa/m | +22.60% |
| Same, `div(phi,U)` bounded upwind | 40×16×16 | `simpleFoam` | 3.484025 Pa/m | 2.851106 Pa/m | +22.20% |
| Same, transient to steady | 40×16×16 | `pimpleFoam` | 3.483039 Pa/m | 2.851106 Pa/m | +22.16% |
| Same, transient to steady | 40×16×16 | `icoFoam` | 2.810508 Pa/m | 2.851106 Pa/m | −1.42% |

The square-duct analytic value is the series solution (Poiseuille number
f·Re = 56.908). The generated level 0 quarter pipe with
`nNonOrthogonalCorrectors 2` gave 4.016806 Pa/m, unchanged from the baseline.
The generated mesh's outlet area is 1.960343e-5 m², 0.16% below the exact
quarter circle; the wall vector area matches the arc geometry.

The excess therefore occurs on a plain orthogonal duct and does not depend on
the generator, O-grid, symmetry patch type, non-orthogonal correction, gradient
or convection scheme, or SIMPLEC. It appears with walls in two transverse
directions but not with one wall direction. `icoFoam` gives the expected
result on the same mesh, flow rate and schemes. `simpleFoam` and `pimpleFoam`
share the laminar `Stokes` momentum-transport term
`-fvc::div(nuEff*dev2(T(grad(U)))) - fvm::laplacian(nuEff, U)`; `icoFoam` uses
`-fvm::laplacian(nu, U)` only. The installed `linearViscousStress.C` source
has the standard form, and `turbulenceFields` reports uniform `nuEff`
1.00380685233e-6 m²/s.

The converged 40×16×16 `simpleFoam` field is also internally inconsistent.
An independent NumPy evaluation of the saved field gives an axial `ν∇²u` of
2.8757 Pa/m equivalent, while its pressure gradient is 3.4828 Pa/m. That
evaluation finds the explicit `dev2(T(grad U))` term negligible in the
developed section. Wall-adjacent cells have an odd–even zig-zag and are not
mirror-symmetric (for example 1.894, 1.985, 3.043, 2.626, 3.619… mm/s
along one wall). The `icoFoam` field is smooth and symmetric, and its own
`ν∇²u` (2.8097 Pa/m) matches its pressure gradient (2.8102 Pa/m). The saved
4-wall square-duct wall-shear balance gives 2.915 Pa/m, while the pressure
gives 3.530 Pa/m. This is the same pattern as the quarter-pipe wall-stress
check above.

**Conclusion at that checkpoint:** the 25% excess comes from the pinned
`simpleFoam`/`pimpleFoam` laminar momentum-transport path of this v2412
build when walls lie in two transverse directions. It is not a spec 216 case
generator defect. The section below identifies the defective operator.

### Defective tensor transpose in the pinned build (2026-10-24)

Rerunning the 40×16×16 `simpleFoam` duct for 2000 iterations with `p` and `U`
tolerance 1e-15, relative tolerance 0.01 and up to 200 sweeps gives
3.483047 Pa/m (+22.17%), the same value as before. Solver-tolerance
stagnation is therefore not the cause.

A diagnostic copy of the same duct then added a run-time-compiled
`vectorCodedSource` fvOption. It adds `+fvc::div(ν dev2(T(fvc::grad(U))))`,
which cancels the explicit `Stokes` term. Nothing else changed. The pressure
gradient became 2.810508 Pa/m (−1.42%), which matches the `icoFoam` control
to all printed digits. The same cancellation on the generated level 0
quarter pipe (300 iterations, final initial residuals ≤ 5.6e-9) gave
3.220790 Pa/m against Hagen–Poiseuille 3.2064 Pa/m (+0.45%), instead of
4.016805 Pa/m (+25.3%). Its centreline speed moved from 0.019521 to
0.019656 m/s (analytic 0.02).

The same fvOption wrote OpenFOAM's own `grad(U)`, `T(grad(U))`,
`dev2(T(grad(U)))` and the divergence term. Their y and z divergence
components match a NumPy replication to seven digits. The axial component
does not: it is −1.415 Pa/m equivalent at the duct centre, where the
continuum and replicated values are about 4e-9. At every cell checked, the
axial value equals `ν ∂_z(∂_z u_x)`, not `ν ∂_z(∂_x u_z)`. The written fields
show why. On a temporary field, `T(fvc::grad(U))` puts (∇U)_zx in both the
XZ and ZX entries. XY/YX and YZ/ZY transpose correctly. For a stored field,
`T(g)` and `g.T()` are exact (zero difference). The installed
`Tensor<Cmpt>::T()` header is correct, so the fault is in the compiled
in-place field transpose of this conda-forge build. `linearViscousStress`
applies `T()` to the temporary returned by `fvc::grad(U)`.

This explains every control. A one-wall channel has no `∂_z u_x`, so the
spurious term is zero and it passes. A duct or pipe has walls in both
transverse directions, so the solver adds about half a transverse Laplacian
as a body force. The error does not shrink with refinement.

**Conclusion for the next repair:** the pinned build's transpose of a
temporary tensor field is wrong. Every pinned `simpleFoam`/`pimpleFoam`
momentum-transport model that calls `dev2(T(fvc::grad(U)))` is affected:
laminar `Stokes` and the RAS/LES `linearViscousStress` family. The turbulent
qualification is affected too. Spec 216 requirement 1 allows working around a
known release defect in the adapter, with documentation, or moving the pin.
The repair should do one of these and add a regression control: the
40×16×16 square duct within about 2% of f·Re = 56.908 and the one-wall
channel. The typed laminar and turbulent qualification must then be rerun.
The coded-source run above is diagnostic only and is not a typed case.
Pressure output stays unpublishable until that rerun passes. Inputs, logs,
written fields and analysis scripts are in
`~/jarvis-control/work/evidence/216-solver-controls/c80-*`.
