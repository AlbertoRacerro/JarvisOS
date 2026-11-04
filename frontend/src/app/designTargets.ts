// Spec 229 acceptance 6: the one registry of stable semantic targets on the Design surface, and the
// per-stage postconditions that the operator discovery proof checks now and guided walkthroughs (224/225)
// will read later. Targets name operator-visible controls and panels; they never carry behaviour.

export type DesignTargetSurface = "rail" | "design";

export type DesignTarget = Readonly<{
  /** Accessible role the element exposes (implicit or explicit). */
  role: string;
  /** Accessible name; a RegExp source when the name embeds live data. */
  name: string;
  surface: DesignTargetSurface;
  /** Rendered only in a given state (stage, selection, empty project); otherwise always present on its surface. */
  when?: string;
}>;

export const DESIGN_TARGETS = {
  "rail.back": { role: "button", name: "Back", surface: "rail" },
  "rail.forward": { role: "button", name: "Forward", surface: "rail" },
  "rail.design": { role: "link", name: "Design", surface: "rail" },
  "rail.memory": { role: "link", name: "Memory", surface: "rail" },
  "rail.development": { role: "link", name: "Development", surface: "rail" },
  "rail.coding": { role: "link", name: "Coding", surface: "rail" },
  "rail.settings": { role: "link", name: "Settings", surface: "rail" },
  "rail.tools": { role: "navigation", name: "Design tools", surface: "rail" },
  "rail.tool.process": { role: "link", name: "Process editor", surface: "rail", when: "Design tools expanded" },
  "rail.tool.studies": { role: "link", name: "Engineering studies", surface: "rail", when: "Design tools expanded" },
  "rail.tool.bluecad": { role: "link", name: "BlueCad workbench", surface: "rail", when: "Design tools expanded" },
  "rail.tool.studio": { role: "link", name: "Studio", surface: "rail", when: "Design tools expanded" },
  "rail.tool.engineering-data": { role: "link", name: "Engineering data", surface: "rail", when: "Design tools expanded" },
  "rail.tool.runs": { role: "link", name: "Runs", surface: "rail", when: "Design tools expanded" },
  "design.project-picker": { role: "combobox", name: "Project", surface: "design" },
  "design.version-picker": { role: "combobox", name: "Project version", surface: "design" },
  "design.example-open": { role: "button", name: "Open case study C1", surface: "design", when: "project without Design content while the C1 case study (Example workspace) exists" },
  "design.stages": { role: "navigation", name: "Design stages", surface: "design" },
  "design.stage.plant": { role: "button", name: "Plant", surface: "design" },
  "design.stage.dynamic": { role: "button", name: "Dynamic", surface: "design" },
  "design.stage.cad": { role: "button", name: "CAD", surface: "design" },
  "design.stage.mechanics": { role: "button", name: "Motion/Mechanics (placeholder)", surface: "design" },
  "design.stage.cfd": { role: "button", name: "CFD", surface: "design" },
  "design.stage.optics": { role: "button", name: "Optics", surface: "design" },
  "design.stage.reduced-model": { role: "button", name: "Reduced model", surface: "design" },
  "design.tree": { role: "tree", name: "Component model tree", surface: "design", when: "version has components" },
  "design.tree.node.parameters": { role: "treeitem", name: "^Parameters", surface: "design", when: "component selected" },
  "design.tree.node.geometry": { role: "treeitem", name: "^Geometry", surface: "design", when: "component selected" },
  "design.tree.node.materials": { role: "treeitem", name: "^Materials", surface: "design", when: "component selected" },
  "design.tree.node.physics": { role: "treeitem", name: "^Physics", surface: "design", when: "component selected" },
  "design.tree.node.mesh": { role: "treeitem", name: "^Mesh", surface: "design", when: "component selected" },
  "design.tree.node.results": { role: "treeitem", name: "^Results", surface: "design", when: "component selected" },
  "design.tree.node.reduced-model": { role: "treeitem", name: "^Reduced model", surface: "design", when: "component selected" },
  "design.panel.plant": { role: "region", name: "Plant", surface: "design", when: "Plant stage without a selected component" },
  "design.panel.viewport": { role: "region", name: "Component viewport", surface: "design", when: "component stages" },
  "design.panel.inspector": { role: "complementary", name: "Inspector", surface: "design", when: "component stages" },
  "design.panel.feature-inspector": { role: "region", name: "Feature inspector", surface: "design", when: "Geometry node" },
  "design.node.attach-model": { role: "button", name: "Attach model", surface: "design", when: "Physics or Geometry node" },
  "design.panel.cfd-studies": { role: "region", name: "CFD study templates", surface: "design", when: "Results node, Study templates expanded" },
  "design.panel.cfd-runs": { role: "region", name: "CFD runs", surface: "design", when: "Results node, Study templates expanded" },
  "design.panel.cfd-field-3d": { role: "region", name: "CFD 3D field", surface: "design", when: "Results node, a finished run" },
  "design.panel.cfd-field": { role: "region", name: "CFD field slice", surface: "design", when: "Results node (the first compared run)" },
  "design.field.run": { role: "group", name: "Compare runs", surface: "design", when: "Results node with the inspector shown" },
  "design.field.pressure": { role: "button", name: "p", surface: "design", when: "field available" },
  "design.field.velocity": { role: "button", name: "|U|", surface: "design", when: "field available" },
  "design.field.zoom-in": { role: "button", name: "Zoom in", surface: "design", when: "field available" },
  "design.field.zoom-out": { role: "button", name: "Zoom out", surface: "design", when: "field available" },
  "design.field.reset": { role: "button", name: "Reset view", surface: "design", when: "field available" },
  "design.field.expand": { role: "button", name: "^(Expand field|Show inspector)$", surface: "design", when: "Results node" },
  "design.workshop.library": { role: "button", name: "^(Show|Hide) library$", surface: "design", when: "Workshop open" },
  "design.workshop.focus": { role: "button", name: "^(Exit focus|Focus)$", surface: "design", when: "Workshop model open" },
  "design.workshop.view-explanation": { role: "button", name: "Explanation", surface: "design", when: "Workshop Explanation & code" },
  "design.workshop.view-split": { role: "button", name: "Side by side", surface: "design", when: "Workshop Explanation & code" },
  "design.workshop.view-code": { role: "button", name: "Python", surface: "design", when: "Workshop Explanation & code" },
  "design.workshop.notes": { role: "button", name: "Contract & notes", surface: "design", when: "Workshop focused Explanation & code" },
  "design.workshop.splitter": { role: "separator", name: "Resize explanation and code", surface: "design", when: "Workshop side by side" },
  "design.panel.reduced-model": { role: "region", name: "CFD reduced models", surface: "design", when: "Reduced model node" },
  "design.panel.optics": { role: "region", name: "Optics and prediction", surface: "design", when: "Optics stage" },
  "design.panel.optics-inputs": { role: "region", name: "Optics inputs", surface: "design", when: "Optics stage" },
  "design.panel.prediction": { role: "region", name: "Dynamic Studio prediction", surface: "design", when: "Optics stage" },
  "design.panel.dynamic": { role: "region", name: "Dynamic simulation", surface: "design", when: "Dynamic stage" },
  "design.dynamic.draft": { role: "combobox", name: "Process draft to simulate", surface: "design", when: "Dynamic stage, version with several Process drafts" },
  "design.dynamic.steps": { role: "navigation", name: "Dynamic simulation steps", surface: "design", when: "Dynamic stage with a Process draft" },
  "design.link.process-editor": { role: "link", name: "Open full Process editor", surface: "design", when: "Plant stage, first version" },
  "design.link.environment": { role: "button", name: "Environment…", surface: "design", when: "Plant stage" },
  "design.link.bluecad": { role: "link", name: "Open BlueCad workbench", surface: "design", when: "Geometry node, first version" },
  "design.link.studies": { role: "link", name: "Open engineering Studies", surface: "design", when: "Results node, first version" },
  "design.link.studio": { role: "link", name: "Open Studio", surface: "design", when: "Library expanded, first version" }
} as const satisfies Readonly<Record<string, DesignTarget>>;

export type DesignTargetId = keyof typeof DESIGN_TARGETS;

/** Spread onto the element that carries the target's role and accessible name. */
export const designTarget = (id: DesignTargetId) => ({ "data-jarvis-target": id });

/**
 * Components-tree rows are data, not registry entries: each carries its component's name so Bubu can point at one
 * ("design.tree.component.c1-cfd"). A repeated name gets "-2", "-3" ...; `taken` collects the ids handed out so far.
 */
export function componentTargetId(name: string, taken: Set<string>): string {
  const base = `design.tree.component.${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "") || "component"}`;
  let id = base;
  for (let n = 2; taken.has(id); n += 1) id = `${base}-${n}`;
  taken.add(id);
  return id;
}

export type DesignStageName = "Plant" | "Dynamic" | "CAD" | "Motion/Mechanics" | "CFD" | "Optics" | "Reduced model";

export type StagePostcondition = Readonly<{
  /** Target that must be visible once the stage is reached. */
  target: DesignTargetId;
  /** Human-readable condition on that target, checked by the discovery proof. */
  holds: string;
}>;

/** What must be true once each stage is reached on a project with that stage's content. */
export const DESIGN_STAGE_POSTCONDITIONS: Readonly<Record<DesignStageName, readonly StagePostcondition[]>> = {
  Plant: [
    { target: "design.panel.plant", holds: "the version's Plant flowsheet shows its Process units" },
    { target: "design.tree", holds: "the Components tree lists the version's components" }
  ],
  Dynamic: [
    { target: "design.panel.dynamic", holds: "one sentence says what the simulation does and the Tier 1 lumped, unqualified notice is shown once" },
    { target: "design.dynamic.steps", holds: "the numbered steps Scenario, Schedule & controllers, Run, Results & compare each show the chosen draft's state and open their section" }
  ],
  CAD: [
    { target: "design.tree.node.geometry", holds: "the Geometry node of the selected component is current" },
    { target: "design.panel.viewport", holds: "the attached validated geometry renders in the 3D viewport" }
  ],
  "Motion/Mechanics": [
    { target: "design.stage.mechanics", holds: "the stage is labelled a placeholder and claims no capability" }
  ],
  CFD: [
    { target: "design.field.run", holds: "every stored run is listed with its state; the latest finished run is checked by default" },
    { target: "design.panel.cfd-field", holds: "each checked run's field slice renders with legend and units, stacked one below the other" },
    { target: "design.panel.cfd-studies", holds: "with Study templates expanded, the toolchain line and study templates are shown" },
    { target: "design.panel.cfd-runs", holds: "with Study templates expanded, the runs list shows every stored run with its state" }
  ],
  Optics: [
    { target: "design.panel.optics-inputs", holds: "every photobioreactor unit's optics inputs are listed, unset values as not set" },
    { target: "design.panel.prediction", holds: "the stored Dynamic Studio prediction of the chosen draft is shown" }
  ],
  "Reduced model": [
    { target: "design.panel.reduced-model", holds: "published reduced models show their status and Process pin" }
  ]
};
