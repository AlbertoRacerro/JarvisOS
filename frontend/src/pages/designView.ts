import { DESIGN_NODES, type DesignNode } from "../components/design/DesignComponentTree";

// The Design view lives in the URL (/design/project?stage=cad&component=<id>&node=geometry), so the shell's Back and
// Forward walk it like pages: Plant, then the component opened from it, then a node of that component.
// Dynamic sits next to Plant: both work on the version's Process draft, the stages after them on one component.
export const STAGES = ["Plant", "Dynamic", "CAD", "Motion/Mechanics", "CFD", "Optics", "Reduced model"] as const;
export type Stage = (typeof STAGES)[number];
/** `draftId` is Dynamic's chosen Process draft (`&draft=<id>`); absent means the version's Plant draft. */
export type DesignView = Readonly<{ stage: Stage; componentId: string | null; node: DesignNode; draftId?: string | null }>;

const STAGE_SLUG: Record<Stage, string> = { Plant: "plant", Dynamic: "dynamic", CAD: "cad", "Motion/Mechanics": "mechanics", CFD: "cfd", Optics: "optics", "Reduced model": "reduced-model" };
/** The node each stage opens on; a link with a component but no node opens there too. */
export const STAGE_NODE: Record<Stage, DesignNode> = { Plant: "Parameters", Dynamic: "Parameters", CAD: "Geometry", "Motion/Mechanics": "Physics", CFD: "Results", Optics: "Physics", "Reduced model": "Reduced model" };
const VIEW_PARAMS = ["stage", "component", "node", "draft"] as const;
const nodeSlug = (node: DesignNode) => node.toLowerCase().replace(/ /g, "-");
/** Retired node slugs that old links still carry: Studies merged into Results. */
const NODE_ALIASES: Readonly<Record<string, DesignNode>> = { studies: "Results" };
const idParam = (value: string | null) => { const id = value?.trim() ?? ""; return id && id.length <= 200 ? id : null; };

/** Unknown or malformed values fall back to Plant, no component and the stage's own node; a draft is read on Dynamic only. */
export function readDesignView(search: string): DesignView {
  const params = new URLSearchParams(search);
  const stage = STAGES.find((item) => STAGE_SLUG[item] === params.get("stage")) ?? "Plant";
  const componentId = idParam(params.get("component"));
  const slug = params.get("node") ?? "";
  const node = componentId ? DESIGN_NODES.find((item) => nodeSlug(item) === slug) ?? NODE_ALIASES[slug] : undefined;
  const draftId = stage === "Dynamic" ? idParam(params.get("draft")) : null;
  return { stage, componentId, node: node ?? STAGE_NODE[stage], ...(draftId ? { draftId } : {}) };
}

/** Plant without a component is the bare /design/project; other query parameters are kept as they are. */
export function designViewHref(view: DesignView, search: string): string {
  const params = new URLSearchParams(search);
  VIEW_PARAMS.forEach((name) => params.delete(name));
  if (view.stage !== "Plant") params.set("stage", STAGE_SLUG[view.stage]);
  if (view.componentId) { params.set("component", view.componentId); params.set("node", nodeSlug(view.node)); }
  if (view.stage === "Dynamic" && view.draftId) params.set("draft", view.draftId);
  const query = params.toString();
  return `/design/project${query ? `?${query}` : ""}`;
}
