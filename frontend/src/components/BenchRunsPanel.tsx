import { useCallback, useEffect, useState, type FormEvent } from "react";
import NumericInput from "./ui/NumericInput";
import {
  compareBenchRuns, createBenchRun, getBenchRun, listBenchCards, listBenchRuns,
  type BenchCard, type BenchCompare, type BenchRunDetail, type BenchRunList, type BenchRunRow, type BenchVerdict
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
  "bluecad.solid_code": "Solido da codice (BlueCAD)",
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
        <th scope="col">Controllo</th><th scope="col">Famiglia</th><th scope="col">Esito</th><th scope="col">Valore</th><th scope="col">Limite</th><th scope="col">Unità</th><th scope="col">Nota</th><th scope="col">Fonte</th>
      </tr></thead><tbody>{run.checks.map((check) =>
        <tr key={check.id} data-status={check.status}>
          <td>{check.id}</td><td>{check.family}</td>
          <td><span className="bench-light" aria-hidden="true" />{CHECK_STATUS_LABELS[check.status] ?? check.status}</td>
          <td>{num(check.actual)}</td><td>{num(check.limit)}</td><td>{check.unit ?? "—"}</td><td>{check.hint}</td><td>{check.source}</td>
        </tr>)}</tbody></table>}
    {run.truncated ? <p className="bench-note">Vista abbreviata: i controlli superati o non valutati possono mancare.</p> : null}
    {run.ports && run.ports.length > 0 ? <>
      <h4>Porte</h4>
      <ul className="bench-ports">{run.ports.map((port) =>
        <li key={port.name}>{port.name} ({port.interface}{port.outer_d !== undefined ? `, Ø ${port.outer_d} mm` : port.pad_d !== undefined ? `, Ø ${port.pad_d} mm` : ""})</li>)}</ul>
    </> : null}
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
    <table><thead><tr><th scope="col">Risultato</th><th scope="col">A</th><th scope="col">B</th><th scope="col">Differenza</th></tr></thead><tbody>
      {Object.entries(doc.outputs).map(([name, row]) => <tr key={name}>
        <td>{name}</td><td>{side(row.a)}</td><td>{side(row.b)}</td>
        <td>{row.mark === "match" ? `${num(row.absolute_difference)} ${row.unit ?? ""}`.trim()
          : row.mark === "unit_mismatch" ? "unità diverse" : "mancante"}</td>
      </tr>)}</tbody></table>
    <table className="bench-checks"><thead><tr><th scope="col">Controllo</th><th scope="col">A</th><th scope="col">B</th></tr></thead><tbody>
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
  const [cards, setCards] = useState<BenchCard[]>([]);
  const [cardId, setCardId] = useState("");
  const [inputs, setInputs] = useState<Record<string, number>>({});
  const [options, setOptions] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);

  const selectedCard = cards.find((card) => card.card_id === cardId) ?? null;

  const chooseCard = (nextId: string) => {
    const card = cards.find((item) => item.card_id === nextId);
    setCardId(nextId);
    setInputs(Object.fromEntries((card?.fields ?? []).map((field) => [field.name, field.default])));
    setOptions(Object.fromEntries((card?.options ?? []).map((option) => [option.name, option.default])));
  };

  const load = useCallback(() => {
    if (!workspaceId) return;
    setFailed(false);
    listBenchRuns(workspaceId).then(setList).catch(() => setFailed(true));
  }, [workspaceId]);
  useEffect(() => {
    setList(null); setSelected(null); setDetail(null); setMarked([]); setCompare(null); setError(null);
    setCards([]); setCardId(""); setInputs({}); setOptions({});
    load();
    if (workspaceId) {
      listBenchCards(workspaceId).then((catalog) => {
        setCards(catalog.cards);
        if (catalog.cards[0]) {
          const first = catalog.cards[0];
          setCardId(first.card_id);
          setInputs(Object.fromEntries(first.fields.map((field) => [field.name, field.default])));
          setOptions(Object.fromEntries(first.options.map((option) => [option.name, option.default])));
        }
      }).catch(() => setError("Catalogo delle schede non disponibile."));
    }
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
  const executeCard = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!workspaceId || !selectedCard) return;
    setRunning(true); setError(null);
    createBenchRun(workspaceId, { card_id: selectedCard.card_id, inputs, options })
      .then((run) => {
        setSelected(run.run_id); setCompare(null); setDetail(run);
        listBenchRuns(workspaceId).then(setList).catch(() => setFailed(true));
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "Esecuzione non riuscita."))
      .finally(() => setRunning(false));
  };

  return <section className="bench-panel" aria-label={BENCH_TITLE}>
    <header><h2>{BENCH_TITLE}</h2><button type="button" onClick={load}>Aggiorna</button></header>
    <section className="bench-card-form" aria-label="Nuova esecuzione">
      <h3>Nuova esecuzione</h3>
      {cards.length > 0 ? <form onSubmit={executeCard}>
        <label>Correlazione
          <select value={cardId} onChange={(event) => chooseCard(event.target.value)}>
            {cards.map((card) => <option key={card.card_id} value={card.card_id}>{card.title}</option>)}
          </select>
        </label>
        {selectedCard ? <>
          <p><strong>Formula:</strong> {selectedCard.formula}</p>
          <p><strong>Intervallo di validità:</strong> {selectedCard.validity}</p>
          <p><strong>Fonte:</strong> {selectedCard.source}</p>
          {selectedCard.fields.map((field) =>
            <label key={field.name}>{field.label} ({field.unit})
              <NumericInput required value={inputs[field.name] ?? field.default}
                onValueChange={(value) => setInputs((current) => ({ ...current, [field.name]: Number(value) }))} />
            </label>)}
          {selectedCard.options.map((option) =>
            <label key={option.name}>{option.label}
              <select value={options[option.name] ?? option.default}
                onChange={(event) => setOptions((current) => ({ ...current, [option.name]: event.target.value }))}>
                {option.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
              </select>
            </label>)}
          <button type="submit" disabled={running}>{running ? "Esecuzione…" : "Esegui"}</button>
        </> : null}
      </form> : <p>Caricamento delle schede…</p>}
    </section>
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
