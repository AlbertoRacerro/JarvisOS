#!/usr/bin/env python3
"""Exercise the revisioned editor through HTTP against a configured DWSIM MCP."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

def main() -> int:
    runtime = Path(os.environ["JARVISOS_DWSIM_MCP_PATH"]).resolve()
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    os.environ["JARVISOS_DWSIM_MCP_SHA256"] = digest
    evidence_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="jarvisos-149-smoke-") as data_root:
        os.environ["JARVISOS_DATA_ROOT"] = data_root
        from app.core.config import get_settings
        from app.core.database import initialize_database
        from app.main import app
        from app.modules.process_stack.dwsim_mcp import DwsimMcpClient

        get_settings.cache_clear()
        initialize_database()
        calls: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        active_command = "setup"
        original_call = DwsimMcpClient.call

        def timed_call(self, name, arguments, timeout_s):
            started = time.perf_counter()
            try:
                return original_call(self, name, arguments, timeout_s)
            finally:
                calls[active_command][name] += time.perf_counter() - started

        DwsimMcpClient.call = timed_call
        try:
            with TestClient(app) as client:
                workspace_response = client.post(
                    "/workspaces",
                    json={"name": "DWSIM editor smoke", "slug": "dwsim-editor-smoke"},
                )
                workspace_response.raise_for_status()
                workspace_id = workspace_response.json()["id"]
                base = f"/workspaces/{workspace_id}/process/dwsim"

                active_command = "create_case"
                created = client.post(
                    f"{base}/cases", params={"name": "149 editor smoke"}
                )
                created.raise_for_status()
                case = created.json()
                case_id = case["case_id"]
                revisions = [case["revision"]]

                def command(kind: str, **fields):
                    nonlocal active_command
                    active_command = kind
                    response = client.post(
                        f"{base}/cases/{case_id}/commands",
                        json={
                            "kind": kind,
                            "expected_revision": revisions[-1],
                            **fields,
                        },
                    )
                    if response.is_error:
                        raise AssertionError(
                            f"{kind} failed: {response.status_code} {response.text}"
                        )
                    payload = response.json()
                    revisions.append(payload["case"]["revision"])
                    return payload

                command("add_compounds", compounds=["Water"])
                command("set_property_package", name="Steam Tables (IAPWS-IF97)")
                command("create_material_stream", tag="FeedA", x=40, y=80)
                command("create_material_stream", tag="FeedB", x=40, y=180)
                command("create_material_stream", tag="Product", x=360, y=130)
                command("create_unit", unit_type="Mixer", tag="Mixer1", x=200, y=130)
                command("connect", unit="Mixer1", stream="FeedA", role="feed", port=0)
                command("connect", unit="Mixer1", stream="FeedB", role="feed", port=1)
                command(
                    "connect", unit="Mixer1", stream="Product", role="product", port=0
                )
                command("move", object="Mixer1", x=230, y=145)
                command("rename", object="FeedA", new_tag="FeedA_renamed")
                command(
                    "set_stream_conditions",
                    stream="FeedA_renamed",
                    temperature={"value": 298.15, "unit": "K"},
                    pressure={"value": 101325, "unit": "Pa"},
                    mass_flow={"value": 1.0, "unit": "kg/s"},
                    composition={"Water": 1.0},
                )
                command(
                    "set_stream_conditions",
                    stream="FeedB",
                    temperature={"value": 298.15, "unit": "K"},
                    pressure={"value": 101325, "unit": "Pa"},
                    mass_flow={"value": 1.0, "unit": "kg/s"},
                    composition={"Water": 1.0},
                )
                solved = command("solve")
                active_command = "head_projection"
                projection_response = client.get(f"{base}/cases/{case_id}")
                projection_response.raise_for_status()
                projection = projection_response.json()
                streams = {
                    item["tag"]: item["results"]
                    for item in projection["objects"]
                    if item["category"] == "material_stream"
                }
                product_flow = streams["Product"].get("mass_flow_kg_s")
                feed_flow = streams["FeedA_renamed"].get("mass_flow_kg_s", 0) + streams[
                    "FeedB"
                ].get("mass_flow_kg_s", 0)
                helper_residual = solved["readback"].get("mass_balance_residual_kg_s")
                residual = (
                    helper_residual
                    if helper_residual is not None
                    else feed_flow - product_flow
                )
                if product_flow is None or abs(product_flow - feed_flow) > 1e-6:
                    raise AssertionError(
                        f"product mass flow {product_flow} != feed sum {feed_flow}"
                    )
                if residual is None or abs(residual) > 1e-6:
                    raise AssertionError(
                        f"mass balance residual is not qualified: {residual}"
                    )

                active_command = "stale_revision"
                stale = client.post(
                    f"{base}/cases/{case_id}/commands",
                    json={
                        "kind": "move",
                        "expected_revision": revisions[0],
                        "object": "Mixer1",
                        "x": 2,
                        "y": 3,
                    },
                )
                if stale.status_code != 409:
                    raise AssertionError(
                        f"stale revision returned {stale.status_code}, expected 409"
                    )

                active_command = "download"
                download = client.get(
                    f"{base}/cases/{case_id}/revisions/{revisions[-1]}/download"
                )
                download.raise_for_status()
                download_sha = hashlib.sha256(download.content).hexdigest()
                active_command = "roundtrip_import"
                imported = client.post(
                    f"{base}/cases/import?filename=head.dwxmz",
                    content=download.content,
                    headers={"content-type": "application/octet-stream"},
                )
                imported.raise_for_status()
                imported_case_id = imported.json()["case_id"]
                imported_projection = client.get(f"{base}/cases/{imported_case_id}")
                imported_projection.raise_for_status()
                roundtrip = imported_projection.json()
                original_objects = {
                    item["tag"]: (item["x"], item["y"])
                    for item in projection["objects"]
                }
                imported_objects = {
                    item["tag"]: (item["x"], item["y"]) for item in roundtrip["objects"]
                }
                if (
                    original_objects != imported_objects
                    or projection["connections"] != roundtrip["connections"]
                ):
                    raise AssertionError(
                        "download/re-import changed DWSIM layout or native connector graph"
                    )
                history = client.get(f"{base}/cases/{case_id}/revisions")
                history.raise_for_status()
                result = {
                    "runtime": str(runtime),
                    "dwsim_version": case["dwsim_version"],
                    "mcp_sha256": digest,
                    "workspace_id": workspace_id,
                    "source_sha": subprocess.run(
                        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
                        cwd=Path(__file__).resolve().parent,
                    ).stdout.strip(),
                    "case_id": case_id,
                    "revision_chain": revisions,
                    "revision_sha256s": [
                        item["case_sha256"] for item in history.json()
                    ],
                    "object_count": len(projection["objects"]),
                    "connection_count": len(projection["connections"]),
                    "compound_names": projection["compounds"],
                    "property_package": projection["property_package"],
                    "feed_mass_flow_kg_s": feed_flow,
                    "product_mass_flow_kg_s": product_flow,
                    "mass_balance_residual_kg_s": residual,
                    "mass_balance_residual_source": "DWSIM boundary helper"
                    if helper_residual is not None
                    else "authoritative feed stream sum minus product stream result",
                    "solve_status": solved["readback"].get("solve_status"),
                    "stale_revision_status": stale.status_code,
                    "download_sha256": download_sha,
                    "roundtrip_case_sha256": roundtrip["case_sha256"],
                    "roundtrip_geometry_and_connections_match": True,
                    "mcp_call_seconds_by_command": {
                        key: dict(value) for key, value in calls.items()
                    },
                }
                output = evidence_dir / "dwsim_editor_smoke.json"
                output.write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(result, indent=2, sort_keys=True))
        finally:
            DwsimMcpClient.call = original_call
            get_settings.cache_clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
