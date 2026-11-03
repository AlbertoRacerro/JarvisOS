// Spec 216 contract 8: pure presentation rules for CFD reduced-model status on the Design component page.
import type { ReducedModelStatus, ReducedSegmentStatus } from "../api/designProject";

export const OBSOLETE_TEXT = "Reduced model obsolete, rerun the study";

/** User-facing status label; obsolete carries the exact rerun instruction. */
export function reducedModelStatusLabel(status: string): string {
  switch (status) {
    case "current": return "Current";
    case "obsolete": return OBSOLETE_TEXT;
    case "unknown": return "Status unknown";
    case "invalid": return "Invalid reduced model";
    default: return `Status: ${status}`;
  }
}

/** Reasons are shown for every non-current status; a current model has none to explain. */
export const showsReasons = (item: Pick<ReducedModelStatus, "status" | "obsolete_reasons">): boolean =>
  item.status !== "current" && item.obsolete_reasons.length > 0;

/** Pinning is offered only for a current model that is not already the Process unit's pin. */
export const canPinToProcess = (item: Pick<ReducedModelStatus, "status" | "process_unit_pinned">): boolean =>
  item.status === "current" && !item.process_unit_pinned;

const num = (value: number): string => Number(value.toPrecision(4)).toString();

export function segmentValidityText(segment: ReducedSegmentStatus): string {
  return `${segment.regime} · Re ${num(segment.reynolds_min)}–${num(segment.reynolds_max)} · D ${num(segment.diameter_m * 1000)} mm · uncertainty ${num(segment.combined_uncertainty * 100)} %`;
}
