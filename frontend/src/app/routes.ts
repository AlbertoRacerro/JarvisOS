import type { DesignTargetId } from "./designTargets";

export type PrimaryNavId = "design" | "memory" | "development" | "coding" | "settings";

export type StageKind = "model" | "process" | "review" | "studies";

export type RouteId =
  | "design-project"
  | "design-process"
  | "design-studies"
  | "design-bluecad"
  | "design-studio"
  | "memory-project-basis"
  | "memory-models"
  | "memory-literature"
  | "development-roadmap-timeline"
  | "development-roadmap-calendar"
  | "coding-repository"
  | "coding-runtime"
  | "settings-appearance"
  | "settings-ai"
  | "settings-system"
  | "runs"
  | "engineering-data"
  | "review"
  | "ai-threads"
  | "not-found";

export type AppRouteDefinition = Readonly<{
  id: RouteId;
  path: string;
  title: string;
  primaryNav?: PrimaryNavId;
  stageKind?: StageKind;
  legacy?: true;
}>;

export type PeerNavItem = Readonly<{ label: string; href: string }>;

export const PRODUCTION_ROUTES: readonly AppRouteDefinition[] = [
  { id: "design-project", path: "/design/project", title: "Design", primaryNav: "design" },
  { id: "design-process", path: "/design/process", title: "Process", primaryNav: "design", stageKind: "process" },
  { id: "design-studies", path: "/design/studies", title: "Studies", primaryNav: "design", stageKind: "studies" },
  { id: "design-bluecad", path: "/design/bluecad", title: "BlueCad", primaryNav: "design", stageKind: "model" },
  { id: "design-studio", path: "/design/studio", title: "Studio", primaryNav: "design" },
  { id: "memory-project-basis", path: "/memory/project-basis", title: "Project Basis", primaryNav: "memory" },
  { id: "memory-models", path: "/memory/models", title: "Models", primaryNav: "memory" },
  { id: "memory-literature", path: "/memory/literature", title: "Literature", primaryNav: "memory" },
  { id: "development-roadmap-timeline", path: "/development/roadmap/timeline", title: "Roadmap · Timeline", primaryNav: "development" },
  { id: "development-roadmap-calendar", path: "/development/roadmap/calendar", title: "Roadmap · Calendar", primaryNav: "development" },
  { id: "coding-repository", path: "/coding/repository", title: "Repository", primaryNav: "coding" },
  { id: "coding-runtime", path: "/coding/runtime", title: "Runtime", primaryNav: "coding" },
  { id: "settings-appearance", path: "/settings/appearance", title: "Settings · Appearance", primaryNav: "settings" },
  { id: "settings-ai", path: "/settings/ai", title: "Settings · AI", primaryNav: "settings" },
  { id: "settings-system", path: "/settings/system", title: "Settings · System", primaryNav: "settings" },
  { id: "runs", path: "/runs", title: "Runs" },
  { id: "engineering-data", path: "/engineering-data", title: "Engineering Data" },
  { id: "review", path: "/review", title: "Review", primaryNav: "memory", stageKind: "review" },
  { id: "ai-threads", path: "/ai-threads", title: "AI Threads" }
] as const;

export const PRIMARY_NAV_ITEMS = [
  { id: "design", label: "Design", href: "/design/project" },
  { id: "memory", label: "Memory", href: "/memory/project-basis" },
  { id: "development", label: "Development", href: "/development/roadmap/timeline" },
  { id: "coding", label: "Coding", href: "/coding/repository" },
  { id: "settings", label: "Settings", href: "/settings/appearance" }
] as const satisfies readonly Readonly<{ id: PrimaryNavId; label: string; href: string }>[];

export const PEER_NAV_ITEMS: Readonly<Record<PrimaryNavId, readonly PeerNavItem[]>> = {
  design: [
    { label: "Design", href: "/design/project" },
    { label: "Process", href: "/design/process" },
    { label: "BlueCad", href: "/design/bluecad" },
    { label: "Studio", href: "/design/studio" }
  ],
  memory: [
    { label: "Project Basis", href: "/memory/project-basis" },
    { label: "Models", href: "/memory/models" },
    { label: "Literature", href: "/memory/literature" },
    { label: "Review", href: "/review" }
  ],
  development: [
    { label: "Roadmap", href: "/development/roadmap/timeline" }
  ],
  coding: [
    { label: "Repository", href: "/coding/repository" },
    { label: "Runtime", href: "/coding/runtime" }
  ],
  settings: [
    { label: "Appearance", href: "/settings/appearance" },
    { label: "AI", href: "/settings/ai" },
    { label: "System", href: "/settings/system" }
  ]
};

// Spec 229: the older Design surfaces stay reachable from a secondary, collapsed rail group and from
// contextual links inside the matching Design stage; New Design is the single Design entry.
export const DESIGN_TOOL_ITEMS = [
  { label: "Process editor", href: "/design/process", target: "rail.tool.process" },
  { label: "Engineering studies", href: "/design/studies", target: "rail.tool.studies" },
  { label: "BlueCad workbench", href: "/design/bluecad", target: "rail.tool.bluecad" },
  { label: "Studio", href: "/design/studio", target: "rail.tool.studio" },
  { label: "Engineering data", href: "/engineering-data", target: "rail.tool.engineering-data" },
  { label: "Runs", href: "/runs", target: "rail.tool.runs" }
] as const satisfies readonly Readonly<{ label: string; href: string; target: DesignTargetId }>[];

export const ROADMAP_STAGE_ITEMS = [
  { label: "Timeline", href: "/development/roadmap/timeline" },
  { label: "Calendar", href: "/development/roadmap/calendar" }
] as const satisfies readonly PeerNavItem[];

export type ResolvedRoute = Readonly<{
  route: AppRouteDefinition;
  canonicalPath: string;
  shouldReplace: boolean;
}>;

export function normalizePathname(pathname: string): string {
  const pathOnly = pathname.split(/[?#]/, 1)[0] ?? "/";
  if (pathOnly === "/" || pathOnly.length === 0) return "/";
  if (!pathOnly.startsWith("/") || pathOnly.startsWith("//")) return pathOnly;
  const trimmed = pathOnly.replace(/\/+$/g, "");
  return trimmed || "/";
}

const REDIRECTS: Readonly<Record<string, string>> = {
  "/": "/design/project",
  "/home": "/design/project",
  "/design/model": "/memory/models",
  "/design/results": "/memory/models",
  "/design/lineage": "/memory/models",
  "/design/flowsheet": "/memory/models",
  "/settings": "/settings/appearance"
};

export function resolveRoute(pathname: string): ResolvedRoute {
  const normalized = normalizePathname(pathname);
  const redirect = REDIRECTS[normalized];
  if (redirect) {
    const route = PRODUCTION_ROUTES.find((candidate) => candidate.path === redirect);
    if (!route) throw new Error(`Canonical redirect target is missing: ${redirect}`);
    return { route, canonicalPath: route.path, shouldReplace: true };
  }

  const route = PRODUCTION_ROUTES.find((candidate) => candidate.path === normalized);
  if (route) return { route, canonicalPath: route.path, shouldReplace: normalized !== pathname };

  return { route: { id: "not-found", path: normalized, title: "Page not found" }, canonicalPath: normalized, shouldReplace: normalized !== pathname };
}
