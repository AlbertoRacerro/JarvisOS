"""T2 optics hand-off through a real draft, immutable profile and dynamic run."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.modules.environment import profiles
from app.modules.process_stack import draft, dynamic_engine, dynamic_jobs
from app.modules.process_stack.draft_models import DraftQuantity, SetScenario, SetUnitParams
from tests.plumbing_170_support import new_workspace, pbr_document
from tests.test_process_dynamic_engine import _real_scenario


def _t2_state(workspace_id: str, *, include_dhi: bool = True, resolution: int = 15):
    state, old = _real_scenario(workspace_id, 3600, cadence_s=900)
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    stamps = [start + timedelta(minutes=resolution * i) for i in range(60 // resolution + 1)]
    channels = {"ghi": [500.0] * len(stamps), "dni": [700.0] * len(stamps)}
    if include_dhi:
        channels["dhi"] = [100.0] * len(stamps)
    profile = profiles.create_profile(
        workspace_id, name="T2 interval profile",
        timestamps=[stamp.isoformat().replace("+00:00", "Z") for stamp in stamps],
        channels=channels, resolution_minutes=resolution,
        provenance={"kind": "deterministic_test", "site_snapshot": {
            "latitude": 36.5, "longitude": -6.3, "elevation_m": 0.0, "timezone": "Europe/Madrid",
        }},
    )
    values = {
        "tubes_across": (2, "dimensionless"), "layers": (2, "dimensionless"),
        "pitch_h": (0.09, "m"), "pitch_v": (0.09, "m"), "stagger_offset": (0, "m"),
        "axis_azimuth": (0, "dimensionless"), "centre_height_above_surface": (0.1, "m"),
        "n_wall": (1.5, "dimensionless"), "n_water": (1.33, "dimensionless"),
        "surface_albedo": (0.06, "dimensionless"),
    }
    scenario = {
        **old.payload["scenario"],
        "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
        "start_utc": start.isoformat().replace("+00:00", "Z"),
        "end_utc": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "par_from_ghi_factor": 2.0,
    }
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetUnitParams(op="set_unit_params", unit="pbr", options={"optics_model": "t2"},
                      values={key: {"value": value, "unit": unit} for key, (value, unit) in values.items()}),
        SetScenario(op="set_scenario", id="run", value=scenario),
    ])
    return state


def test_t2_prepare_and_run_expose_optics_without_changing_t1() -> None:
    workspace_id = new_workspace()
    state = _t2_state(workspace_id)
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert snapshot.payload["units"][0]["optics_t2"]["provenance"]["model"] == "t2"
    # Dynamic jobs persist the snapshot as JSON before their worker reloads it.
    restored = json.loads(json.dumps(snapshot.payload, allow_nan=False))
    result = dynamic_engine.run(dynamic_engine.Snapshot(payload=restored, digest=snapshot.digest),
                                cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded", result.error
    assert result.manifest["fidelity"] == "T2"
    assert result.manifest["units"][0]["optics"]["version"] == "optics_t2.v1"
    intercepted = result.series["PBR_par_intercepted_umol_s_m"]
    absorbed = result.series["PBR_par_absorbed_umol_s_m"]
    assert np.all(intercepted > 0)
    assert np.all((absorbed >= 0) & (absorbed <= intercepted))
    assert np.all(result.series["PBR_par_eq_umol_m2_s"] > 0)


def test_t2_missing_dhi_is_typed_refusal() -> None:
    workspace_id = new_workspace()
    state = _t2_state(workspace_id, include_dhi=False)
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert exc.value.code == "OPTICS_CHANNELS_MISSING"


def test_t2_hourly_profile_is_typed_refusal() -> None:
    workspace_id = new_workspace()
    state = _t2_state(workspace_id, resolution=60)
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert exc.value.code == "OPTICS_RESOLUTION"


def test_switching_back_to_t1_restores_legacy_unit_payload() -> None:
    original = pbr_document(model=True)
    entered = draft.apply_ops(original, [SetUnitParams(
        op="set_unit_params", unit="pbr", options={"optics_model": "t2"},
        values={"n_wall": DraftQuantity(value=1.5, unit="dimensionless")},
    )])
    restored = draft.apply_ops(entered, [SetUnitParams(
        op="set_unit_params", unit="pbr", options={"optics_model": "t1"},
    )])
    assert restored["objects"]["pbr"] == original["objects"]["pbr"]
    with pytest.raises(draft.DraftError) as exc:
        draft.apply_ops(restored, [SetUnitParams(
            op="set_unit_params", unit="pbr",
            values={"n_wall": DraftQuantity(value=1.5, unit="dimensionless")},
        )])
    assert exc.value.code == "param_inactive"


def test_legacy_surface_aliases_and_conflicts() -> None:
    original = pbr_document(model=True)
    old = {"centre_height_above_sea": DraftQuantity(value=0.1, unit="m"),
           "sea_albedo": DraftQuantity(value=0.06, unit="dimensionless")}
    entered = draft.apply_ops(original, [SetUnitParams(
        op="set_unit_params", unit="pbr", options={"optics_model": "t2"}, values=old,
    )])
    params = entered["objects"]["pbr"]["params"]
    assert params["centre_height_above_surface"]["si"] == 0.1
    assert params["surface_albedo"]["si"] == 0.06
    assert not old.keys() & params.keys()
    with pytest.raises(draft.DraftError, match="Conflicting values"):
        draft.apply_ops(entered, [SetUnitParams(
            op="set_unit_params", unit="pbr", values={**old,
                "surface_albedo": DraftQuantity(value=0.2, unit="dimensionless")},
        )])


def test_legacy_stored_optics_matches_current() -> None:
    values = {"tubes_across": 2, "layers": 2, "pitch_h": 0.09, "pitch_v": 0.09,
              "stagger_offset": 0, "axis_azimuth": 0, "centre_height_above_surface": 0.1,
              "n_wall": 1.5, "n_water": 1.33, "surface_albedo": 0.06}
    item = {"tag": "PBR", "unit": {"params": {k: {"si": v} for k, v in values.items()}},
            "params": {"tube_inner_diameter": 0.06, "tube_count": 100}}
    current = dynamic_engine.t2_optics_for_unit(item)
    params = item["unit"]["params"]
    for old, new in draft.T2_PARAMETER_ALIASES.items():
        params[old] = params.pop(new)
    assert dynamic_engine.t2_optics_for_unit(item) == current


def test_legacy_revision_preserves_digest_and_reads_current_names(tmp_path) -> None:
    document = pbr_document(model=True)
    params = document["objects"]["pbr"]["params"]
    values = ({"value": 0.1, "unit": "m", "si": 0.1},
              {"value": 0.06, "unit": "dimensionless", "si": 0.06})
    for (old, _new), value in zip(draft.T2_PARAMETER_ALIASES.items(), values, strict=True):
        params[old] = value
    record = draft._write_revision(tmp_path, document, parent=None, actor="test", ops=[])
    loaded = draft.load_revision(tmp_path, record["revision"])
    assert draft._digest(loaded["document"]) == loaded["document_sha256"]
    displayed = next(item for item in draft._display(loaded["document"]) if item["id"] == "pbr")
    for (old, new), value in zip(draft.T2_PARAMETER_ALIASES.items(), values, strict=True):
        assert loaded["document"]["objects"]["pbr"]["params"][old] == value
        assert displayed["params"][new] == value
        assert old not in displayed["params"]


def test_legacy_revision_keeps_existing_dynamic_run_current(tmp_path, monkeypatch) -> None:
    workspace_id, draft_id = "workspace", "draft"
    directory = tmp_path
    monkeypatch.setattr(draft, "draft_dir", lambda *_args: directory)
    document = pbr_document(model=True)
    params = document["objects"]["pbr"]["params"]
    for old, new in draft.T2_PARAMETER_ALIASES.items():
        params[old] = {"value": 0.1, "unit": "dimensionless", "si": 0.1}
        params.pop(new, None)
    legacy = draft._write_revision(directory, document, parent=None, actor="test", ops=[])
    bound = draft.content_digest(document)
    assert dynamic_jobs._current(workspace_id, draft_id, {"identity": {"content_digest": bound}})
    loaded = draft.load_revision(directory, legacy["revision"])
    assert loaded["document_sha256"] == draft._digest(loaded["document"])
    assert draft.current_digest(workspace_id, draft_id) == bound
