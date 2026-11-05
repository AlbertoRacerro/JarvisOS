// Spec 242: the guided tour is a hand-maintained, ordered list. Each stop names the page it opens, the stable
// data-jarvis-target it points at, and plain English copy. No model is involved; nothing here is persisted or sent.
// Targets are only referenced here: the registry (app/designTargets.ts) and the components that render them place them.

export type TourNeeds = "case-study" | "component";

export type TourStop = Readonly<{
  /** Path and query the stop opens with the app router. */
  route: string;
  /** The data-jarvis-target id the pointer rings. */
  target: string;
  title: string;
  text: string;
  /** "case-study": open the C1 case study first when it exists. "component": also select its first component. */
  needs?: TourNeeds;
  /** A node row to select after the component, when the stage does not open that node by itself. */
  node?: string;
  /** Why the target can be missing; shown as "Not available here: <why>". */
  absent: string;
}>;

const NO_COMPONENT = "there is no component to open here (no C1 case study in this workspace and no component in this project)";

export const TOUR_STOPS: readonly TourStop[] = [
  {
    route: "/design/project", target: "rail.design", title: "The left rail",
    text: "The rail is the way around the app: Design, Memory, Development, Coding and Settings, with Back and Forward arrows at the top.",
    absent: "the rail is not on screen"
  },
  {
    route: "/design/project", target: "design.project-picker", title: "Project picker",
    text: "Pick the project you work in. Inside a case study such as C1 the picker is replaced by a Back button to your project.",
    absent: "you are inside a case study, where the picker is replaced by a Back button"
  },
  {
    route: "/design/project", target: "design.stages", title: "Design stages",
    text: "One Design screen, seven stages: Plant, Dynamic, CAD, Motion/Mechanics (a placeholder), CFD, Optics and Reduced model.",
    absent: "the Design stages are not on this page"
  },
  {
    route: "/design/project?stage=cad", target: "design.tree", title: "Components tree", needs: "case-study",
    text: "Components live in this tree. Click a component to select it; only then does it expand into its nodes.",
    absent: "this project version has no components yet and there is no C1 case study to open"
  },
  {
    route: "/design/project?stage=cad", target: "design.tree.node.parameters", title: "Component nodes", needs: "component",
    text: "A selected component has the nodes Parameters, Geometry, Materials, Physics, Mesh, Results and Reduced model. Each opens its own inspector.",
    absent: NO_COMPONENT
  },
  {
    route: "/design/project?stage=cad", target: "design.tree.node.geometry", title: "Geometry node", needs: "component", node: "design.tree.node.geometry",
    text: "In the CAD stage the Geometry node shows the component's 3D geometry in the viewport and where geometry is attached.",
    absent: NO_COMPONENT
  },
  {
    route: "/design/project?stage=cad", target: "design.panel.feature-inspector", title: "Feature inspector", needs: "component", node: "design.tree.node.geometry",
    text: "The feature inspector edits geometry features: add, edit, enable or disable, rebuild and undo. Add a serpentine as a later feature, choose Placed freely, set Position X/Y/Z and Heading, then Rebuild geometry; it appears as a detached group in the geometry viewport. Place in viewport shows a move/rotate handle.",
    absent: "no component with a Geometry node could be opened, so there is no feature inspector"
  },
  {
    route: "/design/project?stage=cfd", target: "design.stage.cfd", title: "CFD stage",
    text: "CFD holds the solver runs of a component: results, meshes, study templates and the run list.",
    absent: "the Design stages are not on this page"
  },
  {
    route: "/design/project?stage=cfd", target: "design.field.run", title: "Compare runs", needs: "component", node: "design.tree.node.results",
    text: "On the Results node, Compare runs lists every stored run of the component. Each checked run is stacked in the centre with its field slice.",
    absent: "no component with a Results node could be opened here"
  },
  {
    route: "/design/project?stage=cfd", target: "design.panel.cfd-field-3d", title: "3D symmetry view", needs: "component", node: "design.tree.node.results",
    text: "For a finished run the 3D field shows the computed surfaces, coloured by pressure or velocity. Show full pipe mirrors the computed part across its symmetry planes and says it was mirrored, not computed.",
    absent: "the selected component has no finished CFD run to show in 3D"
  },
  {
    route: "/design/project?stage=plant", target: "design.panel.plant", title: "Plant",
    text: "Plant is the process flowsheet of the version. Double-click a unit to open it as a component.",
    absent: "this version has no flowsheet to show"
  },
  {
    route: "/design/project?stage=dynamic", target: "design.panel.dynamic", title: "Dynamic",
    text: "Dynamic simulates a Process draft over time in four steps: Scenario, Schedule and controllers, Run, Results and compare.",
    absent: "the Dynamic stage is not on screen"
  },
  {
    route: "/design/project", target: "rail.tools", title: "More Design tools",
    text: "Expand More Design tools in the rail for the Process editor, Engineering studies, BlueCad workbench, Studio, Engineering data and Runs.",
    absent: "the rail is not on screen"
  },
  {
    route: "/memory/project-basis", target: "rail.memory", title: "Memory",
    text: "Memory keeps what the project knows: Project Basis, Models and their dossiers, and Literature.",
    absent: "the rail is not on screen"
  },
  {
    route: "/development/roadmap/timeline", target: "rail.development", title: "Development",
    text: "Development holds the Roadmap, as a Timeline or a Calendar.",
    absent: "the rail is not on screen"
  },
  {
    route: "/coding/repository", target: "rail.coding", title: "Coding",
    text: "Coding has the Repository browser and the Runtime view of the coding workers.",
    absent: "the rail is not on screen"
  },
  {
    route: "/settings/appearance", target: "rail.settings", title: "Settings",
    text: "Settings has Appearance, AI (permission, budget, providers and usage) and System.",
    absent: "the rail is not on screen"
  }
];

export const TOUR_COUNT = TOUR_STOPS.length;

/** "n / N" for the zero-based stop index. */
export const tourPosition = (index: number): string => `${index + 1} / ${TOUR_COUNT}`;

/** The one-line truthful note shown when a stop's target is missing. */
export const notAvailableLine = (why: string): string => `Not available here: ${why}.`;

/** The tour only ever activates rows of the Components tree; it never clicks anything else on the page. */
export const TOUR_CLICKABLE = /^design\.tree\.(component|node)\./;
