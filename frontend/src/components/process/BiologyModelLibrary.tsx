import { createElement, useEffect, useMemo, useState, type ReactNode } from "react";
import { API_BASE_URL } from "../../api/client";
import { createBioCard, createBioSet, duplicateBioSet, editBioValue, evaluateBioCard, listBioCards, listBioForms, listBioSets, reviewBioValue, verifyBioValue, BioModelsError } from "../../api/bioModels";
import type { BioCard, BioForm, BioQuantity, BioSet, MathNode } from "../../api/bioModels";
import "./BiologyModelLibrary.css";

import NumericInput from "../ui/NumericInput";
const allowedMathTags = new Set(["math", "mrow", "mi", "mn", "mo", "mtext", "msup", "msub", "mfrac", "msqrt"]);
export const labels: Record<string, string> = {
  "light.monod": "Monod light response", "light.haldane": "Haldane light response", "light.steele": "Steele light response",
  "light.eilers_peeters_steady": "Eilers–Peeters steady light response", "light.hill": "Hill light response", "optics.slab_mean_irradiance": "Mean slab irradiance",
  "optics.slab_response_average": "Depth averaged light response", "temperature.isothermal": "Isothermal temperature",
  "temperature.ctmi": "Cardinal temperature (CTMI)", "temperature.arrhenius_ref": "Arrhenius temperature",
  "nutrient.monod": "Monod nutrient limitation", "nutrient.droop": "Droop quota limitation",
  "combine.multiplicative": "Multiplicative nutrient combination", "combine.liebig": "Liebig minimum",
  "loss.first_order": "First order biomass loss", "loss.light_dark": "Light and dark biomass loss",
  "stoich.photoautotrophic": "Photoautotrophic elemental balance",
  "optics.cylinder_beam_diffuse_response_average": "Cylindrical beam and diffuse light",
};
const keyLabel: Record<string, string> = {
  mu_max: "Maximum specific growth rate", K_I: "Light half saturation", K_i: "Light inhibition constant", I_opt: "Optimum irradiance",
  beta: "Eilers–Peeters curve shape", T_min: "Minimum cardinal temperature", T_opt: "Optimum cardinal temperature", T_max: "Maximum cardinal temperature",
  T_ref: "Reference temperature", E_a: "Activation energy", k_d: "Specific biomass loss", I_dark: "Dark threshold", m_L: "Light loss rate", m_D: "Dark loss rate",
  k_X: "Specific light extinction k_X — PBR input", X: "Biomass concentration", L: "Optical path length",
  a: "Biomass formula coefficient a, H per C-mol — PBR input", b: "Biomass formula coefficient b, O per C-mol — PBR input",
  c: "Biomass formula coefficient c, N per C-mol — PBR input", d: "Biomass formula coefficient d, P per C-mol — PBR input",
  w_ash: "Ash mass fraction w_ash — PBR input",
};
const symbolKeys = (symbol: BioForm["symbols"][number], index = 0) => {
  if (symbol.key === "formula_coefficients") return ["a", "b", "c", "d"];
  if (symbol.key === "K_j") return [`K_j_${index}`];
  if (symbol.key === "Q_min") return [`Q_min_${index}`];
  return [symbol.key];
};
const humanName = (id: string) => labels[id] ?? "Biological form";
// Listed for reference only: the photobioreactor evaluates this form internally, so it is never a card factor.
const PBR_ONLY_FORMS = new Set(["optics.cylinder_beam_diffuse_response_average"]);
const displayUnit = (unit: string) => ({ "1/hour": "per hour (h⁻¹)", "1/day": "per day (d⁻¹)", "dimensionless": "dimensionless", "1": "dimensionless", "kg/m3": "kg m⁻³", "kg/m**3": "kg m⁻³", "kg/kg": "kg element per kg dry biomass", "m**2/kg": "m² kg⁻¹", "umol/(m**2*s)": "µmol m⁻² s⁻¹" }[unit] ?? unit);

export function MathTree({ node }: { node: MathNode }) {
  if (!allowedMathTags.has(node.tag)) return <span>{node.text ?? ""}</span>;
  const children: ReactNode = node.children?.map((child, index) => <MathTree key={`${child.tag}-${index}`} node={child} />) ?? node.text ?? "";
  return createElement(node.tag, node.tag === "math" ? { xmlns: "http://www.w3.org/1998/Math/MathML", display: "block" } : {}, children);
}

async function createLiteratureBasis(workspaceId: string, title: string, citation: string, value: number, unit: string, locator: string) {
  const sourceResponse = await fetch(`${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/literature/sources`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, source_kind: "paper", citation, state: "raw" }),
  });
  const source = await sourceResponse.json() as { id?: string; detail?: { message?: string } };
  if (!sourceResponse.ok || !source.id) throw new Error(source.detail?.message ?? "Could not create literature source");
  const entryResponse = await fetch(`${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/literature/sources/${source.id}/entries`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entry_kind: "datum", value_number: value, unit, status: "raw", value_text: locator, context_text: citation }),
  });
  const entry = await entryResponse.json() as { id?: string; detail?: { message?: string } };
  if (!entryResponse.ok || !entry.id) throw new Error(entry.detail?.message ?? "Could not create literature entry");
  return { object_type: "literature_entry", object_id: entry.id };
}

export default function BiologyModelLibrary({ workspaceId, onClose }: { workspaceId: string; onClose(): void }) {
  const [forms, setForms] = useState<BioForm[]>([]); const [sets, setSets] = useState<BioSet[]>([]); const [cards, setCards] = useState<BioCard[]>([]);
  const [setId, setSetId] = useState(""); const [selectedForm, setSelectedForm] = useState("light.haldane"); const [notice, setNotice] = useState("");
  const [sourceTitle, setSourceTitle] = useState(""); const [sourceCitation, setSourceCitation] = useState(""); const [sourceLocator, setSourceLocator] = useState("");
  const [muValue, setMuValue] = useState("0.08"); const [muUnit, setMuUnit] = useState("1/hour");
  const [cardName, setCardName] = useState("Growth model"); const [cardLight, setCardLight] = useState("light.monod"); const [cardTemperature, setCardTemperature] = useState("temperature.ctmi");
  const [cardNutrients, setCardNutrients] = useState(["nutrient.monod", ""]); const [cardCombination, setCardCombination] = useState("combine.liebig");
  const [cardLoss, setCardLoss] = useState("loss.first_order"); const [nSource, setNSource] = useState<"NH3" | "HNO3">("NH3");
  const [point, setPoint] = useState<Record<string, BioQuantity>>({ I0: { value: 800, unit: "umol/(m**2*s)" }, k_X: { value: 120, unit: "m**2/kg" }, X: { value: 0.7, unit: "kg/m3" }, L: { value: 0.05, unit: "m" }, T: { value: 298.15, unit: "K" }, S_0: { value: 0.1, unit: "kg/m3" }, S_1: { value: 0.05, unit: "kg/m3" }, Q_0: { value: 0.2, unit: "kg/kg" }, Q_1: { value: 0.2, unit: "kg/kg" } });
  const [preview, setPreview] = useState<{ mu_net: { value: number; unit: string }; breakdown: Record<string, { value: number; unit: string }>; stoichiometry?: { coefficients_mol_per_C_mol: Record<string, number>; yields: Record<string, number> } } | null>(null);
  const [editTarget, setEditTarget] = useState<{ key: string; value: string; unit: string; min: string; max: string } | null>(null); const [reviewTarget, setReviewTarget] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState(""); const [reviewNote, setReviewNote] = useState(""); const [verifyTarget, setVerifyTarget] = useState<string | null>(null); const [locatorConfirmed, setLocatorConfirmed] = useState(false); const [locatorConfirmation, setLocatorConfirmation] = useState("");
  const currentSet = sets.find((item) => item.id === setId) ?? null; const activeForm = forms.find((item) => item.id === selectedForm) ?? null;
  const symbols = useMemo(() => {
    const selected = new Map<string, { unit: string; range: string }>();
    const nutrients = cardNutrients.filter(Boolean);
    const ids = [cardLight, "optics.slab_response_average", cardTemperature, cardCombination, cardLoss, "stoich.photoautotrophic"];
    for (const form of forms) if (ids.includes(form.id)) for (const symbol of form.parameters) for (const key of symbolKeys(symbol)) {
      const unit = symbol.key === "formula_coefficients" ? "dimensionless" : symbol.unit;
      selected.set(key, { unit, range: symbol.valid_range });
    }
    nutrients.forEach((id, index) => { const form = forms.find((candidate) => candidate.id === id); for (const symbol of form?.parameters ?? []) for (const key of symbolKeys(symbol, index)) selected.set(key, { unit: symbol.unit, range: symbol.valid_range }); });
    for (const key of ["a", "b", "c", "d"]) selected.set(key, { unit: "dimensionless", range: "≥ 0" });
    selected.set("w_ash", { unit: "dimensionless", range: "[0, 1)" });
    selected.set("mu_max", { unit: "h⁻¹", range: "> 0" });
    selected.set("k_X", { unit: "m**2/kg", range: "≥ 0" });
    return [...selected.entries()];
  }, [forms, cardLight, cardTemperature, cardNutrients, cardCombination, cardLoss]);
  const reload = async () => { const [f, s, c] = await Promise.all([listBioForms(workspaceId), listBioSets(workspaceId), listBioCards(workspaceId)]); setForms(f); setSets(s); setCards(c); setSetId((current) => current || s[0]?.id || ""); };
  useEffect(() => { void reload().catch((error: unknown) => setNotice(error instanceof Error ? error.message : "Could not load biology models")); }, [workspaceId]);
  const updateSet = (next: BioSet) => { setSets((items) => [next, ...items.filter((item) => item.id !== next.id)]); setSetId(next.id); };
  const act = async (operation: () => Promise<unknown>) => { setNotice(""); try { await operation(); } catch (error) { if (error instanceof BioModelsError && error.status === 409) { await reload(); setNotice("This set changed elsewhere. Reloaded the latest revision; retry your action."); } else { const message = error instanceof Error ? error.message : "Biology model action failed"; setNotice(message.replace(/mu_max/g, "maximum specific growth rate").replace(/1\/hour/g, "per hour (h⁻¹)").replace(/1\/day/g, "per day (d⁻¹)").replace(/K_j_/g, "nutrient half saturation ").replace(/Q_min_/g, "minimum quota ").replace(/\bbeta\b/g, "Eilers–Peeters curve shape")); } } };
  const saveMu = async () => { if (!currentSet || !(Number(muValue) > 0)) return; const basis = sourceTitle.trim() ? await createLiteratureBasis(workspaceId, sourceTitle.trim(), sourceCitation.trim(), Number(muValue), muUnit, sourceLocator.trim()) : undefined; updateSet(await editBioValue(workspaceId, currentSet, "mu_max", Number(muValue), muUnit, "1/hour", basis)); if (basis) setNotice("Saved an operator-entered source. Verify requires confirming its section locator."); };
  const createCard = async () => { if (!currentSet || !(Number(muValue) > 0)) return; const nutrients = cardNutrients.filter(Boolean); const card = await createBioCard(workspaceId, cardName.trim() || "Growth model", currentSet, { light: cardLight, optics: "optics.slab_response_average", temperature: cardTemperature, nutrients, combination: cardCombination, loss: cardLoss, stoichiometry: "stoich.photoautotrophic" }, { value: Number(muValue), unit: muUnit }, nSource); setCards((items) => [card, ...items]); setNotice("Model card created with pinned form versions."); };
  const saveEdit = async () => { if (!currentSet || !editTarget) return; const definition = symbols.find(([key]) => key === editTarget.key)?.[1]; if (!definition) return; const previous = currentSet.values[editTarget.key]; const validity = editTarget.min || editTarget.max ? { ...(editTarget.min ? { min: Number(editTarget.min) } : {}), ...(editTarget.max ? { max: Number(editTarget.max) } : {}) } : undefined; const next = await editBioValue(workspaceId, currentSet, editTarget.key, Number(editTarget.value), editTarget.unit, definition.unit === "h⁻¹" ? "1/hour" : definition.unit === "dimensionless" ? "dimensionless" : definition.unit, previous?.basis_ref ?? undefined, validity); updateSet(next); setEditTarget(null); };
  const saveOperating = (key: string, value: string) => setPoint((old) => ({ ...old, [key]: { ...old[key], value: Number(value) } }));
  const unitsFor = (unit: string): string[] => unit === "h⁻¹" ? ["1/hour", "1/day"] : unit === "K" ? ["K"] : unit.includes("µmol") ? ["umol/(m**2*s)", "mmol/(m**2*s)"] : unit.includes("m² kg") ? ["m**2/kg", "cm**2/g"] : unit.includes("kg element") ? ["kg/kg", "g/kg"] : unit.includes("kg m⁻³") ? ["kg/m3", "g/L"] : unit.includes("J mol") ? ["J/mol", "kJ/mol"] : unit === "dimensionless" ? ["dimensionless"] : [unit];
  const labelForKey = (key: string) => keyLabel[key] ?? (key.startsWith("K_j_") ? `Nutrient ${Number(key.slice(4)) + 1} half saturation` : key.startsWith("Q_min_") ? `Nutrient ${Number(key.slice(6)) + 1} minimum quota` : key);
  const historyAction = (action: unknown) => { const text = String(action); const match = /^(edit|verify|review):(.+)$/.exec(text); if (match) return `${match[1] === "edit" ? "Edited" : match[1] === "verify" ? "Verified" : "Reviewed"} ${labelForKey(match[2])}`; return text === "duplicate" ? "Duplicated parameter set" : text === "create" ? "Created parameter set" : "Updated parameter set"; };
  const updateNutrient = (index: number, value: string) => setCardNutrients((items) => items.map((item, i) => i === index ? value : item));
  const onBackdrop = (event: React.MouseEvent<HTMLDivElement>) => { if (event.target === event.currentTarget) onClose(); };

  return <div className="bio-library__backdrop" onMouseDown={onBackdrop}><section className="bio-library" role="dialog" aria-modal="true" aria-label="Biology model library" data-testid="biology-model-library">
    <header className="bio-library__header"><div><p className="eyebrow">Process · Biology</p><h2>Biology model library</h2></div><button type="button" onClick={onClose} aria-label="Close biology model library">Close</button></header>
    {notice && <p role="status" className="bio-library__notice">{notice}</p>}
    <div className="bio-library__layout">
      <section className="bio-library__forms" aria-label="Biological forms"><h3>Reviewed forms</h3><label>Form<select aria-label="Biological form" value={selectedForm} onChange={(event) => setSelectedForm(event.target.value)}>{forms.map((form) => <option key={form.id} value={form.id}>{humanName(form.id)}{PBR_ONLY_FORMS.has(form.id) ? " — evaluated inside Photobioreactor (T1)" : ""}</option>)}</select></label>
        {activeForm && <article className="bio-form-card"><h4>{humanName(activeForm.id)} <small>v{activeForm.version}</small></h4><div className="bio-equation" aria-label={`Equation: ${activeForm.equation_text}`}><MathTree node={activeForm.equation} /></div>
          <table><caption>Parameters and valid ranges</caption><thead><tr><th>Parameter</th><th>Meaning</th><th>Unit · range</th></tr></thead><tbody>{activeForm.parameters.map((symbol) => <tr key={symbol.symbol}><th>{symbol.symbol}</th><td>{symbol.meaning}</td><td>{symbol.unit === "dimensionless" ? "dimensionless" : symbol.unit} · {symbol.valid_range}</td></tr>)}</tbody></table>
          {PBR_ONLY_FORMS.has(activeForm.id) && <p className="bio-library__metadata" data-testid="pbr-only-form">Evaluated inside Photobioreactor (T1). It is not selectable as a model card factor.</p>}<p>{activeForm.applies_to}</p><p className="bio-library__metadata">Version {activeForm.version}{activeForm.citations.length ? ` · References: ${activeForm.citations.join(", ")}` : ""}</p>
        </article>}
      </section>
      <section className="bio-library__sets" aria-label="Parameter sets"><h3>Parameter sets</h3><div className="bio-library__actions"><select aria-label="Parameter set" value={setId} onChange={(event) => setSetId(event.target.value)}><option value="">Select set</option>{sets.map((set) => <option key={set.id} value={set.id}>{set.name}</option>)}</select>
        <button type="button" onClick={() => void act(async () => updateSet(await createBioSet(workspaceId, "N. gaditana T1 — empty")))}>New empty template</button><button type="button" disabled={!currentSet} onClick={() => currentSet && void act(async () => updateSet(await duplicateBioSet(workspaceId, currentSet)))}>Duplicate set</button></div>
        {currentSet && <><p className="bio-library__metadata">Revision {currentSet.revision} · {currentSet.species || "species unspecified"} {currentSet.strain}</p>
          <table className="bio-parameter-table"><caption>Model parameters and provenance</caption><thead><tr><th>Parameter</th><th>Value</th><th>Valid range</th><th>Unit</th><th>Source and state</th><th>Actions</th></tr></thead><tbody>
            {symbols.map(([key, definition]) => {
              const record = currentSet.values[key];
              const state = record?.display_state === "source_changed_since_verification" ? "source changed since verification" : record?.display_state ?? record?.state;
              const provenance = record?.provenance;
              const resolved = record?.verification?.resolved as { source?: { title?: string; state?: string }; entry?: { status?: string } } | undefined;
              const sourceTitleLabel = provenance?.source_title ?? resolved?.source?.title ?? "Operator-entered source";
              const sourceState = provenance?.source_state ?? resolved?.source?.state ?? "operator-entered";
              const entryState = provenance?.entry_state ?? resolved?.entry?.status;
              const shownUnit = record?.entered_unit ?? record?.unit ?? definition.unit;
              const activeEdit = editTarget?.key === key;
              const customRange = record?.validity_range && typeof record.validity_range === "object" ? record.validity_range : null;
              const rangeMin = typeof customRange?.min === "number" ? customRange.min : null;
              const rangeMax = typeof customRange?.max === "number" ? customRange.max : null;
              const shownValue = record?.entered_value ?? record?.value;
              const offRange = customRange && typeof shownValue === "number" &&
                ((rangeMin !== null && shownValue < rangeMin) || (rangeMax !== null && shownValue > rangeMax));
              const progress = rangeMin !== null && rangeMax !== null && typeof shownValue === "number";
              return <tr key={key}><th>{labelForKey(key)}</th>
                <td>{activeEdit ? <NumericInput aria-label={`${labelForKey(key)} value`} value={editTarget.value} onValueChange={(text) => setEditTarget({ ...editTarget, value: text })} /> : shownValue ?? "—"}</td>
                <td>{activeEdit ? <span className="bio-range-editor"><label>Minimum<NumericInput aria-label={`${labelForKey(key)} minimum valid value`} value={editTarget.min} onValueChange={(text) => setEditTarget({ ...editTarget, min: text })} /></label><label>Maximum<NumericInput aria-label={`${labelForKey(key)} maximum valid value`} value={editTarget.max} onValueChange={(text) => setEditTarget({ ...editTarget, max: text })} /></label></span> : customRange ? <>{rangeMin ?? "−∞"} to {rangeMax ?? "+∞"}{progress && <progress aria-label={`${labelForKey(key)} validity range`} max={1} value={(Math.max(rangeMin!, Math.min(rangeMax!, shownValue!)) - rangeMin!) / (rangeMax! - rangeMin!)} />}{offRange && <strong role="alert">Off range</strong>}</> : typeof record?.validity_range === "string" ? record.validity_range : definition.range}</td>
                <td>{activeEdit ? <select aria-label={`${labelForKey(key)} unit`} value={editTarget.unit} onChange={(event) => setEditTarget({ ...editTarget, unit: event.target.value })}>{unitsFor(definition.unit).map((unit) => <option key={unit} value={unit}>{displayUnit(unit)}</option>)}</select> : displayUnit(shownUnit)}</td>
                <td>{record?.basis_ref ? <><a href={`/memory/literature?entry_id=${encodeURIComponent(String(record.basis_ref.object_id ?? ""))}`}>{state ?? "candidate"}</a><small> · {sourceTitleLabel} ({sourceState}{entryState ? ` / ${entryState}` : ""})</small>{provenance?.locator_kind && <small> · {provenance.locator_kind} {provenance.locator_start}{provenance.locator_end && provenance.locator_end !== provenance.locator_start ? `–${provenance.locator_end}` : ""}</small>}{record.verification?.no_backing_document === true && <small> · no backing document</small>}</> : "operator assumption"}</td>
                <td>{activeEdit ? <><button type="button" onClick={() => void act(saveEdit)}>Save</button><button type="button" onClick={() => setEditTarget(null)}>Cancel</button></> : <button type="button" onClick={() => { const saved = record?.validity_range && typeof record.validity_range === "object" ? record.validity_range : {}; setEditTarget({ key, value: String(shownValue ?? ""), unit: record?.entered_unit ?? unitsFor(definition.unit)[0], min: String(saved.min ?? ""), max: String(saved.max ?? "") }); }}>Edit</button>}{record?.basis_ref && !activeEdit && <button type="button" onClick={() => { setVerifyTarget(key); setLocatorConfirmed(false); setLocatorConfirmation(""); }}>Verify</button>}{record?.state === "source_verified" && !activeEdit && <button type="button" onClick={() => setReviewTarget(key)}>Review</button>}</td>
              </tr>;
            })}
          </tbody></table>
          <details><summary>Revision history ({currentSet.history.length})</summary><ol>{currentSet.history.map((revision, index) => <li key={String(revision.revision ?? index)}>Revision {index + 1} · {historyAction(revision.action)} · {String(revision.actor)}</li>)}</ol></details>
          <fieldset className="bio-parameter-editor"><legend>Maximum specific growth rate</legend><label>Value<NumericInput aria-label="μ_max value" value={muValue} onValueChange={(text) => setMuValue(text)} /></label><label>Unit<select aria-label="μ_max unit" value={muUnit} onChange={(event) => setMuUnit(event.target.value)}><option value="1/hour">per hour (h⁻¹)</option><option value="1/day">per day (d⁻¹)</option></select></label>
            <label>Literature source title<input aria-label="Literature source title" value={sourceTitle} onChange={(event) => setSourceTitle(event.target.value)} placeholder="Optional operator-entered source" /></label><label>Citation<input aria-label="Literature citation" value={sourceCitation} onChange={(event) => setSourceCitation(event.target.value)} /></label><label>Section locator to confirm<input aria-label="Literature locator" value={sourceLocator} onChange={(event) => setSourceLocator(event.target.value)} placeholder="e.g. Table 2" /></label>
            <button type="button" disabled={!(Number(muValue) > 0) || Boolean(sourceTitle.trim() && !sourceLocator.trim())} onClick={() => void act(saveMu)}>Save μ_max revision</button></fieldset>
          {verifyTarget && <fieldset className="bio-inline-action"><legend>Verify {labelForKey(verifyTarget)}</legend><p>Source: {String(((currentSet.values[verifyTarget].verification?.resolved as { source?: { title?: string } } | undefined)?.source?.title ?? sourceTitle) || "Operator-entered literature record")}. Record the page, line, or section where you checked this value.</p><label>Confirmed locator<input aria-label="Confirmed literature locator" value={locatorConfirmation} onChange={(event) => setLocatorConfirmation(event.target.value)} placeholder="e.g. Table 2" /></label><label><input type="checkbox" checked={locatorConfirmed} onChange={(event) => setLocatorConfirmed(event.target.checked)} /> I checked the shown page, line, or section locator</label><button type="button" disabled={!locatorConfirmed || !locatorConfirmation.trim()} onClick={() => void act(async () => { updateSet(await verifyBioValue(workspaceId, currentSet, verifyTarget, locatorConfirmed, locatorConfirmation)); setVerifyTarget(null); })}>Confirm and verify</button><button type="button" onClick={() => setVerifyTarget(null)}>Cancel</button></fieldset>}
          {reviewTarget && <fieldset className="bio-inline-action"><legend>Expert review · {labelForKey(reviewTarget)}</legend><label>Reviewer name<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Review note<textarea value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} /></label><button type="button" disabled={!reviewer.trim() || !reviewNote.trim()} onClick={() => void act(async () => { updateSet(await reviewBioValue(workspaceId, currentSet, reviewTarget, reviewer, reviewNote)); setReviewTarget(null); setReviewer(""); setReviewNote(""); })}>Save review</button><button type="button" onClick={() => setReviewTarget(null)}>Cancel</button></fieldset>}
        </>}
      </section>
      <section className="bio-library__builder" aria-label="Model card builder"><h3>Growth model card</h3><p>Choose reviewed factors. Nutrient parameter keys use zero-based slots, for example K_j_0 / S_0 and Q_min_0 / Q_0.</p>
        <label>Card name<input aria-label="Model card name" value={cardName} maxLength={80} onChange={(event) => setCardName(event.target.value)} /></label><label>Light response<select aria-label="Light factor" value={cardLight} onChange={(event) => setCardLight(event.target.value)}>{["light.monod", "light.haldane", "light.steele", "light.eilers_peeters_steady", "light.hill"].map((id) => <option key={id} value={id}>{humanName(id)}</option>)}</select></label><label>Temperature response<select aria-label="Temperature factor" value={cardTemperature} onChange={(event) => setCardTemperature(event.target.value)}>{["temperature.ctmi", "temperature.isothermal", "temperature.arrhenius_ref"].map((id) => <option key={id} value={id}>{humanName(id)}</option>)}</select></label>
        {cardNutrients.map((nutrient, index) => <label key={index}>Nutrient {index + 1} {index === 1 && "(optional)"}<select aria-label={`Nutrient factor ${index + 1}`} value={nutrient} onChange={(event) => updateNutrient(index, event.target.value)}><option value="">None</option><option value="nutrient.monod">Monod nutrient limitation</option><option value="nutrient.droop">Droop quota limitation</option></select></label>)}<label>Nutrient combination<select aria-label="Nutrient combination" value={cardCombination} onChange={(event) => setCardCombination(event.target.value)}><option value="combine.liebig">Liebig minimum</option><option value="combine.multiplicative">Multiplicative</option></select></label>
        <label>Biomass loss<select aria-label="Loss factor" value={cardLoss} onChange={(event) => setCardLoss(event.target.value)}><option value="loss.first_order">First order biomass loss</option><option value="loss.light_dark">Light and dark biomass loss</option></select></label><label>Nitrogen source<select aria-label="Nitrogen source" value={nSource} onChange={(event) => setNSource(event.target.value as "NH3" | "HNO3")}><option value="NH3">Ammonia (NH₃)</option><option value="HNO3">Nitric acid (HNO₃)</option></select></label>
        <button type="button" disabled={!currentSet || !(Number(muValue) > 0)} onClick={() => void act(createCard)}>Save model card</button>
        <ul>{cards.map((card) => <li key={card.id}><strong>{card.name}</strong> · {Object.values(card.factors).flatMap((value) => Array.isArray(value) ? value : [value]).map((id) => humanName(String(id))).join(" · ")} <button type="button" onClick={() => void act(async () => { setPreview(await evaluateBioCard(workspaceId, card, point)); })}>Evaluate preview</button></li>)}</ul>
        <fieldset className="bio-operating-point"><legend>Editable operating point</legend>{Object.entries(point).map(([key, quantity]) => <label key={key}>{key === "I0" ? "Surface PAR irradiance" : key === "k_X" ? "Specific light extinction" : key === "X" ? "Biomass concentration" : key === "L" ? "Optical path length" : key === "T" ? "Temperature" : key.startsWith("S_") ? `Nutrient ${Number(key.slice(2)) + 1} concentration` : key.startsWith("Q_") ? `Nutrient ${Number(key.slice(2)) + 1} quota` : "Dark threshold"}<span><NumericInput value={quantity.value} onValueChange={(text) => saveOperating(key, text)} /><small>{displayUnit(quantity.unit)}</small></span></label>)}</fieldset>
        {preview && <><table><caption>Evaluation preview · net growth = {preview.mu_net.value.toPrecision(5)} {preview.mu_net.unit}</caption><tbody>{Object.entries(preview.breakdown).map(([name, value]) => <tr key={name}><th>{name === "mu_max" ? "Maximum growth rate" : name === "light_average" ? "Depth averaged light response" : name === "temperature" ? "Temperature factor" : name === "nutrients" ? "Nutrient limitation" : "Specific biomass loss"}</th><td>{value.value.toPrecision(5)}{value.unit === "1" ? " dimensionless" : ` ${value.unit}`}</td></tr>)}</tbody></table>{preview.stoichiometry && <><table><caption>Photoautotrophic stoichiometry · signed coefficients (positive reactants, negative products)</caption><tbody>{Object.entries(preview.stoichiometry.coefficients_mol_per_C_mol).map(([name, value]) => <tr key={name}><th>{name === "H2O" ? "Water" : name === "O2" ? "Oxygen" : name}</th><td>{value.toPrecision(5)} mol per C-mol biomass</td></tr>)}</tbody></table><table><caption>Mass yields per kg total dry biomass</caption><tbody>{Object.entries(preview.stoichiometry.yields).map(([name, value]) => <tr key={name}><th>{name.startsWith("O2_") ? "Oxygen produced" : name.startsWith("CO2_") ? "Carbon dioxide consumed" : name.startsWith("N_") ? "Nitrogen consumed" : "Phosphorus consumed"}</th><td>{value.toPrecision(5)} kg per kg total dry biomass</td></tr>)}</tbody></table></>}</>}
      </section>
    </div>
  </section></div>;
}
