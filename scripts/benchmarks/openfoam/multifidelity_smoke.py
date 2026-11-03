"""Run one decision-triggered fluids-to-OpenFOAM fidelity smoke with real v2412 binaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
# Never touch a real Jarvis data root from this smoke.
os.environ.setdefault("JARVISOS_DATA_ROOT", tempfile.mkdtemp(prefix="jarvis-110-smoke-"))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database import initialize_database  # noqa: E402
from app.modules.bluecad.cfd_adapter import OpenFoamEvaluator, case_digest  # noqa: E402
from app.modules.engineering.evaluator_contracts import EvaluationRequest, EvaluationResult, NamedQuantity  # noqa: E402
from app.modules.engineering.multifidelity import DecisionMargin, EscalationPolicy, escalate_study  # noqa: E402
from app.modules.engineering.refs import DomainBound, PhysicsCaseRef, Quantity, StudyRef  # noqa: E402
from app.modules.engineering.studies import DesignVariable, StudyDefinition, StudyObjective, run_study  # noqa: E402
from app.modules.process_stack.correlations import PIPE_EVALUATOR_ID, PipePressureDropEvaluator  # noqa: E402
from app.modules.workspaces.models import WorkspaceCreate  # noqa: E402
from app.modules.workspaces.service import create_workspace  # noqa: E402

PROBE_SPAN_TO_LENGTH = 2.0  # L / (0.75 L - 0.25 L) for the 014 centreline probes.


class ChannelPressureDrop:
    """Express the channel's probe pressure difference as a full-length drop in Pa.

    The 014 adapter samples kinematic pressure on the centreline at 0.25 L and 0.75 L, so its drop spans
    0.5 L. Assuming the fully developed gradient between the probes holds over L, the full-length drop is
    twice the probe drop; this ignores the inlet development length and is stated in the evidence.
    """

    def __init__(self, adapter: OpenFoamEvaluator, density: float) -> None:
        self.adapter = adapter
        self.density = density

    def descriptor(self):
        return self.adapter.descriptor()

    def availability(self):
        return self.adapter.availability()

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        values = {item.name: item.value for item in request.inputs}
        params = {
            "diameter": values["diameter"].value,
            "length": values["length"].value,
            "velocity": values["velocity"].value,
            "viscosity": values["dynamic_viscosity"].value / self.density,
        }
        translated = request.model_copy(update={
            "subject_ref": PhysicsCaseRef(
                authority_owner="bluecad", object_id="channel-case", workspace_id=request.request_ref.workspace_id,
                content_digest=case_digest(params),
            ),
            "inputs": tuple(NamedQuantity(name=name, value=Quantity(value=value, unit=unit)) for name, value, unit in (
                ("diameter", params["diameter"], "m"), ("length", params["length"], "m"),
                ("velocity", params["velocity"], "m/s"), ("viscosity", params["viscosity"], "m**2/s"),
            )),
        })
        result = self.adapter.evaluate(translated)
        if result.status != "succeeded":
            return result
        probe = next(item.value for item in result.outputs if item.name == "kinematic_pressure_drop")
        full_length = NamedQuantity(name="pressure_drop", value=Quantity(
            value=probe.value * self.density * PROBE_SPAN_TO_LENGTH, unit="Pa"))
        return result.model_copy(update={"outputs": (*result.outputs, full_length)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("channel_multifidelity_v2412.json"))
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    base_config = yaml.safe_load((ROOT / "configs/bluecad_tools.yaml").read_text())
    binaries = {"openfoam": prefix / "bin/icoFoam", "openfoam_blockmesh": prefix / "bin/blockMesh"}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    for tool in base_config["tools"]:
        if tool["id"] in binaries:
            tool.update(entrypoint=str(binaries[tool["id"]]), binary_sha256=hashes[tool["id"]], enabled=True,
                        provenance_url="https://www.openfoam.com/releases/openfoam-v2412/")
    registry_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", encoding="utf-8", delete=False)
    registry_path = Path(registry_file.name)
    with registry_file:
        registry_file.write(yaml.safe_dump(base_config))
    initialize_database()
    workspace = create_workspace(WorkspaceCreate(name="Multifidelity channel smoke", slug="multifidelity-110"))

    density = 1000.0  # Explicit reference density for converting OpenFOAM p/rho to Pa.
    fluid = PipePressureDropEvaluator()
    cfd = ChannelPressureDrop(OpenFoamEvaluator(registry_path), density)
    definition = StudyDefinition(
        study_ref=StudyRef(authority_owner="engineering", object_id="multifidelity-110", workspace_id=workspace.id,
                           revision="1"),
        evaluator_id=PIPE_EVALUATOR_ID,
        subject_ref=PhysicsCaseRef(authority_owner="bluecad", object_id="hydraulic-comparison",
                                   workspace_id=workspace.id, content_digest="sha256:" + "0" * 64),
        method="grid",
        variables=(DesignVariable(name="velocity", domain=DomainBound(
            variable="velocity", lower=Quantity(value=0.008, unit="m/s"),
            upper=Quantity(value=0.012, unit="m/s")), step=Quantity(value=0.002, unit="m/s")),),
        fixed_inputs=(
            NamedQuantity(name="density", value=Quantity(value=density, unit="kg/m3")),
            NamedQuantity(name="dynamic_viscosity", value=Quantity(value=0.001, unit="Pa*s")),
            NamedQuantity(name="diameter", value=Quantity(value=0.01, unit="m")),
            NamedQuantity(name="length", value=Quantity(value=0.2, unit="m")),
            NamedQuantity(name="roughness", value=Quantity(value=0, unit="m")),
        ),
        objectives=(StudyObjective(output="pressure_drop", sense="minimize"),), seed=1, budget=3, sample_count=3,
        deadline_per_point_s=300,
    )
    lower = run_study(definition, fluid)
    # Smoke decision threshold, not a design requirement: escalate points whose screening drop lies
    # within 0.05 Pa of a 0.65 Pa allowable; with the 0.008/0.010/0.012 m/s grid only the centre qualifies.
    high = escalate_study(definition, lower, (cfd,), EscalationPolicy(
        decision_margins=(DecisionMargin(output="pressure_drop", boundary=Quantity(value=0.65, unit="Pa"),
                                          margin=Quantity(value=0.05, unit="Pa")),),
        target_fidelity="field_resolved",
    ))
    assert len(high.points) == 1
    evidence = {
        "generated_by": "scripts/benchmarks/openfoam/multifidelity_smoke.py",
        "solver": "OpenFOAM v2412", "binary_sha256": hashes,
        "workspace_id": workspace.id, "study_ref": definition.study_ref.model_dump(mode="json"),
        "lower_run_digest": lower.content_digest, "supplemental_run_digest": high.content_digest,
        "lower_fidelity": fluid.descriptor().fidelity,
        "lower_points": [{"index": point.index, "inputs": [item.model_dump(mode="json") for item in point.inputs],
                          "status": point.status,
                          "evaluation": point.evaluation.model_dump(mode="json") if point.evaluation else None}
                         for point in lower.points],
        "escalations": [point.model_dump(mode="json") for point in high.points],
        "decision_policy": {"output": "pressure_drop", "boundary_pa": 0.65, "margin_pa": 0.05,
                            "note": "smoke trigger threshold, not a design requirement"},
        "comparison_scope": (
            "Both sides report pressure_drop in Pa over length 0.2 m with nominal dimension 0.01 m, density "
            "1000 kg/m3 and dynamic viscosity 1e-3 Pa*s. The screening evaluator is a smooth circular pipe "
            "(laminar dp = 32 mu L U / D^2); OpenFOAM is a 2-D plane channel of height 0.01 m (analytic "
            "dp = 12 mu L U / H^2). OpenFOAM's probe drop spans 0.5 L and is doubled assuming the fully "
            "developed gradient holds over L (inlet development ignored). The geometries are not equivalent: "
            "the expected analytic ratio pipe/channel is 32/12 = 2.667, so the discrepancy is a geometry "
            "difference, not a model error, and qualifies neither evaluator."
        ),
        "qualification": "unqualified",
        "openfoam_validity": "none supplied by adapter; retained as none",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    registry_path.unlink(missing_ok=True)
    print(json.dumps({
        "study_points": len(lower.points), "escalated_points": len(high.points),
        "status": [point.status for point in high.points],
        "discrepancy_pa": high.points[0].discrepancies[0].delta.value if high.points and high.points[0].discrepancies else None,
        "evidence": str(args.output),
    }))


if __name__ == "__main__":
    main()
