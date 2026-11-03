# BLUECAD strict real-tool proof

This runbook executes the 021b-A integration proof and the spec 024 verification
proofs with the real Gmsh and CalculiX binaries. The 021b path proves the
integrated CAD → mesh → static-FEM → evidence path. Spec 024 separately proves
quadratic tetrahedra, solid-face pressure mapping, and prescribed analytic
accuracy at stated sampling locations.

## Safety boundary

- Use an external operator-owned registry. Do not edit `configs/bluecad_tools.yaml`
  with machine-local paths or hashes.
- The registry must contain exact executable paths, version pins, SHA-256 hashes,
  provenance URLs, and health checks.
- The BLUECAD AI response is supplied by the caller-injected scripted offline
  adapter. No live provider or network route is used.
- Pytest creates an isolated temporary `JARVISOS_DATA_ROOT`; the normal operator
  data root is not touched.
- The analytic battery uses only checked-in hash-bound STEP/manifest fixtures and
  the production registry-bound mesh/FEM adapters. It does not invoke Gmsh or
  CalculiX directly.

## Prepare the registry

Locate the actual executables and calculate their hashes:

```bash
command -v gmsh
command -v ccx
sha256sum "$(command -v gmsh)" "$(command -v ccx)"
gmsh -version
```

Create a YAML file outside the repository using
`registry_version: bluecad_tool_registry_v0_1`. Enable only the tools being
proved. Gmsh and CalculiX must remain subprocess integrations with license
boundary `C`. Example fields for each tool:

```yaml
id: gmsh                 # or calculix
kind: mesher             # or fem_solver
integration_mode: subprocess
version_pin: "<exact version>"
enabled: true
entrypoint: "<absolute executable path>"
binary_sha256: "<64 hex characters>"
provenance_url: "<operator-recorded source>"
health_check: "<same executable path> <operator-owned health arguments>"
```

Gmsh may use `-version`. CalculiX 2.21 does not provide a reliable zero-exit
version-only command in every package build; use a minimal valid static deck in
the registry directory and set the health check to `<ccx-path> <deck-basename>`.
The dedicated GitHub workflow creates and solves such a tetrahedron deck.

## Check the registry

```bash
export JARVISOS_BLUECAD_TOOL_REGISTRY=/absolute/path/bluecad-tools.yaml
cd backend
python -m app.modules.bluecad.registry check
```

The command must report `hash=ok` and `health=ok` for both tools. A missing
binary, hash mismatch, invalid license boundary, or failing health check returns
nonzero.

## Ordinary versus strict execution

Ordinary development/CI remains skip-capable:

```bash
cd backend
python -m pytest -q \
  tests/bluecad/test_alpha_real_tools.py \
  tests/bluecad/test_fem_verification_real_tools.py \
  tests/bluecad/test_fem_verification_battery_real_tools.py
```

When tools are unavailable, these commands record pytest skips. A skip is not a
real-tool proof.

Strict execution converts the same condition into failure:

```bash
cd backend
python -m pytest -q \
  tests/bluecad/test_alpha_real_tools.py \
  tests/bluecad/test_fem_verification_real_tools.py \
  tests/bluecad/test_fem_verification_battery_real_tools.py \
  --require-bluecad-real-tools
```

The command exits zero only after the integration proof and verification battery
complete with:

- valid geometry and successful bounded attempts;
- non-empty Gmsh meshes with required BODY/BC/LOAD groups;
- C3D10 volume elements for every spec 024 benchmark;
- successful native CalculiX FRD parsing with component-aware displacement and
  stress blocks;
- completed simulation rows with passing mesh/FEM verdicts for the 021b path;
- linked validation, mesh, and FEM evidence;
- registered artifact hashes matching the files;
- byte-identical canonical manifests;
- solid-face pressure mapping with the required area/resultant evidence;
- cantilever tip displacement within 2% and a non-degrading coarse/fine sanity
  comparison;
- Lamé bore hoop stress within 5%, radial stress with correct sign and within
  10%, eight or more angular samples, area within 1%, and self-equilibrated
  applied/reaction resultants;
- finite-width-hole tangential stress within 7% at symmetric mid-thickness
  transverse-diameter samples using the prescribed net-section convention;
- a bounded JSON/Markdown battery report containing tool pins/hashes, fixture
  digests, mesh counts, sampling evidence, load balances, relative artifact
  paths, and explicit limitations.

GitHub Actions runs the same strict command in
`.github/workflows/bluecad-real-tool-proof.yml` using an external registry built
from the runner's installed packages. The workflow first runs the complete
offline backend test suite, then uploads the integration proof, analytic battery
JSON, generated Markdown/JSON reports, and bounded raw diagnostics.

## Analytic battery artifacts

The 024-C2 run writes these files below its isolated proof root:

```text
reports/bluecad_fem_verification_battery.json
reports/bluecad_fem_verification_battery.md
```

Per-case directories retain mesh GEO/INP/MSH/log output, CalculiX INP/FRD/DAT/log
output, pressure-face mapping evidence where applicable, and the tool summaries
referenced by the report. Every path written into the report is relative to the
proof root. Runtime reports are uploaded by CI and are never committed back to
the repository.

## Determinism boundary

The canonical manifest excludes elapsed runtime. It retains STEP/STL/GLB hashes
and sizes as integrity evidence. Open Cascade writes the wall-clock export time
into the STEP `FILE_NAME` header, so BLUECAD normalizes only that header timestamp
to `1970-01-01T00:00:00` immediately after export. Geometry data, entity ordering,
and all other STEP content remain untouched. A missing or ambiguous header fails
as `EXPORT_ERROR` rather than silently weakening the manifest.

CalculiX input uses a relative mesh include path. This avoids its fixed input-line
limit without copying or duplicating the canonical mesh artifact.

The analytic report is byte-deterministic only when timestamp, git SHA,
environment strings, tool metadata, fixture digests, and numerical case evidence
are supplied as normalized inputs. Real runs intentionally record their actual
environment and timestamp.

## Interpreting failures

- **Skip without strict flag:** expected when tools are unavailable; not proof.
- **Registry failure:** fix the operator registry or tool installation. Do not
  weaken hash/license checks.
- **Mesh group failure:** inspect `mesh.geo`, `mesh.inp`, and `gmsh.log`; do not
  substitute fake groups.
- **Element-order failure:** preserve the generated INP/log and fix the bounded
  adapter/fixture contract; do not accept C3D4 in the analytic battery.
- **Solver input failure:** inspect `analysis.inp` and `analysis.log`. Keep mesh
  includes relative; do not shorten the test data root to hide a line-length bug.
- **FRD parse failure:** preserve the real `.frd` artifact and fix the narrow
  public-format parser; do not make the real solver emit the fake fixture format.
- **Sampling failure:** inspect the selected node IDs, coordinates, component
  headers, location residuals, and mesh groups. Do not substitute a global maximum.
- **Tolerance failure:** keep the prescribed 2%/5%/7% limits. Do not silently tune
  solver options, manually edit meshes, claim nonexistent local refinement, or
  substitute different boundary conditions.
- **Manifest mismatch:** compare artifact hashes first. Runtime duration is outside
  the manifest; STEP header time is normalized, while every geometry artifact hash
  remains binding integrity evidence.
- **Tier 3 or battery failure:** the proof failed and cannot be represented as
  green. Preserve all bounded artifacts before changing code or definition.
