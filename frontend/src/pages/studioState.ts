import type { StudioCase, StudioModel } from "../api/studio";

export function canPublishStudioDraft(model: StudioModel | null, cases: StudioCase[], dirty: boolean, runtimeReady: boolean): boolean {
  if (!model || dirty || !runtimeReady || cases.length === 0) return false;
  const report = model.last_test_report;
  return Boolean(report?.passed && report.source_sha256 === model.source_sha256 && report.contract_sha256 === model.contract_sha256 && JSON.stringify(cases) === JSON.stringify(model.test_cases));
}
