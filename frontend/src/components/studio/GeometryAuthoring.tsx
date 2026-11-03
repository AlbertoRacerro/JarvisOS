import BluecadGlbViewer from "../BluecadGlbViewer";
import type { StudioGeometryArtifact, StudioGeometryCase, StudioGeometryCaseResult, StudioGeometryFacts, StudioReport, StudioVariable } from "../../api/studio";
import { blankGeometryCase, limitText, optional, size } from "./geometryLogic";
import NumericInput from "../ui/NumericInput";
import { numericOrUndefined } from "../ui/numericStep";

// Spec 189 surfaces for the geometry category. The 188 wizard, editor, documentation and lifecycle are reused as is.
export function GeometryCases({ inputs, cases, onChange }: { inputs: StudioVariable[]; cases: StudioGeometryCase[]; onChange(cases: StudioGeometryCase[]): void }) {
  const update = (index: number, patch: Partial<StudioGeometryCase>) => onChange(cases.map((item, i) => i === index ? { ...item, ...patch } : item));
  return <>{cases.map((item, index) => <fieldset className="studio-case" key={index}><legend>Build {index + 1}</legend>
    <div className="studio-number-map"><strong>Inputs</strong>{inputs.map((variable) => <label key={variable.name}>{variable.label || variable.name} [{variable.unit}]<NumericInput min={variable.min ?? undefined} max={variable.max ?? undefined} value={item.inputs[variable.name]} onValueChange={(text) => {
      const next = { ...item.inputs }; const parsed = numericOrUndefined(text); if (parsed === undefined) delete next[variable.name]; else next[variable.name] = parsed; update(index, { inputs: next });
    }} /></label>)}</div>
    <div className="studio-number-map"><strong>Declared bounds (optional, model units: mm and mm³)</strong>
      <label>Minimum volume<NumericInput min={0} value={item.volume_min} onValueChange={(text) => update(index, { volume_min: optional(text) })} /></label>
      <label>Maximum volume<NumericInput min={0} minExclusive value={item.volume_max} onValueChange={(text) => update(index, { volume_max: optional(text) })} /></label>
      <label>Largest extent<NumericInput min={0} minExclusive value={item.max_extent} onValueChange={(text) => update(index, { max_extent: optional(text) })} /></label>
    </div>
    <button type="button" onClick={() => onChange(cases.filter((_, i) => i !== index))}>Remove build</button>
  </fieldset>)}
  <button type="button" disabled={cases.length >= 4} onClick={() => onChange([...cases, blankGeometryCase()])}>+ Add build</button></>;
}

export function GeometryFacts({ facts }: { facts: StudioGeometryFacts }) {
  const extent = facts.bbox_max.map((high, axis) => high - facts.bbox_min[axis]);
  return <dl className="studio-geometry-facts">
    <div><dt>Valid solid</dt><dd>{facts.valid ? "Yes" : "No"}</dd></div><div><dt>Solids</dt><dd>{facts.solids}</dd></div>
    <div><dt>Volume</dt><dd>{facts.volume.toPrecision(6)} mm³</dd></div>
    <div><dt>Bounding box</dt><dd>{extent.map((value) => value.toPrecision(4)).join(" × ")} mm</dd></div>
  </dl>;
}

export function GeometryArtifacts({ artifacts, url }: { artifacts: StudioGeometryArtifact[]; url(name: string): string }) {
  const glb = artifacts.find((item) => item.name === "model.glb");
  return <div className="studio-geometry-artifacts">
    {glb ? <div className="studio-geometry-viewer"><BluecadGlbViewer artifactUrl={url(glb.name)} /></div> : null}
    <ul>{artifacts.map((item) => <li key={item.name}>{item.name === "model.glb" ? <span>{item.name}</span> : <a href={url(item.name)} download={item.name}>Download {item.name.slice(6).toUpperCase()}</a>} · {size(item.size_bytes)} · SHA-256 {item.sha256.slice(0, 12)}</li>)}</ul>
  </div>;
}

export function GeometryReport({ report, url }: { report: StudioReport; url(previewId: string, name: string): string }) {
  const cases = report.cases as unknown as StudioGeometryCaseResult[];
  return <section className="studio-report"><h4>{report.passed ? "Passing report" : "Failed report"}</h4><p>Source {report.source_sha256.slice(0, 12)} · Contract {report.contract_sha256.slice(0, 12)}</p>
    {cases.map((result, index) => <div key={index}><strong>Build {index + 1}: {result.passed ? "Pass" : "Fail"}</strong> · {result.classification}{result.duration_seconds !== undefined ? ` · ${result.duration_seconds} s` : ""}
      {limitText(result.limit, result.limit_value) ? <p className="studio-warning" role="status">{limitText(result.limit, result.limit_value)}</p> : null}
      {result.errors.length ? <ul>{result.errors.map((message, n) => <li key={n}>{message}</li>)}</ul> : null}
      {result.facts ? <GeometryFacts facts={result.facts} /> : null}
      {result.preview_id && result.artifacts.length ? <><h5>Preview</h5><GeometryArtifacts artifacts={result.artifacts} url={(name) => url(result.preview_id as string, name)} /></> : null}
    </div>)}
  </section>;
}
