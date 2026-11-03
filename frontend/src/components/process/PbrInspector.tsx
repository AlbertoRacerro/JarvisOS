import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { createBioCard, listBioCards, listBioForms, listBioSets, type BioCard, type BioForm, type BioSet } from "../../api/bioModels";
import type { DraftObject, DraftOp, DraftRegistry, DraftRun, Finding, PbrFeedBasis, RegistryParam, RegistryUnit, ResultsState } from "../../api/processDraft";
import PbrModelPicker from "./PbrModelPicker";
import PbrDynamicRuns from "./PbrDynamicRuns";
import PbrResults, { type PbrFailure } from "./PbrResults";
import QuantityInput from "./QuantityInput";
import { ownerLabel } from "./processOwners";
import { OPTICS_V2_KEYS, T2_OPTICS_KEYS, cardOnLatestSet, entrySi, formatReported, formatSig, liquidVolumeM3, pbrFieldError, revisionLabel, type PbrFormValues } from "./pbrLogic";
import "./PbrInspector.css";

const TABS = ["Overview", "Geometry", "Biology", "Operation", "Light & environment", "Results"] as const;
type Tab = (typeof TABS)[number];
const PARAM_TABS = new Set<Tab>(["Geometry", "Operation", "Light & environment"]);

/** Typed jarvis-unit run with the matching registry/segment context; see ProcessDraftEditor. */
export default function PbrInspector({ workspaceId, draftId, revision, unit, spec, registry, result, results, lastRun, failure, feedBasis, findings, apply, libraryOpen, openLibrary, showError }: {
  workspaceId: string; draftId: string; revision: string; unit: DraftObject; spec: RegistryUnit; registry: DraftRegistry;
  result?: NonNullable<DraftRun["units"]>[string]; results: ResultsState; lastRun: DraftRun | null; failure: PbrFailure | null;
  feedBasis?: PbrFeedBasis;
  findings: Finding[]; apply(ops: DraftOp[]): Promise<unknown>; libraryOpen: boolean; openLibrary(): void; showError(message: string): void;
}) {
  const base = useId();
  const [tab, setTab] = useState<Tab>("Overview");
  const [form, setForm] = useState<PbrFormValues>({});
  const [cards, setCards] = useState<BioCard[]>([]);
  const [sets, setSets] = useState<BioSet[]>([]);
  const [forms, setForms] = useState<BioForm[]>([]);
  const [loadError, setLoadError] = useState("");
  const [adopting, setAdopting] = useState(false);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => { setTab("Overview"); setForm({}); }, [unit.id]);
  // Saved parameters changed (apply, undo, agent action): drop pending edits so the inputs show stored values.
  useEffect(() => { setForm({}); }, [unit.params]);

  const reload = useCallback(() => {
    void Promise.all([listBioCards(workspaceId), listBioSets(workspaceId), listBioForms(workspaceId)])
      .then(([nextCards, nextSets, nextForms]) => { setCards(nextCards); setSets(nextSets); setForms(nextForms); setLoadError(""); })
      .catch((error: unknown) => setLoadError(error instanceof Error ? error.message : "Biology models could not be loaded"));
  }, [workspaceId]);
  // Loaded on mount and whenever the Biology tab opens, so a card edited in the library shows up as a newer revision.
  useEffect(() => { if (tab === "Biology" || cards.length === 0) reload(); }, [tab, reload]);
  // Closing the Biology model library can add cards or set revisions the picker must show.
  const libraryWasOpen = useRef(libraryOpen);
  useEffect(() => { if (libraryWasOpen.current && !libraryOpen) reload(); libraryWasOpen.current = libraryOpen; }, [libraryOpen, reload]);

  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = TABS.length - 1;
    const next = event.key === "ArrowRight" ? (index === last ? 0 : index + 1)
      : event.key === "ArrowLeft" ? (index === 0 ? last : index - 1)
      : event.key === "Home" ? 0 : event.key === "End" ? last : null;
    if (next === null) return;
    event.preventDefault();
    setTab(TABS[next]);
    tabRefs.current[next]?.focus();
  };

  const stored = (key: string) => unit.params?.[key];
  const currentSi = (key: string): number | null => {
    const param = [...spec.params, ...(registry.pbr_t2_params ?? [])].find((item) => item.key === key);
    const typed = param ? entrySi(param.kind, form[key]) : null;
    return typed ?? stored(key)?.si ?? null;
  };
  const paramsFor = (name: Tab): RegistryParam[] => spec.params.filter((param) => (param.classification ?? "input") === "input" && param.group === name && !T2_OPTICS_KEYS.has(param.key));
  const opticsParams = registry.pbr_t2_params ?? [];
  const opticsModel = unit.options?.optics_model === "t2" ? "t2" : "t1";
  const tierLabel = opticsModel === "t2"
    ? "Tier 2 · unqualified · profile-driven tube/array optics"
    : "Tier 1 · unqualified · screening estimate";
  const errors = Object.fromEntries([...spec.params, ...opticsParams].map((param) => [param.key, pbrFieldError(param.key, form[param.key], currentSi)]));
  const invalid = Object.entries(form).some(([key, entry]) => entry && (errors[key] || !entry.text.trim()));
  const saveParams = (items: RegistryParam[]) => {
    const keys = items.map((param) => param.key).filter((key) => form[key]);
    if (!keys.length) return;
    if (keys.some((key) => errors[key] || !form[key]!.text.trim())) return showError("Correct the highlighted photobioreactor inputs before applying them.");
    void apply([{ op: "set_unit_params", unit: unit.id, values: Object.fromEntries(keys.map((key) => [key, { value: Number(form[key]!.text), unit: form[key]!.unit }])) }]);
  };
  const pin = (card: BioCard | null) => void apply([{ op: "set_unit_model", unit: unit.id, model: card ? { card_id: card.id, card_revision: card.revision, card_digest: card.digest } : null }]);
  // A card is immutable on its set revision, so adopting a newer set pins a copy of the card on it
  // (or an identical copy already in the library). Operator-initiated only; nothing re-pins by itself.
  const adoptSet = (card: BioCard, set: BioSet) => {
    const existing = cardOnLatestSet(cards, card, set);
    if (existing) return pin(existing);
    if (!card.n_source) return showError("This card does not record its nitrogen source; create the new card in the Biology model library.");
    setAdopting(true);
    void createBioCard(workspaceId, `${card.name} · set ${revisionLabel(set.history, set.revision)}`, set, card.factors, card.mu_max, card.n_source)
      .then((next) => { pin(next); reload(); })
      .catch((error: unknown) => showError(error instanceof Error ? error.message : "The card on the newer set could not be created"))
      .finally(() => setAdopting(false));
  };

  const volume = liquidVolumeM3(unit.params);
  const staleNote = results.state === "stale" ? " (previous Run)" : "";
  const pinnedCard = cards.find((card) => card.id === unit.model?.card_id);
  // A value that only a Run produces reads `empty` until then; no per-row explanation.
  const runStatus = lastRun ? `${lastRun.action === "validate" ? "Validate" : "Run"} ${lastRun.status.replace(/_/g, " ")}` : "none";
  const unitFindings = findings.filter((finding) => finding.object === unit.tag);

  const overview = <>
    <dl className="pbr-rows">
      <div><dt>Owner</dt><dd>{ownerLabel(spec.owner)}</dd></div>
      <div><dt>Selected optics</dt><dd>{tierLabel}</dd></div>
      {result?.fidelity && <div><dt>Last run fidelity</dt><dd>{result.fidelity}{staleNote}</dd></div>}
      <div><dt>Liquid volume V = n·π/4·D²·L</dt><dd>{volume === null ? "Enter diameter, length and tube count" : `${formatSig(volume)} m³ (${formatSig(volume * 1000)} L)`}</dd></div>
      <div><dt>Inlet flow Q</dt><dd>{result?.reported.volumetric_flow_m3_h ? `${formatReported("volumetric_flow_m3_h", result.reported.volumetric_flow_m3_h)}${staleNote}`
        : feedBasis ? `${formatSig(feedBasis.volume_flow_m3_h)} m³ h⁻¹ (feed basis)` : "empty"}</dd></div>
      <div><dt>Residence time HRT</dt><dd>{result?.reported.hrt_d ? `${formatReported("hrt_d", result.reported.hrt_d)}${staleNote}`
        : feedBasis ? `${formatSig(feedBasis.hrt_d)} d (feed basis)` : "empty"}</dd></div>
      <div><dt>Model card</dt><dd>{unit.model ? `${pinnedCard?.name ?? result?.model_pin?.card_name ?? "Pinned card"} · ${pinnedCard ? revisionLabel(pinnedCard.history, unit.model.card_revision) : "revision " + unit.model.card_revision.replace(/^r-/, "").slice(0, 8)}` : "None pinned"}</dd></div>
      <div><dt>Last run</dt><dd>{runStatus}</dd></div>
      <div><dt>Branch</dt><dd>{result?.branch ? `${result.branch === "washout" ? "Washout" : "Productive"}${staleNote}` : "empty"}</dd></div>
      <div><dt>Steady-state results</dt><dd className={results.state === "stale" ? "pbr-stale-text" : undefined}>{results.state === "none" ? "empty" : results.state === "current" ? "Current" : "Stale · inputs changed since this Run"}</dd></div>
    </dl>
    {unitFindings.length > 0 && <p className="draft-hint">{unitFindings.length} finding{unitFindings.length === 1 ? "" : "s"} on this unit; see the findings list below the canvas.</p>}
  </>;

  const paramPanel = (name: Tab) => {
    const items = paramsFor(name);
    return <>
      <div className="pbr-parameters">{items.map((param) => <QuantityInput key={param.key} label={param.label} kind={param.kind}
        stored={stored(param.key)} units={registry.quantity_units[param.kind]?.display ?? []} value={form[param.key]} error={errors[param.key]}
        onChange={(next) => setForm((old) => ({ ...old, [param.key]: next }))} />)}</div>
      {name === "Light & environment" && <p className="draft-hint">Temperature amplitude is a difference, so °C and K are the same size.</p>}
      <button type="button" disabled={!items.some((param) => form[param.key]) || invalid} onClick={() => saveParams(items)}>Apply {name.toLowerCase()} inputs</button>
      {name === "Light & environment" && <details className="pbr-optics" open={opticsModel === "t2"}>
        <summary>Optics · {opticsModel.toUpperCase()}</summary>
        <label className="pbr-optics__selector">Optics model
          <select value={opticsModel} onChange={(event) => void apply([{ op: "set_unit_params", unit: unit.id, options: { optics_model: event.target.value } }])}>
            <option value="t1">T1 · cylindrical screening</option>
            <option value="t2">T2 · profile-driven tube array</option>
          </select>
        </label>
        {opticsModel === "t2" && <>
          <p className="draft-hint">Dynamic profile only. Enter the module layout, wall and water refractive indices, and sea albedo. The ten layout and index values are required for T2. Tube outer diameter and wall absorption are optional but must be set together; with both, optics v2 is used, with neither, optics v1.</p>
          <div className="pbr-parameters">{opticsParams.map((param) => <QuantityInput key={param.key} label={param.label} kind={param.kind}
            stored={stored(param.key)} units={registry.quantity_units[param.kind]?.display ?? []} value={form[param.key]} error={errors[param.key]}
            onChange={(next) => setForm((old) => ({ ...old, [param.key]: next }))} />)}</div>
          <button type="button" disabled={!opticsParams.some((param) => form[param.key]) || invalid} onClick={() => saveParams(opticsParams)}>Apply optics inputs</button>
          {OPTICS_V2_KEYS.some((key) => stored(key)) && <button type="button" onClick={() => {
            setForm((old) => Object.fromEntries(Object.entries(old).filter(([key]) => !OPTICS_V2_KEYS.includes(key))));
            void apply([{ op: "set_unit_params", unit: unit.id, unset: [...OPTICS_V2_KEYS] }]);
          }}>Clear outer diameter and wall absorption (use optics v1)</button>}
        </>}
      </details>}
    </>;
  };

  return <section className="pbr-inspector" aria-label="Photobioreactor inspector">
    <p className="draft-owner-line"><span className="draft-owner-badge-label">{ownerLabel(spec.owner)}</span> · {tierLabel}</p>
    <div className="pbr-tabs" role="tablist" aria-label="Photobioreactor sections">
      {TABS.map((name, index) => <button key={name} ref={(node) => { tabRefs.current[index] = node; }} type="button" role="tab" id={`${base}-tab-${index}`}
        aria-selected={tab === name} aria-controls={`${base}-panel-${index}`} tabIndex={tab === name ? 0 : -1}
        onClick={() => setTab(name)} onKeyDown={(event) => onTabKey(event, index)}>{name}</button>)}
    </div>
    {TABS.map((name, index) => <div key={name} role="tabpanel" id={`${base}-panel-${index}`} aria-labelledby={`${base}-tab-${index}`} hidden={tab !== name} tabIndex={0}>
      {tab === name && (name === "Overview" ? overview
        : PARAM_TABS.has(name) ? paramPanel(name)
        : name === "Biology" ? <PbrModelPicker cards={cards} sets={sets} forms={forms} pin={unit.model} unitTag={unit.tag} findings={unitFindings}
            loadError={loadError} adopting={adopting} onPin={pin} onAdoptSet={adoptSet} openLibrary={openLibrary} onReload={reload} />
        : <><PbrResults result={result} results={results} failure={failure} />
          <PbrDynamicRuns workspaceId={workspaceId} draftId={draftId} revision={revision} tag={unit.tag} apply={apply} /></>)}
    </div>)}
  </section>;
}
