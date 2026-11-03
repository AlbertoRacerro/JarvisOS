import { useEffect, useMemo, useState } from "react";
import type { DraftProjection } from "../../api/processDraft";
import { ACTION_TYPES, EVENT_TYPES, blankAction, blankEvent, changeEventType, deleteOp, describeEvent, eventIssues, eventShape, fmtDuration, isId, linkBadge, linkConfirmation, needsLinkConfirmation, referencedBy, setEventShape, setOp, setPath, timelineRows, type ActionType, type DynAction, type DynEvent, type DynSchedule, type DynamicContent, type EventType, type LinkInfo } from "./dynamicLogic";
import { NumField, SelectField, TextField, TimeField, type OnPatch } from "./fields";
import { Refusal } from "./SimulationNotice";

type Props = { draft: DraftProjection; content: DynamicContent; links: LinkInfo[]; onPatch: OnPatch };
const clone = <T,>(value: T): T => structuredClone(value);
const string = (value: unknown) => String(value ?? "");
const number = (value: unknown) => typeof value === "number" ? value : undefined;
const select = (label: string, value: string, options: string[], onChange: (next: string) => void) => <SelectField label={label} value={value} options={options.map((item) => ({ value: item, label: item }))} onChange={onChange} />;

function ActionFields({ action, tanks, onChange }: { action: DynAction; tanks: string[]; onChange(next: DynAction): void }) {
  const field = (key: string, value: unknown) => onChange(setPath(action, [key], value));
  const amount = (label: string, key: string, optional = false) => <NumField label={label} optional={optional} value={number(action[key])} onChange={(next) => field(key, next)} />;
  const medium = action.type === "refill" ? "medium" : "culture";
  return <div className="dyn-grid" aria-label={`${action.type} action fields`}>
    {action.type !== "separate" && select("Tank", string(action.tank), tanks, (tank) => field("tank", tank))}
    {action.type === "draw" && <>{amount("Draw volume [m3]", "volume_m3", true)}{amount("Loop fraction [1]", "loop_fraction", true)}</>}
    {action.type === "separate" && <>{amount("Recovery [1]", "recovery")}{amount("Concentration factor", "concentration_factor")}{select("Return to tank", string(action.return_to), tanks, (return_to) => field("return_to", return_to))}</>}
    {action.type === "refill" && <>{amount("Refill volume [m3]", "volume_m3", true)}{amount("Fill to volume [m3]", "to_volume_m3", true)}</>}
    {action.type === "dose" && <>{amount("Nitrogen [kg]", "nitrogen_kg")}{amount("Dose carrier volume [m3]", "volume_m3", true)}</>}
    {action.type === "inoculate" && amount("Culture volume [m3]", "volume_m3")}
    {(action.type === "refill" || action.type === "inoculate") && (action.type === "refill" ? ["N", "O2"] : ["X", "N", "O2"]).map((name) =>
      <NumField key={name} label={`${medium} ${name} [kg/m3]`} value={number((action[medium] as Record<string, unknown> | undefined)?.[name])}
        onChange={(next) => onChange(setPath(action, [medium, name], next))} />)}
  </div>;
}

function EventFields({ event, tanks, feeds, splitters, controllers, onChange }: { event: DynEvent; tanks: string[]; feeds: string[]; splitters: string[]; controllers: string[]; onChange(next: DynEvent): void }) {
  const field = (key: string, value: unknown) => onChange(setPath(event, [key], value));
  const nested = (path: string[], value: unknown) => onChange(setPath(event, path, value));
  const shape = eventShape(event);
  const actions = event.actions ?? [];
  const targetOptions = event.type === "setpoint_change" ? controllers : event.type === "dilution" ? [...splitters.map((tag) => `splitter:${tag}`), ...feeds.map((tag) => `feed:${tag}`)] : [];
  return <div className="dyn-event-fields">
    <div className="dyn-grid">
      <SelectField label="Event type" value={event.type} options={EVENT_TYPES.map((type) => ({ value: type, label: type }))} onChange={(type) => onChange(changeEventType(event, type as EventType, tanks[0] ?? ""))} />
      <TimeField label="Earliest time" seconds={event.time_s} onChange={(time_s) => time_s !== undefined && field("time_s", time_s)} />
      <SelectField label="Trigger" value={shape} options={[{ value: "once", label: "Once" }, { value: "recurring", label: "Recurring" }, { value: "condition", label: "State condition" }]} onChange={(next) => onChange(setEventShape(event, next as "once" | "recurring" | "condition", tanks[0] ?? ""))} />
      {shape === "recurring" && <><TimeField label="Repeat every" seconds={number(event.every_s)} onChange={(every_s) => field("every_s", every_s)} />
        <NumField label="Repeat count" optional value={number(event.count)} onChange={(count) => field("count", count)} />
        <TimeField label="Repeat until" optional seconds={number(event.end_s)} onChange={(end_s) => field("end_s", end_s)} /></>}
      {shape === "condition" && (event.type === "actions" ? <>
        <TextField label="Observed channel" value={event.condition?.observed ?? ""} onChange={(observed) => nested(["condition", "observed"], observed)} />
        {select("Direction", event.condition?.direction ?? "above", ["above", "below"], (direction) => nested(["condition", "direction"], direction))}
        <NumField label="Threshold [kg/m3]" value={event.condition?.threshold} onChange={(threshold) => nested(["condition", "threshold"], threshold)} />
        <NumField label="Hysteresis [kg/m3]" value={event.condition?.hysteresis} onChange={(hysteresis) => nested(["condition", "hysteresis"], hysteresis)} />
        <TimeField label="Cooldown" seconds={event.condition?.cooldown_s} onChange={(cooldown_s) => nested(["condition", "cooldown_s"], cooldown_s)} />
        <NumField label="Maximum fires" value={event.condition?.max_fires} onChange={(max_fires) => nested(["condition", "max_fires"], max_fires)} />
      </> : <>
        <TextField label="Observed channel" value={string(event.observed)} onChange={(observed) => field("observed", observed)} />
        {select("Direction", string(event.direction), ["above", "below"], (direction) => field("direction", direction))}
        <NumField label="Threshold [kg/m3]" value={number(event.threshold)} onChange={(threshold) => field("threshold", threshold)} />
        <NumField label="Hysteresis [kg/m3]" value={number(event.hysteresis)} onChange={(hysteresis) => field("hysteresis", hysteresis)} />
      </>)}
      {["inoculation", "harvest"].includes(event.type) && select("Unit", string(event.unit), tanks, (unit) => field("unit", unit))}
      {event.type === "feed_change" && select("Feed stream", string(event.stream), feeds, (stream) => field("stream", stream))}
      {targetOptions.length > 0 && select("Target", string(event.target), targetOptions, (target) => field("target", target))}
      {["feed_change", "inoculation"].includes(event.type) && <SelectField label="Value form" value={event.value !== null && typeof event.value === "object" ? "concentrations" : "number"}
        options={[{ value: "number", label: "One number" }, { value: "concentrations", label: event.type === "feed_change" ? "Flow and feed culture" : "Concentrations" }]}
        onChange={(form) => field("value", form === "number" ? 0 : event.type === "feed_change" ? { flow_m3_s: 0, culture: { X: 0, N: 0, O2: 0 }, culture_unit: "kg/m3" } : { X: 0, N: 0, O2: 0 })} />}
      {["feed_change", "dilution", "setpoint_change", "inoculation"].includes(event.type) && (event.value !== null && typeof event.value === "object" ? <>
        {event.type === "feed_change" ? <NumField label="Feed flow [m3/s]" value={number((event.value as Record<string, unknown>).flow_m3_s)} onChange={(value) => nested(["value", "flow_m3_s"], value)} /> : null}
        {(["X", "N", "O2"] as const).map((name) => <NumField key={name} optional label={`Value ${name} [kg/m3]`}
          value={number(event.type === "feed_change" ? ((event.value as Record<string, unknown>).culture as Record<string, unknown> | undefined)?.[name] : (event.value as Record<string, unknown>)[name])}
          onChange={(value) => nested(event.type === "feed_change" ? ["value", "culture", name] : ["value", name], value)} />)}
      </> : <NumField label={`Value [${string(event.value_unit || (event.type === "dilution" ? "1" : event.type === "feed_change" ? "m3/s" : "kg/m3"))}]`} value={number(event.value)} onChange={(value) => field("value", value)} />)}
      {event.type === "harvest" && <NumField label="Harvest fraction [1]" value={number(event.fraction)} onChange={(fraction) => field("fraction", fraction)} />}
    </div>
    {event.type === "actions" && <fieldset className="dyn-fieldset"><legend>Culture actions in order</legend>{actions.map((action, index) => <div className="dyn-action" key={index}>
      <SelectField label={`Action ${index + 1}`} value={action.type} options={ACTION_TYPES.map((type) => ({ value: type, label: type }))}
        onChange={(type) => field("actions", actions.map((item, n) => n === index ? blankAction(type as ActionType, tanks[0] ?? "") : item))} />
      <ActionFields action={action} tanks={tanks} onChange={(next) => field("actions", actions.map((item, n) => n === index ? next : item))} />
      <button type="button" onClick={() => field("actions", actions.filter((_, n) => n !== index))}>Remove action {index + 1}</button></div>)}
      <button type="button" onClick={() => field("actions", [...actions, blankAction("draw", tanks[0] ?? "")])}>Add action</button></fieldset>}
    <p className="dyn-description">{describeEvent(event)}</p>{eventIssues(event).map((issue) => <p className="dyn-warning" key={issue}>{issue}</p>)}
  </div>;
}

export default function ScheduleEditor({ draft, content, links, onPatch }: Props) {
  const ids = Object.keys(content.schedules);
  const tanks = draft.objects.filter((item) => item.kind === "unit" && ["PhotobioreactorT1", "HoldupTank"].includes(item.type)).map((item) => item.tag);
  const feeds = draft.objects.filter((item) => item.kind === "stream" && !item.source).map((item) => item.tag);
  const splitters = draft.objects.filter((item) => item.kind === "unit" && item.type === "Splitter").map((item) => item.tag);
  const [selected, setSelected] = useState(ids[0] ?? ""); const [fresh, setFresh] = useState(!ids.length); const [dirty, setDirty] = useState(false);
  const [value, setValue] = useState<DynSchedule>(() => ids[0] ? clone(content.schedules[ids[0]]) : { id: "schedule-1", events: [] });
  const [active, setActive] = useState<number | null>(null); const [problem, setProblem] = useState<{ code: string | null; message: string } | null>(null);
  const [info, setInfo] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { if (!dirty && !fresh && content.schedules[selected]) setValue(clone(content.schedules[selected])); }, [content, selected, dirty, fresh]);
  const edit = (next: DynSchedule) => { setValue(next); setDirty(true); setInfo(""); };
  const link = links.find((item) => item.schedule_id === value.id) ?? null;
  const duration = Math.max(86400, ...Object.values(content.scenarios).filter((item) => item.schedule_id === value.id).map((item) => (Date.parse(item.end_utc) - Date.parse(item.start_utc)) / 1000));
  const rows = useMemo(() => timelineRows(value.events, duration), [value.events, duration]);
  const choose = (id: string) => { setSelected(id); setValue(clone(content.schedules[id])); setFresh(false); setDirty(false); setActive(null); setProblem(null); };
  const create = () => { let n = ids.length + 1; while (ids.includes(`schedule-${n}`)) n++; setValue({ id: `schedule-${n}`, events: [] }); setSelected(""); setFresh(true); setDirty(true); setActive(null); setProblem(null); };
  const submit = async (remove: boolean) => {
    if (link && needsLinkConfirmation(link) && !window.confirm(linkConfirmation(link, remove ? "delete" : "edit"))) return;
    setBusy(true); setProblem(null);
    const result = await onPatch([remove ? deleteOp("schedule", value.id) : setOp("schedule", value)]);
    setBusy(false);
    if (!result.ok) { setProblem(result); return; }
    setInfo(`${remove ? "Deleted" : "Saved"} schedule ${value.id}. Draft revision ${result.seq}.`);
    if (remove) { setSelected(""); setFresh(true); setValue({ id: "schedule-1", events: [] }); }
    else { setSelected(value.id); setFresh(false); }
    setDirty(false);
  };
  return <section className="dyn-section" aria-label="Schedule editor"><h4>Schedule timeline</h4>
    <div className="dyn-row"><SelectField label="Schedule" value={fresh ? "" : selected} options={ids.map((id) => ({ value: id, label: id }))} onChange={choose} /><button type="button" onClick={create}>New schedule</button></div>
    <TextField label="Schedule id" value={value.id} disabled={!fresh} onChange={(id) => edit({ ...value, id })} />
    {link && <p className={`dyn-badge dyn-badge--${linkBadge(link)?.tone}`} role="status">{linkBadge(link)?.text}</p>}
    <p className="dyn-hint">Events are shown in time order on a scale of {fmtDuration(duration)}. Expanded repeats are read only; edit their source event. Up to 1000 events.</p>
    <div className="dyn-timeline" aria-label="Schedule timeline" role="list">{rows.map((row) => <div key={row.key} role="listitem" className="dyn-timeline-row">
      <span className="dyn-timeline-axis" style={{ left: `${Math.min(100, Math.max(0, row.time_s / duration * 100))}%` }} aria-hidden="true" />
      <button type="button" onClick={() => setActive(row.eventIndex)} aria-label={`Edit source event ${row.eventIndex + 1}`}>
        {fmtDuration(row.time_s)} · {row.repeat === null || row.repeat === 0 ? `Event ${row.eventIndex + 1}` : `Repeat ${row.repeat + 1} of event ${row.eventIndex + 1}`} · {row.text}</button></div>)}</div>
    <details><summary>Timeline table</summary><table className="dyn-table"><thead><tr><th scope="col">Time [s]</th><th scope="col">Source event</th><th scope="col">Description</th></tr></thead><tbody>
      {rows.map((row) => <tr key={row.key}><td>{row.time_s}</td><td>{row.eventIndex + 1}{row.repeat ? ` (repeat ${row.repeat + 1})` : ""}</td><td>{row.text}</td></tr>)}</tbody></table></details>
    <div className="dyn-actions"><button type="button" onClick={() => { edit({ ...value, events: [...value.events, blankEvent("actions", tanks[0] ?? "")] }); setActive(value.events.length); }}>Add event</button></div>
    {active !== null && value.events[active] && <fieldset className="dyn-fieldset"><legend>Edit source event {active + 1}</legend>
      <EventFields event={value.events[active]} tanks={tanks} feeds={feeds} splitters={splitters} controllers={Object.keys(content.controllers)}
        onChange={(next) => edit({ ...value, events: value.events.map((item, index) => index === active ? next : item) })} />
      <button type="button" onClick={() => { edit({ ...value, events: value.events.filter((_, index) => index !== active) }); setActive(null); }}>Remove event</button></fieldset>}
    {problem && <Refusal {...problem} />}{info && <p role="status">{info}</p>}
    <div className="dyn-actions"><button type="button" disabled={busy || !isId(value.id)} onClick={() => void submit(false)}>Save schedule</button>
      <button type="button" disabled={busy || fresh || referencedBy(content, "schedule", value.id).length > 0} onClick={() => void submit(true)}>Delete schedule</button>
      {dirty && <span role="status">Unsaved changes</span>}</div>
  </section>;
}
