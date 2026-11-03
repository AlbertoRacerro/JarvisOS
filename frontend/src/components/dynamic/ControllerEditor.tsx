import { useEffect, useState } from "react";
import type { DraftProjection } from "../../api/processDraft";
import { blankController, deleteOp, describeController, isId, referencedBy, setOp, type DynamicContent, type DynController } from "./dynamicLogic";
import { NumField, SelectField, TextField, type OnPatch } from "./fields";
import { Refusal } from "./SimulationNotice";

export default function ControllerEditor({ draft, content, onPatch }: { draft: DraftProjection; content: DynamicContent; onPatch: OnPatch }) {
  const ids = Object.keys(content.controllers);
  const units = draft.objects.filter((item) => item.kind === "unit" && ["PhotobioreactorT1", "HoldupTank"].includes(item.type)).map((item) => item.tag);
  const feeds = draft.objects.filter((item) => item.kind === "stream" && !item.source).map((item) => `feed:${item.tag}`);
  const splitters = draft.objects.filter((item) => item.kind === "unit" && item.type === "Splitter").map((item) => `splitter:${item.tag}`);
  const [selected, setSelected] = useState(ids[0] ?? "");
  const [fresh, setFresh] = useState(!ids.length);
  const [dirty, setDirty] = useState(false);
  const [value, setValue] = useState<DynController>(() => ids[0] ? structuredClone(content.controllers[ids[0]]) : blankController("controller-1", units[0] ?? "", feeds[0] ?? splitters[0] ?? "feed:"));
  const [problem, setProblem] = useState<{ code: string | null; message: string } | null>(null);
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!dirty && !fresh && content.controllers[selected]) setValue(structuredClone(content.controllers[selected])); }, [content, dirty, fresh, selected]);
  const edit = (patch: Partial<DynController>) => { setValue((old) => ({ ...old, ...patch })); setDirty(true); setInfo(""); };
  const choose = (id: string) => { setSelected(id); setFresh(false); setDirty(false); setValue(structuredClone(content.controllers[id])); setProblem(null); };
  const create = () => { let n = ids.length + 1; while (ids.includes(`controller-${n}`)) n++; setValue(blankController(`controller-${n}`, units[0] ?? "", feeds[0] ?? splitters[0] ?? "feed:")); setFresh(true); setDirty(true); setSelected(""); setProblem(null); };
  const submit = async (remove: boolean) => {
    setBusy(true); setProblem(null);
    const result = await onPatch([remove ? deleteOp("controller", value.id) : setOp("controller", value)]);
    setBusy(false);
    if (!result.ok) { setProblem(result); return; }
    setInfo(`${remove ? "Deleted" : "Saved"} controller ${value.id}. Draft revision ${result.seq}.`);
    if (remove) { setSelected(""); setFresh(true); setValue(blankController("controller-1", units[0] ?? "", feeds[0] ?? splitters[0] ?? "feed:")); }
    else { setSelected(value.id); setFresh(false); }
    setDirty(false);
  };
  return <section className="dyn-section" aria-label="Controller editor"><h4>Controllers</h4>
    <div className="dyn-row"><SelectField label="Controller" value={fresh ? "" : selected} onChange={choose} options={ids.map((id) => ({ value: id, label: id }))} /><button type="button" onClick={create}>New controller</button></div>
    <div className="dyn-grid">
      <TextField label="Controller id" value={value.id} disabled={!fresh} onChange={(id) => edit({ id })} />
      <SelectField label="Type" value={value.type} onChange={(type) => edit({ type: type as DynController["type"] })} options={["onoff", "pi", "turbidostat"].map((type) => ({ value: type, label: type }))} />
      <SelectField label="Unit" value={value.unit} onChange={(unit) => edit({ unit })} options={units.map((unit) => ({ value: unit, label: unit }))} />
      <SelectField label="Measurement" value={value.measurement} onChange={(measurement) => edit({ measurement })} options={["X", "N", "O2", "loop_X", "loop_N"].map((name) => ({ value: name, label: name }))} />
      <SelectField label="Actuator" value={value.actuator} onChange={(actuator) => edit({ actuator, output_unit: actuator.startsWith("splitter:") ? "1" : "m3/s" })} options={[...feeds, ...splitters].map((name) => ({ value: name, label: name }))} />
      <NumField label="Cadence [s]" value={value.cadence_s} onChange={(cadence_s) => cadence_s !== undefined && edit({ cadence_s })} />
      <NumField label="Setpoint [kg/m3]" value={value.setpoint} onChange={(setpoint) => setpoint !== undefined && edit({ setpoint })} />
      <NumField label={`Lower output [${value.output_unit}]`} value={value.lower} onChange={(lower) => lower !== undefined && edit({ lower })} />
      <NumField label={`Upper output [${value.output_unit}]`} value={value.upper} onChange={(upper) => upper !== undefined && edit({ upper })} />
      <NumField label={`Initial output [${value.output_unit}]`} value={value.output} onChange={(output) => output !== undefined && edit({ output })} />
      <NumField label="Hysteresis [kg/m3]" value={value.hysteresis} onChange={(hysteresis) => hysteresis !== undefined && edit({ hysteresis })} />
      <NumField label="Proportional gain" value={value.kp} onChange={(kp) => kp !== undefined && edit({ kp })} />
      <NumField label="Integral gain" value={value.ki} onChange={(ki) => ki !== undefined && edit({ ki })} />
      <SelectField label="Direction" value={value.direction} onChange={(direction) => edit({ direction: direction as "above" | "below" })} options={[{ value: "above", label: "Above" }, { value: "below", label: "Below" }]} />
    </div>
    <p>{describeController(value)}</p><p className="dyn-hint">Cadence must be at least 60 s. The backend checks observability, bounds and actuator compatibility.</p>
    {problem && <Refusal {...problem} />}{info && <p role="status">{info}</p>}
    <div className="dyn-actions"><button type="button" disabled={busy || !isId(value.id)} onClick={() => void submit(false)}>Save controller</button>
      <button type="button" disabled={busy || fresh || referencedBy(content, "controller", value.id).length > 0} onClick={() => void submit(true)}>Delete controller</button>
      {dirty && <span role="status">Unsaved changes</span>}</div>
  </section>;
}
