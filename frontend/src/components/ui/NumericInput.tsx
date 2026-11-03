import { useState, type InputHTMLAttributes, type KeyboardEvent } from "react";
import { formatStepped, parseNumeric, stepNumeric, type NumericBounds } from "./numericStep";

type NumericInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange" | "min" | "max" | "step" | "inputMode"> & NumericBounds & Readonly<{
  value: string | number | null | undefined;
  onValueChange(text: string): void;
}>;

/**
 * Spec 213 shared numeric field: free typing (validation stays with the owning form), plus magnitude-aware
 * ArrowUp/ArrowDown and step buttons that respect the parameter's valid range. Replaces native number spinners,
 * which step by a fixed amount through zero into invalid negative values.
 */
export default function NumericInput({ value, onValueChange, min, max, minExclusive, integer, className, disabled, readOnly, onKeyDown, onBlur, ...rest }: NumericInputProps) {
  const stored = value === null || value === undefined ? "" : String(value);
  // Keep the typed text ("0.", "1e-", "-") while it still means the stored number, so numeric owners can store numbers.
  const [draft, setDraft] = useState<string | null>(null);
  const text = draft !== null && parseNumeric(draft) === parseNumeric(stored) ? draft : stored;
  const bounds: NumericBounds = { min, max, minExclusive, integer };
  const up = stepNumeric(text, 1, bounds);
  const down = stepNumeric(text, -1, bounds);
  const locked = disabled || readOnly;
  const apply = (next: number | null) => {
    if (next === null || locked) return;
    setDraft(null);
    onValueChange(formatStepped(next));
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    onKeyDown?.(event);
    if (event.defaultPrevented || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    event.preventDefault();
    apply(event.key === "ArrowUp" ? up : down);
  };
  return (
    <span className={`numeric-input${className ? ` ${className}` : ""}`}>
      <input
        {...rest}
        type="text"
        role="spinbutton"
        inputMode={integer ? "numeric" : "decimal"}
        autoComplete="off"
        aria-valuenow={parseNumeric(text) ?? undefined}
        aria-valuemin={min}
        aria-valuemax={max}
        value={text}
        disabled={disabled}
        readOnly={readOnly}
        onChange={(event) => { setDraft(event.target.value); onValueChange(event.target.value); }}
        onBlur={(event) => { setDraft(null); onBlur?.(event); }}
        onKeyDown={handleKeyDown}
      />
      <span className="numeric-input__steps" aria-hidden="true">
        <button type="button" tabIndex={-1} disabled={locked || up === null} onClick={() => apply(up)}>▴</button>
        <button type="button" tabIndex={-1} disabled={locked || down === null} onClick={() => apply(down)}>▾</button>
      </span>
    </span>
  );
}
