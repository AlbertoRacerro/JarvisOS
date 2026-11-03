import { useState } from "react";
import AppLink from "../../app/AppLink";
import type { Navigate } from "../../app/AppLink";
import type { DraftObject, DraftOp, StudioPinStatus, StudioRunProvenance } from "../../api/processDraft";
import { inspectHref, parameterOp, pinOf, portLabels, provenanceRows, STUDIO_LIMITS, supersededText, updateStudioUnitOp } from "./studioUnitLogic";
import NumericInput from "../ui/NumericInput";

/** The honest-limits statement, shown on the unit and again in its results (spec 190 decision 9). */
export function StudioLimits() {
  return <p className="draft-hint draft-studio-limits" role="note" data-testid="studio-limits">{STUDIO_LIMITS}.</p>;
}

export function StudioProvenance({ tag, provenance }: Readonly<{ tag: string; provenance: StudioRunProvenance }>) {
  const superseded = supersededText(provenance);
  return (
    <section className="draft-studio-provenance" aria-label={`Custom unit provenance ${tag}`} data-testid="studio-provenance">
      <h4>{tag} · custom unit provenance</h4>
      {superseded ? <p className="draft-hint is-superseded" role="status" data-testid="studio-superseded">{superseded}</p> : null}
      <dl className="draft-results-inline">{provenanceRows(provenance).map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
      <StudioLimits />
    </section>
  );
}

type Props = Readonly<{
  unit: DraftObject;
  objects: DraftObject[];
  status?: StudioPinStatus;
  provenance?: StudioRunProvenance;
  failure: { code: string | null; message: string } | null;
  resultsState: "none" | "current" | "stale";
  apply(ops: DraftOp[]): Promise<unknown>;
  navigate?: Navigate;
  showError(message: string): void;
}>;

export default function StudioUnitInspector({ unit, objects, status, provenance, failure, resultsState, apply, navigate, showError }: Props) {
  const [entered, setEntered] = useState<Record<string, string>>({});
  const studio = unit.studio;
  const pin = pinOf(unit);
  if (!studio || !pin) return <p>This custom unit has no verified pin and cannot be edited.</p>;
  const connected = (end: "source" | "target", port: number) => objects.find((item) => item.kind === "stream" && item.type !== "EnergyStream" && item[end]?.unit === unit.id && item[end]?.port === port)?.tag ?? "—";
  const href = inspectHref(unit);
  const update = status ? updateStudioUnitOp(unit, status) : null;
  const names = studio.contract.parameters;
  return (
    <>
      <p className="draft-owner-line"><span className="draft-owner-badge-label">Custom Studio unit</span> · {studio.name} {studio.version_label}</p>
      <StudioLimits />
      <dl className="draft-studio-pin" aria-label="Pinned version">
        <div><dt>Version</dt><dd>{studio.version_label}</dd></div>
        <div><dt>Source SHA-256</dt><dd title={pin.source_sha256}>{pin.source_sha256.slice(0, 12)}</dd></div>
        <div><dt>Contract SHA-256</dt><dd title={pin.contract_sha256}>{pin.contract_sha256.slice(0, 12)}</dd></div>
      </dl>
      <div className="draft-studio-actions">
        {href ? (navigate ? <AppLink href={href} navigate={navigate} className="draft-studio-inspect">Open model in Studio</AppLink> : <a href={href} className="draft-studio-inspect">Open model in Studio</a>) : null}
        {update ? <button type="button" data-testid="studio-update" onClick={() => void apply([update])}>Update to v{status?.latest_version_number}</button> : null}
      </div>
      {update ? <p className="draft-hint" role="status">v{status?.latest_version_number} of {studio.name} is published. This unit stays on {studio.version_label} until you update it.</p> : null}
      <dl className="draft-ports" aria-label="Declared ports">
        {portLabels(unit, "inlets").map((label, port) => <div key={`in${port}`}><dt>In · {label}</dt><dd>{connected("target", port)}</dd></div>)}
        {portLabels(unit, "outlets").map((label, port) => <div key={`out${port}`}><dt>Out · {label}</dt><dd>{connected("source", port)}</dd></div>)}
      </dl>
      <fieldset className="draft-fieldset draft-inputs" aria-label="Inputs">
        <legend>Inputs (declared units)</legend>
        {names.length ? names.map((item) => {
          const stored = unit.params?.[item.name];
          const bounds = [item.min != null ? `min ${item.min}` : null, item.max != null ? `max ${item.max}` : null].filter(Boolean).join(", ");
          return (
            <label className="draft-field" key={item.name}>
              <span>{item.label || item.name} [{item.unit}]{bounds ? ` · ${bounds}` : ""}</span>
              <NumericInput min={item.min ?? undefined} max={item.max ?? undefined} aria-label={`${item.label || item.name} in ${item.unit}`} value={entered[item.name] ?? (stored ? String(stored.value) : "")}
                onValueChange={(text) => setEntered((current) => ({ ...current, [item.name]: text }))} />
            </label>
          );
        }) : <p className="draft-hint">This unit declares no parameters.</p>}
        {names.length ? <button type="button" disabled={!Object.keys(entered).length} onClick={() => {
          const op = parameterOp(unit, entered);
          if (!op) return showError("Enter numbers only.");
          void apply([op]).then(() => setEntered({}));
        }}>Apply inputs</button> : null}
      </fieldset>
      {failure ? (
        <fieldset className="draft-fieldset draft-outputs" aria-label="Custom unit failure">
          <legend>Failure · {unit.tag}</legend>
          <p className="studio-error" role="alert" data-testid="studio-failure"><strong>{unit.tag}</strong>{failure.code ? <> · <code>{failure.code}</code></> : null} · {failure.message}</p>
        </fieldset>
      ) : null}
      {resultsState !== "none" && provenance ? <StudioProvenance tag={unit.tag} provenance={provenance} /> : null}
    </>
  );
}
