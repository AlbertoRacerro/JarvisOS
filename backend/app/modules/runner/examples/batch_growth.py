import csv
import json
import sys
from math import exp, isfinite
from pathlib import Path


def _read_inputs(input_path: Path) -> dict:
    with input_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _number(parameters: dict, key: str) -> float:
    value = parameters[key]
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric.")
    return float(value)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: batch_growth.py <input.json> <output_dir>", file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir = output_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    input_set = _read_inputs(input_path)
    parameters = input_set.get("parameters") or {}
    mu_max = _number(parameters, "mu_max")
    biomass = _number(parameters, "X0")
    t_final = _number(parameters, "t_final")
    dt = _number(parameters, "dt")

    for key, value in {"mu_max": mu_max, "X0": biomass, "t_final": t_final, "dt": dt}.items():
        if not isfinite(value):
            raise ValueError(f"{key} must be finite.")
    if dt <= 0:
        raise ValueError("dt must be greater than zero.")
    if mu_max < 0:
        raise ValueError("mu_max must be nonnegative.")
    if biomass < 0:
        raise ValueError("X0 must be nonnegative.")
    if t_final < 0:
        raise ValueError("t_final must be nonnegative.")
    if t_final / dt > 10000:
        raise ValueError("The requested time grid is too large for V0.")

    series = []
    t = 0.0
    step = 0
    while t <= t_final + 1e-12:
        series.append({"t": round(t, 10), "X": biomass})
        step += 1
        t = step * dt
        biomass = biomass * exp(mu_max * dt)

    csv_path = outputs_dir / "timeseries.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["t", "X"])
        writer.writeheader()
        writer.writerows(series)

    result = {
        "schema_version": 1,
        "status": "succeeded",
        "outputs": {
            "final_biomass_concentration": series[-1]["X"],
            "point_count": len(series),
        },
        "series": series,
        "artifacts": [
            {
                "path": "outputs/timeseries.csv",
                "role": "csv",
                "artifact_type": "csv",
                "mime_type": "text/csv",
            }
        ],
        "warnings": [],
        "metadata": {
            "model": "batch_growth_v0",
            "deterministic": True,
        },
    }
    with (output_dir / "result.json").open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)

    print(f"Batch growth completed with {len(series)} points.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
