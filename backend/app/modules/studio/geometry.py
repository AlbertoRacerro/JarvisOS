"""Studio geometry category (spec 189): user-authored build123d parts. User source stays inert here."""

import hashlib
import shutil
from math import isfinite
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.paths import build_paths
from app.modules.studio.models import StudioError, Variable
from app.modules.studio_runtime.runtime import ARTIFACT_NAMES, RunRequest, run, status

GEOMETRY_KIND = "studio_geometry_v1"
MEDIA_TYPES = {"model.step": "application/step", "model.stl": "model/stl", "model.glb": "model/gltf-binary"}


class GeometryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["geometry"]
    name: str = Field(min_length=1, max_length=160)
    engineering_question: str = Field(min_length=1, max_length=2000)
    inputs: list[Variable] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_inputs(self) -> "GeometryCreate":
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("Input names must be unique.")
        if "build" in names:
            raise ValueError("Input name build is reserved.")
        return self


class GeometryCase(BaseModel):
    """One build with optional declared bounds, in model length units (build123d works in mm)."""
    model_config = ConfigDict(extra="forbid")
    inputs: dict[str, float]
    volume_min: float | None = None
    volume_max: float | None = None
    max_extent: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def finite_values(self) -> "GeometryCase":
        values = [*self.inputs.values(), self.volume_min, self.volume_max, self.max_extent]
        if any(isinstance(value, bool) or (value is not None and not isfinite(value)) for value in values):
            raise ValueError("Test values must be finite numbers.")
        return self


class GeometryTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[GeometryCase] = Field(min_length=1, max_length=4)


def geometry_contract(data: GeometryCreate) -> dict:
    return {"category": "geometry", "inputs": [item.model_dump(exclude_none=True) for item in data.inputs]}


def geometry_scaffold(contract: dict) -> str:
    """Contract-only deterministic source; the operator writes the body of build."""
    names = [item["name"] for item in contract["inputs"]]
    signature = ", ".join(f"{name}: float" for name in names)
    arguments = ", ".join(f"{name}=inputs[{name!r}]" for name in names)
    return "\n".join([
        "# Studio geometry. Inputs use the declared contract units; build123d lengths are millimetres.",
        "import build123d as bd", "", "",
        f"def build({signature}) -> bd.Part:",
        '    """Return one build123d solid built from the declared inputs.', "", "    Inputs:",
        *(f"        {item['name']} [{item['unit']}]" for item in contract["inputs"]), '    """',
        '    raise NotImplementedError("Complete build before testing")', "", "",
        "def main(inputs: dict[str, float]) -> bd.Part:", f"    return build({arguments})", ""])


def preview_root(workspace_id: str, model_spec_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "studio_previews" / model_spec_id


def _bounds_errors(facts: dict, case: GeometryCase) -> list[str]:
    errors = []
    if not facts.get("valid") or int(facts.get("solids", 0)) < 1:
        errors.append("The shape is not a valid solid.")
    volume = float(facts.get("volume", 0.0))
    if case.volume_min is not None and volume < case.volume_min:
        errors.append(f"Volume {volume:.6g} is below the declared minimum {case.volume_min:.6g}.")
    if case.volume_max is not None and volume > case.volume_max:
        errors.append(f"Volume {volume:.6g} is above the declared maximum {case.volume_max:.6g}.")
    if case.max_extent is not None:
        extent = max(high - low for low, high in zip(facts["bbox_min"], facts["bbox_max"], strict=True))
        if extent > case.max_extent:
            errors.append(f"Largest extent {extent:.6g} exceeds the declared maximum {case.max_extent:.6g}.")
    return errors


def run_cases(workspace_id: str, model_spec_id: str, source: str, contract: dict,
              cases: list[GeometryCase]) -> list[dict]:
    """Build every case in the geometry profile; the previous previews of this model are replaced."""
    root = preview_root(workspace_id, model_spec_id)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    results = []
    names = {item["name"] for item in contract["inputs"]}
    for case in cases:
        invalid = ["Input names do not match the contract."] if set(case.inputs) != names else [
            f"Input {item['name']} is outside its bounds." for item in contract["inputs"]
            if item.get("min") is not None and case.inputs[item["name"]] < item["min"]
            or item.get("max") is not None and case.inputs[item["name"]] > item["max"]]
        if invalid:
            results.append({"passed": False, "classification": "invalid_case", "facts": None, "limit": None,
                            "limit_value": None, "preview_id": None, "artifacts": [], "errors": invalid})
            continue
        preview_id = str(uuid4())
        record = run(RunRequest(source=source, inputs=case.inputs), profile="geometry",
                     artifact_dir=root / preview_id)
        errors: list[str] = []
        if record.classification != "ok":
            reason = (f"the {record.limit} limit ({record.limit_value}) was reached" if record.limit
                      else record.artifact_error or record.stderr.strip() or status()["reason"] or "execution failed")
            errors.append(f"Runtime {record.classification}: {reason}")
        else:
            errors += _bounds_errors(record.result, case)
        results.append({"passed": not errors, "classification": record.classification,
                        "facts": record.result if record.classification == "ok" else None,
                        "limit": record.limit, "limit_value": record.limit_value,
                        "preview_id": preview_id if record.artifacts else None,
                        "artifacts": [item.model_dump() for item in record.artifacts],
                        "duration_seconds": round(record.duration_seconds, 3), "errors": errors})
    return results


def preview_file(workspace_id: str, model_spec_id: str, report: dict | None, preview_id: str, name: str) -> Path:
    """A test artifact of the current report, re-verified against its recorded hash."""
    entry = None
    for case in (report or {}).get("cases", []):
        if case.get("preview_id") == preview_id:
            entry = next((item for item in case["artifacts"] if item["name"] == name), None)
    if entry is None or name not in ARTIFACT_NAMES:
        raise StudioError("studio_geometry_preview_not_found", "Preview artifact not found.")
    path = preview_root(workspace_id, model_spec_id) / preview_id / name
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise StudioError("studio_artifact_hash_mismatch", "Preview artifact is invalid.")
    return path
