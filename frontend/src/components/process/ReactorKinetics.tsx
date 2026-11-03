import { useEffect, useId, useState } from "react";
import type { DraftObject, DraftOp, DraftReaction, DraftRegistry, Finding, KineticsFormId, KineticsRecord, KineticsScript, ResultsState } from "../../api/processDraft";
import { formatQuantity } from "../../api/processDraft";
import { MathTree } from "./BiologyModelLibrary";
import QuantityInput from "./QuantityInput";
import {
  FALLBACK_UNITS, FORM_LABELS, INHIBITION_LABELS, PROVENANCE_LABELS, buildReaction, compilationState, emptyForm, formFromReaction, formLabel,
  humanUnit, kineticsFindingText, equationTree, reactionBody, reactionEquation, reactionFindings, reactorFindings, serverFieldErrors, stoichiometryText,
  validateForm, type InhibitionKind, type KineticsForm, type ProvenanceKind, type Q,
} from "./kineticsLogic";
import "./ReactorKinetics.css";

type Reaction = DraftReaction & { phase?: string };
const FORM_ORDER: KineticsFormId[] = ["monod", "haldane", "power_law_arrhenius"];

function FindingList({ items }: { items: Finding[] }) {
  if (!items.length) return null;
  return <ul className="reactor-findings">{items.map((item, index) =>
    <li key={`${item.code}-${index}`} className={`reactor-finding reactor-finding--${item.severity === "info" ? "info" : item.severity === "warning" ? "warning" : "blocker"}`}>{kineticsFindingText(item)}</li>)}</ul>;
}

export default function ReactorKinetics({ unit, objects, reactions, compounds, registry, findings, results, record, script, apply, showError }: {
  unit: DraftObject; objects: DraftObject[]; reactions: Record<string, DraftReaction>; compounds: string[]; registry: DraftRegistry;
  findings: Finding[]; results: ResultsState; record: KineticsRecord | undefined; script?: KineticsScript;
  apply(ops: DraftOp[]): Promise<unknown>; showError(message: string): void;
}) {
  const base = useId();
  const kinetics = registry.kinetics;
  const [form, setForm] = useState<KineticsForm | null>(null);
  const [pick, setPick] = useState("");
  const [removeId, setRemoveId] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  useEffect(() => { setForm(null); setPick(""); setRemoveId(null); setTouched(false); }, [unit.id]);

  const assigned = unit.reactions ?? [];
  const available = Object.entries(reactions).filter(([key]) => !assigned.includes(key));
  const units = (kind: string) => registry.quantity_units[kind]?.display ?? FALLBACK_UNITS[kind] ?? [];
  const typedHere = assigned.some((id) => Boolean((reactions[id] as Reaction | undefined)?.rate_law));
  const update = (patch: Partial<KineticsForm>) => setForm((old) => (old ? { ...old, ...patch } : old));
  const updateQ = (key: string, next: Q) => setForm((old) => (old ? { ...old, q: { ...old.q, [key]: next } } : old));
  const explanation = (id: string) => kinetics?.forms.find((item) => item.id === id)?.explanation;

  const clientErrors = form ? validateForm(form, kinetics, compounds) : {};
  const serverErrors = form?.existing ? serverFieldErrors(findings, unit.tag, form.id) : {};
  // Entered values are checked as the operator types; "still empty" messages wait until the first save attempt.
  const quiet = /is required|^Choose|at least two/;
  const shown = (key: string) => {
    const client = clientErrors[key];
    return (client && (touched || !quiet.test(client)) ? client : "") || serverErrors[key];
  };
  const invalid = Object.keys(clientErrors).length > 0;

  const save = () => {
    if (!form) return;
    setTouched(true);
    if (invalid) return;
    void apply([{ op: "set_reactor_reaction", unit: unit.id, reaction_id: form.id, reaction: buildReaction(form) }])
      .then((applied) => { if (applied) { setForm(null); setTouched(false); } })
      .catch((cause: unknown) => showError(cause instanceof Error ? cause.message : "Could not save the reaction."));
  };
  const participants = form?.rows.filter((item) => item.compound) ?? [];
  const reactants = participants.filter((item) => Number(item.coeff) < 0).map((item) => item.compound);
  const rangeHint = (param: { minimum: number; maximum: number; unit: string } | undefined) =>
    param ? `Allowed ${param.minimum} to ${param.maximum} ${humanUnit(param.unit)}` : "";
  const formSpec = form ? kinetics?.forms.find((item) => item.id === form.kind) : undefined;
  const qInput = (label: string, key: string, kind: string, errorKey: string, hint?: string) =>
    <QuantityInput label={label} kind={kind} units={units(kind)} value={form!.q[key]} error={shown(errorKey)} hint={hint ?? ""}
      onChange={(next) => updateQ(key, next)} />;
  const setRow = (index: number, patch: Partial<{ compound: string; coeff: string }>) =>
    update({ rows: form!.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)) });
  const setTerm = (index: number, patch: Partial<KineticsForm["inhibitions"][number]>) =>
    update({ inhibitions: form!.inhibitions.map((term, i) => (i === index ? { ...term, ...patch } : term)) });

  const compilation = compilationState(record, results, script?.compilable);
  const reactorLevel = reactorFindings(findings, unit.tag);
  const maxTerms = kinetics?.inhibition.max_terms ?? 3;

  return <section className="reactor-kinetics" aria-label="Reactor kinetics">
    <p className="draft-hint">The rate is the consumption of the base reactant per reactor volume. DWSIM evaluates the rate law; Jarvis checks DWSIM's answer after Run.</p>
    <FindingList items={reactorLevel} />
    {assigned.length === 0 && <p className="draft-hint">No reaction on this reactor. Add one, or assign an existing reaction, before Run.</p>}

    <div className="reactor-reaction-list">{assigned.map((reactionId) => {
      const reaction = reactions[reactionId] as Reaction | undefined;
      if (!reaction) return <p key={reactionId} className="reactor-finding reactor-finding--blocker">An assigned reaction is missing from the draft.</p>;
      const law = reaction.rate_law;
      const formId = law?.form ?? "power_law_arrhenius";
      const shared = objects.filter((other) => other.kind === "unit" && other.id !== unit.id && other.reactions?.includes(reactionId)).map((other) => other.tag);
      const sides = stoichiometryText(reaction);
      const confirmId = `${base}-remove-${reactionId}`;
      return <article key={reactionId} className="reactor-reaction-card" aria-label={`Reaction ${reaction.name}`}>
        <header><h4>{reaction.name}</h4><span className="reactor-badge">{formLabel(formId, kinetics)}</span></header>
        <p className="reactor-stoich">{sides.reactants} → {sides.products}</p>
        <div className="reactor-equation" role="img" aria-label={`${formLabel(formId, kinetics)} rate equation`}><MathTree node={reactionEquation(reaction)} /></div>
        <dl className="reactor-facts">
          <div><dt>Base reactant</dt><dd>{reaction.base_reactant ?? "—"}</dd></div>
          {law && <><div><dt>V_max</dt><dd>{formatQuantity(law.v_max)}</dd></div><div><dt>K_S</dt><dd>{formatQuantity(law.k_s)}</dd></div></>}
          {law?.k_i && <div><dt>K_I</dt><dd>{formatQuantity(law.k_i)}</dd></div>}
          {law?.inhibitions?.map((term, index) => <div key={index}><dt>{INHIBITION_LABELS[term.kind]} by {term.inhibitor}</dt><dd>K_i {formatQuantity(term.k_i)}</dd></div>)}
          {law?.temperature && <div><dt>Temperature factor</dt><dd>E_a {formatQuantity(law.temperature.activation_energy)}, T_ref {formatQuantity(law.temperature.reference_temperature)}</dd></div>}
          {!law && <><div><dt>A</dt><dd>{formatQuantity(reaction.A_forward)}</dd></div><div><dt>E</dt><dd>{formatQuantity(reaction.E_forward)}</dd></div></>}
          {reaction.provenance && <div><dt>Source</dt><dd>{PROVENANCE_LABELS[reaction.provenance.kind]}{reaction.provenance.citation ? ` · ${reaction.provenance.citation}` : ""}</dd></div>}
        </dl>
        <p className="reactor-explain">{explanation(formId)}</p>
        {(law?.inhibitions?.length ?? 0) > 0 && <p className="reactor-explain">{kinetics?.inhibition.explanation}</p>}
        {law?.temperature && <p className="reactor-explain">{kinetics?.temperature.explanation}</p>}
        {shared.length > 0 && <p className="reactor-note">Shared with {shared.join(", ")}. Editing it changes every reactor that uses it.</p>}
        <FindingList items={reactionFindings(findings, unit.tag, reactionId)} />
        <div className="reactor-actions">
          <button type="button" onClick={() => { setForm(formFromReaction(reactionId, reaction, reactions)); setTouched(false); }}>Edit</button>
          <button type="button" aria-expanded={removeId === reactionId} aria-controls={confirmId} onClick={() => setRemoveId(reactionId)}>Remove from this reactor</button>
        </div>
        {removeId === reactionId && <div className="reactor-confirm" id={confirmId} role="alertdialog" aria-label={`Remove ${reaction.name}`}>
          <p>{shared.length ? `Remove "${reaction.name}" from ${unit.tag}? It stays on ${shared.join(", ")}.`
            : `Remove "${reaction.name}" from ${unit.tag}? No other reactor uses it, so it will be deleted from the draft.`}</p>
          <div className="reactor-actions">
            <button type="button" autoFocus onClick={() => { void apply([{ op: "set_reactor_reaction", unit: unit.id, reaction_id: reactionId, reaction: null }]).catch((cause: unknown) => showError(cause instanceof Error ? cause.message : "Could not remove the reaction.")); setRemoveId(null); }}>Remove</button>
            <button type="button" onClick={() => setRemoveId(null)}>Cancel</button>
          </div>
        </div>}
      </article>;
    })}</div>

    {!form && <div className="reactor-actions">
      <button type="button" onClick={() => { setForm(emptyForm(reactions, kinetics ? "monod" : "power_law_arrhenius")); setTouched(false); }}>Add reaction</button>
      {available.length > 0 && <label className="reactor-assign"><span>Assign existing</span>
        <select aria-label="Assign existing reaction" value={pick} onChange={(event) => setPick(event.target.value)}>
          <option value="">Choose a reaction…</option>
          {available.map(([key, reaction]) => <option key={key} value={key}>{reaction.name} · {formLabel((reaction as Reaction).rate_law?.form, kinetics)}</option>)}
        </select></label>}
      {pick && <button type="button" onClick={() => {
        void apply([{ op: "set_reactor_reaction", unit: unit.id, reaction_id: pick, reaction: reactionBody(reactions[pick] as Reaction) }])
          .catch((cause: unknown) => showError(cause instanceof Error ? cause.message : "Could not assign the reaction."));
        setPick("");
      }}>Assign</button>}
    </div>}

    {form && <section className="reactor-editor" aria-label="Reaction editor">
      <h4>{form.existing ? "Edit reaction" : "Add reaction"}</h4>
      <label className="reactor-field"><span>Name</span>
        <input aria-label="Reaction name" aria-invalid={Boolean(shown("name"))} maxLength={80} value={form.name} onChange={(event) => update({ name: event.target.value })} />
        {shown("name") && <small role="alert">{shown("name")}</small>}</label>

      <fieldset><legend>Stoichiometry</legend>
        <p className="draft-hint">Negative coefficients are consumed, positive are produced.</p>
        {form.rows.map((row, index) => <div className="reactor-row" key={index}>
          <select aria-label={`Participant ${index + 1}`} value={row.compound} onChange={(event) => setRow(index, { compound: event.target.value })}>
            <option value="">Compound…</option>
            {compounds.filter((name) => name === row.compound || !form.rows.some((item) => item.compound === name)).map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
          <input inputMode="decimal" aria-label={`Participant ${index + 1} coefficient`} placeholder="e.g. -1" value={row.coeff} onChange={(event) => setRow(index, { coeff: event.target.value })} />
          <button type="button" aria-label={`Remove participant ${index + 1}`} onClick={() => update({ rows: form.rows.filter((_, i) => i !== index), base: form.base === row.compound ? "" : form.base })}>Remove</button>
        </div>)}
        <button type="button" disabled={form.rows.length >= compounds.length} onClick={() => update({ rows: [...form.rows, { compound: "", coeff: "" }] })}>Add participant</button>
        {shown("stoichiometry") && <small role="alert">{shown("stoichiometry")}</small>}
        {compounds.length < 2 && <small role="alert">Declare at least two compounds in Thermo first.</small>}
        <label className="reactor-field"><span>Base reactant</span>
          <select aria-label="Base reactant" value={form.base} onChange={(event) => update({ base: event.target.value })}>
            <option value="">Choose…</option>
            {reactants.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
          {shown("base") && <small role="alert">{shown("base")}</small>}</label>
      </fieldset>

      <label className="reactor-field"><span>Rate-law type</span>
        <select aria-label="Rate-law type" value={form.kind} onChange={(event) => update({ kind: event.target.value as KineticsFormId })}>
          {FORM_ORDER.map((id) => <option key={id} value={id}>{formLabel(id, kinetics)}</option>)}
        </select></label>
      <div className="reactor-equation" role="img" aria-label="Rate equation"><MathTree node={equationTree(form.kind, form.inhibitions, form.useTemperature)} /></div>
      <p className="reactor-explain">{explanation(form.kind) ?? `${FORM_LABELS[form.kind]} rate law.`}</p>

      {form.kind === "power_law_arrhenius" ? <>
        <QuantityInput label="A (pre-exponential)" kind="reaction_rate" units={["kmol/[m3.h]"]} value={form.q.A} error={shown("A")} hint="" onChange={(next) => updateQ("A", next)} />
        <QuantityInput label="E (activation energy)" kind="molar_energy" units={units("molar_energy")} value={form.q.E} error={shown("E")} hint="" onChange={(next) => updateQ("E", next)} />
        <fieldset><legend>Reaction orders (optional)</legend>
          {participants.length === 0 && <p className="draft-hint">Choose participants first.</p>}
          {participants.map((item) => <label className="reactor-field" key={item.compound}><span>{item.compound}</span>
            <input inputMode="decimal" aria-label={`${item.compound} order`} placeholder="default" value={form.orders[item.compound] ?? ""} onChange={(event) => update({ orders: { ...form.orders, [item.compound]: event.target.value } })} /></label>)}
          {shown("orders") && <small role="alert">{shown("orders")}</small>}
        </fieldset>
      </> : <>
        <div className="reactor-substrate"><span>Substrate</span><strong>{form.base || "set by the base reactant"}</strong></div>
        {qInput("Maximum rate V_max", "v_max", "reaction_rate", "rate_law.v_max", rangeHint(formSpec?.parameters.find((item) => item.key === "v_max")))}
        {qInput("Half-saturation K_S", "k_s", "molar_concentration", "rate_law.k_s", rangeHint(formSpec?.parameters.find((item) => item.key === "k_s")))}
        {form.kind === "haldane" && qInput("Substrate inhibition K_I", "k_i", "molar_concentration", "rate_law.k_i", rangeHint(formSpec?.parameters.find((item) => item.key === "k_i")))}

        <fieldset><legend>Inhibition terms (up to {maxTerms})</legend>
          {form.inhibitions.length > 0 && <p className="reactor-explain">{kinetics?.inhibition.explanation}</p>}
          {form.inhibitions.map((term, index) => <div className="reactor-inhibition" key={index}>
            <label className="reactor-field"><span>Kind</span>
              <select aria-label={`Inhibition ${index + 1} kind`} value={term.kind} onChange={(event) => setTerm(index, { kind: event.target.value as InhibitionKind })}>
                {(kinetics?.inhibition.kinds ?? ["noncompetitive", "competitive"]).map((kind) => <option key={kind} value={kind}>{INHIBITION_LABELS[kind]}</option>)}
              </select></label>
            <label className="reactor-field"><span>Inhibitor</span>
              <select aria-label={`Inhibition ${index + 1} inhibitor`} aria-invalid={Boolean(shown(`rate_law.inhibitions.${index}.inhibitor`))} value={term.inhibitor} onChange={(event) => setTerm(index, { inhibitor: event.target.value })}>
                <option value="">Choose…</option>
                {participants.filter((item) => item.compound !== form.base).map((item) => <option key={item.compound} value={item.compound}>{item.compound}</option>)}
              </select>
              {shown(`rate_law.inhibitions.${index}.inhibitor`) && <small role="alert">{shown(`rate_law.inhibitions.${index}.inhibitor`)}</small>}</label>
            <QuantityInput label={`Inhibition ${index + 1} K_i`} kind="molar_concentration" units={units("molar_concentration")} value={term.k_i} hint=""
              error={shown(`rate_law.inhibitions.${index}.k_i`)} onChange={(next) => setTerm(index, { k_i: next })} />
            <button type="button" onClick={() => update({ inhibitions: form.inhibitions.filter((_, i) => i !== index) })}>Remove inhibition</button>
          </div>)}
          {form.inhibitions.length < maxTerms && <button type="button" onClick={() => update({ inhibitions: [...form.inhibitions, { kind: "noncompetitive", inhibitor: "", k_i: { text: "", unit: "kmol/m3" } }] })}>Add inhibition term</button>}
          {shown("inhibitions") && <small role="alert">{shown("inhibitions")}</small>}
        </fieldset>

        <label className="reactor-check"><input type="checkbox" checked={form.useTemperature} onChange={(event) => update({ useTemperature: event.target.checked })} />Temperature factor</label>
        {form.useTemperature && <>
          <p className="reactor-explain">{kinetics?.temperature.explanation}</p>
          {qInput("Activation energy E_a", "activation", "molar_energy", "rate_law.temperature.activation_energy", rangeHint(kinetics?.temperature.activation_energy))}
          {qInput("Reference temperature T_ref", "reference", "temperature", "rate_law.temperature.reference_temperature", rangeHint(kinetics?.temperature.reference_temperature))}
        </>}

        <fieldset><legend>Validity (optional)</legend>
          <p className="draft-hint">Jarvis warns after Run when the reactor is outside these limits.</p>
          {qInput("Minimum temperature", "tMin", "temperature", "tMin")}
          {qInput("Maximum temperature", "tMax", "temperature", "tMax")}
          {qInput("Maximum substrate concentration", "sMax", "molar_concentration", "sMax")}
        </fieldset>

        <fieldset><legend>Where do these parameters come from?</legend>
          <label className="reactor-field"><span>Source kind</span>
            <select aria-label="Provenance kind" value={form.provenance} onChange={(event) => update({ provenance: event.target.value as ProvenanceKind })}>
              {(kinetics?.provenance_kinds ?? Object.keys(PROVENANCE_LABELS) as ProvenanceKind[]).map((kind) => <option key={kind} value={kind}>{PROVENANCE_LABELS[kind]}</option>)}
            </select></label>
          <label className="reactor-field"><span>Citation{form.provenance === "literature" ? " (required)" : ""}</span>
            <input aria-label="Citation" aria-invalid={Boolean(shown("provenance.citation"))} maxLength={300} value={form.citation} onChange={(event) => update({ citation: event.target.value })} />
            {shown("provenance.citation") && <small role="alert">{shown("provenance.citation")}</small>}</label>
          <label className="reactor-field"><span>Note</span>
            <textarea aria-label="Provenance note" maxLength={500} rows={2} value={form.note} onChange={(event) => update({ note: event.target.value })} /></label>
        </fieldset>
      </>}
      {form.existing && reactions[form.id] && objects.some((other) => other.kind === "unit" && other.id !== unit.id && other.reactions?.includes(form.id)) &&
        <p className="reactor-note">This reaction is shared; saving changes it on every reactor that uses it.</p>}
      {touched && invalid && <p className="reactor-finding reactor-finding--blocker" role="alert">Correct the highlighted fields to save.</p>}
      <div className="reactor-actions">
        <button type="button" onClick={save}>Save reaction</button>
        <button type="button" onClick={() => { setForm(null); setTouched(false); }}>Cancel</button>
      </div>
    </section>}

    {typedHere && <section className="reactor-compilation" aria-label="DWSIM compilation">
      <h4>DWSIM compilation</h4>
      <p><strong>Evaluated by DWSIM · authored by Jarvis</strong></p>
      {script?.script_title && <p>Script title: <code>{script.script_title}</code></p>}
      <p className={`reactor-compile reactor-compile--${compilation.tone}`}>{compilation.text}</p>
      <details><summary>Generated script (read-only)</summary>
        {typeof script?.script_text === "string" ? <pre>{script.script_text}</pre> : <p className="draft-hint">No script yet. It appears once the rate law has no findings.</p>}
      </details>
    </section>}

    <section className="reactor-unsupported" aria-label="Unsupported conditions">
      <h4>Not supported</h4>
      <ul>{(kinetics?.unsupported ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
    </section>
  </section>;
}
