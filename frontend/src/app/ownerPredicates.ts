// ADR-061 render/watch slot: a guidance step completes only when an executable predicate over the owner-published
// surface holds. A click, a pixel change or a URL never completes a step. Predicates are pure and read-only.
import type { ProcessSurface } from "../components/ai/workspaceActionPresentation";

/** Draft revisions are "seq:digest"; the owner increments seq on every recorded revision. */
export function revisionSeq(revision: string | null | undefined): number | null {
  const seq = Number((revision ?? "").split(":", 1)[0]);
  return revision && Number.isInteger(seq) && seq > 0 ? seq : null;
}

/** The step "change this Process unit": the same draft still selects the unit, at a revision newer than observed. */
export type ProcessUnitRevised = Readonly<{ draft_id: string; unit_id: string; observed_revision: string }>;

export function processUnitRevised(surface: ProcessSurface | null, expected: ProcessUnitRevised): boolean {
  const unit = surface?.process_selection?.[0];
  const now = revisionSeq(surface?.revision), before = revisionSeq(expected.observed_revision);
  return !!surface && surface.draft_id === expected.draft_id && surface.process_selection?.length === 1
    && unit?.kind === "unit" && unit.id === expected.unit_id && now !== null && before !== null && now > before;
}
