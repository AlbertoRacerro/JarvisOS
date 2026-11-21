"""Static correlation and property cards exposed by the engineering bench."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.engineering import bench, bench_runs_view
from app.modules.engineering.evaluator_contracts import EvaluationRequest, NamedQuantity, OptionValue
from app.modules.engineering.refs import EvaluationRequestRef, MaterialStateRef, Quantity


class CardNumericField(BaseModel):
    name: str
    label: str
    unit: str
    default: float


class CardOption(BaseModel):
    name: str
    label: str
    choices: tuple[str, ...]
    default: str


class BenchCard(BaseModel):
    card_id: str
    evaluator_id: str
    title: str
    formula: str
    validity: str
    source: str
    fields: tuple[CardNumericField, ...]
    options: tuple[CardOption, ...] = ()


class CreateBenchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str
    inputs: dict[str, float] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)

    @field_validator("inputs", mode="before")
    @classmethod
    def strict_numeric_inputs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("inputs must be an object")
        for name, item in value.items():
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"input {name} must be a finite number")
            if not math.isfinite(float(item)):
                raise ValueError(f"input {name} must be a finite number")
        return value

    @field_validator("options", mode="before")
    @classmethod
    def strict_options(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("options must be an object")
        if any(not isinstance(name, str) or not isinstance(item, str) for name, item in value.items()):
            raise ValueError("option names and values must be strings")
        return value


class BenchCardCatalog(BaseModel):
    cards: tuple[BenchCard, ...]


class BenchRunDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    evaluator_id: str
    status: str
    verdict: str


CARDS: tuple[BenchCard, ...] = (
    BenchCard(
        card_id="fluids.pipe_pressure_drop",
        evaluator_id="fluids.pipe_pressure_drop",
        title="Perdita di carico in tubo",
        formula="Δp = f · (L/D) · (ρv²/2), con fattore di attrito Darcy",
        validity="Re < 2100 laminare oppure Re >= 4000 turbolento; rugosità relativa ε/D <= 0,05",
        source="fluids, soluzione di Clamond/Colebrook",
        fields=(
            CardNumericField(name="density", label="Densità", unit="kg/m3", default=997.0),
            CardNumericField(name="dynamic_viscosity", label="Viscosità dinamica", unit="Pa*s", default=0.00089),
            CardNumericField(name="velocity", label="Velocità", unit="m/s", default=1.0),
            CardNumericField(name="diameter", label="Diametro", unit="m", default=0.05),
            CardNumericField(name="length", label="Lunghezza", unit="m", default=10.0),
            CardNumericField(name="roughness", label="Rugosità", unit="m", default=0.000045),
        ),
    ),
    BenchCard(
        card_id="ht.internal_convection",
        evaluator_id="ht.internal_convection",
        title="Convezione interna turbolenta",
        formula="Nu = correlazione del metodo scelto; h = Nu·k/D",
        validity="Gnielinski: 3000 <= Re <= 5e6, 0,5 <= Pr <= 2000; Dittus-Boelter: Re >= 1e4, 0,6 <= Pr <= 160",
        source="Incropera & DeWitt / ht",
        fields=(
            CardNumericField(name="reynolds_number", label="Numero di Reynolds", unit="1", default=5000.0),
            CardNumericField(name="prandtl_number", label="Numero di Prandtl", unit="1", default=7.0),
            CardNumericField(name="thermal_conductivity", label="Conducibilità termica", unit="W/(m*K)", default=0.6),
            CardNumericField(name="diameter", label="Diametro", unit="m", default=0.05),
        ),
        options=(
            CardOption(
                name="method",
                label="Metodo",
                choices=("gnielinski", "dittus_boelter_heating", "dittus_boelter_cooling"),
                default="gnielinski",
            ),
        ),
    ),
    BenchCard(
        card_id="coolprop.pure_fluid_properties",
        evaluator_id="coolprop.pure_fluid_properties",
        title="Proprietà termofisiche di fluido puro",
        formula="ρ, μ, k e cp = PropsSI(T, p) da CoolProp",
        validity="Stato monofase entro i limiti EOS del fluido selezionato",
        source="CoolProp",
        fields=(
            CardNumericField(name="temperature", label="Temperatura", unit="K", default=298.15),
            CardNumericField(name="pressure", label="Pressione", unit="Pa", default=101325.0),
        ),
        options=(
            CardOption(
                name="fluid",
                label="Fluido",
                choices=("Water", "CO2", "Nitrogen", "Oxygen", "Air"),
                default="Water",
            ),
        ),
    ),
    BenchCard(
        card_id="marine.rigid_body_waves",
        evaluator_id="marine.rigid_body_waves",
        title="Moto rigido in onde e ormeggio",
        formula="X = ((K - ω²M) + iωB)^-1 F, con 3 DOF surge-heave-pitch disaccoppiati",
        validity="Risposta lineare di frequenza in onde regolari; ormeggio quasi-statico",
        source="Rigid-body frequency-domain / marine_motion",
        fields=(
            CardNumericField(name="mass_kg", label="Massa", unit="kg", default=500000.0),
            CardNumericField(name="added_mass_surge_kg", label="Massa aggiunta surge", unit="kg", default=50000.0),
            CardNumericField(name="added_mass_heave_kg", label="Massa aggiunta heave", unit="kg", default=150000.0),
            CardNumericField(name="pitch_inertia_kg_m2", label="Inerzia di pitch", unit="kg*m**2", default=5e8),
            CardNumericField(name="waterplane_area_m2", label="Area di galleggiamento", unit="m2", default=400.0),
            CardNumericField(name="gm_pitch_m", label="Metacentro GM pitch", unit="m", default=2.0),
            CardNumericField(name="damping_surge_n_s_m", label="Smorzamento surge", unit="N*s/m", default=500000.0),
            CardNumericField(name="damping_heave_n_s_m", label="Smorzamento heave", unit="N*s/m", default=800000.0),
            CardNumericField(name="damping_pitch_n_m_s_rad", label="Smorzamento pitch", unit="N*m*s/rad", default=2e8),
            CardNumericField(name="mooring_kx_n_m", label="Rigidezza ormeggio surge", unit="N/m", default=500000.0),
            CardNumericField(name="mooring_kz_n_m", label="Rigidezza ormeggio heave", unit="N/m", default=0.0),
            CardNumericField(name="wave_amplitude_m", label="Ampiezza d'onda", unit="m", default=1.0),
            CardNumericField(name="wave_period_s", label="Periodo d'onda", unit="s", default=8.0),
            CardNumericField(name="water_density_kg_m3", label="Densità dell'acqua", unit="kg/m3", default=1025.0),
            CardNumericField(name="gravity_m_s2", label="Accelerazione gravitazionale", unit="m/s2", default=9.80665),
        ),
    ),
    BenchCard(
        card_id="bioprocess.tube_oxygen_buildup",
        evaluator_id="bioprocess.tube_oxygen_buildup",
        title="Accumulo di ossigeno in tubo",
        formula="C_out = C_in + R·L/u; L_max = u·(C_max − C_in)/R, con C = f·C_sat",
        validity="Flusso a pistone, produzione di ossigeno costante, nessuno scambio gassoso nel tubo",
        source="Molina Grima et al. 2001 / oxygen_tube",
        fields=(
            CardNumericField(name="velocity", label="Velocità", unit="m/s", default=0.5),
            CardNumericField(name="oxygen_production_rate", label="Produzione di ossigeno", unit="mol/(m**3*s)", default=0.003),
            CardNumericField(name="saturation_concentration", label="Concentrazione di saturazione", unit="mol/m**3", default=0.25),
            CardNumericField(name="inlet_saturation_fraction", label="Frazione di saturazione in ingresso", unit="1", default=1.0),
            CardNumericField(name="max_saturation_fraction", label="Frazione di saturazione massima", unit="1", default=3.0),
            CardNumericField(name="length", label="Lunghezza", unit="m", default=50.0),
        ),
    ),
)

CARD_BY_ID = {card.card_id: card for card in CARDS}


def list_cards(workspace_id: str) -> dict[str, Any]:
    from app.modules.workspaces.service import get_workspace

    if not workspace_id or get_workspace(workspace_id) is None:
        raise LookupError("workspace_not_found")
    return {"cards": [card.model_dump(mode="json") for card in CARDS]}


def _validation_error(code: str, message: str) -> ValueError:
    return ValueError(code + ": " + message)


def run_card(workspace_id: str, payload: CreateBenchRunRequest) -> dict[str, Any]:
    from app.modules.workspaces.service import get_workspace

    if not workspace_id or get_workspace(workspace_id) is None:
        raise LookupError("workspace_not_found")

    card = CARD_BY_ID.get(payload.card_id)
    if card is None:
        raise _validation_error("unknown_card", "Scheda sconosciuta")

    field_names = {field.name for field in card.fields}
    if set(payload.inputs) != field_names:
        missing = sorted(field_names - set(payload.inputs))
        unknown = sorted(set(payload.inputs) - field_names)
        raise _validation_error("invalid_inputs", f"Campi mancanti={missing}; campi sconosciuti={unknown}")

    for name, value in payload.inputs.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _validation_error("invalid_input", f"Il valore {name} deve essere numerico")
        number = float(value)
        if not math.isfinite(number):
            raise _validation_error("invalid_input", f"Il valore {name} deve essere finito")

    option_names = {option.name: option for option in card.options}
    unknown_options = sorted(set(payload.options) - set(option_names))
    if unknown_options:
        raise _validation_error("unknown_option", f"Opzioni sconosciute: {unknown_options}")
    for name, option in option_names.items():
        opt_value = payload.options.get(name, option.default)
        if opt_value not in option.choices:
            raise _validation_error("unknown_option", f"Valore non valido per {name}")

    now = datetime.now(UTC)
    run_id = f"run_{uuid4().hex}"
    fields_by_name = {field.name: field for field in card.fields}
    inputs = tuple(
        NamedQuantity(
            name=name,
            value=Quantity(value=float(payload.inputs[name]), unit=fields_by_name[name].unit),
        )
        for name in sorted(field_names)
    )
    backend_options: dict[str, OptionValue] = dict(payload.options)
    subject = MaterialStateRef(
        authority_owner="correlation_cards",
        object_type="material_state",
        object_id="card-state",
        workspace_id=workspace_id,
        revision="1",
    )
    request = EvaluationRequest(
        request_ref=EvaluationRequestRef(
            authority_owner="correlation_cards",
            object_type="evaluation_request",
            object_id=f"req-{run_id}",
            workspace_id=workspace_id,
            revision="1",
        ),
        evaluator_id=card.evaluator_id,
        subject_ref=subject,
        inputs=inputs,
        backend_options=backend_options,
        requested_at=now,
        deadline_at=now + timedelta(minutes=5),
    )
    root = bench_runs_view.bench_root(workspace_id)
    out_dir = root / run_id
    bench.run_evaluation(card.evaluator_id, request.model_dump(mode="json"), out_dir=out_dir, root=root)
    return bench_runs_view.show(workspace_id, run_id)
