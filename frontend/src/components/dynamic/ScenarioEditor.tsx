import { useEffect, useMemo, useState } from "react";
import type { DraftProjection } from "../../api/processDraft";
import type { EnvProfile } from "./dynamicApi";
import { blankScenario, deleteOp, fmtDuration, isId, scenarioDurationS, scenarioHints, scenarioLimitText, setOp, startingState, type DynamicContent, type DynScenario } from "./dynamicLogic";
import { NumField, SelectField, TextField, TimeField, type OnPatch } from "./fields";
import { Refusal, SimulationNotice } from "./SimulationNotice";

// Spec 173 decision 4: every field of the typed Scenario. Fidelity is the fixed T1. A save is one set_scenario op; a refusal shows the
// backend code and message and leaves the draft untouched.
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
type Problem = { code: string | null; message: string } | null;

export const cultureUnits = (draft: DraftProjection) => draft.objects.filter((item) => item.kind === "unit" && (item.type === "PhotobioreactorT1" || item.type === "HoldupTank")).map((item) => item.tag);
export const feedStreams = (draft: DraftProjection) => draft.objects.filter((item) => item.kind === "stream" && !item.source).map((item) => item.tag);
export const unitsOfType = (draft: DraftProjection, type: string) => draft.objects.filter((item) => item.kind === "unit" && item.type === type).map((item) => item.tag);

export default function ScenarioEditor({ draft, content, profiles, onPatch }: { draft: DraftProjection; content: DynamicContent; profiles: EnvProfile[]; onPatch: OnPatch }) {
  const ids = Object.keys(content.scenarios);
  const [selected, setSelected] = useState<string>(ids[0] ?? "");
  const [value, setValue] = useState<DynScenario>(() => (ids[0] ? clone(content.scenarios[ids[0]]) : blankScenario("scenario-1", [], null)));
  const [isNew, setIsNew] = useState(!ids[0]);
  const [dirty, setDirty] = useState(false);
  const [problem, setProblem] = useState<Problem>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const units = cultureUnits(draft); const feeds = feedStreams(draft); const pumps = unitsOfType(draft, "Pump");

  // Reload the form from the draft whenever it has no unsaved edits.
  useEffect(() => { if (!dirty && !isNew && content.scenarios[selected]) setValue(clone(content.scenarios[selected])); }, [content, selected, dirty, isNew]);
  const edit = (patch: Partial<DynScenario>) => { setValue((current) => ({ ...current, ...patch })); setDirty(true); setInfo(null); };
  const choose = (id: string) => { if (id === "") return; setSelected(id); setValue(clone(content.scenarios[id])); setIsNew(false); setDirty(false); setProblem(null); setInfo(null); };
  const startNew = () => {
    let n = ids.length + 1; while (ids.includes(`scenario-${n}`)) n += 1;
    const first = profiles[0] ?? null;
    setValue(blankScenario(`scenario-${n}`, units.slice(0, 1), first)); setIsNew(true); setDirty(true); setSelected(""); setProblem(null); setInfo(null);
  };
  const setProfile = (profileId: string) => {
    const profile = profiles.find((item) => item.profile_id === profileId);
    if (!profile) return;
    const fresh = blankScenario(value.id, value.units, profile);
    edit({ profiles: [{ profile_id: profile.profile_id, digest: profile.digest }], ...(isNew ? { start_utc: fresh.start_utc, end_utc: fresh.end_utc } : {}) });
  };
  const toggleUnit = (tag: string, on: boolean) => {
    const next = on ? [...value.units, tag] : value.units.filter((item) => item !== tag);
    const initial = { ...value.initial };
    if (on && !initial[tag]) initial[tag] = startingState();
    edit({ units: next, initial });
  };
  const setInitial = (tag: string, key: "X" | "N" | "O2", next: number | undefined) => {
    const current = { ...(value.initial[tag] ?? {}) };
    if (next === undefined) delete current[key]; else current[key] = next;
    edit({ initial: { ...value.initial, [tag]: current } });
  };
  const setMap = (field: "feed_flows" | "circulation", tag: string, next: number | undefined) => {
    const current = { ...value[field] };
    if (next === undefined) delete current[tag]; else current[tag] = next;
    edit({ [field]: current } as Partial<DynScenario>);
  };
  const run = async (ops: Record<string, unknown>[], done: () => void, message: string) => {
    setBusy(true); setProblem(null);
    const result = await onPatch(ops);
    setBusy(false);
    if (result.ok) { done(); setInfo(`${message} Draft revision ${result.seq}.`); } else setProblem({ code: result.code, message: result.message });
  };
  const save = () => run([setOp("scenario", value)], () => { setSelected(value.id); setIsNew(false); setDirty(false); }, `Saved scenario "${value.id}".`);
  const remove = () => run([deleteOp("scenario", value.id)], () => { setSelected(""); setIsNew(true); setDirty(false); setValue(blankScenario("scenario-1", [], null)); }, `Deleted scenario "${value.id}".`);
  const hints = useMemo(() => scenarioHints(value), [value]);
  const duration = scenarioDurationS(value);
  const schedules = Object.keys(content.schedules); const controllers = Object.keys(content.controllers);

  return <div className="dyn-section" aria-label="Scenario editor">
    <SimulationNotice />
    <div className="dyn-row">
      <label className="dyn-field"><span>Scenario</span><select aria-label="Scenario" value={isNew ? "" : selected} onChange={(event) => choose(event.target.value)}>
        {isNew ? <option value="">New (unsaved)</option> : null}{ids.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
      <button type="button" onClick={startNew}>New scenario</button>
    </div>
    <div className="dyn-grid">
      <TextField label="Scenario id" value={value.id} pattern="[a-z][a-z0-9_\-]{0,39}" disabled={!isNew} onChange={(id) => edit({ id })} />
      <label className="dyn-field"><span>Fidelity</span><output aria-label="Fidelity">T1 (fixed)</output></label>
      <SelectField label="Environment profile" value={value.profiles[0]?.profile_id ?? ""} onChange={setProfile} options={profiles.filter((item) => !item.integrity_error).map((item) => ({ value: item.profile_id, label: `${item.name} (${item.start.slice(0, 10)} to ${item.end.slice(0, 10)})` }))} />
      <TextField label="Start (UTC)" value={value.start_utc} placeholder="2026-01-01T00:00:00Z" onChange={(start_utc) => edit({ start_utc })} />
      <TextField label="End (UTC)" value={value.end_utc} placeholder="2026-01-08T00:00:00Z" onChange={(end_utc) => edit({ end_utc })} />
      <NumField label="Output cadence [s]" value={value.output_cadence_s} hint={`= ${fmtDuration(value.output_cadence_s)}`} onChange={(next) => next !== undefined && edit({ output_cadence_s: next })} />
      <NumField label="Controller cadence [s]" value={value.controller_cadence_s} hint={`= ${fmtDuration(value.controller_cadence_s)}`} onChange={(next) => next !== undefined && edit({ controller_cadence_s: next })} />
      <SelectField label="Temperature source" value={value.temperature_source} onChange={(next) => edit({ temperature_source: next as DynScenario["temperature_source"] })}
        options={[{ value: "unit_mean", label: "Unit mean" }, { value: "sea_temperature", label: "Sea temperature" }, { value: "air_temperature", label: "Air temperature" }]} />
      <NumField label="PAR scale" value={value.par_scale} onChange={(next) => next !== undefined && edit({ par_scale: next })} />
      <NumField label="PAR from GHI factor" optional value={value.par_from_ghi_factor ?? undefined} onChange={(next) => edit({ par_from_ghi_factor: next ?? null })} />
      <SelectField label="Solver method" value={value.solver_method} onChange={(next) => edit({ solver_method: next as "BDF" | "Adams" })} options={[{ value: "BDF", label: "BDF" }, { value: "Adams", label: "Adams" }]} />
      <NumField label="Relative tolerance" value={value.rtol} onChange={(next) => next !== undefined && edit({ rtol: next })} />
      <NumField label="Absolute tolerance" value={value.atol} onChange={(next) => next !== undefined && edit({ atol: next })} />
      <SelectField label="Schedule" value={value.schedule_id ?? ""} onChange={(next) => edit({ schedule_id: next === "" ? null : next })} options={[{ value: "", label: "No schedule" }, ...schedules.map((id) => ({ value: id, label: id }))]} />
    </div>
    <fieldset className="dyn-fieldset"><legend>Participating units</legend>
      {units.length ? units.map((tag) => <label key={tag}><input type="checkbox" aria-label={`Unit ${tag}`} checked={value.units.includes(tag)} onChange={(event) => toggleUnit(tag, event.target.checked)} />{tag}</label>) : <p>The draft has no photobioreactor or holdup tank.</p>}</fieldset>
    <fieldset className="dyn-fieldset"><legend>Controllers</legend>
      {controllers.length ? controllers.map((id) => <label key={id}><input type="checkbox" aria-label={`Controller ${id}`} checked={value.controllers.includes(id)}
        onChange={(event) => edit({ controllers: event.target.checked ? [...value.controllers, id] : value.controllers.filter((item) => item !== id) })} />{id}</label>) : <p>No controller is defined yet.</p>}</fieldset>
    <fieldset className="dyn-fieldset"><legend>Initial state [kg/m3]</legend>
      {value.units.length ? value.units.map((tag) => <div className="dyn-grid" key={tag}>{(["X", "N", "O2"] as const).map((key) =>
        <NumField key={key} label={`Initial ${key} of ${tag}`} value={value.initial[tag]?.[key]} onChange={(next) => setInitial(tag, key, next)} />)}</div>) : <p>Choose a unit first.</p>}
      <p className="dyn-hint">Starting values are filled in when a unit is chosen. Edit them; nothing is corrected for you.</p></fieldset>
    <fieldset className="dyn-fieldset"><legend>Flows [m3/s] (optional)</legend>
      {feeds.map((tag) => <NumField key={tag} label={`Feed flow of ${tag}`} optional value={value.feed_flows[tag]} onChange={(next) => setMap("feed_flows", tag, next)} />)}
      {pumps.map((tag) => <NumField key={tag} label={`Circulation of ${tag}`} optional value={value.circulation[tag]} onChange={(next) => setMap("circulation", tag, next)} />)}
      {!feeds.length && !pumps.length ? <p>The draft has no boundary feed or pump.</p> : null}</fieldset>
    <fieldset className="dyn-fieldset"><legend>Downstream (DWSIM-owned units)</legend>
      <label><input type="checkbox" aria-label="Downstream enabled" checked={value.downstream_enabled} onChange={(event) => edit({ downstream_enabled: event.target.checked })} />Downstream enabled</label>
      <NumField label="Downstream cadence [s]" optional value={value.downstream_cadence_s ?? undefined} hint="At least 3600 s; blank means daily when enabled." onChange={(next) => edit({ downstream_cadence_s: next ?? null })} /></fieldset>
    <p className="dyn-hint">{scenarioLimitText(value)}{duration !== null && duration > 0 ? ` Duration of this scenario: ${fmtDuration(duration)}.` : ""}</p>
    {hints.map((hint) => <p className="dyn-warning" key={hint} data-hint>{hint}</p>)}
    {problem ? <Refusal code={problem.code} message={problem.message} /> : null}{info ? <p className="dyn-success" role="status">{info}</p> : null}
    <div className="dyn-actions">
      <button type="button" disabled={busy || !value.id} onClick={() => void save()}>Save scenario</button>
      <button type="button" disabled={busy || isNew || !isId(value.id)} onClick={() => void remove()}>Delete scenario</button>
      {dirty ? <span role="status">Unsaved changes</span> : null}
    </div>
  </div>;
}
