import type { BioCard, BioForm, BioSet } from "../../api/bioModels";
import type { Finding } from "../../api/processDraft";
import { labels, MathTree } from "./BiologyModelLibrary";
import {
  humanDate, missingSetSymbols, nSourceLabel, pbrCardRefusal, PBR_PICKER_FINDING, pinStatus, revisionLabel,
  setMatchesCard, severityWord, shortRevision, verificationChip, type Pin,
} from "./pbrLogic";

const factorIds = (card: BioCard): string[] =>
  Object.values(card.factors).flatMap((value) => (Array.isArray(value) ? value : [value])).filter((id): id is string => typeof id === "string" && id !== "");
const formName = (id: string) => labels[id] ?? "Biological form";
const valueLabel: Record<string, string> = {
  mu_max: "Maximum growth rate", k_X: "Specific light extinction k_X", a: "Biomass H:C (a)", b: "Biomass O:C (b)", c: "Biomass N:C (c)", d: "Biomass P:C (d)", w_ash: "Ash fraction w_ash",
};
const valueName = (key: string) => valueLabel[key] ?? key.replace(/_/g, " ");

function Chips({ set }: { set: BioSet | undefined }) {
  const entries = Object.entries(set?.values ?? {});
  if (!set) return <p className="draft-hint">Parameter set not found in the library.</p>;
  if (!entries.length) return <p className="draft-hint">No values entered in this set yet.</p>;
  return <ul className="pbr-chips" aria-label={`Verification of ${set.name} values`}>
    {entries.map(([key, record]) => {
      const state = (record as { display_state?: string; state?: string }).display_state ?? (record as { state?: string }).state;
      const chip = verificationChip(state);
      return <li key={key} className={`pbr-chip pbr-chip--${chip.tone}`}><span>{valueName(key)}</span><strong>{chip.text}</strong></li>;
    })}
  </ul>;
}

export default function PbrModelPicker({ cards, sets, forms, pin, unitTag, findings, loadError, adopting, onPin, onAdoptSet, openLibrary, onReload }: {
  cards: BioCard[]; sets: BioSet[]; forms: BioForm[]; pin: Pin | null | undefined; unitTag: string; findings: Finding[]; loadError: string;
  adopting: boolean; onPin(card: BioCard | null): void; onAdoptSet(card: BioCard, set: BioSet): void; openLibrary(): void; onReload(): void;
}) {
  const pinnedCard = cards.find((card) => card.id === pin?.card_id);
  const setOf = (card: BioCard | undefined) => sets.find((item) => item.id === card?.parameter_set_id);
  const status = pinStatus(pin, pinnedCard, setOf(pinnedCard));
  const picked = findings.filter((finding) => PBR_PICKER_FINDING.test(finding.code));
  const formById = new Map(forms.map((form) => [form.id, form]));
  return <section className="pbr-picker" aria-label="Model card picker">
    <div className="pbr-picker__head"><h4>Growth model card</h4>
      <button type="button" className="pbr-link" onClick={onReload}>Refresh</button></div>
    {loadError && <p role="alert" className="pbr-note pbr-note--danger">{loadError}</p>}
    {picked.length > 0 && <ul className="pbr-findings" aria-label={`Model findings for ${unitTag}`}>
      {picked.map((finding, index) => { const tone = severityWord(finding.severity); return <li key={`${finding.code}-${index}`} className={`pbr-finding pbr-finding--${tone.tone}`}>
        <strong>{tone.text}</strong> {finding.message}<small>{finding.code}</small></li>; })}
    </ul>}
    {pin ? <div className="pbr-pinned">
      <p><strong>{pinnedCard ? pinnedCard.name : "Pinned card not found"}</strong>
        <span title={`card ${pin.card_id} · revision ${pin.card_revision} · digest ${pin.card_digest}`}> · pinned {pinnedCard ? revisionLabel(pinnedCard.history, pin.card_revision) : `revision ${shortRevision(pin.card_revision)}`}</span></p>
      {status.missing && <p className="pbr-note pbr-note--danger" role="alert">This card is no longer in the library, so Run is blocked until you pick another card.</p>}
      <button type="button" onClick={() => onPin(null)}>Clear model card</button>
    </div> : <p className="draft-hint">No model card is pinned. Run needs one.</p>}
    {(status.cardNewer || status.setNewer) && <div className="pbr-note pbr-note--warning" role="status">
      <strong>A newer revision is available</strong>
      <ul>{status.changes.map((change) => <li key={change}>{change}</li>)}</ul>
      {status.cardNewer && pinnedCard && <button type="button" onClick={() => onPin(pinnedCard)}>Adopt newer revision</button>}
      {status.setNewer && !status.cardNewer && pinnedCard && setOf(pinnedCard) && <>
        <p>The pin keeps using the parameter-set revision this card was built on until you choose otherwise. Adopting the newer set pins a copy of this card built on {revisionLabel(setOf(pinnedCard)!.history, setOf(pinnedCard)!.revision)}; Results become stale until the next Run.</p>
        <button type="button" disabled={adopting} onClick={() => onAdoptSet(pinnedCard, setOf(pinnedCard)!)}>{adopting ? "Adopting…" : "Adopt newer set revision"}</button>
        <button type="button" className="pbr-link" onClick={openLibrary}>Browse models…</button></>}
    </div>}
    {cards.length === 0 ? <p className="pbr-empty">No model cards — <button type="button" className="pbr-link" onClick={openLibrary}>Browse models…</button></p>
      : <ul className="pbr-cards">{cards.map((card) => {
        const set = setOf(card);
        const setCurrent = setMatchesCard(card, set);
        const refusal = pbrCardRefusal(card, formName);
        const missing = setCurrent ? missingSetSymbols(set) : [];
        const isPinned = pin?.card_id === card.id;
        const current = isPinned && !status.cardNewer;
        const reasonId = `pbr-card-reason-${card.id}`;
        return <li key={card.id} className={`pbr-card${isPinned ? " is-pinned" : ""}${refusal ? " is-refused" : ""}`}>
          <h5>{card.name}</h5>
          <p className="pbr-card__meta">Card uses set <strong>{set?.name ?? "unavailable"}</strong> · {revisionLabel(set?.history, card.parameter_set_revision)}</p>
          {set && !setCurrent && <p className="pbr-note pbr-note--warning">The library set is now {revisionLabel(set.history, set.revision)}. Its current values and verification states do not describe the set revision pinned by this card. Create a new card on the latest set to adopt those values.</p>}
          <p className="pbr-card__meta">{nSourceLabel(card.n_source)}</p>
          {refusal && <p id={reasonId} className="pbr-note pbr-note--danger">Not usable in the photobioreactor: {refusal}.</p>}
          {!refusal && missing.length > 0 && <p className="pbr-note pbr-note--warning">This set has no value yet for {missing.join(", ")}; Run stays blocked until they are entered.</p>}
          <details open={isPinned}><summary>Factors and equations</summary>
            <ul className="pbr-factors">{factorIds(card).map((id) => <li key={id} className="pbr-factor"><strong>{formName(id)}</strong>
              {formById.get(id) && <div className="bio-equation" aria-label={`Equation: ${formById.get(id)!.equation_text}`}><MathTree node={formById.get(id)!.equation} /></div>}</li>)}</ul>
          </details>
          <details open={isPinned}><summary>Verification of {setCurrent ? "pinned" : "current library"} values</summary>
            {setCurrent ? <Chips set={set} /> : <p className="draft-hint">Pinned revision values are not available in this library list. Open the Biology model library to inspect the current set.</p>}
          </details>
          <button type="button" disabled={Boolean(refusal) || current} aria-describedby={refusal ? reasonId : undefined}
            aria-label={`${current ? "Pinned" : "Use model card"} ${card.name}`}
            title={`card ${card.id} · revision ${card.revision} · ${humanDate(card.history?.[card.history.length - 1]?.created_at as string | undefined)}`}
            onClick={() => onPin(card)}>{current ? "Pinned" : isPinned ? "Re-pin this card" : "Use this card"}</button>
        </li>;
      })}</ul>}
  </section>;
}
