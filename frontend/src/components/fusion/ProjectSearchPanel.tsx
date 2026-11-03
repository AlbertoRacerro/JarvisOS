import { useEffect, useRef, useState, type FormEvent } from "react";

import { projectSearch, type ProjectSearchResult } from "../../api/projectSearch";

type Props = Readonly<{
  workspaceId: string | null;
  navigate: (path: string) => void;
}>;

type SearchState = "idle" | "loading" | "results" | "empty" | "error";

function navigationTarget(result: ProjectSearchResult): string {
  const params = new URLSearchParams(result.route_params);
  const suffix = params.toString();
  return suffix ? `${result.route}?${suffix}` : result.route;
}

function resultCue(result: ProjectSearchResult): string {
  const cues = [result.kind.replace(/_/g, " "), result.lifecycle_or_status, result.version_or_revision].filter(Boolean);
  return cues.join(" · ");
}

export default function ProjectSearchPanel({ workspaceId, navigate }: Props) {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<ProjectSearchResult[]>([]);
  const [state, setState] = useState<SearchState>("idle");
  const [truncated, setTruncated] = useState(false);
  const requestGeneration = useRef(0);

  useEffect(() => {
    requestGeneration.current += 1;
    setItems([]);
    setTruncated(false);
    setState("idle");
  }, [workspaceId]);

  function changeQuery(nextQuery: string) {
    requestGeneration.current += 1;
    setQuery(nextQuery);
    setItems([]);
    setTruncated(false);
    setState("idle");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = query.trim();
    if (!workspaceId || normalized.length < 2) {
      requestGeneration.current += 1;
      setItems([]);
      setTruncated(false);
      setState("idle");
      return;
    }
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    const controller = new AbortController();
    const requestWorkspace = workspaceId;
    setState("loading");
    try {
      const response = await projectSearch(requestWorkspace, normalized, controller.signal);
      if (requestGeneration.current !== generation) return;
      setItems(response.items);
      setTruncated(response.truncated);
      setState(response.items.length === 0 ? "empty" : "results");
    } catch {
      if (requestGeneration.current !== generation) return;
      setItems([]);
      setTruncated(false);
      setState("error");
    }
  }

  return <section className="final-fusion__panel" aria-label="Project search">
    <div className="final-fusion__panel-heading">
      <div>
        <span className="final-fusion__eyebrow">Project knowledge</span>
        <h2>Search</h2>
        <p>Find requirements, models and sources using ordinary words.</p>
      </div>
    </div>
    <form onSubmit={submit} role="search">
      <label>
        <span className="final-fusion__eyebrow">Search project records</span>
        <input
          aria-label="Search project records"
          value={query}
          onChange={(event) => changeQuery(event.target.value)}
          placeholder="Requirement, parameter, model, literature…"
          disabled={!workspaceId}
        />
      </label>
      <button type="submit" disabled={!workspaceId || query.trim().length < 2 || state === "loading"}>Search</button>
    </form>
    {state === "idle" ? <p className="final-fusion__source-empty">Enter at least two characters to search this workspace.</p> : null}
    {state === "loading" ? <p className="final-fusion__source-empty" role="status">Searching project records…</p> : null}
    {state === "empty" ? <p className="final-fusion__source-empty">No records match. Try fewer words or another term.</p> : null}
    {state === "error" ? <div className="final-fusion__source-empty" role="alert"><strong>Project search unavailable</strong><p>The bounded owner reads did not complete. No partial result set is shown.</p></div> : null}
    {state === "results" ? <div className="final-fusion__source-list">
      {truncated ? <p role="status">Showing the first bounded results. Refine the query to narrow the project search.</p> : null}
      {items.map((result) => <article key={result.stable_ref} className="final-fusion__record-card" data-search-ref={result.stable_ref}>
        <div className="final-fusion__record-heading"><strong>{result.title}</strong><em>{resultCue(result)}</em></div>
        {result.summary ? <p>{result.summary}</p> : null}
        {result.provenance_refs.length > 0 ? <small>Provenance: {result.provenance_refs.join(", ")}</small> : null}
        {result.source_refs.length > 0 ? <small>Source: {result.source_refs.join(", ")}</small> : null}
        <button type="button" onClick={() => navigate(navigationTarget(result))}>Open in {result.owner === "modeling" ? "Project Basis" : result.owner === "model-dossier" ? "Models" : "Literature"}</button>
      </article>)}
    </div> : null}
  </section>;
}
