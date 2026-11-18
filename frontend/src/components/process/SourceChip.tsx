import type { QuantitySource } from "../../api/processDraft";

export const SOURCE_KIND_LABELS: Record<NonNullable<QuantitySource["kind"]>, string> = {
  typed: "Digitato",
  literature: "Letteratura",
  ingested: "Estratto",
  result: "Risultato",
};

export const CHECK_STATE_LABELS: Record<NonNullable<QuantitySource["check_state"]>, string> = {
  unchecked: "Non verificato",
  source_checked: "Da fonte",
  verified: "Verificato",
  flagged: "Da controllare",
  stale: "Fonte modificata",
};

export interface SourceChipProps {
  source?: QuantitySource | null;
  kind?: QuantitySource["kind"];
  checkState?: QuantitySource["check_state"];
  label?: string | null;
}

export default function SourceChip({
  source,
  kind,
  checkState,
  label,
}: SourceChipProps) {
  const resolvedKind = kind ?? source?.kind ?? "typed";
  const rawState = checkState ?? source?.check_state;
  const resolvedState = rawState ?? "unchecked";
  const resolvedLabel = (label ?? source?.label) || undefined;

  const kindLabel = SOURCE_KIND_LABELS[resolvedKind] ?? "Digitato";
  const stateLabel = CHECK_STATE_LABELS[resolvedState] ?? "Non verificato";

  return (
    <span
      className="source-chip"
      data-source-kind={resolvedKind}
      data-check-state={resolvedState}
      title={resolvedLabel}
    >
      {kindLabel} · {stateLabel}
    </span>
  );
}
