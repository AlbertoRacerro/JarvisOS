"""AI-prepared Engineering Studio example workspace (spec 209).

One explicit, idempotent action creates a dedicated workspace and drives the ordinary Studio services to
create, test and publish four artifacts: a pipe hydraulic calculation, a parametric geometry, a custom process
unit and a procedure tested against an example culture-loop Process draft. Every expected value below was
computed independently of the artifact source (closed form or a separate solver; the spec 209 tests recompute
them). An artifact that already exists by name is never touched, so operator edits survive a repeated request
and an interrupted build resumes with only the missing artifacts.
"""

import math
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.modules.studio import service
from app.modules.studio.geometry import GeometryCreate, GeometryTestRequest
from app.modules.studio.models import StudioCreate, StudioError, StudioUpdate, TestRequest
from app.modules.studio.procedure import ProcedureCreate, ProcedureTestRequest
from app.modules.studio.unit_models import StudioUnitCreate, UnitTestRequest
from app.modules.studio_runtime.runtime import status
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace, get_workspace_by_slug

SLUG = "engineering-studio-example"
WORKSPACE_NAME = "Engineering Studio example"
DRAFT_NAME = "Example culture loop"
SCENARIO_ID = "harvest-check"
SCHEDULE_ID = "harvest"
PLACEHOLDER = '    raise NotImplementedError("Complete evaluate before testing")'
GEOMETRY_PLACEHOLDER = '    raise NotImplementedError("Complete build before testing")'
AI_NOTE = ("AI-prepared by the JarvisOS coordinator as a Studio demonstrator (spec 209). Test expectations were "
           "computed independently of this source. Scientifically unqualified.")
_LOCK = threading.Lock()


def _var(name: str, label: str, unit: str, description: str, low: float | None = None,
         high: float | None = None) -> dict[str, Any]:
    return {"name": name, "label": label, "unit": unit, "description": description,
            **({"min": low} if low is not None else {}), **({"max": high} if high is not None else {})}


# ---------------------------------------------------------------- 1. pipe hydraulic calculation

PIPE_NAME = "Pipe pressure drop (Darcy-Weisbach)"
PIPE = {
    "name": PIPE_NAME,
    "engineering_question": "What pressure drop and head loss does a liquid flow cause in a straight circular pipe?",
    "inputs": [
        _var("flow_rate", "Volumetric flow", "m**3/h", "Liquid volumetric flow rate", 1e-6, 1e4),
        _var("diameter", "Inner diameter", "mm", "Pipe inner diameter", 1, 2000),
        _var("length", "Pipe length", "m", "Straight pipe length", 0.01, 1e5),
        _var("roughness", "Absolute roughness", "mm", "Wall absolute roughness", 0, 10),
        _var("density", "Density", "kg/m**3", "Liquid density", 1, 3000),
        _var("viscosity", "Dynamic viscosity", "mPa*s", "Liquid dynamic viscosity", 0.01, 1e5),
    ],
    "outputs": [
        _var("velocity", "Mean velocity", "m/s", "Mean velocity over the bore"),
        _var("reynolds", "Reynolds number", "dimensionless", "Reynolds number from the inner diameter"),
        _var("friction_factor", "Darcy friction factor", "dimensionless",
             "64/Re below Re 2300, Colebrook-White at and above"),
        _var("pressure_drop", "Pressure drop", "kPa", "Frictional pressure drop over the length"),
        _var("head_loss", "Head loss", "m", "Frictional head loss, g = 9.80665 m/s2"),
    ],
}
PIPE_BODY = '''    q = flow_rate / 3600.0
    d = diameter / 1000.0
    mu = viscosity / 1000.0
    velocity = q / (math.pi * d * d / 4.0)
    reynolds = density * velocity * d / mu
    if reynolds < 2300.0:
        friction = 64.0 / reynolds
    else:
        relative = roughness / 1000.0 / d
        # Swamee-Jain start, then Colebrook-White fixed point on 1/sqrt(f).
        x = -2.0 * math.log10(relative / 3.7 + 5.74 / reynolds ** 0.9)
        for _ in range(100):
            following = -2.0 * math.log10(relative / 3.7 + 2.51 * x / reynolds)
            if abs(following - x) < 1e-14:
                x = following
                break
            x = following
        friction = 1.0 / (x * x)
    pressure_drop = friction * length / d * density * velocity ** 2 / 2.0
    return {"velocity": velocity, "reynolds": reynolds, "friction_factor": friction,
            "pressure_drop": pressure_drop / 1000.0, "head_loss": pressure_drop / (density * 9.80665)}'''
PIPE_ASSUMPTIONS = (AI_NOTE + " Fully developed, incompressible, isothermal single-phase flow in a straight full "
                    "circular pipe; no fittings, elevation change or entrance effects. Laminar 64/Re below Re 2300; "
                    "Colebrook-White at and above, so the 2300-4000 transition band is uncertain.")
# Expected values from an independent bracketing root solve of Colebrook-White (scipy brentq).
PIPE_CASES = [
    {"inputs": {"flow_rate": 10.0, "diameter": 50.0, "length": 100.0, "roughness": 0.045, "density": 998.2,
                "viscosity": 1.002},
     "expected_outputs": {"velocity": 1.41471060526, "reynolds": 70467.2717651, "friction_factor": 0.0226941875692,
                          "pressure_drop": 45.3385288457, "head_loss": 4.6315801379}, "tolerance": 1e-6},
    {"inputs": {"flow_rate": 2.0, "diameter": 25.0, "length": 10.0, "roughness": 0.0015, "density": 998.2,
                "viscosity": 1.002},
     "expected_outputs": {"velocity": 1.13176848421, "reynolds": 28186.908706, "friction_factor": 0.0239880275992,
                          "pressure_drop": 6.13419098508, "head_loss": 0.626641354537}, "tolerance": 1e-6},
    {"inputs": {"flow_rate": 0.5, "diameter": 50.0, "length": 20.0, "roughness": 0.045, "density": 870.0,
                "viscosity": 100.0},
     "expected_outputs": {"velocity": 0.0707355302631, "reynolds": 30.7699556644, "friction_factor": 2.07995099824,
                          "pressure_drop": 1.81082957473, "head_loss": 0.212245089229}, "tolerance": 1e-6},
]

# ---------------------------------------------------------------- 2. parametric geometry

SPOOL_NAME = "Flanged tube spool"
SPOOL = {
    "category": "geometry", "name": SPOOL_NAME,
    "engineering_question": "What does a flanged photobioreactor tube spool of given size look like and weigh?",
    "inputs": [
        _var("tube_od", "Tube outer diameter", "mm", "Tube outer diameter", 10, 500),
        _var("wall", "Wall thickness", "mm", "Tube and flange bore wall", 0.5, 50),
        _var("length", "Spool length", "mm", "Overall length, flange face to flange face", 20, 5000),
        _var("flange_od", "Flange outer diameter", "mm", "Flange outer diameter", 15, 1000),
        _var("flange_thickness", "Flange thickness", "mm", "Thickness of each flange", 1, 100),
    ],
}
SPOOL_BODY = '''    bore = tube_od - 2.0 * wall
    if bore <= 0 or flange_od <= tube_od or 2.0 * flange_thickness >= length:
        raise ValueError("Needs a positive bore, flanges wider than the tube and flanges shorter than the spool.")
    tube = bd.Cylinder(tube_od / 2.0, length) - bd.Cylinder(bore / 2.0, length)
    flange = bd.Cylinder(flange_od / 2.0, flange_thickness) - bd.Cylinder(bore / 2.0, flange_thickness)
    offset = (length - flange_thickness) / 2.0
    return tube + bd.Pos(0, 0, offset) * flange + bd.Pos(0, 0, -offset) * flange'''
SPOOL_ASSUMPTIONS = (AI_NOTE + " Plain slip-on flanges without bolt holes or gasket faces; the flanges sit flush "
                     "with the spool ends. Volume bounds are the analytic annulus volume +/-0.1%.")
SPOOL_INPUTS = [
    {"tube_od": 60.0, "wall": 3.0, "length": 300.0, "flange_od": 120.0, "flange_thickness": 12.0},
    {"tube_od": 40.0, "wall": 2.0, "length": 150.0, "flange_od": 90.0, "flange_thickness": 10.0},
]


def spool_volume(tube_od: float, wall: float, length: float, flange_od: float, flange_thickness: float) -> float:
    """Analytic solid volume in mm3 (annular tube between the flanges plus two annular flanges)."""
    bore = tube_od - 2.0 * wall
    return (math.pi / 4.0 * (tube_od ** 2 - bore ** 2) * (length - 2.0 * flange_thickness)
            + 2.0 * math.pi / 4.0 * (flange_od ** 2 - bore ** 2) * flange_thickness)


def _spool_cases() -> list[dict[str, Any]]:
    return [{"inputs": values, "volume_min": spool_volume(**values) * 0.999,
             "volume_max": spool_volume(**values) * 1.001,
             "max_extent": max(values["length"], values["flange_od"]) * 1.001} for values in SPOOL_INPUTS]


# ---------------------------------------------------------------- 3. custom process unit

SETTLER_NAME = "Harvest settler"
SETTLER = {
    "category": "process_unit", "name": SETTLER_NAME,
    "engineering_question": "How does a gravity settler split culture biomass between concentrate and clarified water?",
    "inlets": [{"name": "feed", "label": "Culture feed"}],
    "outlets": [{"name": "concentrate", "label": "Biomass concentrate"},
                {"name": "clarified", "label": "Clarified water"}],
    "parameters": [
        _var("biomass_recovery", "Biomass recovery", "percent", "Share of feed biomass leaving in the concentrate",
             0, 100),
        _var("concentrate_share", "Concentrate share", "percent", "Share of feed mass flow leaving as concentrate",
             1, 99),
    ],
    "culture_generation": [],
}
SETTLER_BODY = '''    feed = inlets["feed"]
    m = feed["mass_flow_kg_s"]
    share = params["concentrate_share"] / 100.0
    recovery = params["biomass_recovery"] / 100.0
    flows = {"concentrate": share * m, "clarified": m - share * m}
    split = {"concentrate": recovery, "clarified": 1.0 - recovery}
    outlets = {}
    for port, flow in flows.items():
        culture = {}
        for name, value in feed["culture"].items():
            if name == "biomass" and value is not None and flow > 0:
                culture[name] = split[port] * m * value / flow
            else:
                culture[name] = value  # dissolved species and pH leave at the feed concentration
        outlets[port] = {"mass_flow_kg_s": flow, "temperature_K": feed["temperature_K"],
                         "pressure_Pa": feed["pressure_Pa"], "mass_fractions": dict(feed["mass_fractions"]),
                         "culture": culture}
    return {"outlets": outlets}'''
SETTLER_ASSUMPTIONS = (AI_NOTE + " Steady, isothermal and isobaric ideal split: biomass partitions by the recovery, "
                       "dissolved culture species and the liquid composition leave both outlets unchanged. No "
                       "settling kinetics or energy balance.")
_FEED = {"mass_flow_kg_s": 1.0, "temperature_K": 298.15, "pressure_Pa": 101325.0, "mass_fractions": {"Water": 1.0},
         "culture": {"biomass": 0.0005, "nitrogen": 0.00005}}
SETTLER_CASES = [
    {"inlets": {"feed": _FEED}, "params": {"biomass_recovery": 90.0, "concentrate_share": 10.0},
     "expected_outlets": {"concentrate": {"mass_flow_kg_s": 0.1, "temperature_K": 298.15, "pressure_Pa": 101325.0},
                          "clarified": {"mass_flow_kg_s": 0.9, "temperature_K": 298.15, "pressure_Pa": 101325.0}},
     "tolerance": 1e-9},
    {"inlets": {"feed": {**_FEED, "mass_flow_kg_s": 2.5}}, "params": {"biomass_recovery": 0.0, "concentrate_share": 40.0},
     "expected_outlets": {"concentrate": {"mass_flow_kg_s": 1.0}, "clarified": {"mass_flow_kg_s": 1.5}},
     "tolerance": 1e-9},
]

# ---------------------------------------------------------------- 4. procedure and its culture loop

TANK_M3 = 0.2
TUBE_LENGTH_M, TUBE_DIAMETER_M, TUBE_COUNT = 10.0, 0.05, 10.0
O2_SAT = 0.008
INITIAL = {"X": 0.4, "N": 0.05, "O2": O2_SAT}
EVENT_S = 3600
PROCEDURE_NAME = "Harvest cycle"
PROCEDURE = {
    "category": "procedure", "name": PROCEDURE_NAME,
    "engineering_question": "What does one draw, settle, refill and dose harvest cycle leave in the culture tank?",
    "parameters": [_var("draw_fraction", "Draw fraction", "percent", "Share of the loop volume drawn", 0, 50)],
    "steps": [
        {"label": "Draw and settle", "time_s": EVENT_S, "actions": [
            {"type": "draw", "tank": "TankA", "loop_fraction": {"$param": "draw_fraction"}},
            {"type": "separate", "recovery": 0.95, "concentration_factor": 20.0, "return_to": "TankA"}]},
        {"label": "Refill with medium", "time_s": EVENT_S, "actions": [
            {"type": "refill", "tank": "TankA", "to_volume_m3": TANK_M3, "medium": {"N": 0.5, "O2": O2_SAT}}]},
        {"label": "Dose nitrogen", "time_s": EVENT_S, "actions": [
            {"type": "dose", "tank": "TankA", "nitrogen_kg": 0.004}]},
    ],
}
PROCEDURE_ASSUMPTIONS = (AI_NOTE + " The verification scenario runs the example loop dark with no biomass loss, so "
                         "every post-event tank state has a closed form; growth scenarios are left to the bench.")
# Tank state after each event (closed-form mixing arithmetic for a 20 % draw).
PROCEDURE_EXPECTED = [
    {"V": 0.196234679361931, "X": 0.254172376960219, "N": 0.05, "O2": O2_SAT},
    {"V": 0.2, "X": 0.249387174477242, "N": 0.0584719714356551, "O2": O2_SAT},
    {"V": 0.2, "X": 0.249387174477242, "N": 0.0784719714356551, "O2": O2_SAT},
]
_CHANNELS = {"V": "TankA_V_m3", "X": "TankA_X", "N": "TankA_N", "O2": "TankA_O2"}


def _procedure_cases(draft_id: str) -> list[dict[str, Any]]:
    expect: list[dict[str, Any]] = [
        {"kind": "series", "channel": _CHANNELS[key], "time_s": EVENT_S, "phase": "post", "event_order": order,
         "value": state[key], "tolerance": 1e-10, "relative": True}
        for order, state in enumerate(PROCEDURE_EXPECTED) for key in _CHANNELS]
    expect += [{"kind": "series", "channel": "PBR_X", "time_s": EVENT_S, "phase": "post", "event_order": 2,
                "value": INITIAL["X"], "tolerance": 1e-10, "relative": True}, {"kind": "balances_closed"}]
    return [{"name": "20 % draw, dark check", "parameter_values": {"draw_fraction": 20.0}, "draft_id": draft_id,
             "scenario_id": SCENARIO_ID, "schedule_id": SCHEDULE_ID, "simulate": True, "expect": expect}]


def _culture_loop(workspace_id: str) -> str:
    """Pump -> PBR -> TankA -> Pump with a dark, loss-free verification scenario; returns the draft id."""
    from app.modules.bio_models import service as bio_models
    from app.modules.environment import profiles
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        DraftQuantity,
        SetScenario,
        SetSchedule,
        SetThermo,
        SetUnitModel,
        SetUnitParams,
        UnitModelPin,
    )

    def q(value: float, unit: str) -> DraftQuantity:
        return DraftQuantity(value=value, unit=unit)

    hours = 6
    start = datetime(2026, 6, 1, tzinfo=UTC)
    stamps = [(start + timedelta(hours=i)).isoformat().replace("+00:00", "Z") for i in range(hours + 1)]
    profile = profiles.create_profile(workspace_id, name="Example constant light", timestamps=stamps,
                                      channels={"par": [500.0] * len(stamps)}, resolution_minutes=60,
                                      provenance={"kind": "studio_demonstrator"})
    parameter_set = bio_models.create_set(workspace_id, "Example culture parameters")
    for symbol, value, unit in (
        ("K_I", 150.0, "umol/(m**2*s)"), ("K_j_0", 0.001, "kg/m3"), ("k_d", 0.0, "1/hour"),
        ("a", 1.8, "1"), ("b", 0.5, "1"), ("c", 0.1, "1"), ("d", 0.01, "1"), ("w_ash", 0.05, "1"),
        ("k_X", 150.0, "m**2/kg"), ("T_min", 278.15, "K"), ("T_opt", 298.15, "K"), ("T_max", 318.15, "K"),
    ):
        parameter_set = bio_models.edit_set_value(
            workspace_id, parameter_set["id"], symbol, {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    card = bio_models.create_card(
        workspace_id, "Example culture card", parameter_set["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
         "nutrients": ["nutrient.monod"], "combination": "combine.liebig", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"})
    circulation = 5e-4
    velocity = circulation / (TUBE_COUNT * math.pi * TUBE_DIAMETER_M ** 2 / 4.0)
    pbr = {"tube_inner_diameter": q(TUBE_DIAMETER_M, "m"), "tube_length": q(TUBE_LENGTH_M, "m"),
           "tube_count": q(TUBE_COUNT, "dimensionless"), "liquid_velocity": q(velocity, "m/s"),
           "pump_efficiency": q(60.0, "percent"), "baffle_friction_multiplier": q(1.5, "dimensionless"),
           "oxygen_kla": q(20.0, "1/h"), "oxygen_saturation": q(8.0, "mg/L"), "peak_par": q(1500.0, "umol/(m2.s)"),
           "photoperiod": q(12.0, "h"), "diffuse_fraction": q(0.2, "dimensionless"),
           "temperature_mean": q(25.0, "degC"), "temperature_amplitude": q(5.0, "K")}
    ops: list[Any] = [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1", tag="PBR", x=100, y=0),
        SetUnitParams(op="set_unit_params", unit="pbr", values=pbr),
        SetUnitModel(op="set_unit_model", unit="pbr", model=UnitModelPin(
            card_id=card["id"], card_revision=card["revision"], card_digest=card["digest"])),
        AddUnit(op="add_unit", id="tanka", type="HoldupTank", tag="TankA", x=300, y=0),
        SetUnitParams(op="set_unit_params", unit="tanka", mode="dynamic", values={
            "liquid_volume": q(TANK_M3, "m3"), "min_volume": q(0.0, "m3"), "max_volume": q(TANK_M3, "m3"),
            "temperature": q(298.15, "K")}),
        AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=0, y=100),
    ]
    for k, (source, target) in enumerate((("pbr", "tanka"), ("tanka", "pump"), ("pump", "pbr"))):
        ops += [AddStream(op="add_stream", id=f"s{k}", tag=f"S{k}", x=50 * k, y=300),
                Connect(op="connect", stream=f"s{k}", end="source", unit=source, port=0),
                Connect(op="connect", stream=f"s{k}", end="target", unit=target, port=0)]
    ops += [
        SetSchedule(op="set_schedule", id=SCHEDULE_ID, value={"events": []}),
        SetScenario(op="set_scenario", id=SCENARIO_ID, value={
            "units": ["PBR", "TankA"], "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
            "start_utc": stamps[0], "end_utc": stamps[-1], "output_cadence_s": 1800, "rtol": 1e-10, "atol": 1e-13,
            "temperature_source": "unit_mean", "par_scale": 0.0, "circulation": {"Pump": circulation},
            "initial": {tag: dict(INITIAL) for tag in ("PBR", "TankA")}, "schedule_id": SCHEDULE_ID}),
    ]
    state = draft.create_draft(workspace_id, DRAFT_NAME)
    return draft.patch(workspace_id, state["draft_id"], state["revision"], ops)["draft_id"]


# ---------------------------------------------------------------- build


def _publish(workspace_id: str, model: dict[str, Any], update: StudioUpdate,
             cases: TestRequest | GeometryTestRequest | UnitTestRequest | ProcedureTestRequest) -> None:
    service.update(workspace_id, model["id"], update)
    report = service.test(workspace_id, model["id"], cases)
    if not report["passed"]:
        errors = [error for case in report["cases"] for error in case.get("errors", [])]
        raise StudioError("studio_demonstrator_failed", f"{model['name']} did not pass its verification tests.",
                          {"model_spec_id": model["id"], "errors": errors[:8]}, status=500)
    service.publish(workspace_id, model["id"])


def _source(model: dict[str, Any], placeholder: str, body: str, header: str = "") -> str:
    return header + model["source"].replace(placeholder, body)


def ensure_example() -> dict[str, Any]:
    """Create the example workspace or complete a missing part; existing artifacts are never modified."""
    runtime = status()
    if not runtime["available"]:
        raise StudioError("studio_runtime_unavailable",
                          "The Studio runtime is not available, so the example artifacts cannot be verified.",
                          {"reason": runtime["reason"]}, status=503)
    with _LOCK:
        workspace = get_workspace_by_slug(SLUG)
        created = workspace is None
        if workspace is None:
            workspace = create_workspace(WorkspaceCreate(
                name=WORKSPACE_NAME, slug=SLUG,
                description="AI-prepared Studio demonstrator: pipe hydraulics, flanged spool geometry, harvest "
                            "settler unit and harvest-cycle procedure. Scientifically unqualified."))
        wid = workspace.id
        existing = {model["name"] for model in service.list_models(wid)}
        added: list[str] = []
        if PIPE_NAME not in existing:
            model = service.create(wid, StudioCreate.model_validate(PIPE))
            _publish(wid, model, StudioUpdate(source=_source(model, PLACEHOLDER, PIPE_BODY, "import math\n\n"),
                                              assumptions=PIPE_ASSUMPTIONS),
                     TestRequest.model_validate({"cases": PIPE_CASES}))
            added.append(PIPE_NAME)
        if SPOOL_NAME not in existing:
            model = service.create(wid, GeometryCreate.model_validate(SPOOL))
            _publish(wid, model, StudioUpdate(source=_source(model, GEOMETRY_PLACEHOLDER, SPOOL_BODY),
                                              assumptions=SPOOL_ASSUMPTIONS),
                     GeometryTestRequest.model_validate({"cases": _spool_cases()}))
            added.append(SPOOL_NAME)
        if SETTLER_NAME not in existing:
            model = service.create(wid, StudioUnitCreate.model_validate(SETTLER))
            _publish(wid, model, StudioUpdate(source=_source(model, PLACEHOLDER, SETTLER_BODY),
                                              assumptions=SETTLER_ASSUMPTIONS),
                     UnitTestRequest.model_validate({"cases": SETTLER_CASES}))
            added.append(SETTLER_NAME)
        if PROCEDURE_NAME not in existing:
            from app.modules.process_stack import draft

            loops = [row["draft_id"] for row in draft.list_drafts(wid) if row["name"] == DRAFT_NAME]
            draft_id = loops[0] if loops else _culture_loop(wid)
            model = service.create(wid, ProcedureCreate.model_validate(PROCEDURE))
            _publish(wid, model, StudioUpdate(assumptions=PROCEDURE_ASSUMPTIONS),
                     ProcedureTestRequest.model_validate({"cases": _procedure_cases(draft_id)}))
            added.append(PROCEDURE_NAME)
    return {"workspace": workspace.model_dump(), "created": created, "added": added}
