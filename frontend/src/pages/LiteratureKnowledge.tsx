import { useEffect, useMemo, useRef, useState } from "react";
import { listWorkspaces, type Workspace } from "../api/client";
import { createLiteratureEntry, createLiteratureSource, listLiteratureSources, literatureContentUrl,
  type LiteratureEntry, type LiteratureSource, type LiteratureEntryInput, type LiteratureSourceInput } from "../api/literature";
import "./memory-recovery.css";

import NumericInput from "../components/ui/NumericInput";
type Props = Readonly<{
  jarvis?: React.ReactNode;
  searchPanel?: React.ReactNode;
  kind: "literature";
  workspaceId: string | null;
  onWorkspaceChange: (workspaceId: string) => void;
  requestedSourceId?: string | null;
  requestedEntryId?: string | null;
  onKnowledgeSelectionChange?: (stableRef: string | null, label?: string) => void;
}>;

function locationLabel(entry: LiteratureEntry): string {
  if (!entry.locator_kind) return "Exact location not recorded";
  return `${entry.locator_kind} ${entry.locator_start}${entry.locator_end && entry.locator_end !== entry.locator_start ? `–${entry.locator_end}` : ""}`;
}
function entryValue(entry: LiteratureEntry): string {
  if (entry.entry_kind === "claim") return entry.statement ?? "Claim unavailable";
  if (entry.value_number !== null) return `${entry.value_number}${entry.unit ? ` ${entry.unit}` : ""}`;
  return `${entry.value_text ?? "Datum unavailable"}${entry.unit ? ` ${entry.unit}` : ""}`;
}

function normalizedSearch(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase().trim();
}

function matchesSearch(value: string, query: string): boolean {
  const terms = normalizedSearch(query).split(/\s+/).filter(Boolean);
  const haystack = normalizedSearch(value);
  return terms.every((term) => haystack.includes(term));
}
// Retain the same request key after a lost response; changing the payload starts a new write.
function useRequestKey() {
  const pending = useRef({ payload: "", key: "" });
  return (payload: object) => {
    const serialized = JSON.stringify(payload);
    if (serialized !== pending.current.payload) pending.current = { payload: serialized, key: crypto.randomUUID() };
    return pending.current.key;
  };
}

function SourceDisclosure({ source, open, onToggle, requestedEntryId, onSelect, onEntrySaved }: Readonly<{
  source: LiteratureSource; open: boolean; onToggle: (open: boolean) => void; requestedEntryId: string | null;
  onSelect: (ref: string, label?: string) => void; onEntrySaved: (entry: LiteratureEntry) => void;
}>) {
  const contentUrl = literatureContentUrl(source);
  const [kind, setKind] = useState<"claim" | "datum">("claim");
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState("");
  const [context, setContext] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const requestKey = useRequestKey();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const backingUnavailable = source.backing && source.backing.availability !== "available";
  const saveEntry = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || !value.trim()) return;
    const payload: LiteratureEntryInput = { entry_kind: kind, ...(kind === "claim" ? { statement: value.trim() } : { value_text: value.trim(), unit: unit.trim() || undefined }), context_text: context.trim() || undefined };
    setSaving(true); setError(null); setNotice("");
    try {
      const entry = await createLiteratureEntry(source.workspace_id, source.id, payload, requestKey(payload));
      if (!alive.current) return;
      onEntrySaved(entry); setValue(""); setUnit(""); setContext(""); setNotice("Saved as raw. This finding has not been accepted as engineering evidence.");
    } catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : "Finding could not be saved."); }
    finally { if (alive.current) setSaving(false); }
  };
  return <details className="final-fusion__disclosure" data-source-id={source.id} data-backing-availability={source.backing?.availability ?? "none"} open={open}
    onToggle={(event) => { const next = event.currentTarget.open; onToggle(next); }}>
    <summary className="final-fusion__disclosure-row" onClick={() => onSelect(source.source_ref, source.title)}><span><strong>{source.title}</strong><small>{source.source_kind}{source.published_year ? ` · ${source.published_year}` : ""} · {source.entries.length} findings</small></span><em>{source.state}{backingUnavailable ? ` · file ${source.backing?.availability}` : ""}</em></summary>
    <div className="final-fusion__disclosure-body">
      <div className="literature-source-content"><div>
        <h3>Source</h3>
        {backingUnavailable ? <p role="status"><strong>Backing unavailable.</strong> The citation remains saved, but its file cannot currently be used as evidence.</p> : null}
        <p>{source.citation || "Citation not recorded."}</p>{source.publisher ? <p>{source.publisher}</p> : null}
        <p>{source.backing?.filename ?? "Citation only · no source file attached."}</p>
        <button type="button" onClick={() => onSelect(source.source_ref, source.title)}>Select source for Jarvis</button>
        <details className="memory-technical"><summary>Source provenance</summary><small>{source.source_ref}</small>{source.backing ? <small>{source.backing.mime_type} · {source.backing.sha256}</small> : null}</details>
        <h3>Claims & data</h3>
        {!source.entries.length ? <p>No findings yet. Record a claim or datum below.</p> : source.entries.map((entry) => <details key={entry.id} className="literature-entry" data-entry-id={entry.id} open={entry.id === requestedEntryId || undefined}>
          <summary><strong>{entryValue(entry)}</strong><span>{entry.entry_kind} · {entry.status}</span></summary>
          <p>{locationLabel(entry)}</p>{entry.context_text ? <p>{entry.context_text}</p> : null}
          <button type="button" onClick={() => onSelect(`literature_entry:${entry.id}`, entryValue(entry))}>Select finding for Jarvis</button>
          <div><strong>Used by</strong>{!entry.used_by.length ? <p>No canonical records reference this finding.</p> : <ul>{entry.used_by.map((use) => <li key={use.ref}>{use.kind}: {use.title}</li>)}</ul>}</div>
          <details className="memory-technical"><summary>Finding provenance</summary><small>{entry.provenance_ref}</small></details>
        </details>)}
        <details className="literature-add-finding"><summary>Add a finding</summary>
          <form onSubmit={(event) => void saveEntry(event)}>
            <label>Finding type<select value={kind} disabled={saving} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="claim">Claim</option><option value="datum">Datum</option></select></label>
            <label>{kind === "claim" ? "Claim statement" : "Reported value"}<textarea required maxLength={kind === "claim" ? 8000 : 4000} value={value} disabled={saving} onChange={(event) => setValue(event.target.value)} /></label>
            {kind === "datum" ? <label>Unit (optional)<input value={unit} maxLength={128} disabled={saving} onChange={(event) => setUnit(event.target.value)} /></label> : null}
            <label>Context or location notes (optional)<textarea value={context} maxLength={12000} disabled={saving} onChange={(event) => setContext(event.target.value)} /></label>
            <p>Saved as raw. Notes are not a verified page or line reference.</p>
            <button type="submit" disabled={saving || !value.trim()}>{saving ? "Saving finding…" : "Save raw finding"}</button>
          </form>
          {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
        </details>
      </div><aside className="literature-preview">{contentUrl ? <><a href={contentUrl} target="_blank" rel="noreferrer">Open source ↗</a><iframe title={`Preview ${source.title}`} src={contentUrl} /></> : <p>Safe preview unavailable. {source.backing ? "The registered file cannot be previewed." : "This record contains citation metadata only."}</p>}</aside></div>
    </div>
  </details>;
}

export default function LiteratureKnowledge({ jarvis, searchPanel, workspaceId, onWorkspaceChange, requestedSourceId = null, requestedEntryId = null, onKnowledgeSelectionChange }: Props) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sources, setSources] = useState<LiteratureSource[]>([]);
  const [total, setTotal] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
  const [registerOpen, setRegisterOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [title, setTitle] = useState("");
  const [sourceKind, setSourceKind] = useState<LiteratureSource["source_kind"]>("paper");
  const [citation, setCitation] = useState("");
  const [publisher, setPublisher] = useState("");
  const [year, setYear] = useState("");
  const activeWorkspaceId = useMemo(() => workspaceId ?? workspaces[0]?.id ?? null, [workspaceId, workspaces]);
  const workspaceToken = useRef(activeWorkspaceId); workspaceToken.current = activeWorkspaceId;
  const requestKey = useRequestKey();
  const select = (ref: string, label?: string) => onKnowledgeSelectionChange?.(ref, label);
  useEffect(() => {
    let alive = true;
    void listWorkspaces().then((items) => { if (alive) { setWorkspaces(items); if (!workspaceId && items[0]) onWorkspaceChange(items[0].id); } })
      .catch((cause: unknown) => { if (alive) setError(cause instanceof Error ? cause.message : "Workspace read failed"); });
    return () => { alive = false; };
  }, [onWorkspaceChange, workspaceId]);
  useEffect(() => {
    let alive = true;
    setSources([]); setTotal(0); setNextOffset(null); setOpenIds(new Set()); setError(null); setQuery(""); setNotice(""); setSaving(false); setTitle(""); setCitation(""); setPublisher(""); setYear(""); setRegisterOpen(false);
    onKnowledgeSelectionChange?.(null);
    if (!activeWorkspaceId) { setLoading(false); return; }
    setLoading(true);
    void listLiteratureSources(activeWorkspaceId).then((page) => { if (alive) { setSources(page.items); setTotal(page.total); setNextOffset(page.next_offset); } })
      .catch((cause: unknown) => { if (alive) setError(cause instanceof Error ? cause.message : "Literature read failed"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [activeWorkspaceId, onKnowledgeSelectionChange]);
  useEffect(() => {
    if (!requestedSourceId) return;
    const source = sources.find((item) => item.id === requestedSourceId);
    if (!source) return;
    setOpenIds((current) => new Set([...current, source.id]));
    const entry = source.entries.find((item) => item.id === requestedEntryId);
    onKnowledgeSelectionChange?.(entry ? `literature_entry:${entry.id}` : source.source_ref, entry ? entryValue(entry) : source.title);
  }, [requestedSourceId, requestedEntryId, sources, onKnowledgeSelectionChange]);
  const loadMore = async () => {
    if (!activeWorkspaceId || nextOffset === null || loading) return;
    const ws = activeWorkspaceId; setLoading(true); setError(null);
    try { const page = await listLiteratureSources(ws, nextOffset); if (workspaceToken.current === ws) { setSources((current) => [...current, ...page.items.filter((item) => !current.some((old) => old.id === item.id))]); setNextOffset(page.next_offset); setTotal(page.total); } }
    catch (cause) { if (workspaceToken.current === ws) setError(cause instanceof Error ? cause.message : "Literature read failed"); }
    finally { if (workspaceToken.current === ws) setLoading(false); }
  };
  const saveSource = async (event: React.FormEvent) => {
    event.preventDefault(); if (!activeWorkspaceId || saving || !title.trim()) return;
    const ws = activeWorkspaceId;
    const payload: LiteratureSourceInput = { title: title.trim(), source_kind: sourceKind, citation: citation.trim() || null, publisher: publisher.trim() || null, published_year: year ? Number(year) : null };
    setSaving(true); setError(null); setNotice("");
    try {
      const source = await createLiteratureSource(ws, payload, requestKey({ ws, ...payload }));
      if (workspaceToken.current !== ws) return;
      setSources((current) => [source, ...current.filter((item) => item.id !== source.id)]); setTotal((current) => current + 1); setOpenIds((current) => new Set([...current, source.id]));
      setTitle(""); setCitation(""); setPublisher(""); setYear(""); setQuery(""); setRegisterOpen(false); setNotice("Source saved as raw. You can now record claims and data."); select(source.source_ref, source.title);
    } catch (cause) { if (workspaceToken.current === ws) setError(cause instanceof Error ? cause.message : "Source could not be saved."); }
    finally { if (workspaceToken.current === ws) setSaving(false); }
  };
  const filtered = sources.filter((source) => matchesSearch(`${source.title} ${source.citation ?? ""} ${source.publisher ?? ""} ${source.entries.map(entryValue).join(" ")}`, query));
  return <div className={`final-fusion__workbench final-fusion__workbench--literature literature-recovery${searchPanel ? " literature-recovery--with-search" : ""}`}>
    {searchPanel ? <section className="final-fusion__panel literature-search">{searchPanel}</section> : null}
    <section className="final-fusion__panel literature-library">
      <div className="final-fusion__panel-heading"><div><h2>Literature</h2><p>Publications, reference sources and the findings you record from them.</p></div><button type="button" disabled={!activeWorkspaceId} onClick={() => setRegisterOpen((value) => !value)} aria-expanded={registerOpen}>+ Register source</button></div>
      <div className="literature-toolbar"><span>{total} sources</span><button type="button" onClick={() => setOpenIds(new Set())}>Collapse all</button></div>
      <p className="literature-context-note">Browsing does not add Jarvis context. Select a source or finding, then add it explicitly in Jarvis.</p>
      {registerOpen ? <form className="literature-register" onSubmit={(event) => void saveSource(event)}><h3>Register a citation</h3><p>Save source metadata here. File upload is not available in this workspace.</p>
        <label>Source title<input required autoFocus maxLength={500} value={title} disabled={saving} onChange={(event) => setTitle(event.target.value)} /></label>
        <label>Source type<select value={sourceKind} disabled={saving} onChange={(event) => setSourceKind(event.target.value as typeof sourceKind)}>{["paper", "book", "report", "standard", "dataset", "web", "other"].map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></label>
        <label>Citation, DOI or URL (optional)<textarea maxLength={4000} value={citation} disabled={saving} onChange={(event) => setCitation(event.target.value)} /></label>
        <details><summary>Publication details</summary><label>Publisher<input maxLength={500} value={publisher} disabled={saving} onChange={(event) => setPublisher(event.target.value)} /></label><label>Publication year<NumericInput min={0} max={9999} value={year} disabled={saving} onValueChange={(text) => setYear(text)} /></label></details>
        <button type="submit" disabled={saving || !title.trim()}>{saving ? "Saving source…" : "Save source"}</button><button type="button" disabled={saving} onClick={() => setRegisterOpen(false)}>Cancel</button>
      </form> : null}
      <label className="literature-filter">Find in loaded sources<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Title, citation or finding…" /></label>
      {notice ? <p role="status">{notice}</p> : null}{loading ? <p>Loading literature…</p> : null}
      {error ? <div role="alert"><strong>Literature unavailable</strong><p>{error}</p></div> : null}
      {requestedSourceId && !loading && !sources.some((source) => source.id === requestedSourceId) ? <p role="status">Requested source is not in the loaded list. {nextOffset !== null ? "Load more sources below." : "It may no longer be available."}</p> : null}
      {!loading && !error && !sources.length ? <div className="final-fusion__source-empty"><strong>No literature sources yet</strong><p>Register a source above, then record the claims and data you want to keep.</p></div> : null}
      {sources.length > 0 && !filtered.length ? <p>No loaded sources match. Try another phrase{nextOffset !== null ? " or load more sources" : ""}.</p> : null}
      <div className="final-fusion__source-list">{filtered.map((source) => <SourceDisclosure key={`${activeWorkspaceId}:${source.id}`} source={source} open={openIds.has(source.id)} onToggle={(open) => setOpenIds((current) => { if (current.has(source.id) === open) return current; const next = new Set(current); if (open) next.add(source.id); else next.delete(source.id); return next; })} requestedEntryId={source.id === requestedSourceId ? requestedEntryId : null} onSelect={select} onEntrySaved={(entry) => setSources((current) => current.map((item) => item.id === source.id ? { ...item, entries: [...item.entries.filter((old) => old.id !== entry.id), entry] } : item))} />)}</div>
      {nextOffset !== null ? <button type="button" disabled={loading} onClick={() => void loadMore()}>Load more sources</button> : null}
    </section><section className="final-fusion__panel final-fusion__jarvis" aria-label="Jarvis">{jarvis}</section>
  </div>;
}
