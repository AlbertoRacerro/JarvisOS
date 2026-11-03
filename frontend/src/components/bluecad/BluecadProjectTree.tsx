import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  BluecadArtifactRefRead,
  BluecadCandidate,
  BluecadStudioReference,
  BluecadValidationCheck
} from "../../api/client";
import type { GeometryInspectionSnapshot } from "../BluecadGlbViewer";

// Spec 208: the BlueCad navigator as a compact CAD project tree (WAI-ARIA tree pattern).
// Workspace root -> candidates -> selected candidate's parts, Studio references, artifacts, validation.

export type ProjectTreeDetail = Readonly<{
  candidateId: string;
  inspection: GeometryInspectionSnapshot;
  studioRefs: readonly BluecadStudioReference[];
  artifacts: readonly BluecadArtifactRefRead[] | null;
  checks: readonly BluecadValidationCheck[];
  validation: "none" | "loading" | "error" | "ready";
}>;

type Props = Readonly<{
  workspaceName: string;
  candidates: readonly BluecadCandidate[];
  selectedId: string | null;
  disabled: boolean;
  detail: ProjectTreeDetail | null;
  registerCandidate(candidateId: string, node: HTMLElement | null): void;
  onSelectCandidate(candidateId: string): void;
  onSelectMesh(meshKey: string | null): void;
  onInspect(): void;
}>;

type TreeNode = {
  id: string;
  parentId: string | null;
  level: number;
  label: string;
  meta?: string;
  description?: string;
  /** Spec 213: what activating the row does (pointer tooltip). */
  hint?: string;
  status?: string;
  expandable: boolean;
  selected?: boolean;
  activate?: () => void;
  children: TreeNode[];
};

const ROOT_ID = "workspace";

export function candidateTitle(brief: string): string {
  const firstLine = brief.trim().split(/\r?\n/, 1)[0] ?? "";
  return firstLine || "Untitled candidate";
}

export function validationSummary(checks: readonly BluecadValidationCheck[]): string {
  if (checks.length === 0) return "no checks";
  const counts = new Map<string, number>();
  for (const check of checks) {
    const verdict = String(check.status ?? check.verdict ?? "unknown").toLowerCase();
    counts.set(verdict, (counts.get(verdict) ?? 0) + 1);
  }
  return [...counts.entries()].map(([verdict, count]) => `${count} ${verdict}`).join(" · ");
}

function buildTree(props: Props): TreeNode {
  const { workspaceName, candidates, selectedId, detail, onSelectCandidate, onSelectMesh, onInspect } = props;
  const root: TreeNode = { id: ROOT_ID, parentId: null, level: 1, label: workspaceName || "Workspace", meta: `${candidates.length}`, expandable: candidates.length > 0, children: [] };
  for (const candidate of candidates) {
    const candidateNodeId = `candidate:${candidate.id}`;
    const node: TreeNode = {
      id: candidateNodeId,
      parentId: ROOT_ID,
      level: 2,
      label: candidateTitle(candidate.brief_text),
      status: candidate.status,
      description: candidate.parked_reason ? `Parked: ${candidate.parked_reason}` : undefined,
      expandable: candidate.id === selectedId && detail?.candidateId === candidate.id,
      selected: candidate.id === selectedId,
      activate: () => onSelectCandidate(candidate.id),
      children: []
    };
    if (node.expandable && detail) {
      const group = (key: string, label: string, meta: string, children: Omit<TreeNode, "parentId" | "level" | "children" | "expandable">[], activate?: () => void): TreeNode => {
        const groupId = `${candidateNodeId}/${key}`;
        return {
          id: groupId, parentId: candidateNodeId, level: 3, label, meta, activate,
          expandable: children.length > 0,
          children: children.map((child) => ({ ...child, parentId: groupId, level: 4, expandable: false, children: [] }))
        };
      };
      const { inspection } = detail;
      const parts = inspection.status === "ready" ? inspection.meshes : [];
      const partsMeta = inspection.status === "ready" ? `${parts.length}` : inspection.status === "loading" ? "loading" : inspection.status === "error" ? "unavailable" : "no geometry";
      node.children.push(group("parts", "Parts", partsMeta, parts.map((mesh) => ({
        id: `${candidateNodeId}/part:${mesh.meshKey}`,
        label: mesh.displayName,
        selected: inspection.selected?.meshKey === mesh.meshKey,
        meta: inspection.selected?.meshKey === mesh.meshKey ? "highlighted" : undefined,
        hint: inspection.selected?.meshKey === mesh.meshKey ? "Highlighted in the viewport · click to clear" : "Highlight in the viewport and inspect",
        activate: () => onSelectMesh(inspection.selected?.meshKey === mesh.meshKey ? null : mesh.meshKey)
      }))));
      node.children.push(group("references", "Studio references", `${detail.studioRefs.length}`, detail.studioRefs.map((reference) => ({
        id: `${candidateNodeId}/reference:${reference.id}`,
        label: `Version ${reference.version_label}`,
        meta: !reference.valid ? "invalid" : reference.superseded ? "superseded" : reference.stale ? "stale" : "visual only",
        hint: "Open in the inspector",
        activate: onInspect
      })), onInspect));
      const artifacts = detail.artifacts ?? [];
      node.children.push(group("artifacts", "Artifacts", detail.artifacts ? `${artifacts.length}` : "loading", artifacts.map((artifact) => ({
        id: `${candidateNodeId}/artifact:${artifact.id}`,
        label: artifact.filename,
        meta: artifact.roles.join(", "),
        hint: "Open in the inspector",
        activate: onInspect
      })), onInspect));
      const validationMeta = detail.validation === "none" ? "no report" : detail.validation === "loading" ? "loading" : detail.validation === "error" ? "unavailable" : validationSummary(detail.checks);
      node.children.push(group("validation", "Validation", validationMeta, [], onInspect));
    }
    root.children.push(node);
  }
  return root;
}

function flatten(node: TreeNode, expanded: ReadonlySet<string>, into: TreeNode[] = []): TreeNode[] {
  into.push(node);
  if (node.expandable && expanded.has(node.id)) for (const child of node.children) flatten(child, expanded, into);
  return into;
}

function BluecadProjectTree(props: Props) {
  const { candidates, selectedId, disabled, detail, registerCandidate } = props;
  const tree = useMemo(() => buildTree(props), [props]);
  // Collapsed is the user's explicit choice; everything expandable starts open except the
  // reference/artifact groups, which stay compact until asked for.
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set());
  const expanded = useMemo(() => {
    const next = new Set<string>();
    const visit = (node: TreeNode) => {
      if (!node.expandable) return;
      const compactByDefault = /\/(references|artifacts)$/.test(node.id);
      if (compactByDefault ? opened.has(node.id) : !collapsed.has(node.id)) next.add(node.id);
      node.children.forEach(visit);
    };
    visit(tree);
    return next;
  }, [collapsed, opened, tree]);
  const visible = useMemo(() => flatten(tree, expanded), [expanded, tree]);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const itemRefs = useRef<Record<string, HTMLLIElement | null>>({});

  const tabStop = visible.some((node) => node.id === focusedId)
    ? focusedId
    : selectedId && visible.some((node) => node.id === `candidate:${selectedId}`) ? `candidate:${selectedId}` : visible[0]?.id ?? null;

  useEffect(() => {
    if (focusedId && !visible.some((node) => node.id === focusedId)) setFocusedId(null);
  }, [focusedId, visible]);

  const setExpanded = useCallback((node: TreeNode, open: boolean) => {
    const compactByDefault = /\/(references|artifacts)$/.test(node.id);
    const update = (current: ReadonlySet<string>, add: boolean) => {
      const next = new Set(current);
      if (add) next.add(node.id); else next.delete(node.id);
      return next;
    };
    if (compactByDefault) setOpened((current) => update(current, open));
    else setCollapsed((current) => update(current, !open));
  }, []);

  const focusNode = (id: string | undefined) => {
    if (!id) return;
    setFocusedId(id);
    itemRefs.current[id]?.focus();
  };

  const activate = (node: TreeNode) => {
    if (disabled) return;
    if (node.activate) node.activate();
    else if (node.expandable) setExpanded(node, !expanded.has(node.id));
  };

  const onKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const index = visible.findIndex((node) => node.id === (focusedId ?? tabStop));
    const node = visible[index];
    if (!node) return;
    let handled = true;
    switch (event.key) {
      case "ArrowDown": focusNode(visible[index + 1]?.id); break;
      case "ArrowUp": focusNode(visible[index - 1]?.id); break;
      case "Home": focusNode(visible[0]?.id); break;
      case "End": focusNode(visible[visible.length - 1]?.id); break;
      case "ArrowRight":
        if (node.expandable && !expanded.has(node.id)) setExpanded(node, true);
        else if (node.expandable) focusNode(node.children[0]?.id);
        break;
      case "ArrowLeft":
        if (node.expandable && expanded.has(node.id)) setExpanded(node, false);
        else focusNode(node.parentId ?? undefined);
        break;
      case "Enter":
      case " ":
        activate(node);
        break;
      default: handled = false;
    }
    if (handled) {
      event.preventDefault();
      event.stopPropagation();
    }
  };

  const renderNode = (node: TreeNode) => {
    const isExpanded = node.expandable ? expanded.has(node.id) : undefined;
    const candidateId = node.level === 2 ? node.id.slice("candidate:".length) : null;
    const descriptionId = node.description ? `bluecad-tree-desc-${node.id.replace(/[^A-Za-z0-9_-]/g, "_")}` : undefined;
    return <li
      key={node.id}
      ref={(element) => {
        itemRefs.current[node.id] = element;
        if (candidateId) registerCandidate(candidateId, element);
      }}
      role="treeitem"
      aria-level={node.level}
      aria-expanded={isExpanded}
      aria-selected={node.selected ?? false}
      aria-disabled={disabled || undefined}
      aria-describedby={descriptionId}
      tabIndex={node.id === tabStop ? 0 : -1}
      className={`bluecad-tree__item bluecad-tree__item--level-${node.level}`}
      onFocus={(event) => { if (event.target === event.currentTarget) setFocusedId(node.id); }}
    >
      <div
        className={node.selected ? "bluecad-tree__row is-selected" : "bluecad-tree__row"}
        title={candidateId ? candidates.find((item) => item.id === candidateId)?.brief_text : node.hint ?? node.label}
        onClick={(event) => {
          event.stopPropagation();
          focusNode(node.id);
          activate(node);
        }}
      >
        <span
          className="bluecad-tree__twisty"
          aria-hidden="true"
          onClick={node.expandable && node.activate ? (event) => { event.stopPropagation(); focusNode(node.id); setExpanded(node, !isExpanded); } : undefined}
        >{node.expandable ? (isExpanded ? "▾" : "▸") : ""}</span>
        {node.status && <span className={`bluecad-tree__status status-${node.status}`}>{node.status}</span>}
        <span className="bluecad-tree__label">{node.label}</span>
        {node.description && <span className="bluecad-tree__flag" aria-hidden="true">!</span>}
        {node.meta && <span className="bluecad-tree__meta">{node.meta}</span>}
        {node.description && <span id={descriptionId} className="visually-hidden">{node.description}</span>}
      </div>
      {isExpanded && node.children.length > 0 && <ul role="group">{node.children.map(renderNode)}</ul>}
    </li>;
  };

  // Keep the selected candidate's row in view after list or selection changes.
  useEffect(() => {
    if (!selectedId) return;
    const node = itemRefs.current[`candidate:${selectedId}`];
    node?.querySelector(".bluecad-tree__row")?.scrollIntoView?.({ block: "nearest" });
  }, [selectedId, detail?.candidateId]);

  return <ul className="bluecad-tree" role="tree" aria-label="BlueCad project" onKeyDown={onKeyDown}>{renderNode(tree)}</ul>;
}

export default BluecadProjectTree;
