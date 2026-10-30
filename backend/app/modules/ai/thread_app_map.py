"""Bubu's navigation guide: how the app is laid out, selected by the page's route.

The model only receives the flat list of visible targets, so it cannot know that "CFD results" means
CFD stage -> component in the Components tree -> Results node. This module holds a short, hand-maintained
"how to get around" text per area. It is reference data for the model, never an instruction to act.

Facts come from the frontend (rail/Rail.tsx, ProjectSwitcher.tsx, pages/DesignProject.tsx, designView.ts,
components/design/DesignComponentTree.tsx, app/designTargets.ts, app/routes.ts); keep them in step with it.
Target ids are written out so the model can point at them when they are in the visible list.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

# Budget: one area stays around 600-1200 characters so the page block stays short.
MAX_AREA_CHARS = 1200
MAX_GUIDE_CHARS = 2400

_RAIL = (
    "Everywhere: the left rail has Back/Forward arrows (rail.back, rail.forward); the project picker "
    "(design.project-picker), which inside a case study such as C1 is replaced by a \"Back to <project>\" button; "
    "and the links Design (rail.design), Memory, Development, Coding, Settings. Under Design, \"More Design tools\" "
    "(rail.tools; open it first) holds Process editor (rail.tool.process), Engineering studies (rail.tool.studies), "
    "BlueCad workbench (rail.tool.bluecad), Studio (rail.tool.studio), Engineering data (rail.tool.engineering-data), "
    "Runs (rail.tool.runs). The top bar's \"Show navigator\" lists the pages of the current section."
)

_DESIGN = (
    "Design (/design/project) is one screen. Top: the version picker (design.version-picker, it also lists case "
    "studies) and the stage buttons Plant, Dynamic, CAD, Motion/Mechanics (placeholder), CFD, Optics, Reduced model "
    "(ids design.stage.<plant|dynamic|cad|mechanics|cfd|optics|reduced-model>). "
    "Left: the Components tree (design.tree). Click a component row (design.tree.component.<name>) to select it; only "
    "then does it expand into its nodes Parameters, Geometry, Materials, Physics, Mesh, Results, Reduced model "
    "(ids design.tree.node.<parameters|geometry|materials|physics|mesh|results|reduced-model>). "
    "Centre: the viewport (3D geometry, or CFD results on Results); right: the Inspector of the selected node. "
    "Below the tree: add a component; the Library fold lists published Studio models (Workshop) that can be attached "
    "to a node - it is NOT where simulation results are; the Versions fold forks the project version."
)

_DESIGN_STAGES = {
    "cfd": (
        "CFD results, laminar or turbulent: CFD stage (design.stage.cfd), then click the component in the Components "
        "tree (a design.tree.component.<name> row), then the node Results (design.tree.node.results; it opens by itself in the CFD stage). The Inspector "
        "lists every run under \"Compare runs\" (design.field.run) with checkboxes; each checked run is stacked in the "
        "centre with its field slice. Study templates fold (Inspector): record and start runs. Mesh settings: node Mesh "
        "(design.tree.node.mesh). Physics models: node Physics."
    ),
    "plant": (
        "Plant (design.panel.plant) is the process flowsheet: click a unit to select it, double-click to open it as a "
        "component, then choose CAD, CFD or Reduced model above. Its toolbar has Setup, Run and Solver console "
        "(live solver output); Environment... is design.link.environment; \"Open full Process editor\" is "
        "design.link.process-editor."
    ),
    "dynamic": (
        "Dynamic (design.panel.dynamic): pick the Process draft (design.dynamic.draft), then work through the four "
        "steps in design.dynamic.steps: 1 Scenario, 2 Schedule & controllers (optional), 3 Run, 4 Results & compare."
    ),
    "cad": "CAD: select the component, node Geometry: attach a BlueCad candidate or generate geometry from Process; the viewport shows it.",
    "reduced-model": "Reduced model: select the component, node Reduced model: published reduced models and their status.",
    "optics": "Optics lists the optics inputs of every photobioreactor unit and the stored Dynamic Studio prediction (design.panel.optics).",
}

_DESIGN_EXAMPLES = (
    "Examples: For C1 CFD results, point first to the visible C1-CFD component row, "
    "then Results; C1-horizontal is different. If C1-CFD is absent, point to visible CFD "
    "stage or version picker. To return from a case study, point to its visible 'Back to "
    "<project>' button; otherwise use the project picker."
)

_AREAS: tuple[tuple[str, str], ...] = (
    ("/design/studio", (
        "Studio (calculation models). Left: Library of published models, \"Drafts & models\", \"+ New model\", "
        "\"Open example workspace\". Pick a model: its tabs are Contract, Explanation & code, Documentation, Tests, "
        "Versions, Run (process units and procedures have fewer or different tabs, e.g. Steps, Test bench). In "
        "Explanation & code, the buttons Explanation / Side by side / Python switch the view "
        "(ids design.workshop.view-<explanation|split|code>). Inside Design the same editor is the Workshop "
        "(Show library, Focus, Back to Design)."
    )),
    ("/design/process", (
        "Process editor: the same flowsheet as Design > Plant. Add streams and units from the palette, click an "
        "object to open its inspector. Toolbar \"Process actions\": Setup, Run, Solver console (live solver output), "
        "More controls (draft picker, New draft, Validate, History). The status line says whether the steady-state "
        "results are current or stale."
    )),
    ("/design/project", _DESIGN),
    ("/memory", (
        "Memory has three pages: Project Basis (project knowledge revisions and search), Models (model dossiers: "
        "\"Models & versions\", then the version dossier), Literature. Switch with the rail's Memory link or \"Show "
        "navigator\"."
    )),
    ("/development", (
        "Development has Roadmap (Timeline and Calendar views). Switch with \"Show navigator\"."
    )),
    ("/coding", (
        "Coding has Repository (browse the repository files, breadcrumb on top) and Runtime. Switch with \"Show "
        "navigator\"."
    )),
    ("/settings", (
        "Settings pages: Appearance (appearance and accent), AI (permission and budget, provider catalogue, usage), "
        "System. Switch with \"Show navigator\"."
    )),
)


def _area_for(path: str) -> str:
    for prefix, text in _AREAS:
        if path == prefix or path.startswith(prefix + "/"):
            return text
    return ""


def app_map_for_route(route: str) -> str:
    """The navigation guide for the page at ``route`` (path with optional query), at most ``MAX_GUIDE_CHARS``."""
    parts = urlsplit(route or "")
    path = parts.path.rstrip("/") or "/"
    pieces = [_RAIL, _area_for(path)]
    if path == "/design/project":
        stage = (parse_qs(parts.query).get("stage") or ["plant"])[0]
        pieces.append(_DESIGN_STAGES.get(stage, ""))
        pieces.append(_DESIGN_EXAMPLES)
    return "\n".join(piece for piece in pieces if piece)[:MAX_GUIDE_CHARS]
