#!/usr/bin/env python3
"""Exercise revisioned DWSIM dynamics commands against the real tank sample."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = Path("/home/user/jarvis-control/work/tools/dwsim-10.2.9/samples/samples/Dynamic Simulation - Water Tank Level Control.dwxmz")


def main() -> int:
    runtime = Path(os.environ["JARVISOS_DWSIM_MCP_PATH"]).resolve()
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    os.environ["JARVISOS_DWSIM_MCP_SHA256"] = digest
    if not SAMPLE.is_file():
        raise FileNotFoundError(SAMPLE)
    with tempfile.TemporaryDirectory(prefix="jarvisos-149-dynamics-") as data_root:
        os.environ["JARVISOS_DATA_ROOT"] = data_root
        import sys

        sys.path.insert(0, str(ROOT / "backend"))
        from app.core.config import get_settings
        from app.core.database import initialize_database
        from app.main import app

        get_settings.cache_clear()
        initialize_database()
        with TestClient(app) as client:
            workspace = client.post("/workspaces", json={"name": "DWSIM dynamics smoke", "slug": "dwsim-dynamics-smoke"})
            workspace.raise_for_status()
            workspace_id = workspace.json()["id"]
            base = f"/workspaces/{workspace_id}/process/dwsim"
            imported = client.post(
                f"{base}/cases/import?filename=tank-controller.dwxmz",
                content=SAMPLE.read_bytes(),
                headers={"content-type": "application/octet-stream"},
            )
            imported.raise_for_status()
            case = imported.json()
            case_id = case["case_id"]
            revisions = [case["revision"]]
            timings: dict[str, float] = {}

            def command(kind: str, **fields):
                started = time.perf_counter()
                response = client.post(
                    f"{base}/cases/{case_id}/commands",
                    json={"kind": kind, "expected_revision": revisions[-1], **fields},
                )
                timings[kind] = time.perf_counter() - started
                if response.is_error:
                    raise AssertionError(f"{kind}: {response.status_code} {response.text}")
                result = response.json()
                revisions.append(result["case"]["revision"])
                return result

            initial = client.get(f"{base}/cases/{case_id}")
            initial.raise_for_status()
            controllers = initial.json()["dynamics"]["controllers"]
            if not any(item.get("tag") == "PID-008" for item in controllers):
                raise AssertionError("sample controller PID-008 was not projected")
            controller = command("controller_set", tag="PID-008", sp=0.5)
            added = command(
                "event_add", event_set="JarvisSmoke", schedule="Sch1", tag="inlet",
                property="PROP_MS_2", value=5, units="kg/s", at_s=10,
                transition="step", description="Jarvis dynamics smoke inlet step",
            )
            saved = command("state_save", name="JarvisDynamicsSmokeBeforeRun")
            run = command("dynamics_run", schedule="Sch1", duration_s=20, max_wall_time_s=60, max_steps=20000)
            inlet = client.get(f"{base}/cases/{case_id}")
            inlet.raise_for_status()
            # Read the dynamic property directly from the revisioned projection's authoritative object result.
            inlet_object = next(item for item in inlet.json()["objects"] if item.get("tag") == "inlet")
            event_effect_flow = inlet_object.get("results", {}).get("mass_flow_kg_s")
            if event_effect_flow != 5.0:
                raise AssertionError(f"event did not set inlet flow to 5 kg/s: {event_effect_flow!r}")
            run_values = run["readback"].get("series", {}).get("series", {})
            if not run_values:
                raise AssertionError("DWSIM returned no dynamic series variables")
            restored = command("state_restore", name="JarvisDynamicsSmokeBeforeRun")
            final = client.get(f"{base}/cases/{case_id}")
            final.raise_for_status()
            current = final.json()
            restored_controller = next(item for item in current["dynamics"]["controllers"] if item.get("tag") == "PID-008")
            restored_inlet = next(item for item in current["objects"] if item.get("tag") == "inlet")
            restored_flow = restored_inlet.get("results", {}).get("mass_flow_kg_s")
            if restored_flow != 10.0 or float(restored_controller.get("sp", "nan")) != 0.5:
                raise AssertionError(f"state restore mismatch: inlet={restored_flow!r}, controller={restored_controller.get('sp')!r}")
            restored_states = client.get(f"{base}/cases/{case_id}/revisions")
            restored_states.raise_for_status()
            stale = client.post(
                f"{base}/cases/{case_id}/commands",
                json={"kind": "state_restore", "expected_revision": revisions[0], "name": "JarvisDynamicsSmokeBeforeRun"},
            )
            if stale.status_code != 409:
                raise AssertionError(f"stale command returned {stale.status_code}, expected 409")
            evidence = {
                "runtime": str(runtime),
                "dwsim_version": case["dwsim_version"],
                "mcp_sha256": digest,
                "source_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
                "sample_case": str(SAMPLE),
                "workspace_id": workspace_id,
                "case_id": case_id,
                "revision_chain": revisions,
                "revision_sha256s": [row["case_sha256"] for row in restored_states.json()],
                "controller_requested_sp": 0.5,
                "controller_readback": controller["readback"]["controller"],
                "event_readback": added["readback"],
                "saved_state_readback": saved["readback"],
                "dynamic_run": run["readback"],
                "inlet_object_after_run": inlet_object,
                "event_effect_inlet_mass_flow_kg_s": event_effect_flow,
                "restored_state_readback": restored["readback"],
                "restored_inlet_mass_flow_kg_s": restored_flow,
                "controller_after_restore": restored_controller,
                "stale_revision_status": stale.status_code,
                "elapsed_seconds_by_command": timings,
            }
            output = Path(__file__).resolve().parent / "dwsim_dynamics_smoke.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(evidence, indent=2, sort_keys=True))
        get_settings.cache_clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
