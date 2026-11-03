import { useEffect, useState } from "react";
import { splitDuration, toSeconds } from "./dynamicLogic";

// Small controlled inputs for the dynamic forms. They hold the typed text so partial input such as "1e-" is not lost, and they emit a
// number only when the text parses; nothing is clipped or corrected.
export type PatchResult = { ok: true; seq: number } | { ok: false; code: string | null; message: string };
export type OnPatch = (ops: Record<string, unknown>[]) => Promise<PatchResult>;

export function NumField({ label, value, onChange, optional = false, hint, disabled = false }: { label: string; value: number | null | undefined; onChange(next: number | undefined): void; optional?: boolean; hint?: string; disabled?: boolean }) {
  const [text, setText] = useState(value === undefined || value === null ? "" : String(value));
  useEffect(() => {
    const parsed = text.trim() === "" ? undefined : Number(text);
    if (parsed !== (value ?? undefined) && !(typeof parsed === "number" && Number.isNaN(parsed) && value === undefined)) setText(value === undefined || value === null ? "" : String(value));
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps
  const invalid = text.trim() !== "" && !Number.isFinite(Number(text));
  return <label className="dyn-field"><span>{label}{optional ? " (optional)" : ""}</span>
    <input type="text" inputMode="decimal" aria-label={label} aria-invalid={invalid} disabled={disabled} value={text}
      onChange={(event) => {
        const next = event.target.value; setText(next);
        if (next.trim() === "") { if (optional) onChange(undefined); return; }
        const parsed = Number(next);
        if (Number.isFinite(parsed)) onChange(parsed);
      }} />
    {invalid ? <small className="dyn-invalid">Not a number.</small> : hint ? <small>{hint}</small> : null}</label>;
}

export function TextField({ label, value, onChange, pattern, disabled = false, placeholder }: { label: string; value: string; onChange(next: string): void; pattern?: string; disabled?: boolean; placeholder?: string }) {
  return <label className="dyn-field"><span>{label}</span><input type="text" aria-label={label} value={value} pattern={pattern} disabled={disabled} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} /></label>;
}

export function SelectField({ label, value, onChange, options, disabled = false }: { label: string; value: string; onChange(next: string): void; options: { value: string; label: string }[]; disabled?: boolean }) {
  return <label className="dyn-field"><span>{label}</span><select aria-label={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
    {options.some((option) => option.value === value) ? null : <option value={value}>{value === "" ? "Choose…" : `${value} (not in this draft)`}</option>}
    {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>;
}

/** Seconds stored, human unit shown. Switching the display unit never rewrites the stored seconds. */
export function TimeField({ label, seconds, onChange, optional = false }: { label: string; seconds: number | null | undefined; onChange(next: number | undefined): void; optional?: boolean }) {
  const [unit, setUnit] = useState(seconds ? splitDuration(seconds).unit : "d");
  const shown = seconds === undefined || seconds === null ? undefined : Number((seconds / toSeconds(1, unit)).toPrecision(10));
  return <div className="dyn-field dyn-time"><NumField label={label} optional={optional} value={shown} onChange={(next) => onChange(next === undefined ? undefined : toSeconds(next, unit))} />
    <label><span className="dyn-sr">{label} unit</span><select aria-label={`${label} unit`} value={unit} onChange={(event) => setUnit(event.target.value)}>
      {["d", "h", "min", "s"].map((name) => <option key={name} value={name}>{name}</option>)}</select></label></div>;
}
