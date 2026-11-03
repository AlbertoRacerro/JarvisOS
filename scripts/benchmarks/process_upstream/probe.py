"""Offline, synthetic spec 103 qualification probes.

Run from the repository root with an isolated candidate venv (see
docs/specs/103-process-upstream-bakeoff-evidence.md). Local tool locations come
from Q103_FMU, Q103_SOLVER_LIB and Q103_JAVA_HOME. Each candidate gets an
independent result.
These probes measure numerical/API feasibility, not scientific validity.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
OUT = Path(__file__).resolve().parent / "results"
T = 298.15  # K; synthetic PBR screening point
P = 101325.0  # Pa


def coolprop():
    from CoolProp.CoolProp import PropsSI

    return {"water_density_kg_m3": PropsSI("D", "T", T, "P", P, "Water"),
            "water_viscosity_Pa_s": PropsSI("V", "T", T, "P", P, "Water"),
            "co2_density_kg_m3": PropsSI("D", "T", T, "P", P, "CO2"),
            "water_saturation_pressure_Pa": PropsSI("P", "T", T, "Q", 0, "Water")}


def chemicals():
    from chemicals.iapws import iapws95_rho
    from chemicals.vapor_pressure import Antoine

    # IAPWS density and Antoine water coefficients for T in degrees C, mmHg.
    return {"water_density_kg_m3": iapws95_rho(T, P),
            "water_saturation_pressure_Pa": Antoine(T-273.15, 8.07131, 1730.63, 233.426, base=10.0) * 133.322}


def thermo():
    from thermo import Chemical, Mixture

    water = Chemical("water", T=T, P=P)
    co2 = Chemical("carbon dioxide", T=T, P=P)
    brine = Mixture(["water", "sodium chloride"], ws=[0.965, 0.035], T=T, P=P)
    return {"water_density_kg_m3": water.rho, "co2_density_kg_m3": co2.rho,
            "brine_density_kg_m3": brine.rho, "water_phase": water.phase,
            "co2_phase": co2.phase, "brine_phase": brine.phase}


def fluids():
    from fluids.friction import friction_factor

    from app.modules.process_kernel.blocks import Pipe
    from app.modules.process_kernel.streams import MaterialStream

    density, mu, velocity, diameter, length = 997.0, 0.00089, 1.0, 0.05, 10.0
    incumbent = Pipe().solve({"inlet": MaterialStream("water", density_kg_m3=density,
                                dynamic_viscosity_Pa_s=mu)}, {},
                             {"tube_length": length, "tube_inner_diameter": diameter * 1000,
                              "tube_outer_diameter": 60.0, "target_liquid_velocity": velocity}, {})
    old = incumbent.scalar_outputs
    re = density * velocity * diameter / mu
    new_f = friction_factor(Re=re, eD=0.0)
    new_dp = new_f * length / diameter * density * velocity**2 / 2
    return {"reynolds_number": re, "friction_factor_darcy": new_f,
            "pressure_loss_Pa": new_dp, "incumbent_friction_factor_darcy": old["friction_factor"],
            "incumbent_pressure_loss_Pa": old["major_pressure_loss"],
            "pressure_loss_relative_difference": (new_dp-old["major_pressure_loss"])/old["major_pressure_loss"]}


def ht():
    from ht.conv_internal import turbulent_Dittus_Boelter

    re, pr, conductivity, diameter = 56011.0, 6.2, 0.6, 0.05
    nu = turbulent_Dittus_Boelter(re, pr)
    return {"nusselt": nu, "heat_transfer_coefficient_W_m2_K": nu * conductivity / diameter,
            "incumbent": "047 has no heat-transfer correlation"}


def thermosteam():
    import thermosteam as tmo

    tmo.settings.set_thermo(["Water", "CO2"])
    stream = tmo.Stream("water_co2", Water=100, CO2=1, units="kg/hr", T=T, P=P)
    return {"phase": stream.phase, "mass_flow_kg_hr": stream.F_mass,
            "water_mass_flow_kg_hr": stream.imass["Water"], "co2_mass_flow_kg_hr": stream.imass["CO2"]}


def biosteam():
    import biosteam as bst

    bst.settings.set_thermo(["Water", "CO2"])
    fresh = bst.Stream("fresh103", Water=100, units="kg/hr")
    recycle = bst.Stream("recycle103")
    mixer = bst.Mixer("mix103", ins=(fresh, recycle))
    unit = bst.Mixer("unit103", ins=(mixer-0,))  # identity process unit for mass closure
    splitter = bst.Splitter("split103", ins=unit-0, outs=("to_separator103", recycle), split=0.2)
    separator = bst.Splitter("separator103", ins=splitter-0,
                             outs=("product103", "purge103"), split=0.9)
    system = bst.System("recycle_system103", path=(mixer, unit, splitter, separator), recycle=recycle)
    system.simulate()
    closure = fresh.F_mass - separator.outs[0].F_mass - separator.outs[1].F_mass
    assert abs(closure) < 1e-5
    return {"fresh_kg_hr": fresh.F_mass, "mixed_kg_hr": mixer.outs[0].F_mass,
            "product_kg_hr": separator.outs[0].F_mass, "purge_kg_hr": separator.outs[1].F_mass,
            "recycle_kg_hr": recycle.F_mass, "mass_balance_error_kg_hr": closure,
            "expected_recycle_kg_hr": 400.0}


def qsdsan():
    import qsdsan as qs

    # Package-specific stream construction, avoiding global flowsheet/TEA state.
    s = qs.WasteStream("synthetic_water103", H2O=100, units="kg/hr")
    return {"water_mass_flow_kg_hr": s.imass["H2O"], "total_mass_flow_kg_hr": s.F_mass}


def pyomo():
    import pyomo.environ as pyo

    available = {name: bool(pyo.SolverFactory(name).available(exception_flag=False))
                 for name in ("ipopt", "appsi_ipopt", "glpk", "cbc", "highs")}
    if not any(available.values()):
        raise RuntimeError(f"No local Pyomo solver available: {available}")
    model = pyo.ConcreteModel()
    model.recycle = pyo.Var(initialize=400, bounds=(0, 1000))
    model.mixed = pyo.Var(initialize=500, bounds=(0, 2000))
    model.product = pyo.Var(initialize=90, bounds=(0, 200))
    model.purge = pyo.Var(initialize=10, bounds=(0, 200))
    model.mix_balance = pyo.Constraint(expr=model.mixed == 100 + model.recycle)
    model.recycle_split = pyo.Constraint(expr=model.recycle == 0.8 * model.mixed)
    model.separator_product = pyo.Constraint(expr=model.product == 0.18 * model.mixed)
    model.separator_purge = pyo.Constraint(expr=model.purge == 0.02 * model.mixed)
    model.obj = pyo.Objective(expr=(model.recycle - 400)**2)
    solver = next(name for name, ok in available.items() if ok)
    result = pyo.SolverFactory(solver).solve(model)
    closure = 100 - pyo.value(model.product) - pyo.value(model.purge)
    assert str(result.solver.termination_condition) == "optimal" and abs(closure) < 1e-5
    return {"solver": solver, "termination": str(result.solver.termination_condition),
            "recycle_kg_hr": pyo.value(model.recycle), "mixed_kg_hr": pyo.value(model.mixed),
            "product_kg_hr": pyo.value(model.product), "purge_kg_hr": pyo.value(model.purge),
            "mass_balance_error_kg_hr": closure, "solvers_available": available}


def idaes():
    os.environ.setdefault("IDAES_DATA", "/tmp/q103-idaes")
    import idaes
    import pyomo.environ as pyo

    available = pyo.SolverFactory("ipopt").available(exception_flag=False)
    if not available:
        raise RuntimeError("IDAES IPOPT unavailable locally; idaes get-extensions requires network")
    return {"idaes_version": idaes.__version__, "ipopt_available": available,
            "equation_recycle_owner": "pyomo probe; IDAES process model not solved"}


def watertap():
    import pyomo.environ as pyo
    import watertap

    available = pyo.SolverFactory("ipopt").available(exception_flag=False)
    if not available:
        raise RuntimeError("WaterTAP flowsheet solve unavailable: IPOPT binary absent")
    return {"imported": watertap is not None, "ipopt_available": available}


def rhs(t, y):
    light = max(0.0, math.sin(2 * math.pi * (t % 24) / 24))
    return (0.08 * light - 0.025) * y


def sundae():
    import numpy as np
    from sksundae.cvode import CVODE
    from sksundae.ida import IDA

    def native_rhs(t, y, yp):
        yp[0] = rhs(t, y[0])

    t = np.linspace(0, 48, 97)
    result = CVODE(native_rhs, rtol=1e-9, atol=1e-11).solve(t, np.array([1.0]))
    def dae_residual(hour, y, yp, residual):
        residual[0] = yp[0] - rhs(hour, y[1])
        residual[1] = y[1] - y[0]  # algebraic instantaneous mixed inventory

    dae = IDA(dae_residual, algebraic_idx=[1], rtol=1e-8, atol=1e-10).solve(
        t, np.array([1.0, 1.0]), np.array([-0.025, 0.0]))
    return {"success": bool(result.success), "final_biomass_arbitrary": float(result.y[-1, 0]),
            "minimum_biomass_arbitrary": float(result.y[:, 0].min()), "time_points": len(result.t),
            "dae_success": bool(dae.success), "dae_final_biomass_arbitrary": float(dae.y[-1, 0]),
            "dae_max_algebraic_residual": float(np.max(np.abs(dae.y[:, 1]-dae.y[:, 0]))),
            "fixture": "synthetic periodic growth/loss ODE; not biology validation"}


def casadi():
    import casadi as ca

    x = ca.MX.sym("x")
    light = ca.MX.sym("light")
    # CasADi owns the symbolic RHS; this probe owns fixed-step integration time.
    f = ca.Function("step_rhs", [x, light], [(0.08 * light - 0.025) * x])
    z = ca.MX.sym("mixed_inventory")
    ida = ca.integrator("dae_step", "idas", {"x": x, "z": z, "p": light,
                                               "ode": (0.08*light-0.025)*z, "alg": z-x}, 0, 0.25)
    state = 1.0
    dae_state = 1.0
    dae_residual = 0.0
    dt = 0.25

    def illumination(hour):
        return max(0.0, math.sin(2 * math.pi * hour / 24))

    for step in range(192):
        t = step * dt
        k1 = float(f(state, illumination(t)))
        k2 = float(f(state + dt*k1/2, illumination(t+dt/2)))
        k3 = float(f(state + dt*k2/2, illumination(t+dt/2)))
        k4 = float(f(state + dt*k3, illumination(t+dt)))
        state += dt * (k1 + 2*k2 + 2*k3 + k4) / 6
        dae_step = ida(x0=dae_state, z0=dae_state, p=illumination(t+dt/2))
        dae_state = float(dae_step["xf"])
        dae_residual = max(dae_residual, abs(float(dae_step["zf"])-dae_state))
    return {"final_biomass_arbitrary": state, "symbolic_graph_nodes": int(ca.n_nodes(f(x, light))),
            "dae_final_biomass_arbitrary": dae_state, "dae_max_algebraic_residual": dae_residual,
            "method": "symbolic RHS with fixed-step RK4; IDAS DAE midpoint light; 0.25 hour steps"}


def dompc():
    import do_mpc
    import numpy as np

    model = do_mpc.model.Model("discrete")
    x = model.set_variable("_x", "inventory")
    u = model.set_variable("_u", "harvest")
    light = model.set_variable("_tvp", "light")
    model.set_rhs("inventory", x + (0.08*light - 0.025)*x - u)
    model.setup()
    mpc = do_mpc.controller.MPC(model)
    mpc.set_param(n_horizon=3, t_step=1, store_full_solution=False)
    mpc.set_objective(mterm=(x-1)**2, lterm=(x-1)**2)
    mpc.set_rterm(harvest=0.1)
    mpc.bounds["lower", "_u", "harvest"] = 0
    mpc.bounds["upper", "_u", "harvest"] = 0.2
    tvp = mpc.get_tvp_template()
    def light_schedule(t_now):
        for k in range(4):
            tvp["_tvp", k, "light"] = max(0.0, math.sin(2 * math.pi * (np.asarray(t_now).item() + k) / 24))
        return tvp
    mpc.set_tvp_fun(light_schedule)
    mpc.setup()
    mpc.x0 = 1.0
    mpc.set_initial_guess()
    estimator = do_mpc.estimator.StateFeedback(model)
    state = 1.0
    controls = []
    for k in range(4):
        estimate = estimator.make_step(np.array([[state]]))
        control = float(mpc.make_step(estimate)[0, 0])
        controls.append(control)
        state += (0.08 * max(0.0, math.sin(2*math.pi*k/24)) - 0.025)*state - control
    return {"model_state_count": model.n_x, "mpc_horizon": 3,
            "solver_execution": "four closed-loop IPOPT steps", "controls": controls,
            "final_inventory_arbitrary": state, "estimator": "StateFeedback",
            "fixture": "synthetic day/night; not biology validation"}


def fmpy():
    from zipfile import ZipFile

    import fmpy
    fmu = Path(os.environ['Q103_FMU'])  # Reference-FMUs 2.0 Dahlquist.fmu
    result = fmpy.simulate_fmu(str(fmu), stop_time=1.0, output_interval=0.1)
    me_result = fmpy.simulate_fmu(str(fmu), fmi_type='ModelExchange',
                                  stop_time=1.0, output_interval=0.1)
    with ZipFile(fmu) as archive:
        references = [name for name in archive.namelist() if 'reference' in name.lower()]
        documentation = archive.read('documentation/index.html').decode('utf-8')
    assert 'x(t) = exp(-k * t)' in documentation
    analytical = math.exp(-1.0)  # documented k=1, x(0)=1
    return {"fmu": fmu.name, "samples": len(result), "final_time_s": float(result['time'][-1]),
            "output_names": list(result.dtype.names), "reference_files_in_fmu": references,
            "documented_analytical_x_at_1": analytical,
            "analytical_absolute_error": abs(float(result['x'][-1]) - analytical),
            "model_exchange_final_x": float(me_result['x'][-1]),
            "model_exchange_analytical_absolute_error": abs(float(me_result['x'][-1]) - analytical),
            "last_values": {name: float(result[name][-1]) for name in result.dtype.names if name != 'time'}}


def openmdao():
    import openmdao.api as om

    problem = om.Problem(reports=False)
    problem.model.add_subsystem("pump", om.ExecComp("pump_w=flow*head/eff", flow=1.0, head=1.0,
                                                     eff=0.8, pump_w=1.0), promotes=["*"])
    problem.driver = om.ScipyOptimizeDriver()
    problem.model.add_design_var("flow", lower=0.1, upper=2.0)
    problem.model.add_objective("pump_w")
    problem.setup()
    problem.set_val("head", 10.0)
    problem.run_driver()
    return {"flow": float(problem.get_val("flow")[0]),
            "power": float(problem.get_val("pump_w")[0]), "driver": "ScipyOptimizeDriver"}


def neqsim():
    from CoolProp.CoolProp import PropsSI
    from neqsim.thermo import TPflash, fluid
    values = {}
    for component in ('water', 'CO2'):
        system = fluid('srk')
        system.addComponent(component, 1.0)
        system.setTemperature(T)
        system.setPressure(P / 100000)
        TPflash(system)
        system.initPhysicalProperties()
        density = float(system.getPhase(0).getDensity('kg/m3'))
        reference = PropsSI('D', 'T', T, 'P', P, 'Water' if component == 'water' else 'CO2')
        values[component] = {"neqsim_density_kg_m3": density,
                             "coolprop_density_kg_m3": reference,
                             "relative_difference": (density-reference)/reference,
                             "phase_count": int(system.getNumberOfPhases()),
                             "phase": str(system.getPhase(0).getPhaseTypeName())}
    return values


PROBES = {
    "CoolProp": coolprop, "chemicals": chemicals, "thermo": thermo,
    "fluids": fluids, "ht": ht, "thermosteam": thermosteam,
    "biosteam": biosteam, "qsdsan": qsdsan, "pyomo": pyomo,
    "idaes-pse": idaes, "watertap": watertap, "scikit-sundae": sundae,
    "casadi": casadi, "do-mpc": dompc, "fmpy": fmpy,
    "openmdao": openmdao, "neqsim": neqsim,
}
PREREQUISITE_CHECKS = {"pyomo", "idaes-pse", "watertap", "fmpy"}


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', nargs='+', choices=PROBES)
    parser.add_argument('--venv-role', choices=('base', 'bio'), default='base')
    args = parser.parse_args()
    os.environ['PATH'] = str(Path.home() / '.idaes' / 'bin') + os.pathsep + os.environ['PATH']
    if solver_lib := os.environ.get('Q103_SOLVER_LIB'):  # Fortran runtime for IPOPT
        os.environ['LD_LIBRARY_PATH'] = solver_lib + os.pathsep + os.environ.get('LD_LIBRARY_PATH', '')
    if java_home := os.environ.get('Q103_JAVA_HOME'):
        os.environ['JAVA_HOME'] = java_home
    OUT.mkdir(exist_ok=True)
    for name, probe in PROBES.items():
        if args.only and name not in args.only:
            continue
        dist = metadata.distribution(name)
        license_text = (dist.metadata.get("License-Expression") or dist.metadata.get("License")
                        or next((c.rsplit("::", 1)[-1].strip() for c in dist.metadata.get_all("Classifier", [])
                                 if c.startswith("License ::")), "not declared in metadata"))
        started = time.perf_counter()
        record = {"candidate": name, "version": dist.version, "license": license_text,
                  "status": "succeeded", "wall_time_s": 0.0,
                  "interpreter": sys.executable, "environment": args.venv_role,
                  "numba_disable_jit": os.environ.get('NUMBA_DISABLE_JIT') == '1'}
        try:
            record["measurements"] = probe()
        except Exception as exc:  # noqa: BLE001 - preserve each package's native failure
            record["status"] = "failed"
            record["native_error"] = f"{type(exc).__module__}.{type(exc).__name__}: {exc}"
            record["error_source"] = "prerequisite_check" if name in PREREQUISITE_CHECKS else "package"
        record["wall_time_s"] = round(time.perf_counter() - started, 6)
        (OUT / f"{name}.json").write_text(json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n")
        print(name, record["status"], record.get("native_error", ""))


if __name__ == "__main__":
    run()
