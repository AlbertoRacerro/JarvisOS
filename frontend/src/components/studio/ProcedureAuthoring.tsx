import { useState } from "react";
import type { StudioVariable } from "../../api/studio";
import type { ActionType, Expectation, Num, ProcedureAction, ProcedureCase, ProcedureReport, ProcedureStep } from "../../api/studioProcedure";
import { ACTIONS, ACTION_TYPES, blankAction, blankCase, blankStep, boundHint, changeActionType, fromSeconds, getPath, isRef, joinObserved, MAX_ACTIONS, NOTICE, OBSERVED_CHANNELS, setPath, setStepKind, splitObserved, stepIssues, stepKind, TIME_UNITS, toSeconds, type FieldDef, type TimeUnit } from "./procedureLogic";

import NumericInput from "../ui/NumericInput";
import { numericOrUndefined } from "../ui/numericStep";
// Spec 191 authoring surfaces for the procedure category. The 188 draft, test, publish and revise lifecycle is reused as is.
export const SimulationNotice = () => <p className="studio-notice studio-procedure-notice" role="note">{NOTICE}</p>;

/** A typed refusal is shown with its code on its own line, verbatim. Nothing is corrected or hidden. */
export function Refusal({ code, message, detail }: { code: string | null | undefined; message: string | null | undefined; detail?: unknown }) {
  return <div className="studio-error studio-refusal" role="alert"><p><code data-refusal-code={code ?? ""}>{code ?? "refused"}</code> {message}</p>
    {detail ? <details><summary>Host detail</summary><pre>{JSON.stringify(detail, null, 2)}</pre></details> : null}</div>;
}

function NumField({ label, unit, value, params, onChange, time = false, optional = false }: { label: string; unit: string; value: Num | undefined; params: StudioVariable[]; onChange(value: Num | undefined): void; time?: boolean; optional?: boolean }) {
  const bound = isRef(value) ? value.$param : "";
  const parameter = params.find((item) => item.name === bound);
  const [timeUnit, setTimeUnit] = useState<TimeUnit>(typeof value === "number" && value !== 0 ? fromSeconds(value).unit : "h");
  const size = TIME_UNITS.find(([name]) => name === timeUnit)?.[1] ?? 1;
  const shown = typeof value === "number" ? (time ? Number((value / size).toPrecision(12)) : value) : "";
  return <label className="studio-numfield"><span>{label} [{bound && parameter ? parameter.unit : time ? timeUnit : unit}]{optional ? " (optional)" : ""}</span>
    <span className="studio-numfield__row">
      {bound ? <output>= parameter {bound}</output> : <NumericInput aria-label={label} value={shown} onValueChange={(text) => onChange(numericOrUndefined(text) === undefined ? undefined : time ? toSeconds((numericOrUndefined(text) ?? 0), timeUnit) : (numericOrUndefined(text) ?? 0))} />}
      {time && !bound ? <select aria-label={`${label} unit`} value={timeUnit} onChange={(event) => setTimeUnit(event.target.value as TimeUnit)}>{TIME_UNITS.map(([name]) => <option key={name} value={name}>{name}</option>)}</select> : null}
      {params.length ? <select aria-label={`Bind ${label} to a parameter`} value={bound} onChange={(event) => onChange(event.target.value ? { $param: event.target.value } : undefined)}><option value="">Literal value</option>{params.map((item) => <option key={item.name} value={item.name}>Parameter {item.name} [{item.unit}]</option>)}</select> : null}
    </span></label>;
}

function TankField({ label, value, tanks, none = false, onChange }: { label: string; value: unknown; tanks: string[]; none?: boolean; onChange(value: string | undefined): void }) {
  const list = `tanks-${label.replace(/\W/g, "")}`;
  return <label>{label}<input list={list} aria-label={label} value={typeof value === "string" ? value : ""} onChange={(event) => onChange(event.target.value || undefined)} placeholder={none ? "tank tag or none" : "tank tag"} />
    <datalist id={list}>{none ? <option value="none" /> : null}{tanks.map((tag) => <option key={tag} value={tag} />)}</datalist></label>;
}

/** The choice selector swaps which of the exclusive fields holds the value (draw and refill); the other is removed, not converted. */
function ActionForm(props: { action: ProcedureAction; params: StudioVariable[]; tanks: string[]; onChange(action: ProcedureAction): void; onRemove(): void }) {
  const def = ACTIONS[props.action.type];
  const [override, setOverride] = useState<string | null>(null);
  const choice = def.choice ?? [];
  const active = choice.find((field) => getPath(props.action, field.path) !== undefined)?.path.join(".") ?? override ?? choice[0]?.path.join(".");
  const action = props.action; const numeric = (field: FieldDef) => <NumField key={field.path.join(".")} label={field.label} unit={field.unit} optional={field.optional} params={props.params} value={getPath(action, field.path) as Num | undefined} onChange={(value) => props.onChange(setPath(action, field.path, value) as ProcedureAction)} />;
  const selected = choice.find((field) => field.path.join(".") === active) ?? choice[0];
  return <fieldset className="studio-action" data-action-type={action.type}><legend>{def.label}</legend>
    <label>Action<select aria-label="Action type" value={action.type} onChange={(event) => { setOverride(null); props.onChange(changeActionType(action, event.target.value as ActionType)); }}>{ACTION_TYPES.map((type) => <option key={type} value={type}>{ACTIONS[type].label}</option>)}</select></label>
    {def.tanks.map((tag) => <TankField key={tag} label={tag === "return_to" ? "Return to" : "Tank"} none={tag === "return_to"} tanks={props.tanks} value={action[tag]} onChange={(value) => props.onChange(setPath(action, [tag], value) as ProcedureAction)} />)}
    {choice.length ? <label>Specify by<select aria-label="Volume basis" value={active} onChange={(event) => { const cleared = choice.reduce((acc, field) => setPath(acc, field.path, undefined), action) as ProcedureAction; setOverride(event.target.value); props.onChange(cleared); }}>{choice.map((field) => <option key={field.path.join(".")} value={field.path.join(".")}>{field.label} [{field.unit}]</option>)}</select></label> : null}
    {selected ? numeric(selected) : null}{def.fields.map(numeric)}
    <button type="button" onClick={props.onRemove}>Remove action</button></fieldset>;
}

function StepForm({ step, index, params, tanks, units, onChange, onRemove, onMove }: { step: ProcedureStep; index: number; params: StudioVariable[]; tanks: string[]; units: string[]; onChange(step: ProcedureStep): void; onRemove(): void; onMove(delta: number): void }) {
  const kind = stepKind(step);
  const set = (key: keyof ProcedureStep, value: unknown) => { const next = { ...step } as Record<string, unknown>; if (value === undefined) delete next[key]; else next[key] = value; onChange(next as unknown as ProcedureStep); };
  const condition = step.condition; const observed = splitObserved(condition?.observed ?? "");
  const setCondition = (patch: Record<string, unknown>) => onChange({ ...step, condition: { ...condition!, ...patch } as ProcedureStep["condition"] });
  return <fieldset className="studio-step" data-step={index + 1}><legend>Step {index + 1}</legend>
    <div className="studio-step__head"><label>Label<input aria-label="Step label" value={step.label} maxLength={120} onChange={(event) => set("label", event.target.value)} /></label>
      <button type="button" disabled={index === 0} onClick={() => onMove(-1)}>Move up</button><button type="button" onClick={() => onMove(1)}>Move down</button><button type="button" onClick={onRemove}>Remove step</button></div>
    <NumField label={kind === "condition" ? "Earliest time" : "Time"} unit="s" time params={params} value={step.time_s} onChange={(value) => set("time_s", value)} />
    <div className="studio-paths" role="group" aria-label={`Trigger of step ${index + 1}`}>
      {([["once", "Once"], ["recurring", "Repeats every..."], ["condition", "When a condition holds"]] as const).map(([id, text]) => <button type="button" key={id} aria-pressed={kind === id} onClick={() => onChange(setStepKind(step, id))}>{text}</button>)}</div>
    {kind === "recurring" ? <div className="studio-number-map"><NumField label="Every" unit="s" time params={params} value={step.every_s} onChange={(value) => set("every_s", value)} />
      <NumField label="Count" unit="1" optional params={params} value={step.count} onChange={(value) => set("count", value)} /><NumField label="End time" unit="s" time optional params={params} value={step.end_s} onChange={(value) => set("end_s", value)} />
      <small>Give a count or an end time. The host refuses a recurrence without either.</small></div> : null}
    {kind === "condition" && condition ? <div className="studio-number-map studio-condition"><label>Observed unit<input list={`units-${index}`} aria-label="Observed unit" value={observed.unit} onChange={(event) => setCondition({ observed: joinObserved(event.target.value, observed.channel) })} placeholder="unit tag, e.g. PBR" /><datalist id={`units-${index}`}>{units.map((tag) => <option key={tag} value={tag} />)}</datalist></label>
      <label>Channel<select aria-label="Observed channel" value={observed.channel} onChange={(event) => setCondition({ observed: joinObserved(observed.unit, event.target.value) })}>{OBSERVED_CHANNELS.map((channel) => <option key={channel} value={channel}>{channel}</option>)}</select></label>
      <label>Direction<select aria-label="Direction" value={condition.direction} onChange={(event) => setCondition({ direction: event.target.value })}><option value="below">falls below</option><option value="above">rises above</option></select></label>
      <NumField label="Threshold" unit="kg/m3" params={params} value={condition.threshold} onChange={(value) => setCondition({ threshold: value })} />
      <NumField label="Hysteresis" unit="kg/m3" params={params} value={condition.hysteresis} onChange={(value) => setCondition({ hysteresis: value })} />
      <NumField label="Cooldown" unit="s" time optional params={params} value={condition.cooldown_s} onChange={(value) => setCondition({ cooldown_s: value })} />
      <NumField label="Max fires" unit="1" optional params={params} value={condition.max_fires} onChange={(value) => setCondition({ max_fires: value })} />
      <small>A conditioned step repeats only through max fires; it cannot also recur by time.</small></div> : null}
    <h4>Actions ({step.actions.length}/{MAX_ACTIONS}, applied in order)</h4>
    {step.actions.map((action, position) => <ActionForm key={position} action={action} params={params} tanks={tanks} onChange={(next) => onChange({ ...step, actions: step.actions.map((item, i) => i === position ? next : item) })} onRemove={() => onChange({ ...step, actions: step.actions.filter((_, i) => i !== position) })} />)}
    <button type="button" disabled={step.actions.length >= MAX_ACTIONS} onClick={() => onChange({ ...step, actions: [...step.actions, blankAction("draw")] })}>+ Add action</button></fieldset>;
}

export function StepEditor({ steps, parameters, tanks, units, onChange, disabled }: { steps: ProcedureStep[]; parameters: StudioVariable[]; tanks: string[]; units: string[]; onChange(steps: ProcedureStep[]): void; disabled: boolean }) {
  const issues = stepIssues(steps, parameters);
  const move = (index: number, delta: number) => { const next = [...steps]; const to = index + delta; if (to < 0 || to >= next.length) return; [next[index], next[to]] = [next[to], next[index]]; onChange(next); };
  return <fieldset className="studio-panel studio-steps" disabled={disabled}><SimulationNotice />
    <p>Steps run in order of time. Any number can be bound to a parameter. Tanks and units can be chosen from the target draft; the host resolves them on Test and Apply and refuses unknown tags.</p>
    {steps.map((step, index) => <StepForm key={index} step={step} index={index} params={parameters} tanks={tanks} units={units} onChange={(next) => onChange(steps.map((item, i) => i === index ? next : item))} onRemove={() => onChange(steps.filter((_, i) => i !== index))} onMove={(delta) => move(index, delta)} />)}
    <button type="button" onClick={() => onChange([...steps, blankStep(steps.length)])}>+ Add step</button>
    {issues.map((issue) => <p className="studio-warning" key={issue}>{issue}</p>)}</fieldset>;
}

function ExpectEditor({ rows, onChange }: { rows: Expectation[]; onChange(rows: Expectation[]): void }) {
  const update = (index: number, patch: Partial<Expectation>) => onChange(rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  return <div className="studio-expect"><strong>Expectations (only checked when simulating)</strong>
    {rows.map((row, index) => <div className="studio-number-map" key={index}>
      <label>Kind<select value={row.kind} onChange={(event) => update(index, { kind: event.target.value as Expectation["kind"] })}><option value="series">Series value</option><option value="fire_time">Fire time</option><option value="balances_closed">Balances closed</option></select></label>
      {row.kind === "series" ? <><label>Channel<input value={row.channel ?? ""} onChange={(event) => update(index, { channel: event.target.value })} placeholder="TankA_V_m3" /></label>
        <label>Time [s]<NumericInput value={row.time_s ?? ""} onValueChange={(text) => update(index, { time_s: numericOrUndefined(text) })} /></label>
        <label>Phase<select value={row.phase ?? "post"} onChange={(event) => update(index, { phase: event.target.value as Expectation["phase"] })}><option value="post">after events</option><option value="pre">before events</option><option value="sample">sample</option></select></label></> : null}
      {row.kind === "fire_time" ? <label>Fire index (1-based)<NumericInput integer min={1} value={row.fire_index ?? ""} onValueChange={(text) => update(index, { fire_index: numericOrUndefined(text) })} /></label> : null}
      {row.kind !== "balances_closed" ? <><label>Expected value<NumericInput value={row.value ?? ""} onValueChange={(text) => update(index, { value: numericOrUndefined(text) })} /></label>
        <label>Tolerance<NumericInput min={0} value={row.tolerance ?? 1e-9} onValueChange={(text) => update(index, { tolerance: (numericOrUndefined(text) ?? 0) })} /></label>
        <label><input type="checkbox" checked={row.relative ?? false} onChange={(event) => update(index, { relative: event.target.checked })} /> relative tolerance</label></> : null}
      <button type="button" onClick={() => onChange(rows.filter((_, i) => i !== index))}>Remove expectation</button></div>)}
    <button type="button" onClick={() => onChange([...rows, { kind: "balances_closed" }])}>+ Add expectation</button></div>;
}

export function ProcedureCases({ cases, parameters, drafts, onChange }: { cases: ProcedureCase[]; parameters: StudioVariable[]; drafts: { draft_id: string; name: string; scenarios: { id: string }[] }[]; onChange(cases: ProcedureCase[]): void }) {
  const update = (index: number, patch: Partial<ProcedureCase>) => onChange(cases.map((item, i) => i === index ? { ...item, ...patch } : item));
  return <>{cases.map((item, index) => {
    const draft = drafts.find((entry) => entry.draft_id === item.draft_id);
    return <fieldset className="studio-case" key={index}><legend>Case {index + 1}</legend>
      <label>Case name<input value={item.name} onChange={(event) => update(index, { name: event.target.value })} /></label>
      <div className="studio-number-map"><strong>Parameter values (declared units)</strong>{parameters.map((variable) => <label key={variable.name}>{variable.label || variable.name} [{variable.unit}]{boundHint(variable)}
        <NumericInput aria-label={`Value of ${variable.name}`} value={item.parameter_values[variable.name] ?? ""} onValueChange={(text) => { const next = { ...item.parameter_values }; if (numericOrUndefined(text) === undefined) delete next[variable.name]; else next[variable.name] = (numericOrUndefined(text) ?? 0); update(index, { parameter_values: next }); }} /></label>)}</div>
      <label>Target draft<select aria-label="Target draft" value={item.draft_id} onChange={(event) => update(index, { draft_id: event.target.value, scenario_id: drafts.find((entry) => entry.draft_id === event.target.value)?.scenarios[0]?.id ?? "" })}><option value="">Choose a Process draft…</option>{drafts.map((entry) => <option key={entry.draft_id} value={entry.draft_id}>{entry.name}</option>)}</select></label>
      <label>Scenario<select aria-label="Scenario" value={item.scenario_id} onChange={(event) => update(index, { scenario_id: event.target.value })}><option value="">Choose a scenario…</option>{(draft?.scenarios ?? []).map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.id}</option>)}</select></label>
      {draft && !draft.scenarios.length ? <p className="studio-warning">This draft has no scenario. Create one in the Test bench tab.</p> : null}
      <label>Schedule id<input value={item.schedule_id} pattern="[a-z][a-z0-9_\-]{0,39}" onChange={(event) => update(index, { schedule_id: event.target.value })} /></label>
      <label><input type="checkbox" checked={item.simulate} onChange={(event) => update(index, { simulate: event.target.checked })} /> Also simulate the scenario and check expectations</label>
      {item.simulate ? <ExpectEditor rows={item.expect} onChange={(expect) => update(index, { expect })} /> : null}
      <button type="button" onClick={() => onChange(cases.filter((_, i) => i !== index))}>Remove case</button></fieldset>;
  })}
  <button type="button" onClick={() => onChange([...cases, { ...blankCase(parameters, drafts[0]?.draft_id ?? "", drafts[0]?.scenarios[0]?.id ?? ""), name: `case ${cases.length + 1}` }])}>+ Add test case</button></>;
}

export function ProcedureReportView({ report }: { report: ProcedureReport }) {
  return <section className="studio-report" data-passed={report.passed}><h4>{report.passed ? "Passing report" : "Failed report"}</h4>
    <p>Source {report.source_sha256.slice(0, 12)} · Contract {report.contract_sha256.slice(0, 12)}</p><SimulationNotice />
    {report.cases.map((result, index) => <div className="studio-case-result" key={index}><strong>Case {index + 1} ({result.name}): {result.passed ? "Pass" : "Fail"}</strong>
      {result.code ? <Refusal code={result.code} message={result.message} detail={result.detail} /> : null}
      {result.compiled_sha256 ? <p className="studio-hash">Compiled schedule SHA-256: {result.compiled_sha256}</p> : null}
      {result.prepare ? <p>Host prepare: {result.prepare.ok ? "accepted" : "refused"} · {result.prepare.expanded_event_count ?? 0} events ({result.prepare.time_triggered_events ?? 0} timed, {result.prepare.state_triggered_events ?? 0} state-triggered){result.prepare.duration_s !== undefined ? ` · duration ${result.prepare.duration_s} s` : ""}</p> : null}
      {result.simulation ? <p>Simulation: {result.simulation.status}{result.simulation.error ? <> · <code>{result.simulation.error.code}</code> {result.simulation.error.message}</> : null} · {result.simulation.event_count ?? 0} events · {result.simulation.state_fires?.length ?? 0} state fires</p> : null}
      {(result.expectations ?? []).map((item, n) => <p key={n} className={item.passed ? "" : "studio-error"}>Expectation {item.kind}: {item.passed ? "pass" : "fail"} · actual {JSON.stringify(item.actual)} vs expected {JSON.stringify(item.expected)} {item.message}</p>)}
      {(result.errors ?? []).map((message, n) => <p className="studio-error" key={n}>{message}</p>)}
      {result.compiled ? <details><summary>Compiled schedule (read-only)</summary><pre>{JSON.stringify(result.compiled, null, 2)}</pre></details> : null}</div>)}</section>;
}
