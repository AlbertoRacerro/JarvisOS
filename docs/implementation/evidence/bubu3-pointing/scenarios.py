"""Hand-built page snapshots (ui_context) and questions for the Bubu pointing measurement.

Each snapshot mimics what frontend/src/components/bubu/pageContext.ts collects: stable ids
(data-jarvis-target) first, then everything else as ephemeral ``el-<n>`` ids in DOM order. Only target names are
used; no project data. ``ids="old"`` renders Components-tree rows the way master did (ephemeral ids), ``ids="new"``
the way this change does (``design.tree.component.<slug>``).
"""

from __future__ import annotations

import re
from typing import Any

Item = tuple[str | None, str, str, str | None, str | None]  # (stable id, role, name, state text, key)


def it(stable: str | None, role: str, name: str, text: str | None = None, key: str | None = None) -> Item:
    return (stable, role, name, text, key)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "component"


def snapshot(route: str, title: str, items: list[Item]) -> tuple[dict[str, Any], dict[str, str]]:
    """Stable targets first (DOM order), then the rest with el-<n> ids, exactly like collectPageContext."""
    keys: dict[str, str] = {}
    out: list[dict[str, str]] = []
    counter = 0
    resolved: list[tuple[bool, dict[str, str]]] = []
    for stable, role, name, text, key in items:
        if stable is None:
            counter += 1
            target_id = f"el-{counter}"
        else:
            target_id = stable
        entry = {"id": target_id, "role": role, "name": name}
        if text:
            entry["text"] = text
        if key:
            keys[key] = target_id
        resolved.append((stable is not None, entry))
    out = [e for stable, e in resolved if stable] + [e for stable, e in resolved if not stable]
    return {"route": route, "title": title, "targets": out}, keys


STAGES = [("plant", "Plant"), ("dynamic", "Dynamic"), ("cad", "CAD"), ("mechanics", "Motion/Mechanics (placeholder)"),
          ("cfd", "CFD"), ("optics", "Optics"), ("reduced-model", "Reduced model")]
NODES = [("parameters", "Parameters"), ("geometry", "Geometry"), ("materials", "Materials"), ("physics", "Physics"),
         ("mesh", "Mesh"), ("results", "Results"), ("reduced-model", "Reduced model")]
COMPONENTS = ["C1-CFD", "C1-horizontal", "Photobioreactor loop"]
LIBRARY = [("CFD", "Darcy · smooth pipe", "v1"), ("CFD", "Dittus-Boelter · smooth pipe", "v1"), ("Reduced", "Friction factor", "v2")]


def shell(case_study: bool, home: str = "Zephyr") -> list[Item]:
    items = [it(None, "link", "Skip to main content"),
             it(None, "button", "Show navigator"), it(None, "button", "Show Jarvis"), it(None, "button", "Show analysis"),
             it(None, "combobox", "Appearance preference", "System"),
             it("rail.back", "button", "Back"), it("rail.forward", "button", "Forward", "disabled")]
    if case_study:
        items.append(it(None, "button", f"Back to {home}", key="back_home"))
    else:
        items.append(it("design.project-picker", "combobox", "Project", home))
    items += [it("rail.design", "link", "Design", "current"), it("rail.tools", "navigation", "Design tools"),
              it(None, "button", "More Design tools", "collapsed"),
              it(None, "link", "Memory", key="rail_memory"), it(None, "link", "Development"), it(None, "link", "Coding"), it(None, "link", "Settings")]
    return items


def design_header(stage: str, case_study: bool) -> list[Item]:
    items = [it(None, "heading", f"{'Zephyr · case study C1' if case_study else 'Zephyr'}"),
             it("design.version-picker", "combobox", "Project version", "C1 case study" if case_study else "v1"),
             it("design.stages", "navigation", "Design stages")]
    for slug_id, label in STAGES:
        items.append(it(f"design.stage.{slug_id}", "button", label, "current" if label == stage else None, key=f"stage_{slug_id}"))
    return items


def tree(ids: str, selected: str | None = None, node: str | None = None, stat: dict[str, str] | None = None) -> list[Item]:
    items = [it("design.tree", "tree", "Component model tree")]
    for name in COMPONENTS:
        is_sel = name == selected
        stable = f"design.tree.component.{slug(name)}" if ids == "new" else None
        label = name if ids == "new" else (f"▾ {name}" if is_sel else name)
        items.append(it(stable, "treeitem", label, ("selected, expanded" if is_sel else None), key=f"row_{slug(name)}"))
        if is_sel:
            for node_slug, node_name in NODES:
                state = (stat or {}).get(node_name, "empty")
                items.append(it(f"design.tree.node.{node_slug}", "treeitem", f"{node_name} {state}",
                                "selected" if node_name == node else None, key=f"node_{node_slug}"))
    return items


def left_pane_tail(library_open: bool, selected: str | None) -> list[Item]:
    items = [it(None, "textbox", "Add within " + selected if selected else "Add a component"), it(None, "button", "Add", "disabled"),
             it(None, "button", "Library", "expanded" if library_open else "collapsed", key="library_fold")]
    if library_open:
        for kind, name, version in LIBRARY:
            items.append(it(None, "button", f"{kind} {name} {version}", key=f"lib_{slug(name)}"))
        items += [it(None, "button", "Generate new in Workshop"), it("design.link.studio", "link", "Open Studio")]
    items.append(it(None, "button", "Versions", "collapsed"))
    return items


def design_cfd_page(ids: str, *, case_study: bool = True, library_open: bool = True, selected: str | None = None,
                    node: str | None = None, runs: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    stage = "CFD"
    items = shell(case_study) + design_header(stage, case_study)
    items += [it(None, "heading", "Components")] + tree(ids, selected, node, {"Results": "ready", "Mesh": "ready"} if selected else None)
    items += left_pane_tail(library_open, selected)
    items += [it("design.panel.viewport", "region", "Component viewport"),
              it(None, "heading", f"{selected} · CFD results" if selected and node == "Results" else "3D viewport")]
    items += [it("design.panel.inspector", "complementary", "Inspector"),
              it(None, "heading", f"{selected} · {node}" if selected else stage)]
    if selected and node == "Results":
        items += [it("design.field.expand", "button", "Expand field", "pressed" if False else None),
                  it("design.field.run", "group", "Compare runs"),
                  it(None, "checkbox", "Turbulent · Re 20622 · 3 Oct, 14:05", "checked", key="run_turb"),
                  it(None, "checkbox", "Laminar · Re 100 · 3 Oct, 13:40", key="run_lam"),
                  it("design.panel.cfd-field", "region", "CFD field slice"),
                  it("design.field.pressure", "button", "p", "pressed"), it("design.field.velocity", "button", "|U|"),
                  it(None, "button", "Study templates", "collapsed")]
    return snapshot("/design/project?stage=cfd" + (f"&component=c-1&node={(node or '').lower()}" if selected else ""),
                    "JarvisOS · Design", items)


def design_plant_page(ids: str) -> tuple[dict[str, Any], dict[str, str]]:
    items = shell(False) + design_header("Plant", False)
    items += [it("design.panel.plant", "region", "Plant"), it(None, "heading", "Plant"),
              it("design.link.environment", "button", "Environment…"), it("design.link.process-editor", "link", "Open full Process editor"),
              it(None, "button", "Biology models…"), it(None, "button", "Setup"), it(None, "button", "Run", key="run"),
              it(None, "button", "Solver console", key="solver_console"), it(None, "button", "More controls"),
              it(None, "button", "Material stream"), it(None, "button", "Photobioreactor"), it(None, "button", "Pump"),
              it(None, "button", "Zoom out"), it(None, "button", "Zoom in"), it(None, "button", "Fit all", "pressed")]
    return snapshot("/design/project", "JarvisOS · Design", items)


def design_dynamic_page(ids: str) -> tuple[dict[str, Any], dict[str, str]]:
    items = shell(False) + design_header("Dynamic", False)
    items += [it("design.panel.dynamic", "region", "Dynamic simulation"), it(None, "heading", "Dynamic simulation"),
              it("design.dynamic.draft", "combobox", "Process draft to simulate", "Plant"),
              it("design.dynamic.steps", "navigation", "Dynamic simulation steps"),
              it(None, "button", "1 Scenario No scenario yet", key="dyn_step1"),
              it(None, "button", "2 Schedule & controllers Optional", key="dyn_step2"),
              it(None, "button", "3 Run Needs a scenario first", key="dyn_step3"),
              it(None, "button", "4 Results & compare No results yet", key="dyn_step4")]
    return snapshot("/design/project?stage=dynamic", "JarvisOS · Design", items)


def studio_page(ids: str) -> tuple[dict[str, Any], dict[str, str]]:
    items = shell(False) + [it(None, "heading", "Engineering Studio"), it(None, "button", "Open example workspace"), it(None, "button", "+ New model"),
                            it(None, "heading", "Library"), it(None, "button", "Monod growth v3"), it(None, "button", "Heat loss draft"),
                            it(None, "heading", "Monod growth"),
                            it(None, "button", "Contract", "current", key="tab_contract"), it(None, "button", "Explanation & code", key="tab_code"),
                            it(None, "button", "Documentation"), it(None, "button", "Tests", key="tab_tests"),
                            it(None, "button", "Versions", key="tab_versions"), it(None, "button", "Run")]
    return snapshot("/design/studio", "JarvisOS · Studio", items)


def memory_page(ids: str) -> tuple[dict[str, Any], dict[str, str]]:
    items = shell(False) + [it(None, "heading", "Project Knowledge"), it(None, "combobox", "Project Knowledge revision", "Working revision"),
                            it(None, "heading", "Project search"), it(None, "textbox", "Search"), it(None, "button", "Search")]
    items = [x if x[4] != "rail_memory" else it(None, "link", "Memory", "current", key="rail_memory") for x in items]
    return snapshot("/memory/project-basis", "JarvisOS · Project Basis", items)


def build(ids: str) -> list[dict[str, Any]]:
    """Scenarios: question, ui_context, and the ids that count as correct / plausible as the FIRST directive."""
    out: list[dict[str, Any]] = []

    def add(name: str, question: str, page: tuple[dict[str, Any], dict[str, str]], correct: list[str], plausible: list[str]) -> None:
        ui, keys = page
        resolve = lambda ref: keys[ref[4:]] if ref.startswith("key:") else ref  # noqa: E731
        out.append({"name": name, "question": question, "ui_context": ui,
                    "correct": [resolve(r) for r in correct], "plausible": [resolve(r) for r in plausible]})

    cfd_none = design_cfd_page(ids)
    add("c1_cfd_turbulent_results", "how can I see the results of the turbulent simulations?", cfd_none,
        ["key:row_c1-cfd"], ["design.tree", "design.stage.cfd"])
    add("c1_cfd_pathway_followup", "no, I know there's a specific pathway to see the turbulent CFD results", cfd_none,
        ["key:row_c1-cfd"], ["design.tree", "design.stage.cfd"])
    add("c1_cfd_vs_horizontal", "Where do I open the CFD results for C1?",
        cfd_none, ["key:row_c1-cfd"], ["design.tree", "design.stage.cfd"])
    add("c1_cfd_mesh_nothing_selected", "where do I change the mesh?", cfd_none, ["key:row_c1-cfd"], ["design.tree"])
    cfd_results = design_cfd_page(ids, selected="C1-CFD", node="Results")
    add("c1_cfd_mesh_selected", "where do I change the mesh?", cfd_results, ["design.tree.node.mesh"], [])
    add("c1_cfd_compare_runs", "how do I compare laminar and turbulent runs?", cfd_results,
        ["design.field.run"], ["key:run_turb", "key:run_lam", "design.panel.cfd-field"])
    add("plant_turbulent_results", "how can I see the results of the turbulent CFD simulations?", design_plant_page(ids),
        ["design.stage.cfd"], [])
    add("plant_solver_console", "where is the solver console?", design_plant_page(ids), ["key:solver_console"], ["key:run"])
    add("case_study_back_home", "how do I go back to Zephyr?", cfd_none, ["key:back_home"], [])
    add("dynamic_run_simulation", "how do I run a dynamic simulation?", design_dynamic_page(ids),
        ["key:dyn_step1"], ["design.dynamic.steps", "design.panel.dynamic"])
    add("studio_python_code", "where can I read the Python code of this model?", studio_page(ids), ["key:tab_code"], [])
    add("memory_literature", "where do I find the literature?", memory_page(ids), ["Show navigator"], [])
    # The navigator button is ephemeral; resolve it by name.
    last = out[-1]
    nav = next(t["id"] for t in last["ui_context"]["targets"] if t["name"] == "Show navigator")
    last["correct"], last["plausible"] = [nav], []
    return out
