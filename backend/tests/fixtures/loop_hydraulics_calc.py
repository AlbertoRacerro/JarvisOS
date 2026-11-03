import json
import math

EXPECTED_UNITS = {
    "tube_length": "m",
    "tube_inner_diameter": "mm",
    "tube_outer_diameter": "mm",
    "reservoir_liquid_volume": "L",
    "target_liquid_velocity": "m/s",
    "liquid_density": "kg/m3",
    "dynamic_viscosity": "Pa*s",
    "minor_loss_coefficient": "1",
    "pump_efficiency": "1",
}


def fail(reason, field_name=None):
    message = "calc_error:" + reason
    if field_name is not None:
        message += ":" + field_name
    raise SystemExit(message)


with open("input.json", encoding="utf-8") as handle:
    inputs = json.load(handle)

if not isinstance(inputs, dict) or set(inputs) != set(EXPECTED_UNITS):
    fail("input_contract_invalid")

values = {}
for name, expected_unit in EXPECTED_UNITS.items():
    item = inputs.get(name)
    if not isinstance(item, dict):
        fail("input_contract_invalid", name)
    # source_parameter_updated_at is runner-owned provenance/CAS evidence. The
    # runner validates it before execution; the physical model deliberately
    # ignores it while accepting the normalized schema-v3 input envelope.
    allowed_keys = {"value", "unit", "source_parameter_id", "source_parameter_updated_at"}
    if "value" not in item or "unit" not in item or not set(item).issubset(allowed_keys):
        fail("input_contract_invalid", name)
    value = item["value"]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        fail("input_contract_invalid", name)
    if item["unit"] != expected_unit:
        fail("input_unit_invalid", name)
    values[name] = float(value)

positive_fields = (
    "tube_length",
    "tube_inner_diameter",
    "target_liquid_velocity",
    "liquid_density",
    "dynamic_viscosity",
)
for name in positive_fields:
    if values[name] <= 0:
        fail("input_domain_invalid", name)

if values["tube_outer_diameter"] < values["tube_inner_diameter"]:
    fail("input_domain_invalid", "tube_outer_diameter")
if values["reservoir_liquid_volume"] < 0:
    fail("input_domain_invalid", "reservoir_liquid_volume")
if values["minor_loss_coefficient"] < 0:
    fail("input_domain_invalid", "minor_loss_coefficient")
if values["pump_efficiency"] <= 0 or values["pump_efficiency"] > 1:
    fail("input_domain_invalid", "pump_efficiency")

tube_length = values["tube_length"]
diameter_inner = values["tube_inner_diameter"] / 1000.0
diameter_outer = values["tube_outer_diameter"] / 1000.0
reservoir_volume = values["reservoir_liquid_volume"] / 1000.0
velocity = values["target_liquid_velocity"]
density = values["liquid_density"]
viscosity = values["dynamic_viscosity"]
minor_loss_coefficient = values["minor_loss_coefficient"]
pump_efficiency = values["pump_efficiency"]

gravity = 9.80665
hydraulic_area = math.pi * diameter_inner**2 / 4.0
tube_volume = hydraulic_area * tube_length
total_inventory = tube_volume + reservoir_volume
external_area = math.pi * diameter_outer * tube_length
internal_area = math.pi * diameter_inner * tube_length
internal_area_to_volume = internal_area / tube_volume
external_area_to_volume = external_area / tube_volume
circulation_flow = velocity * hydraulic_area
tube_transit_time = tube_length / velocity
inventory_turnover_time = total_inventory / circulation_flow
reynolds_number = density * velocity * diameter_inner / viscosity

if reynolds_number < 2300.0:
    friction_factor = 64.0 / reynolds_number
    friction_correlation = "laminar_64_over_Re"
elif reynolds_number >= 4000.0 and reynolds_number <= 100000.0:
    friction_factor = 0.3164 * reynolds_number**-0.25
    friction_correlation = "blasius_smooth_pipe_v0"
else:
    fail("correlation_not_qualified")

dynamic_pressure = density * velocity**2 / 2.0
major_pressure_loss = friction_factor * (tube_length / diameter_inner) * dynamic_pressure
minor_pressure_loss = minor_loss_coefficient * dynamic_pressure
total_pressure_loss = major_pressure_loss + minor_pressure_loss
equivalent_static_head = total_pressure_loss / (density * gravity)
hydraulic_power = total_pressure_loss * circulation_flow
pump_electric_power = hydraulic_power / pump_efficiency

outputs = {
    "tube_hydraulic_cross_section_area": {"value": hydraulic_area, "unit": "m2"},
    "tube_liquid_volume": {"value": tube_volume, "unit": "m3"},
    "total_liquid_inventory": {"value": total_inventory, "unit": "m3"},
    "external_illuminated_area_proxy": {"value": external_area, "unit": "m2"},
    "internal_wetted_area_to_tube_volume": {"value": internal_area_to_volume, "unit": "1/m"},
    "external_area_to_tube_volume_proxy": {"value": external_area_to_volume, "unit": "1/m"},
    "circulation_flow_rate": {"value": circulation_flow, "unit": "m3/s"},
    "tube_nominal_transit_time": {"value": tube_transit_time, "unit": "s"},
    "total_inventory_turnover_time": {"value": inventory_turnover_time, "unit": "s"},
    "reynolds_number": {"value": reynolds_number, "unit": "1"},
    "darcy_friction_factor": {"value": friction_factor, "unit": "1"},
    "major_pressure_loss": {"value": major_pressure_loss, "unit": "Pa"},
    "minor_pressure_loss": {"value": minor_pressure_loss, "unit": "Pa"},
    "total_pressure_loss": {"value": total_pressure_loss, "unit": "Pa"},
    "equivalent_static_head": {"value": equivalent_static_head, "unit": "m"},
    "hydraulic_power": {"value": hydraulic_power, "unit": "W"},
    "pump_electric_power": {"value": pump_electric_power, "unit": "W"},
}

result = {
    "schema_version": 1,
    "status": "succeeded",
    "outputs": outputs,
    "diagnostics": {
        "model_id": "loop_hydraulics_v0",
        "model_fidelity": "M0_static_screening",
        "friction_factor_convention": "Darcy",
        "friction_correlation": friction_correlation,
        "circulation_semantics": "closed_loop_recirculation",
        "time_semantics": [
            "tube_nominal_transit_time",
            "total_inventory_turnover_time",
        ],
        "external_area_is_proxy": True,
        "pump_curve_not_applied": True,
        "npsh_not_evaluated": True,
        "transient_pressure_not_evaluated": True,
        "minor_loss_coefficient_provisional": True,
        "workbook_runtime_dependency": False,
    },
}

with open("result.json", "w", encoding="utf-8") as handle:
    json.dump(result, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
