import { useState } from "react";
import { CULTURE_FIELDS, type StudioExpectedStream, type StudioPort, type StudioReport, type StudioUnitCase, type StudioUnitContract, type StudioUnitStream, type StudioVariable } from "../../api/studio";
import { blankPort, blankUnitCase, CULTURE_INPUTS, fractionSum, hostCheckHelp, MAX_PORTS, svgDataUri } from "./unitLogic";
import NumericInput from "../ui/NumericInput";
import { numericOrUndefined } from "../ui/numericStep";

// Spec 190 authoring surfaces for the process_unit category. The 188 editor, documentation and lifecycle are reused as is.
export function PortForm({ title, rows, onChange }: { title: string; rows: StudioPort[]; onChange(rows: StudioPort[]): void }) {
  const update = (index: number, patch: Partial<StudioPort>) => onChange(rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  const kind = title.toLowerCase().slice(0, -1);
  return <fieldset className="studio-variables"><legend>{title} (1 to {MAX_PORTS})</legend>
    {rows.map((row, index) => <div className="studio-port" key={index}>
      <label>Port name<input required pattern="[A-Za-z][A-Za-z0-9_]{0,63}" value={row.name} onChange={(event) => update(index, { name: event.target.value })} placeholder={kind === "inlet" ? "e.g. culture" : "e.g. outlet"} /></label>
      <label>Label<input required value={row.label} onChange={(event) => update(index, { label: event.target.value })} placeholder="Meaning of this port" /></label>
      <button type="button" onClick={() => onChange(rows.filter((_, i) => i !== index))} disabled={rows.length === 1}>Remove</button>
    </div>)}
    <button type="button" disabled={rows.length >= MAX_PORTS} onClick={() => onChange([...rows, blankPort()])}>+ Add {kind} port</button>
  </fieldset>;
}

export function SymbolField({ value, onChange, refusal }: { value: string; onChange(value: string): void; refusal: string | null }) {
  return <fieldset className="studio-variables studio-symbol"><legend>Symbol (optional SVG)</legend>
    <p>Without a symbol the Process canvas draws a generic box labelled with the port names. The backend sanitizes the SVG on save and refuses anything outside its allowlist (no scripts, images, links or styles).</p>
    <label>Upload an .svg file<input type="file" accept=".svg,image/svg+xml" onChange={(event) => { const file = event.target.files?.[0]; if (file) void file.text().then(onChange); event.target.value = ""; }} /></label>
    <label>Or paste SVG markup<textarea aria-label="SVG symbol markup" value={value} onChange={(event) => onChange(event.target.value)} placeholder='<svg viewBox="0 0 100 60"><rect x="5" y="5" width="90" height="50" fill="none" stroke="currentColor"/></svg>' /></label>
    {refusal ? <p className="studio-error" role="alert">Symbol refused: {refusal}</p> : null}
    {value.trim() && !refusal ? <div className="studio-symbol-preview"><img src={svgDataUri(value)} alt="Symbol preview" width={96} height={64} /><small>Preview of the pending markup. The stored symbol is the sanitized form.</small></div> : null}
    {value ? <button type="button" onClick={() => onChange("")}>Remove symbol</button> : null}
  </fieldset>;
}

export function CultureGenerationField({ value, onChange }: { value: string[]; onChange(value: string[]): void }) {
  return <fieldset className="studio-variables"><legend>Culture generation (optional)</legend>
    <p>Culture species are conserved unless declared here. A declared field may be returned as <code>culture_generation</code> from evaluate (kg/s, dic in mol/s) and is checked by the culture balance.</p>
    <div className="studio-checks">{CULTURE_FIELDS.map((name) => <label key={name}><input type="checkbox" checked={value.includes(name)} onChange={(event) => onChange(event.target.checked ? [...value, name] : value.filter((item) => item !== name))} />{name}</label>)}</div>
  </fieldset>;
}

function NumberField({ label, value, onChange, min, minExclusive, max }: { label: string; value: number | null | undefined; onChange(value: number | undefined): void; min?: number; minExclusive?: boolean; max?: number }) {
  return <label>{label}<NumericInput min={min} minExclusive={minExclusive} max={max} value={value} onValueChange={(text) => onChange(numericOrUndefined(text))} /></label>;
}

function Fractions({ title, compounds, values, onChange }: { title: string; compounds: string[]; values: Record<string, number>; onChange(values: Record<string, number>): void }) {
  const names = [...new Set([...compounds, ...Object.keys(values)])];
  return <div className="studio-number-map"><strong>{title} (sum {Number(fractionSum(values).toPrecision(10))})</strong>
    {names.map((name) => <NumberField key={name} label={name} min={0} max={1} value={values[name]} onChange={(next) => { const copy = { ...values }; if (next === undefined) delete copy[name]; else copy[name] = next; onChange(copy); }} />)}
    {names.length ? null : <small>No flowsheet compounds are available to list.</small>}</div>;
}

function CultureInputs({ values, onChange }: { values: Record<string, number | null>; onChange(values: Record<string, number | null>): void }) {
  return <div className="studio-number-map"><strong>Culture fields (mass-specific, optional)</strong>
    {CULTURE_INPUTS.map((name) => <NumberField key={name} label={name} min={0} value={values[name]} onChange={(next) => { const copy = { ...values }; if (next === undefined) delete copy[name]; else copy[name] = next; onChange(copy); }} />)}</div>;
}

function StreamEditor({ name, stream, compounds, onChange }: { name: string; stream: StudioUnitStream; compounds: string[]; onChange(stream: StudioUnitStream): void }) {
  const set = (patch: Partial<StudioUnitStream>) => onChange({ ...stream, ...patch });
  return <fieldset className="studio-stream"><legend>Inlet {name}</legend>
    <div className="studio-number-map">
      <NumberField label="Mass flow [kg/s]" min={0} value={stream.mass_flow_kg_s} onChange={(value) => set({ mass_flow_kg_s: value ?? 0 })} />
      <NumberField label="Temperature [K]" min={0} minExclusive value={stream.temperature_K} onChange={(value) => set({ temperature_K: value ?? 0 })} />
      <NumberField label="Pressure [Pa]" min={0} minExclusive value={stream.pressure_Pa} onChange={(value) => set({ pressure_Pa: value ?? 0 })} />
    </div>
    <Fractions title="Mass fractions" compounds={compounds} values={stream.mass_fractions} onChange={(mass_fractions) => set({ mass_fractions })} />
    <CultureInputs values={stream.culture} onChange={(culture) => set({ culture })} />
  </fieldset>;
}

function ExpectedEditor({ name, expected, compounds, onChange }: { name: string; expected: StudioExpectedStream; compounds: string[]; onChange(expected: StudioExpectedStream | null): void }) {
  const set = (patch: Partial<StudioExpectedStream>) => onChange({ ...expected, ...patch });
  return <fieldset className="studio-stream"><legend>Expected outlet {name}</legend>
    <div className="studio-number-map">
      <NumberField label="Mass flow [kg/s]" min={0} value={expected.mass_flow_kg_s} onChange={(value) => set({ mass_flow_kg_s: value ?? null })} />
      <NumberField label="Temperature [K]" min={0} minExclusive value={expected.temperature_K} onChange={(value) => set({ temperature_K: value ?? null })} />
      <NumberField label="Pressure [Pa]" min={0} minExclusive value={expected.pressure_Pa} onChange={(value) => set({ pressure_Pa: value ?? null })} />
    </div>
    <Fractions title="Expected mass fractions" compounds={compounds} values={expected.mass_fractions ?? {}} onChange={(values) => set({ mass_fractions: Object.keys(values).length ? values : null })} />
    <button type="button" onClick={() => onChange(null)}>Remove expectation for {name}</button>
  </fieldset>;
}

export function UnitCases({ contract, cases, compounds, onChange }: { contract: Pick<StudioUnitContract, "inlets" | "outlets" | "parameters">; cases: StudioUnitCase[]; compounds: string[]; onChange(cases: StudioUnitCase[]): void }) {
  const update = (index: number, patch: Partial<StudioUnitCase>) => onChange(cases.map((item, i) => i === index ? { ...item, ...patch } : item));
  return <>{cases.map((item, index) => <fieldset className="studio-case" key={index}><legend>Case {index + 1}</legend>
    {contract.inlets.map((port) => <StreamEditor key={port.name} name={port.name} compounds={compounds} stream={item.inlets[port.name] ?? { mass_flow_kg_s: 1, temperature_K: 298.15, pressure_Pa: 101325, mass_fractions: {}, culture: {} }}
      onChange={(stream) => update(index, { inlets: { ...item.inlets, [port.name]: stream } })} />)}
    {contract.parameters.length ? <div className="studio-number-map"><strong>Parameters (declared units)</strong>{contract.parameters.map((variable: StudioVariable) =>
      <NumberField key={variable.name} label={`${variable.label || variable.name} [${variable.unit}]`} min={variable.min ?? undefined} max={variable.max ?? undefined} value={item.params[variable.name]} onChange={(value) => { const params = { ...item.params }; if (value === undefined) delete params[variable.name]; else params[variable.name] = value; update(index, { params }); }} />)}</div> : null}
    <details><summary>Expected outlet values (optional)</summary>
      <div className="studio-actions">{contract.outlets.map((port) => <button type="button" key={port.name} disabled={Boolean(item.expected_outlets?.[port.name])} onClick={() => update(index, { expected_outlets: { ...(item.expected_outlets ?? {}), [port.name]: {} } })}>Expect values at {port.name}</button>)}</div>
      {Object.entries(item.expected_outlets ?? {}).map(([port, expected]) => <ExpectedEditor key={port} name={port} compounds={compounds} expected={expected}
        onChange={(next) => { const copy = { ...(item.expected_outlets ?? {}) }; if (next === null) delete copy[port]; else copy[port] = next; update(index, { expected_outlets: Object.keys(copy).length ? copy : null }); }} />)}
      <NumberField label="Tolerance (absolute)" value={item.tolerance} onChange={(value) => update(index, { tolerance: value ?? 1e-6 })} />
    </details>
    <button type="button" onClick={() => onChange(cases.filter((_, i) => i !== index))}>Remove case</button>
  </fieldset>)}
  <button type="button" onClick={() => onChange([...cases, blankUnitCase(contract)])}>+ Add case</button></>;
}

const outletLine = (stream: unknown) => {
  const item = stream as { mass_flow_kg_s?: number; temperature_K?: number; pressure_Pa?: number; mass_fractions?: Record<string, number> };
  const fractions = Object.entries(item.mass_fractions ?? {}).filter(([, value]) => value > 0).map(([name, value]) => `${name} ${Number(value.toPrecision(6))}`).join(", ");
  return `${Number(Number(item.mass_flow_kg_s).toPrecision(6))} kg/s · ${Number(Number(item.temperature_K).toPrecision(6))} K · ${Number(Number(item.pressure_Pa).toPrecision(6))} Pa · ${fractions}`;
};

/** Typed host-check codes appear on their own line with the plain meaning, never merged into prose. */
export function UnitReport({ report }: { report: StudioReport }) {
  const [open, setOpen] = useState(true);
  return <section className="studio-report"><h4>{report.passed ? "Passing report" : "Failed report"}</h4>
    <p>Source {report.source_sha256.slice(0, 12)} · Contract {report.contract_sha256.slice(0, 12)}</p>
    <button type="button" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} case details</button>
    {open ? report.cases.map((result, index) => <div key={index} className="studio-case-result"><strong>Case {index + 1}: {result.passed ? "Pass" : "Fail"}</strong> · runtime {result.classification}
      {(result.codes ?? []).map((code) => <p key={code} className="studio-code" data-code={code}><code>{code}</code> {hostCheckHelp(code)}</p>)}
      {result.errors.map((message, i) => <p className="studio-error" key={i}>{message}</p>)}
      {result.outputs ? <ul>{Object.entries(result.outputs).map(([port, stream]) => <li key={port}><strong>{port}</strong>: {outletLine(stream)}</li>)}</ul> : null}
      {result.culture_generation && Object.keys(result.culture_generation).length ? <p>Culture generation: {Object.entries(result.culture_generation).map(([name, rate]) => `${name} ${rate}`).join(", ")}</p> : null}</div>) : null}
  </section>;
}

export function ContractTables({ contract }: { contract: Partial<StudioUnitContract> }) {
  const ports = (title: string, rows: StudioPort[] | undefined) => <section><h4>{title}</h4>{(rows ?? []).map((row) => <p key={row.name}>{row.label} ({row.name})</p>)}</section>;
  return <div className="studio-contract-tables">{ports("Inlet ports", contract.inlets)}{ports("Outlet ports", contract.outlets)}
    <section><h4>Parameters</h4>{(contract.parameters ?? []).length ? contract.parameters?.map((row) => <p key={row.name}>{row.label} ({row.name}) [{row.unit}] · {row.description}</p>) : <p>None</p>}</section>
    <section><h4>Symbol and generation</h4>{contract.symbol ? <img src={svgDataUri(contract.symbol)} alt="Stored sanitized symbol" width={96} height={64} /> : <p>Generic symbol labelled with the port names.</p>}<p>Culture generation: {(contract.culture_generation ?? []).join(", ") || "none"}</p></section></div>;
}
