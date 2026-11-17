import { useCallback, useEffect, useState } from "react";
import {
  compareBenchRuns, getBenchRun, listBenchRuns,
  type BenchCompare, type BenchRunDetail, type BenchRunList, type BenchRunRow, type BenchVerdict
} from "../api/benchRuns";

// Spec 299: read-only bench results. Nothing here starts, edits or deletes a run.
export const BENCH_TITLE = "Banco di prova";
export const BENCH_EMPTY = "Nessun risultato: i risultati compaiono qui quando l'assistente usa il banco di prova.";
export const VERDICT_LABELS: Record<BenchVerdict, string> = {
  usable: "Utilizzabile",
  usable_with_warnings: "Utilizzabile con avvertenze",
  unchecked: "Non verificato",
  not_usable: "Non utilizzabile"
};
export const CHECK_STATUS_LABELS: Record<string, string> = {
  pass: "Superato", warn: "Avvertenza", fail: "Fallito", not_evaluated: "Non valutato"
};
const EVALUATOR_NAMES: Record<string, string> = {
  "bluecad.solid": "Solido BlueCAD",
  "bluecad.mesh": "Mesh BlueCAD",
  "bluecad.fem": "Struttura statica (FEM)",
  "fluids.pipe_pressure_drop": "Perdita di carico in tubo",
  "ht.internal_convection": "Convezione interna",
  "coolprop.pure_fluid_properties": "Proprietà di fluido puro",
  "retrieval.eval": "Qualità del recupero documentale"
};
const RUN_STATUS_LABELS: Record<string, string> = { succeeded: "riuscita", failed: "fallita" };
const KNOWN_VERDICTS = new Set(Object.keys(VERDICT_LABELS));

export const evaluatorName = (id: string): string => EVALUATOR_NAMES[id] ?? id;
export const runStatusWord = (status: string): string => RUN_STATUS_LABELS[status] ?? status;
// The stored verdict is shown as is; anything outside the four values reads as unchecked.
export const verdictOf = (value: unknown): BenchVerdict =>
  typeof value === "string" && KNOWN_VERDICTS.has(value) ? (value as BenchVerdict) : "unchecked";
const num = (value: number | null | undefined): string => (value === null || value === undefined ? "—" : String(value));
const when = (value: string): string => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("it-IT");
};

export function VerdictLight({ verdict }: Readonly<{ verdict: unknown }>) {
  const safe = verdictOf(verdict);
  return <span className="bench-verdict" data-verdict={safe}>
    <span className="bench-light" aria-hidden="true" />{VERDICT_LABELS[safe]}
  </span>;
}

export function BenchRunsList({ data, selected, marked, onOpen, onMark }: Readonly<{
  data: BenchRunList; selected: string | null; marked: readonly string[];
  onOpen?: (run: BenchRunRow) => void; onMark?: (run: BenchRunRow) => void;
}>) {
  return <div className="bench-list">
    {data.runs.length === 0 ? <p>{BENCH_EMPTY}</p> : <ul>{data.runs.map((run) =>
      <li key={run.run_id} className="bench-row" data-selected={run.run_id === selected}>
        <input type="checkbox" aria-label={`Confronta ${run.run_id}`} checked={marked.includes(run.run_id)}
          onChange={() => onMark?.(run)} />
        <button type="button" className="bench-row__open" onClick={() => onOpen?.(run)}>
          <strong>{evaluatorName(run.evaluator_id)}</strong>
          <VerdictLight verdict={run.verdict} />
          <span className="bench-row__meta">{runStatusWord(run.status)} · {when(run.completed_at)}</span>
        </button>
      </li>)}</ul>}
    {data.skipped > 0 ? <p className="bench-note">{data.skipped} cartelle non leggibili ignorate.</p> : null}
  </div>;
}

export function BenchRunDetailView({ run }: Readonly<{ run: BenchRunDetail }>) {
  return <section className="bench-detail" aria-label="Dettaglio risultato">
    <h3>{evaluatorName(run.evaluator_id)}</h3>
    <p><VerdictLight verdict={run.verdict} /> · {runStatusWord(run.status)}</p>
    {run.failure ? <p className="bench-failure" role="alert">Errore{run.failure.code ? ` ${run.failure.code}` : ""}: {run.failure.message ?? "senza dettaglio"}</p> : null}
    <h4>Controlli</h4>
    {run.checks.length === 0 ? <p>Nessun controllo registrato.</p> :
      <table className="bench-checks"><thead><tr>
        <th>Controllo</th><th>Famiglia</th><th>Esito</th><th>Valore</th><th>Limite</th><th>Unità</th><th>Nota</th><th>Fonte</th>
      </tr></thead><tbody>{run.checks.map((check) =>
        <tr key={check.id} data-status={check.status}>
          <td>{check.id}</td><td>{check.family}</td>
          <td><span className="bench-light" aria-hidden="true" />{CHECK_STATUS_LABELS[check.status] ?? check.status}</td>
          <td>{num(check.actual)}</td><td>{num(check.limit)}</td><td>{check.unit ?? "—"}</td><td>{check.hint}</td><td>{check.source}</td>
        </tr>)}</tbody></table>}
    {run.truncated ? <p className="bench-note">Vista abbreviata: l'elenco dei controlli superati può essere incompleto.</p> : null}
    <h4>Risultati</h4>
    {run.outputs_withheld ? <p>Risultati non mostrati: il verdetto è «non utilizzabile».</p>
      : run.outputs.length === 0 ? <p>Nessun risultato numerico.</p>
      : <ul className="bench-outputs">{run.outputs.map((out) => <li key={out.name}>{out.name}: {out.value.value} {out.value.unit}</li>)}</ul>}
  </section>;
}

export function BenchCompareView({ doc }: Readonly<{ doc: BenchCompare }>) {
  const side = (s: { value: number | null; unit: string | null } | null) => (s ? `${num(s.value)} ${s.unit ?? ""}`.trim() : "—");
  return <section className="bench-compare" aria-label="Confronto risultati">
    <h3>Confronto</h3>
    <p>A: {doc.run_id_a} ({evaluatorName(doc.evaluator_a)}, <VerdictLight verdict={doc.verdict_a} />) — B: {doc.run_id_b} ({evaluatorName(doc.evaluator_b)}, <VerdictLight verdict={doc.verdict_b} />)</p>
    <table><thead><tr><th>Risultato</th><th>A</th><th>B</th><th>Differenza</th></tr></thead><tbody>
      {Object.entries(doc.outputs).map(([name, row]) => <tr key={name}>
        <td>{name}</td><td>{side(row.a)}</td><td>{side(row.b)}</td>
        <td>{row.mark === "match" ? `${num(row.absolute_difference)} ${row.unit ?? ""}`.trim()
          : row.mark === "unit_mismatch" ? "unità diverse" : "mancante"}</td>
      </tr>)}</tbody></table>
    <table className="bench-checks"><thead><tr><th>Controllo</th><th>A</th><th>B</th></tr></thead><tbody>
      {Object.entries(doc.checks).map(([id, row]) => <tr key={id}>
        <td>{id}</td>
        <td>{row.status_a ? `${CHECK_STATUS_LABELS[row.status_a] ?? row.status_a} (${num(row.actual_a)})` : "—"}</td>
        <td>{row.status_b ? `${CHECK_STATUS_LABELS[row.status_b] ?? row.status_b} (${num(row.actual_b)})` : "—"}</td>
      </tr>)}</tbody></table>
    {doc.truncated ? <p className="bench-note">Confronto abbreviato: solo le differenze principali.</p> : null}
  </section>;
}

export default function BenchRunsPanel({ workspaceId }: Readonly<{ workspaceId: string | null }>) {
  const [list, setList] = useState<BenchRunList | null>(null);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<BenchRunDetail | null>(null);
  const [marked, setMarked] = useState<string[]>([]);
  const [compare, setCompare] = useState<BenchCompare | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!workspaceId) return;
    setFailed(false);
    listBenchRuns(workspaceId).then(setList).catch(() => setFailed(true));
  }, [workspaceId]);
  useEffect(() => {
    setList(null); setSelected(null); setDetail(null); setMarked([]); setCompare(null); setError(null);
    load();
  }, [load]);

  const open = (run: BenchRunRow) => {
    if (!workspaceId) return;
    setSelected(run.run_id); setCompare(null); setError(null);
    getBenchRun(workspaceId, run.run_id).then(setDetail).catch(() => { setDetail(null); setError("Risultato non leggibile."); });
  };
  const mark = (run: BenchRunRow) => setMarked((current) =>
    current.includes(run.run_id) ? current.filter((id) => id !== run.run_id) : [...current, run.run_id].slice(-2));
  const runCompare = () => {
    if (!workspaceId || marked.length !== 2) return;
    setError(null);
    compareBenchRuns(workspaceId, marked[0], marked[1]).then((doc) => { setCompare(doc); setDetail(null); setSelected(null); })
      .catch(() => setError("Confronto non riuscito."));
  };

  return <section className="bench-panel" aria-label={BENCH_TITLE}>
    <header><h2>{BENCH_TITLE}</h2><button type="button" onClick={load}>Aggiorna</button></header>
    {failed ? <p role="alert">Elenco non disponibile.</p> : null}
    {list === null && !failed ? <p>Caricamento…</p> : null}
    {list ? <BenchRunsList data={list} selected={selected} marked={marked} onOpen={open} onMark={mark} /> : null}
    {list && list.runs.length > 0
      ? <button type="button" disabled={marked.length !== 2} onClick={runCompare}>Confronta i due selezionati</button> : null}
    {error ? <p role="alert">{error}</p> : null}
    {detail ? <BenchRunDetailView run={detail} /> : null}
    {compare ? <BenchCompareView doc={compare} /> : null}
  </section>;
}
