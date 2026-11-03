import { useId } from "react";
import { formatQuantity, unitLabel, type StoredQuantity } from "../../api/processDraft";

/** Unit-bearing input: the operator's value and unit travel to the server, which converts. */
export default function QuantityInput({
  label,
  kind,
  stored,
  units,
  value,
  onChange,
  error,
  hint,
}: {
  label: string;
  kind: string;
  stored?: StoredQuantity;
  units: string[];
  value: { text: string; unit: string } | undefined;
  onChange(next: { text: string; unit: string }): void;
  /** Inline domain message; the input is marked invalid while it is set. */
  error?: string;
  /** Replaces the stored-value line (an empty string hides it), e.g. the accepted range. */
  hint?: string;
}) {
  const current = value ?? { text: stored ? String(stored.value) : "", unit: stored?.unit ?? units[0] };
  const errorId = useId();
  return (
    <label className="draft-quantity">
      <span>{label}</span>
      <span className="draft-quantity__row">
        <input
          inputMode="decimal"
          aria-label={label}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          value={current.text}
          onChange={(event) => onChange({ ...current, text: event.target.value })}
        />
        <select
          aria-label={`${label} unit`}
          value={current.unit}
          onChange={(event) => onChange({ ...current, unit: event.target.value })}
          disabled={units.length < 2}
        >
          {units.map((unit) => (
            <option key={unit} value={unit}>
              {unitLabel(unit)}
            </option>
          ))}
        </select>
      </span>
      {error && <small id={errorId} className="draft-quantity__error">{error}</small>}
      {hint === undefined ? <small data-kind={kind}>{stored ? `stored ${formatQuantity(stored)}` : "not set"}</small>
        : hint && <small data-kind={kind}>{hint}</small>}
    </label>
  );
}
