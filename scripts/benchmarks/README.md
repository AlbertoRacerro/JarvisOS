# Solver benchmarks

Reference runs for the numerical tools JarvisOS wraps. They run real binaries or solver packages and are
not part of the default test suite.

**Status: to develop into regression tests.** Today each directory is a script that records evidence
(JSON or text) from a real run; the next step is a pinned expected-value table per benchmark, a tolerance,
and a nightly or manual CI job that fails when a solver, package or adapter change moves the numbers.
Pinned tool versions and local tool paths are read from environment variables named in each script header.

| Directory | Solver | Content |
|---|---|---|
| `openfoam/` | OpenFOAM v2412 | `channel_benchmark.py`: bounded laminar 2-D channel case with hash-pinned install; `multifidelity_smoke.py`: decision-triggered fluids -> OpenFOAM fidelity check; recorded results (`*.json`, `solver_tail.txt`, `probe_*.txt`) are the fixtures read by `backend/tests/test_cfd_adapter.py` |
| `process_upstream/` | Python process libraries (CoolProp, thermo, fluids, ...) | `probe.py`: offline synthetic feasibility probes, one isolated result per candidate in `results/` (read by `backend/tests/test_process_upstream_103_results.py` and `test_process_stack_104.py`); Windows install/smoke helpers |
| `dwsim/` | DWSIM 10.2.9 via the pinned MCP | `manifest_generator.py` + `manifest_probe_inputs.py` (capability manifest from captured probes; the generator is exercised by `backend/tests/test_process_draft_158.py`), `editor_smoke.py` and `dynamics_smoke.py` (revisioned editor and dynamics commands against a real DWSIM), `kinetics_acceptance.py` + `kinetics_reference.py` (real-solver acceptance against an independent, product-free reference) |
| `pbr_reference/` | none (independent SciPy references) | `cylinder_reference.py` and `periodic_reference.py`: reference values for the photobioreactor unit tests (`backend/tests/test_pbr_unit_170*.py`); `pbr_unit.v1.ledger.json` is the qualification record those tests read |

Method worth reusing when adding a benchmark: an independent reference that imports no product code,
an isolated temporary data root, evidence written on failure as well as success, measured values only (never a golden
number copied from the implementation).
