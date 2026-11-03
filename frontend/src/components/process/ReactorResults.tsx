import type { DraftObject, KineticsRecord, ReportedValue, ResultsState } from "../../api/processDraft";
import { FAILURE_WORDS, formatNumber, kineticsFindingText } from "./kineticsLogic";
import { Empty, StatusLine } from "./RunResults";

const DUTY_KEY = /(^|[ _])(duty|heat\s*load|heat\s*flow|energy)/i;
const REACTION_OUTPUT = /^R\d+[: ]/;

/** Results tab for a PFR or CSTR running a typed rate law: DWSIM's solve, checked by Jarvis (spec 180 sections 6 and 8). */
export default function ReactorResults({ unit, record, reported, results }: {
  unit: DraftObject; record: KineticsRecord | undefined; reported: Record<string, ReportedValue> | undefined; results: ResultsState;
}) {
  const stale = results.state === "stale";
  // Before a typed rate-law result the same rows show `empty` under one status line.
  if (!record) return <section className="reactor-results" aria-label="Reactor results">
    <h4>Results · DWSIM</h4>
    <StatusLine tone="idle">{results.state === "none" ? "No run yet" : "No rate-law result in the last Run"}</StatusLine>
    <dl className="reactor-rows">{["Conversion", "Residence time", "Outlet temperature", "Duty",
      ...(unit.type === "PFR" ? ["Rate at inlet", "Rate at outlet"] : ["Rate (at outlet conditions)"]), "Extent of reaction", "Verification residual",
    ].map((label) => <div key={label}><dt>{label}</dt><dd><Empty /></dd></div>)}</dl>
  </section>;
  const failed = !record.verified;
  const summary = record.summary ?? {};
  const concentrations = Object.entries(summary.outlet_concentrations_kmol_m3 ?? {});
  const duty = Object.entries(reported ?? {}).find(([key]) => DUTY_KEY.test(key) && !REACTION_OUTPUT.test(key));
  const rows: [string, string][] = [];
  if (!failed) {
    rows.push([`Conversion of ${record.base_reactant ?? "base reactant"}`, record.conversion == null ? "empty" :`${formatNumber(record.conversion * 100)} %`]);
    if (summary.residence_time_h !== undefined) rows.push(["Residence time", `${formatNumber(summary.residence_time_h)} h`]);
    if (summary.outlet_temperature_K !== undefined) rows.push(["Outlet temperature", `${formatNumber(summary.outlet_temperature_K)} K`]);
    if (duty) rows.push(["Duty", `${formatNumber(Number(duty[1].value))} ${duty[1].units ?? ""}`.trim()]);
    if (unit.type === "PFR" && record.rate_inlet != null) rows.push(["Rate at inlet", `${formatNumber(record.rate_inlet)} kmol/(m³·h)`]);
    if (record.rate_outlet != null) rows.push([unit.type === "PFR" ? "Rate at outlet" : "Rate (at outlet conditions)", `${formatNumber(record.rate_outlet)} kmol/(m³·h)`]);
    if (record.extent_kmol_h != null) rows.push(["Extent of reaction", `${formatNumber(record.extent_kmol_h)} kmol/h`]);
    if (record.residual != null) rows.push(["Verification residual", `${formatNumber(record.residual, 3)} (tolerance ${formatNumber(record.tolerance, 3)})`]);
  }
  const findings = record.findings ?? [];
  return <section className={`reactor-results${stale ? " is-stale" : ""}`} aria-label="Reactor results">
    <h4>Results · DWSIM{stale ? " (stale)" : ""}</h4>
    {stale && <p className="reactor-note">Previous Run: the draft has changed since, so these values may no longer apply.</p>}
    {record.verified && !stale && <span className="reactor-chip reactor-chip--good">Verified by Jarvis</span>}
    {record.verified && stale && <span className="reactor-chip reactor-chip--warn">Verified on a previous Run</span>}
    {failed && <div className="reactor-failure" role="alert">
      <strong>Not verified · {record.code ?? "KINETICS_VERIFICATION_FAILED"}</strong>
      <p>{FAILURE_WORDS[record.code ?? "KINETICS_VERIFICATION_FAILED"] ?? "Jarvis could not verify this reactor's result."}</p>
    </div>}
    {rows.length > 0 && <dl className="reactor-rows">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
    {!failed && concentrations.length > 0 && <>
      <h5>Outlet concentrations</h5>
      <dl className="reactor-rows">{concentrations.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{formatNumber(value)} kmol/m³</dd></div>)}</dl>
    </>}
    {findings.length > 0 && <ul className="reactor-findings">{findings.map((item, index) =>
      <li key={`${item.code}-${index}`} className={`reactor-finding reactor-finding--${item.severity === "warning" ? "warning" : item.severity === "info" ? "info" : "blocker"}`}>{kineticsFindingText(item)}</li>)}</ul>}
  </section>;
}
