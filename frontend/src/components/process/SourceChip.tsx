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

/** Read-only chip: where a number came from and its check state. Unsourced values read as typed and unchecked. */
export default function SourceChip({ source }: Readonly<{ source?: QuantitySource | null }>) {
  const resolvedKind = source?.kind ?? "typed";
  const resolvedState = source?.check_state ?? "unchecked";
  const resolvedLabel = source?.label || undefined;

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
