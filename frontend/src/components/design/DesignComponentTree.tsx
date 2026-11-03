import { type KeyboardEvent, useMemo, useRef, useState } from "react";
import type { DesignComponent } from "../../api/designProject";
import { componentTargetId, designTarget, type DesignTargetId } from "../../app/designTargets";
import type { NodeBadge } from "./designNodeStatus";

// Spec 214 / #920 item 5: the Design model tree (WAI-ARIA tree pattern, as the spec 208 BlueCad project tree).
// Component hierarchy; the selected component expands to its model nodes.
// CFD studies are not a separate step: Results holds the study templates and runs next to the results they produced.
export const DESIGN_NODES = ["Parameters", "Geometry", "Materials", "Physics", "Mesh", "Results", "Reduced model"] as const;
export type DesignNode = (typeof DESIGN_NODES)[number];
const NODE_TARGET: Record<DesignNode, DesignTargetId> = {
  Parameters: "design.tree.node.parameters", Geometry: "design.tree.node.geometry", Materials: "design.tree.node.materials",
  Physics: "design.tree.node.physics", Mesh: "design.tree.node.mesh",
  Results: "design.tree.node.results", "Reduced model": "design.tree.node.reduced-model"
};

type Row = { id: string; level: number; label: string; meta?: NodeBadge; target?: string; expandable: boolean; expanded: boolean; selected: boolean; parentId: string | null; activate(): void };

type Props = Readonly<{
  components: readonly DesignComponent[];
  selectedId: string | null;
  selectedNode: DesignNode;
  onSelectComponent(item: DesignComponent): void;
  onSelectNode(node: DesignNode): void;
  /** Spec 229: badge read from the panel owners; replaces the Studio-model status for those nodes. */
  nodeStatus?(node: DesignNode): NodeBadge | undefined;
}>;

/** One status word per node (empty / ready / …); the specifics are the badge's tooltip. */
export function componentNodeStatus(item: DesignComponent, node: DesignNode): NodeBadge {
  if (node === "Parameters") return item.source_unit_id ? { state: "ready", detail: "From Process" } : { state: "empty" };
  if (node === "Geometry") return item.geometry_candidate_id ? { state: "ready", detail: "BlueCad geometry attached" } : { state: "empty" };
  const count = item.models.filter((model) => model.node === node).length;
  return count ? { state: "ready", detail: `${count} model${count === 1 ? "" : "s"}` } : { state: "empty" };
}

function DesignComponentTree({ components, selectedId, selectedNode, onSelectComponent, onSelectNode, nodeStatus }: Props) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const [tabStop, setTabStop] = useState<string | null>(null);
  const refs = useRef(new Map<string, HTMLElement>());
  const rows = useMemo(() => {
    const out: Row[] = [];
    const taken = new Set<string>();
    const visit = (parentId: string | null, level: number, parentRow: string | null) => {
      for (const item of components.filter((candidate) => candidate.parent_id === parentId)) {
        const id = `c:${item.id}`;
        const hasChildren = components.some((candidate) => candidate.parent_id === item.id);
        const isSelected = item.id === selectedId;
        const expandable = hasChildren || isSelected;
        const expanded = expandable && !collapsed.has(id);
        out.push({ id, level, label: item.name, target: componentTargetId(item.name, taken), expandable, expanded, selected: isSelected, parentId: parentRow, activate: () => onSelectComponent(item) });
        if (expanded) {
          if (isSelected) for (const node of DESIGN_NODES) {
            out.push({ id: `n:${item.id}:${node}`, level: level + 1, label: node, meta: nodeStatus?.(node) ?? componentNodeStatus(item, node), target: NODE_TARGET[node], expandable: false, expanded: false,
              selected: selectedNode === node, parentId: id, activate: () => onSelectNode(node) });
          }
          visit(item.id, level + 1, id);
        }
      }
    };
    visit(null, 1, null);
    return out;
  }, [components, selectedId, selectedNode, collapsed, onSelectComponent, onSelectNode, nodeStatus]);
  const stop = rows.some((row) => row.id === tabStop) ? tabStop : (rows.find((row) => row.selected)?.id ?? rows[0]?.id ?? null);
  const focusRow = (id: string | undefined) => { if (id) { setTabStop(id); refs.current.get(id)?.focus(); } };
  const setOpen = (row: Row, open: boolean) => setCollapsed((current) => {
    const next = new Set(current);
    if (open) next.delete(row.id); else next.add(row.id);
    return next;
  });
  const onKeyDown = (event: KeyboardEvent<HTMLElement>, row: Row, index: number) => {
    switch (event.key) {
      case "ArrowDown": event.preventDefault(); focusRow(rows[index + 1]?.id); break;
      case "ArrowUp": event.preventDefault(); focusRow(rows[index - 1]?.id); break;
      case "Home": event.preventDefault(); focusRow(rows[0]?.id); break;
      case "End": event.preventDefault(); focusRow(rows[rows.length - 1]?.id); break;
      case "ArrowRight":
        event.preventDefault();
        if (row.expandable && !row.expanded) setOpen(row, true); else if (row.expanded) focusRow(rows[index + 1]?.id);
        break;
      case "ArrowLeft":
        event.preventDefault();
        if (row.expandable && row.expanded) setOpen(row, false); else focusRow(row.parentId ?? undefined);
        break;
      case "Enter": case " ": event.preventDefault(); row.activate(); break;
      default:
    }
  };
  if (!rows.length) return <p className="design-empty">empty</p>;
  return <ul className="design-tree" role="tree" aria-label="Component model tree" {...designTarget("design.tree")}>
    {rows.map((row, index) => <li key={row.id} role="none">
      <div className={`design-tree__item${row.selected ? " is-selected" : ""}`} role="treeitem" aria-level={row.level}
        aria-expanded={row.expandable ? row.expanded : undefined} aria-selected={row.selected} aria-label={row.meta ? undefined : row.label}
        {...(row.target ? { "data-jarvis-target": row.target } : {})}
        tabIndex={row.id === stop ? 0 : -1} style={{ paddingInlineStart: `${0.4 + (row.level - 1) * 1}rem` }}
        ref={(node) => { if (node) refs.current.set(row.id, node); else refs.current.delete(row.id); }}
        onFocus={() => setTabStop(row.id)} onClick={() => { setTabStop(row.id); row.activate(); }}
        onKeyDown={(event) => onKeyDown(event, row, index)}>
        <span className="design-tree__twisty" aria-hidden="true">{row.expandable ? (row.expanded ? "▾" : "▸") : ""}</span>
        <span className="design-tree__label">{row.label}</span>
        {row.meta ? <span className="design-tree__meta" data-tone={row.meta.state} title={row.meta.detail}>{row.meta.state}</span> : null}
      </div>
    </li>)}
  </ul>;
}

export default DesignComponentTree;
