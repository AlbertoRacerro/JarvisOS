import type { ReactNode } from "react";
import type { ResultsState, UnitResult } from "../../api/processDraft";
import {
  BALANCE_FIELDS, branchExplanation, formatReported, formatSig, gasTransferWords, growthVersusDilution, nSourceNote, pbrFailureHeading,
  pinDescription, prettyUnits, severityWord,
} from "./pbrLogic";
import { firstSentence } from "./resultsLayout";
import { Empty, StatusLine } from "./RunResults";

export type PbrFailure = { code: string | null; message: string };

const GROUPS: Array<{ title: string; keys: string[] }> = [
  { title: "Residence time", keys: ["hrt_d", "volume_m3", "volumetric_flow_m3_h", "dilution_h"] },
  { title: "Internal circulation", keys: ["circulation_flow_m3_h", "pass_transit_time_s", "circulation_to_throughflow_ratio"] },
  { title: "Culture", keys: ["biomass_mean", "volumetric_productivity", "net_biomass_production", "outlet_biomass_throughput"] },
  { title: "Nitrogen, oxygen and light", keys: ["nitrogen_mean", "nitrogen_min", "oxygen_mean", "oxygen_max", "oxygen_saturation_ratio_max", "oxygen_gas_transfer", "optical_depth_max"] },
  { title: "Hydraulics", keys: ["reynolds_number", "pressure_drop", "pumping_power"] },
];
const SHOWN_ELSEWHERE = new Set(["lambda_h"]);
const LABEL_OVERRIDES: Record<string, string> = {
  dilution_h: "Dilution rate D (process-inlet basis)",
  hrt_d: "Hydraulic residence time (process-inlet basis)",
  volumetric_flow_m3_h: "Volumetric flow Q (process-inlet basis)",
};
const humanKey = (key: string) => key.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());

function Rows({ result, keys }: { result: UnitResult; keys: string[] }) {
  const rows = keys.filter((key) => result.reported[key] !== undefined);
  if (!rows.length) return null;
  return <dl className="pbr-rows">{rows.map((key) => <div key={key}><dt>{LABEL_OVERRIDES[key] ?? result.reported[key].label ?? humanKey(key)}</dt><dd>{formatReported(key, result.reported[key])}</dd></div>)}</dl>;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="pbr-section" aria-label={title}><h5>{title}</h5>{children}</section>;
}

export default function PbrResults({ result, results, failure }: {
  result: UnitResult | undefined; results: ResultsState; failure: PbrFailure | null;
}) {
  const stale = results.state === "stale";
  const reported = result?.reported ?? {};
  const known = new Set([...GROUPS.flatMap((group) => group.keys), ...SHOWN_ELSEWHERE]);
  const other = Object.keys(reported).filter((key) => !known.has(key));
  const lambda = reported.lambda_h?.value;
  const dilution = reported.dilution_h?.value;
  const gas = reported.oxygen_gas_transfer;
  return <section className={`pbr-results${stale ? " is-stale" : ""}`} aria-label="Results · Jarvis">
    <h4>Results · Jarvis{stale ? " (stale)" : ""}</h4>
    {/* The run panel holds the full engine text; here one sentence, the rest and the code in the tooltip. */}
    {failure && <div className="pbr-failure" role="alert" title={`${failure.code ? `${failure.code}: ` : ""}${failure.message}`}>
      <strong>{pbrFailureHeading(failure.code)}</strong>
      <p>{firstSentence(failure.message)}</p>
    </div>}
    {stale && <p className="pbr-note pbr-note--warning" role="status">Inputs changed since this Run. The values below describe the earlier Run, not the current draft.</p>}
    {/* Before a result the same sections show their rows with `empty` values under one status line. */}
    {!result && <>
      {!failure && <StatusLine tone="idle">{results.state === "none" ? "No run yet" : "No result for this unit"}</StatusLine>}
      <Section title="Steady state"><dl className="pbr-rows"><div><dt>Branch</dt><dd><Empty /></dd></div></dl></Section>
      {GROUPS.map((group) => <Section key={group.title} title={group.title}>
        <dl className="pbr-rows">{group.keys.map((key) => <div key={key}><dt>{LABEL_OVERRIDES[key] ?? humanKey(key)}</dt><dd><Empty /></dd></div>)}</dl>
      </Section>)}
    </>}
    {result && <>
      {result.fidelity && <p className="pbr-fidelity"><strong>Fidelity</strong> {result.fidelity}</p>}
      {result.model_pin && <p className="pbr-provenance" title={`Card id ${result.model_pin.card_id}, digest ${result.model_pin.card_digest}; parameter set id ${result.model_pin.set_id ?? "unknown"}, digest ${result.model_pin.set_digest ?? "unknown"}`}>
        <strong>Biological model</strong> {pinDescription(result.model_pin)}<br />{nSourceNote(result.model_pin.n_source)}</p>}
      <Section title="Steady state">
        {result.branch === "washout" || result.branch === "productive" ? <>
          <p><strong className={`pbr-branch pbr-branch--${result.branch}`}>{result.branch === "washout" ? "Washout" : "Productive"}</strong></p>
          <p>{branchExplanation(result.branch)}</p>
        </> : <dl className="pbr-rows"><div><dt>Branch</dt><dd><Empty /></dd></div></dl>}
        {typeof lambda === "number" && typeof dilution === "number" && <>
          <dl className="pbr-rows">
            <div><dt>{reported.lambda_h.label ?? "Thin-culture growth rate Λ"}</dt><dd>{formatSig(lambda)} {prettyUnits(reported.lambda_h.units ?? "1/h")}</dd></div>
            <div><dt>{LABEL_OVERRIDES.dilution_h}</dt><dd>{formatSig(dilution)} {prettyUnits(reported.dilution_h?.units ?? "1/h")}</dd></div>
          </dl>
          <p>{growthVersusDilution(lambda, dilution)}</p></>}
      </Section>
      {GROUPS.map((group) => {
        const keys = group.keys.filter((key) => key !== "dilution_h" || typeof lambda !== "number");
        if (!keys.some((key) => reported[key] !== undefined)) return null;
        return <Section key={group.title} title={group.title}>
          <Rows result={result} keys={keys} />
          {group.title === "Nitrogen, oxygen and light" && gas && <p>{gasTransferWords(gas.value)}</p>}
        </Section>;
      })}
      {other.length > 0 && <Section title="Other reported values"><Rows result={result} keys={other} /></Section>}
      {result.unit_balances && Object.keys(result.unit_balances).length > 0 && <Section title="Unit balances">
        <p className="draft-hint">Residual must stay within the tolerance plus the declared generation allowance.</p>
        <ul className="pbr-balances">{Object.entries(result.unit_balances).map(([field, row]) => {
          const unit = prettyUnits(row.unit);
          return <li key={field} className={row.passed ? "is-ok" : "is-bad"}>
            <strong>{BALANCE_FIELDS[field] ?? humanKey(field)}</strong> <span className="pbr-chip pbr-chip--plain">{row.passed ? "closes" : "does not close"}</span>
            <dl className="pbr-rows">
              <div><dt>In / out</dt><dd>{formatSig(row.in)} / {formatSig(row.out)} {unit}</dd></div>
              <div><dt>Generated</dt><dd>{formatSig(row.generated ?? 0)} {unit}</dd></div>
              <div><dt>Residual</dt><dd>{formatSig(row.residual)} {unit}</dd></div>
              <div><dt>Tolerance</dt><dd>{formatSig(row.tolerance)} {unit}</dd></div>
              <div><dt>Generation allowance</dt><dd>{formatSig(row.generation_allowance ?? 0)} {unit}</dd></div>
            </dl></li>;
        })}</ul>
      </Section>}
      {result.numerics && <details className="pbr-numerics"><summary>Numerics</summary>
        <dl className="pbr-rows">
          {result.numerics.map_evaluations !== undefined && <div><dt>Map evaluations</dt><dd>{result.numerics.map_evaluations}</dd></div>}
          {result.numerics.bracket && <div><dt>Root bracket</dt><dd>{formatSig(result.numerics.bracket.value[0])} to {formatSig(result.numerics.bracket.value[1])} {prettyUnits(result.numerics.bracket.units)}</dd></div>}
          {Object.entries(result.numerics.periodicity ?? {}).map(([name, row]) => <div key={name}><dt>Periodicity residual · {name}</dt>
            <dd>{formatSig(row.residual)} (tolerance {formatSig(row.tolerance)}) {prettyUnits(row.units)}</dd></div>)}
          {result.numerics.z_identity_max && <div><dt>Conserved-state identity, largest error</dt><dd>{formatSig(result.numerics.z_identity_max.value)} {prettyUnits(result.numerics.z_identity_max.units)}</dd></div>}
          {result.numerics.wall_time_s !== undefined && <div><dt>Wall time</dt><dd>{formatSig(result.numerics.wall_time_s)} s</dd></div>}
        </dl></details>}
      {(result.findings?.length ?? 0) > 0 && <Section title="Findings">
        <ul className="pbr-findings">{result.findings!.map((finding, index) => { const tone = severityWord(finding.severity); return <li key={`${finding.code}-${index}`} className={`pbr-finding pbr-finding--${tone.tone}`}>
          <strong>{tone.text}</strong> {finding.message}<small>{finding.code}</small></li>; })}</ul>
      </Section>}
      {(result.caveats?.length ?? 0) > 0 && <Section title="Caveats"><ul className="pbr-caveats">{result.caveats!.map((caveat) => <li key={caveat}>{caveat}</li>)}</ul></Section>}
    </>}
  </section>;
}
