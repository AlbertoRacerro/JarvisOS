$ErrorActionPreference = "Continue"
$d = "$env:LOCALAPPDATA\jarvis-work"
if (-not (Test-Path "$d\venv103")) { python -m venv "$d\venv103" }
$py = "$d\venv103\Scripts\python.exe"
$code = @'
import importlib, json, platform, time
from importlib.metadata import version
out = {"platform": platform.platform(), "python": platform.python_version(), "candidates": {}}
def run(name, fn):
    t = time.perf_counter()
    try:
        value = fn()
        out["candidates"][name] = {"version": version(name), "status": "ok", "value": value, "s": round(time.perf_counter() - t, 3)}
    except Exception as exc:
        out["candidates"][name] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
run("CoolProp", lambda: importlib.import_module("CoolProp.CoolProp").PropsSI("D", "T", 298.15, "P", 101325, "Water"))
run("fluids", lambda: importlib.import_module("fluids").friction_factor(Re=56011.23596, eD=0.0))
run("ht", lambda: importlib.import_module("ht").conv_internal.turbulent_Dittus_Boelter(Re=56011.23596, Pr=6.2))
run("thermo", lambda: importlib.import_module("thermo").Chemical("water", T=298.15, P=101325).rho)
def _bio():
    bst = importlib.import_module("biosteam")
    bst.settings.set_thermo(["Water", "Ethanol"], cache=True)
    return bst.__version__
run("biosteam", _bio)
def _pyomo():
    pyo = importlib.import_module("pyomo.environ")
    m = pyo.ConcreteModel(); m.x = pyo.Var(bounds=(0, 10)); m.o = pyo.Objective(expr=(m.x - 3) ** 2)
    solver = pyo.SolverFactory("ipopt")
    return {"ipopt_available": bool(solver.available(exception_flag=False))}
run("pyomo", _pyomo)
run("casadi", lambda: str(importlib.import_module("casadi").SX.sym("x") ** 2))
def _sundae():
    import numpy as np
    from sksundae.cvode import CVODE
    return float(CVODE(lambda t, y, yp: yp.__setitem__(0, -y[0])).solve(np.array([0.0, 1.0]), np.array([1.0])).y[-1, 0])
run("scikit-sundae", _sundae)
run("openmdao", lambda: importlib.import_module("openmdao.api").Problem().__class__.__name__)
run("fmpy", lambda: importlib.import_module("fmpy").__version__)
run("do-mpc", lambda: importlib.import_module("do_mpc").__version__)
run("idaes-pse", lambda: importlib.import_module("idaes").__version__)
print(json.dumps(out, indent=2))
'@
$code | & $py - | Out-File -Encoding utf8 "$d\smoke103.json"
Copy-Item -Force "$d\smoke103.json" "<EVIDENCE_OUT_DIR>\smoke103-windows.json"
