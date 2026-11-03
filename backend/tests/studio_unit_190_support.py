"""Shared fixtures for the spec 190 Studio process-unit tests (a nutrient dosing unit with declared generation)."""

from __future__ import annotations

from fastapi.testclient import TestClient

DOSING_CONTRACT = {
    "category": "process_unit", "name": "Nutrient dosing",
    "engineering_question": "How does the dose change the outlet nitrogen?",
    "inlets": [{"name": "culture", "label": "Culture"}, {"name": "dose", "label": "Nutrient dose"}],
    "outlets": [{"name": "outlet", "label": "Dosed culture"}],
    "parameters": [{"name": "dose_split", "label": "Dose split", "unit": "percent",
                    "description": "Share of the dose kept as nitrogen", "min": 0, "max": 100}],
    "culture_generation": ["nitrogen"],
}

PLACEHOLDER = '    raise NotImplementedError("Complete evaluate before testing")'

MIX_BODY = '''    a, b = inlets["culture"], inlets["dose"]
    ma, mb = a["mass_flow_kg_s"], b["mass_flow_kg_s"]
    m = ma + mb
    names = set(a["mass_fractions"]) | set(b["mass_fractions"])
    fractions = {n: (ma * a["mass_fractions"].get(n, 0.0) + mb * b["mass_fractions"].get(n, 0.0)) / m for n in names}
    generated = params["dose_split"] / 100.0 * 1e-6
    culture = {}
    for field in ("biomass", "nitrogen"):
        amount = ma * (a["culture"].get(field) or 0.0) + mb * (b["culture"].get(field) or 0.0)
        culture[field] = (amount + (generated if field == "nitrogen" else 0.0)) / m
    outlet = {"mass_flow_kg_s": m, "temperature_K": (ma * a["temperature_K"] + mb * b["temperature_K"]) / m,
              "pressure_Pa": min(a["pressure_Pa"], b["pressure_Pa"]), "mass_fractions": fractions, "culture": culture}
    return {"outlets": {"outlet": outlet}, "culture_generation": {"nitrogen": generated}}'''


def stream(flow: float = 1.0, *, nitrogen: float = 0.001, biomass: float = 0.0, temperature: float = 300.0) -> dict:
    return {"mass_flow_kg_s": flow, "temperature_K": temperature, "pressure_Pa": 200000.0,
            "mass_fractions": {"Water": 1.0}, "culture": {"biomass": biomass, "nitrogen": nitrogen}}


def unit_case(**extra) -> dict:
    return {"inlets": {"culture": stream(1.0, biomass=0.5), "dose": stream(0.5, nitrogen=0.02, temperature=290.0)},
            "params": {"dose_split": 50.0}, **extra}


# Golden dosing unit (spec 190 acceptance 1): dose_split percent of the dose joins the culture in `outlet`, the rest
# leaves as `unused_dose`. Conserves total mass, every compound and every culture species; no generation.
GOLDEN_CONTRACT = {
    "category": "process_unit", "name": "Nutrient dosing",
    "engineering_question": "How does the dose split change the outlet nitrogen?",
    "inlets": [{"name": "culture", "label": "Culture"}, {"name": "dose", "label": "Nutrient dose"}],
    "outlets": [{"name": "outlet", "label": "Dosed culture"}, {"name": "unused_dose", "label": "Unused dose"}],
    "parameters": [{"name": "dose_split", "label": "Dose split", "unit": "percent",
                    "description": "Share of the dose stream sent to the outlet", "min": 0, "max": 100}],
    "culture_generation": [],
}

GOLDEN_BODY = '''    c, d = inlets["culture"], inlets["dose"]
    share = params["dose_split"] / 100.0
    mc, md = c["mass_flow_kg_s"], d["mass_flow_kg_s"]
    kept = share * md
    m = mc + kept
    names = set(c["mass_fractions"]) | set(d["mass_fractions"])
    fractions = {n: (mc * c["mass_fractions"].get(n, 0.0) + kept * d["mass_fractions"].get(n, 0.0)) / m for n in names}
    total = sum(fractions.values())
    fractions = {n: v / total for n, v in fractions.items()}
    culture = {}
    for field in set(c["culture"]) | set(d["culture"]):
        a, b = c["culture"].get(field), d["culture"].get(field)
        culture[field] = None if (a is None or b is None or field == "ph") else (mc * a + kept * b) / m
    outlet = {"mass_flow_kg_s": m, "temperature_K": (mc * c["temperature_K"] + kept * d["temperature_K"]) / m,
              "pressure_Pa": min(c["pressure_Pa"], d["pressure_Pa"]), "mass_fractions": fractions, "culture": culture}
    unused = {**d, "mass_flow_kg_s": md - kept}
    return {"outlets": {"outlet": outlet, "unused_dose": unused}}'''


def golden_case() -> dict:
    return {"inlets": {"culture": stream(1.0, biomass=0.5), "dose": stream(0.5, nitrogen=0.02, temperature=290.0)},
            "params": {"dose_split": 50.0}}


def publish_unit(client: TestClient, body: str = MIX_BODY, contract: dict | None = None,
                 workspace: str = "main", case: dict | None = None) -> tuple[str, str]:
    """Create, edit, test and publish a unit; returns (model id, version id)."""
    created = client.post(f"/workspaces/{workspace}/studio/models", json=contract or DOSING_CONTRACT)
    assert created.status_code == 201, created.text
    model = created.json()
    path = f"/workspaces/{workspace}/studio/models/{model['id']}"
    assert client.put(path, json={"source": model["source"].replace(PLACEHOLDER, body)}).status_code == 200
    report = client.post(path + "/test", json={"cases": [case or unit_case()]})
    assert report.status_code == 200 and report.json()["passed"], report.text
    published = client.post(path + "/publish")
    assert published.status_code == 200, published.text
    return model["id"], published.json()["id"]


def publish_next_version(client: TestClient, model_id: str, workspace: str = "main", suffix: str = "\n# v2\n",
                         case: dict | None = None) -> str:
    """Revise a published unit, change its source, test and publish; returns the new version id."""
    path = f"/workspaces/{workspace}/studio/models/{model_id}"
    revised = client.post(path + "/revise")
    assert revised.status_code == 200, revised.text
    assert client.put(path, json={"source": revised.json()["source"] + suffix}).status_code == 200
    report = client.post(path + "/test", json={"cases": [case or unit_case()]})
    assert report.status_code == 200 and report.json()["passed"], report.text
    published = client.post(path + "/publish")
    assert published.status_code == 200, published.text
    return published.json()["id"]
