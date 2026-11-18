import { useCallback, useEffect, useState } from "react";
import { readActivity, readActivityDetail, type ActivityDetailRead, type ActivityRead, type ActivityRow } from "../api/coding";

// Spec 298: one read-only panel for long-running work. It starts, stops and changes nothing.
const POLL_OPEN_MS = 5_000;
const POLL_CLOSED_MS = 30_000;
export const ACTIVITY_LABEL = "Attività";
export const NO_DOWNLOADS = "nessun download registrato";

function elapsed(row: ActivityRow, now: number): string {
  const start = row.started_at ? Date.parse(row.started_at) : NaN;
  if (Number.isNaN(start)) return "";
  const end = row.ended_at ? Date.parse(row.ended_at) : now;
  const minutes = Math.max(0, Math.round((end - start) / 60_000));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

export function ActivityRowView({ row, now, onOpen }: Readonly<{ row: ActivityRow; now: number; onOpen?: (row: ActivityRow) => void }>) {
  const running = row.state === "running";
  return <li className="activity-row" data-state={row.state}>
    <button type="button" className="activity-row__open" onClick={() => onOpen?.(row)}>
      <strong>{row.title}</strong>
      <span className="activity-row__meta">{row.runner} · {elapsed(row, now)} · {row.state_word}</span>
      {row.progress
        ? <progress value={row.progress.done} max={row.progress.total} aria-label="Avanzamento" />
        : running ? <span className="activity-spinner" role="status" aria-label="In corso, avanzamento non misurabile" /> : null}
      {row.step ? <span className="activity-row__step">{row.step}</span> : null}
      {row.outcome && row.outcome !== row.state_word ? <span className="activity-row__outcome">{row.outcome}</span> : null}
    </button>
  </li>;
}

export function ActivityView({ data, now, onOpen }: Readonly<{ data: ActivityRead; now: number; onOpen?: (row: ActivityRow) => void }>) {
  return <div className="activity-view">
    <p className="activity-quota" aria-label="Quota disponibile">
      {data.quota.state === "ok"
        ? data.quota.pools.map((pool) => `${pool.label} ${pool.percent_free === null ? "n/d" : `${pool.percent_free}% libero`}`).join(" · ")
        : "Quota non disponibile"}
    </p>
    {data.groups.filter((group) => group.state !== "ok").map((group) =>
      <p key={group.id} className="activity-group-down">{group.title}: non disponibile ({group.reason})</p>)}
    {data.rows.length === 0 ? <p>Nessuna attività in corso.</p>
      : <ul className="activity-list">{data.rows.map((row) => <ActivityRowView key={row.key} row={row} now={now} onOpen={onOpen} />)}</ul>}
    <section aria-label="Download">
      <h3>Download</h3>
      {data.downloads.length === 0 ? <p>{data.downloads_empty || NO_DOWNLOADS}</p>
        : <ul className="activity-list">{data.downloads.map((row) => <ActivityRowView key={row.key} row={row} now={now} />)}</ul>}
    </section>
  </div>;
}

export default function ActivityControl() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ActivityRead | null>(null);
  const [failed, setFailed] = useState(false);
  const [detail, setDetail] = useState<{ row: ActivityRow; read: ActivityDetailRead | null } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const load = useCallback(() => {
    readActivity().then((next) => { setData(next); setFailed(false); setNow(Date.now()); }).catch(() => setFailed(true));
  }, []);
  useEffect(() => {
    load();
    const timer = window.setInterval(load, open ? POLL_OPEN_MS : POLL_CLOSED_MS);
    return () => window.clearInterval(timer);
  }, [load, open]);
  const openRow = (row: ActivityRow) => {
    setDetail({ row, read: null });
    readActivityDetail(row.key).then((read) => setDetail({ row, read })).catch(() => setDetail({ row, read: { state: "not_found", reason: null, title: null, label: null, lines: [] } }));
  };
  const count = data?.running ?? 0;
  return <>
    <button type="button" className="activity-control" aria-expanded={open} aria-controls="activity-panel" onClick={() => setOpen((v) => !v)}>
      {ACTIVITY_LABEL}{count > 0 ? <span className="activity-count" aria-label={`${count} in corso`}>{count}</span> : null}
    </button>
    {open ? <aside id="activity-panel" className="activity-panel" role="region" aria-label="Pannello attività">
      <header><h2>{ACTIVITY_LABEL}</h2><button type="button" onClick={() => { setOpen(false); setDetail(null); }}>Chiudi</button></header>
      {detail ? <section className="activity-detail" aria-label="Dettaglio attività">
        <button type="button" onClick={() => setDetail(null)}>Indietro</button>
        <h3>{detail.row.title}</h3>
        {detail.read === null ? <p>Caricamento…</p>
          : detail.read.state === "refused" ? <p>Il registro contiene dati riservati e non viene mostrato.</p>
          : detail.read.lines.length === 0 ? <p>Nessuna riga di registro leggibile.</p>
          : <pre>{detail.read.lines.join("\n")}</pre>}
        {detail.row.links.map((link) => <a key={link.target} href={link.target}>{link.label}</a>)}
      </section>
        : data ? <ActivityView data={data} now={now} onOpen={openRow} /> : <p>{failed ? "Attività non disponibile." : "Caricamento…"}</p>}
    </aside> : null}
  </>;
}
