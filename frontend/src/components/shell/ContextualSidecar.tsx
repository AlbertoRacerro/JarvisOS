import { createContext, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { StageSelection } from "../../app/selection";
import Button from "../ui/Button";
import InlineNotice from "../ui/InlineNotice";

export type SidecarChromeValue = Readonly<{ close(): void; propertiesOpen: boolean; toggleProperties(): void }>;
/** Shell-owned Sidecar controls (Close, Properties) offered to the conversation header. */
export const SidecarChrome = createContext<SidecarChromeValue | null>(null);
type ContextualSidecarProps = Readonly<{
  open: boolean;
  selection: StageSelection | null;
  onClose(): void;
  content?: ReactNode;
  propertiesContent?: ReactNode;
}>;

type BindingStatusSelection = Extract<StageSelection, { kind: "bluecad-binding-status" }>;

function BindingStatusContext({ selection }: { selection: BindingStatusSelection }) {
  const title = selection.state === "resolving"
    ? "Resolving engineering binding"
    : selection.state === "unresolved"
      ? "Unresolved engineering binding"
      : "Ambiguous engineering binding";
  const detail = selection.state === "resolving"
    ? "Checking the current artifact binding. No engineering object is editable until resolution finishes."
    : selection.state === "unresolved"
      ? "Geometry remains viewable, but this hit cannot be mapped to a current engineering object. Candidate authority is unchanged."
      : "More than one authoritative binding is possible for this hit. No engineering object was selected.";
  return <div className="shell-properties__selection" role="status"><strong>{title}</strong><p>{detail}</p><details><summary>Technical details</summary><dl className="details"><div><dt>Workspace</dt><dd>{selection.workspaceId}</dd></div><div><dt>Candidate</dt><dd>{selection.candidateId}</dd></div><div><dt>Artifact</dt><dd>{selection.artifactId}</dd></div><div><dt>Viewer session</dt><dd>{selection.viewerSessionId}</dd></div><div><dt>Mesh inspection key</dt><dd>{selection.meshKey}</dd></div><div><dt>Semantic key</dt><dd>{selection.semanticKey}</dd></div></dl></details></div>;
}

function PropertiesFallback({ selection }: { selection: StageSelection | null }) {
  if (selection === null) {
    return <InlineNotice tone="neutral">No object selected. Select a current engineering or viewer object to inspect available properties.</InlineNotice>;
  }
  if (selection.kind === "geometry-hit") {
    return <div className="shell-properties__selection"><strong>Viewer geometry selection</strong><p>This hit is ephemeral viewer-session data and is not yet an engineering record.</p><details><summary>Technical details</summary><dl className="details"><div><dt>Viewer session</dt><dd>{selection.viewerSessionId}</dd></div><div><dt>Ephemeral object</dt><dd>{selection.ephemeralObjectId}</dd></div></dl></details></div>;
  }
  if (selection.kind === "bluecad-binding-status") {
    return <BindingStatusContext selection={selection} />;
  }
  if (selection.kind === "bluecad-part") {
    return <div className="shell-properties__selection"><strong>{selection.partId}</strong><p>{selection.partKind ? `${selection.partKind} · selected BlueCad part` : "Selected BlueCad part"}</p><details><summary>Technical details</summary><dl className="details"><div><dt>Workspace</dt><dd>{selection.workspaceId}</dd></div><div><dt>Candidate</dt><dd>{selection.candidateId}</dd></div><div><dt>Artifact</dt><dd>{selection.artifactId}</dd></div><div><dt>Viewer session</dt><dd>{selection.viewerSessionId}</dd></div><div><dt>Mesh inspection key</dt><dd>{selection.meshKey}</dd></div><div><dt>Semantic key</dt><dd>{selection.semanticKey}</dd></div></dl></details></div>;
  }
  return <div className="shell-properties__selection"><strong>Engineering record selection</strong><p>No editable model-contract Properties are available for this context yet. Current machine identity remains inspectable below.</p><details><summary>Technical details</summary><dl className="details"><div><dt>Resource</dt><dd>{selection.ref.resource}</dd></div><div><dt>Workspace</dt><dd>{selection.ref.workspaceId}</dd></div><div><dt>Record</dt><dd>{selection.ref.recordId}</dd></div></dl></details></div>;
}

function ContextualSidecar({ open, selection, onClose, content, propertiesContent }: ContextualSidecarProps) {
  const panelRef = useRef<HTMLElement | null>(null);
  const [propertiesOpen, setPropertiesOpen] = useState(false);
  useEffect(() => { if (open) panelRef.current?.querySelector<HTMLElement>("[data-sidecar-focus]")?.focus(); }, [open]);
  const onPanelKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (event.target instanceof HTMLElement && event.target.closest('[role="menu"]')) return;
    event.stopPropagation();
    onClose();
  };
  const chrome = useMemo<SidecarChromeValue>(() => ({
    close: onClose,
    propertiesOpen,
    toggleProperties: () => setPropertiesOpen((current) => !current)
  }), [onClose, propertiesOpen]);
  const semanticTarget = selection?.kind === "bluecad-part"
    ? <div className="shell-properties__selection"><strong>{selection.partId}</strong><p>{selection.partKind ? `${selection.partKind} · selected BlueCad part` : "Selected BlueCad part"}</p><details><summary>Technical details</summary><dl className="details"><div><dt>Workspace</dt><dd>{selection.workspaceId}</dd></div><div><dt>Candidate</dt><dd>{selection.candidateId}</dd></div><div><dt>Artifact</dt><dd>{selection.artifactId}</dd></div><div><dt>Viewer session</dt><dd>{selection.viewerSessionId}</dd></div><div><dt>Mesh inspection key</dt><dd>{selection.meshKey}</dd></div><div><dt>Semantic key</dt><dd>{selection.semanticKey}</dd></div></dl></details></div>
    : selection?.kind === "bluecad-binding-status"
      ? <BindingStatusContext selection={selection} />
      : null;
  if (!open) return null;
  // 161: the conversation owns the panel. Properties remain one click away as a
  // secondary view instead of permanently reserving half of the Sidecar.
  return <aside id="shell-sidecar" ref={panelRef} className="shell-panel shell-sidecar" aria-label="Jarvis" onKeyDown={onPanelKeyDown}>
    <SidecarChrome.Provider value={chrome}>
      <div className="shell-sidecar__view" hidden={propertiesOpen}>
        {content ?? <><div className="shell-panel__header"><h2 data-sidecar-focus tabIndex={-1}>Jarvis</h2><Button variant="ghost" onClick={onClose}>Close</Button></div><InlineNotice tone="neutral">Jarvis is unavailable for this route.</InlineNotice></>}
      </div>
      {propertiesOpen ? <section className="shell-sidecar__properties" aria-labelledby="shell-sidecar-properties-title">
        <header className="shell-panel__header"><h2 id="shell-sidecar-properties-title">Properties</h2><Button variant="ghost" onClick={() => setPropertiesOpen(false)}>Back to Jarvis</Button></header>
        {semanticTarget}
        {propertiesContent ?? <PropertiesFallback selection={selection} />}
      </section> : null}
    </SidecarChrome.Provider>
  </aside>;
}
export default ContextualSidecar;
