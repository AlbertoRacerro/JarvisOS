import { useEffect, useRef, useState } from "react";

import {
  previewKnowledgeContext,
  proposeKnowledgeAction,
  type JarvisExactRef,
  type KnowledgeContextPreview,
  type KnowledgeOwner,
  type KnowledgeProposal,
  type KnowledgeRouteId,
  type StableKnowledgeRef
} from "../../api/knowledgeActions";

type Props = Readonly<{
  workspaceId: string | null;
  routeId: string;
  stableRef: string | null;
  selectedLabel?: string | null;
  onContextChange?: (preview: KnowledgeContextPreview | null) => void;
}>;

const OWNERS: Partial<Record<KnowledgeRouteId, KnowledgeOwner>> = {
  "memory-project-basis": "modeling",
  "memory-models": "model-dossier",
  "memory-literature": "literature"
};

function isKnowledgeRoute(routeId: string): routeId is KnowledgeRouteId {
  return routeId in OWNERS;
}

function emptyBasket(): Record<KnowledgeRouteId, string[]> {
  return {
    "memory-project-basis": [],
    "memory-models": [],
    "memory-literature": []
  };
}

function emptyPreviews(): Partial<Record<KnowledgeRouteId, KnowledgeContextPreview>> {
  return {};
}

function boundedManifestEntry(entry: Record<string, unknown>): string {
  const serialized = JSON.stringify(entry);
  return serialized.length > 800 ? `${serialized.slice(0, 797)}…` : serialized;
}

function sameExactRef(left: JarvisExactRef, right: JarvisExactRef): boolean {
  return left.workspace_id === right.workspace_id
    && left.owner === right.owner
    && left.kind === right.kind
    && left.id === right.id
    && left.version === right.version
    && left.revision === right.revision
    && left.immutable_ref === right.immutable_ref
    && left.content_digest === right.content_digest;
}

export default function JarvisKnowledgeActions({ workspaceId, routeId, stableRef, selectedLabel, onContextChange }: Props) {
  const [basket, setBasket] = useState<Record<KnowledgeRouteId, string[]>>(emptyBasket);
  const [previews, setPreviews] = useState<Partial<Record<KnowledgeRouteId, KnowledgeContextPreview>>>(emptyPreviews);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [intent, setIntent] = useState("");
  const [proposal, setProposal] = useState<KnowledgeProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestGeneration = useRef(0);

  useEffect(() => {
    requestGeneration.current += 1;
    setBasket(emptyBasket());
    setLabels({});
    setPreviews(emptyPreviews());
    setProposal(null);
    setIntent("");
    setError(null);
    setBusy(false);
  }, [workspaceId]);

  useEffect(() => {
    requestGeneration.current += 1;
    setProposal(null);
    setIntent("");
    setError(null);
    setBusy(false);
  }, [routeId, stableRef]);

  useEffect(() => {
    const current = isKnowledgeRoute(routeId) ? previews[routeId] : null;
    onContextChange?.(current?.workspace_id === workspaceId ? current : null);
  }, [previews, routeId, workspaceId, onContextChange]);

  if (!isKnowledgeRoute(routeId)) return null;
  const owner = OWNERS[routeId];
  if (!owner) return null;
  const routeBasket = basket[routeId];
  const preview = previews[routeId] ?? null;

  const refsFor = (stableRefs: string[]): StableKnowledgeRef[] => stableRefs.map((ref) => ({ owner, stable_ref: ref }));

  const inspectBasket = async (stableRefs: string[]) => {
    if (!workspaceId || stableRefs.length === 0) return null;
    return previewKnowledgeContext(workspaceId, routeId, refsFor(stableRefs));
  };

  const addContext = async () => {
    if (!workspaceId || !stableRef || busy) return;
    const nextRefs = routeBasket.includes(stableRef) ? routeBasket : [...routeBasket, stableRef];
    const generation = requestGeneration.current;
    setBusy(true);
    setError(null);
    setProposal(null);
    try {
      const nextPreview = await inspectBasket(nextRefs);
      if (generation !== requestGeneration.current || !nextPreview) return;
      if (preview && preview.exact_refs.some((exactRef, index) => !nextPreview.exact_refs[index] || !sameExactRef(exactRef, nextPreview.exact_refs[index]))) {
        setError("Previously inspected context changed. Remove stale context and inspect the intended evidence again.");
        return;
      }
      setLabels(current => ({ ...current, ...(stableRef && selectedLabel ? { [stableRef]: selectedLabel } : {}) }));
      setBasket((current) => ({ ...current, [routeId]: nextRefs }));
      setPreviews((current) => ({ ...current, [routeId]: nextPreview }));
    } catch (caught) {
      if (generation !== requestGeneration.current) return;
      setError(caught instanceof Error ? caught.message : "Exact context preview failed");
    } finally {
      if (generation === requestGeneration.current) setBusy(false);
    }
  };

  const removeContext = async (refToRemove: string) => {
    if (!workspaceId || busy) return;
    const removedIndex = routeBasket.indexOf(refToRemove);
    const nextRefs = routeBasket.filter((ref) => ref !== refToRemove);
    const generation = requestGeneration.current;
    setBusy(true);
    setError(null);
    setProposal(null);
    try {
      if (nextRefs.length === 0) {
        if (generation !== requestGeneration.current) return;
        setBasket((current) => ({ ...current, [routeId]: [] }));
        setPreviews((current) => {
          const next = { ...current };
          delete next[routeId];
          return next;
        });
        return;
      }
      const nextPreview = await inspectBasket(nextRefs);
      if (generation !== requestGeneration.current || !nextPreview) return;
      if (preview) {
        const expectedRefs = preview.exact_refs.filter((_, index) => index !== removedIndex);
        if (expectedRefs.length !== nextPreview.exact_refs.length || expectedRefs.some((exactRef, index) => !sameExactRef(exactRef, nextPreview.exact_refs[index]))) {
          setError("Remaining context changed. Clear stale context before continuing.");
          return;
        }
      }
      setBasket((current) => ({ ...current, [routeId]: nextRefs }));
      setPreviews((current) => ({ ...current, [routeId]: nextPreview }));
    } catch (caught) {
      if (generation !== requestGeneration.current) return;
      setError(caught instanceof Error ? caught.message : "Exact context preview failed");
    } finally {
      if (generation === requestGeneration.current) setBusy(false);
    }
  };

  const propose = async () => {
    const text = intent.trim();
    if (!preview || !text || busy) return;
    const generation = requestGeneration.current;
    setBusy(true);
    setError(null);
    setProposal(null);
    try {
      const nextProposal = await proposeKnowledgeAction(preview, text);
      if (generation === requestGeneration.current) setProposal(nextProposal);
    } catch (caught) {
      if (generation !== requestGeneration.current) return;
      setError(caught instanceof Error ? caught.message : "Knowledge proposal failed");
    } finally {
      if (generation === requestGeneration.current) setBusy(false);
    }
  };

  return <section className="jarvis-sidecar__stage-context jarvis-sidecar__stage-context--visible" aria-label="Knowledge actions" data-testid="knowledge-actions">
    <strong>Selected context</strong>
    <p>{stableRef ? selectedLabel || `Selected ${stableRef.split(":")[0].replace(/_/g, " ")}` : "Select a Project search result or exact model/literature record first."}</p>
    <button type="button" onClick={() => void addContext()} disabled={!workspaceId || !stableRef || busy || Boolean(stableRef && routeBasket.includes(stableRef))}>
      {busy && !preview ? "Inspecting…" : stableRef && routeBasket.includes(stableRef) ? "Selected context added" : "Add selected to Jarvis context"}
    </button>
    {routeBasket.length ? <div data-testid="knowledge-context-basket">
      <button type="button" onClick={() => { requestGeneration.current += 1; setBasket(current => ({ ...current, [routeId]: [] })); setPreviews(current => ({ ...current, [routeId]: undefined })); setProposal(null); setError(null); setBusy(false); }}>Clear context</button>
      <p><strong>Context basket</strong> · {routeBasket.length} exact selection{routeBasket.length === 1 ? "" : "s"}</p>
      <ul>{routeBasket.map((ref) => <li key={ref}><span>{labels[ref] ?? `${ref.split(":")[0].replace(/_/g, " ")} ${routeBasket.indexOf(ref) + 1}`}</span> <button type="button" onClick={() => void removeContext(ref)} disabled={busy}>Remove</button></li>)}</ul>
    </div> : null}
    {preview ? <div data-testid="knowledge-context-preview">
      <p><strong>Inspected context</strong> · {preview.included_count} exact ref{preview.included_count === 1 ? "" : "s"}</p>
      <details><summary>Technical context identity</summary><code>{preview.context_digest}</code></details>
      <small>{preview.estimated_token_count} estimated tokens</small>
      <details>
        <summary>Inspected source manifest · {preview.context_sources_manifest.length}</summary>
        <ul>{preview.context_sources_manifest.map((source, index) => <li key={`${index}:${boundedManifestEntry(source)}`}><code>{boundedManifestEntry(source)}</code></li>)}</ul>
      </details>
      <p>{routeId === "memory-project-basis" ? "Exact-context discussion is unavailable for Project Basis. You can prepare a written proposal for review." : "These exact records will accompany your next local message, subject to sensitivity checks."}</p><details><summary>Prepare a written proposal</summary><p>This creates a deterministic draft for review.</p><label htmlFor="knowledge-proposal-intent">Your proposed change</label>
      <textarea id="knowledge-proposal-intent" rows={3} maxLength={4000} value={intent} onChange={(event) => setIntent(event.target.value)} disabled={busy} placeholder="Describe your proposed change to the selected records…" />
      <button type="button" onClick={() => void propose()} disabled={!intent.trim() || busy}>{busy ? "Preparing…" : "Prepare proposal"}</button></details>
    </div> : null}
    {proposal ? <div data-testid="knowledge-proposal">
      <p><strong>Prepared proposal · {proposal.target_domain.replace(/_/g, " ")}</strong></p><p>This is a deterministic draft of your instruction, not an AI answer. No record was changed.</p>
      <p>{proposal.summary}</p>
      {proposal.proposed_items.length ? <ul>{proposal.proposed_items.map((item) => <li key={item}>{item}</li>)}</ul> : null}
      {proposal.authoritative_next_action ? <small>Authoritative next action: {proposal.authoritative_next_action}</small> : null}
      <small>No domain mutation was performed.</small>
    </div> : null}
    {error ? <p role="status">{error}</p> : null}
  </section>;
}