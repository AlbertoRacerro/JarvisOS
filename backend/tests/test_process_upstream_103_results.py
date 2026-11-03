"""Check the offline qualification artifact envelope without importing solvers."""

import json
import math
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[2] / "scripts" / "benchmarks" / "process_upstream" / "results"
CANDIDATES = {
    "CoolProp", "chemicals", "thermo", "fluids", "ht", "thermosteam",
    "biosteam", "qsdsan", "pyomo", "idaes-pse", "watertap",
    "scikit-sundae", "casadi", "do-mpc", "fmpy", "openmdao", "neqsim",
}


def test_qualification_results_are_complete() -> None:
    files = {path.stem: path for path in RESULTS.glob("*.json")}
    assert set(files) == CANDIDATES
    for name, path in files.items():
        result = json.loads(path.read_text(encoding="utf-8"))
        assert result["candidate"] == name
        assert isinstance(result["version"], str) and result["version"]
        assert isinstance(result["license"], str) and result["license"]
        assert isinstance(result["interpreter"], str) and result["interpreter"]
        assert result["environment"] in {"base", "bio"}
        assert isinstance(result["numba_disable_jit"], bool)
        assert result["status"] in {"succeeded", "failed"}
        assert math.isfinite(result["wall_time_s"]) and result["wall_time_s"] >= 0
        if result["status"] == "succeeded":
            assert isinstance(result["measurements"], dict) and result["measurements"]
            assert "native_error" not in result
        else:
            assert isinstance(result["native_error"], str) and result["native_error"]
            assert result["error_source"] in {"package", "prerequisite_check"}
            assert "measurements" not in result
