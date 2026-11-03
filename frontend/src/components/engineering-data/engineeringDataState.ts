export const ENGINEERING_KINDS = ["model-spec", "assumption", "parameter", "decision"] as const;
export type EngineeringKind = (typeof ENGINEERING_KINDS)[number];

export type ModelSpecRecord = Readonly<{
  id: string; workspace_id: string; title: string; engineering_question: string; scope?: string | null;
  status: string; maturity_status: string; schema_version: number; created_at: string; updated_at: string;
}>;
export type AssumptionRecord = Readonly<{ id: string; workspace_id: string; statement: string; confidence?: number | null; status: string }>;
export type ParameterRecord = Readonly<{
  id: string; workspace_id: string; name: string; symbol?: string | null; value?: string | null; unit?: string | null;
  value_status: string; value_min?: number | null; value_max?: number | null; source_ref?: string | null; confidence?: number | null;
  status: string; notes?: string | null; supersedes_parameter_id?: string | null; lifecycle_state: string; created_at: string; updated_at: string;
}>;
export type DecisionRecord = Readonly<{ id: string; workspace_id: string; title: string; decision_text: string; status: string }>;

export type EngineeringRecordProjection =
  | Readonly<{ kind: "model-spec"; id: string; workspaceId: string; primary: string; secondary: string; status: string; searchText: string; record: ModelSpecRecord }>
  | Readonly<{ kind: "assumption"; id: string; workspaceId: string; primary: string; secondary: string; status: string; searchText: string; record: AssumptionRecord }>
  | Readonly<{ kind: "parameter"; id: string; workspaceId: string; primary: string; secondary: string; status: string; searchText: string; record: ParameterRecord }>
  | Readonly<{ kind: "decision"; id: string; workspaceId: string; primary: string; secondary: string; status: string; searchText: string; record: DecisionRecord }>;

export type EngineeringDataInput = Readonly<{
  modelSpecs: readonly ModelSpecRecord[];
  assumptions: readonly AssumptionRecord[];
  parameters: readonly ParameterRecord[];
  decisions: readonly DecisionRecord[];
}>;

const KIND_ORDER: Readonly<Record<EngineeringKind, number>> = {
  "model-spec": 0,
  assumption: 1,
  parameter: 2,
  decision: 3,
};

function fold(value: string | null | undefined): string {
  return (value ?? "").trim().toLocaleLowerCase("en-US");
}

function searchable(values: readonly (string | number | null | undefined)[]): string {
  return values.filter((value) => value !== null && value !== undefined).map((value) => String(value)).join("\n");
}

export function recordKey(record: Pick<EngineeringRecordProjection, "kind" | "id">): string {
  return `${record.kind}:${record.id}`;
}

export function acceptsWorkspaceResponse(requestGeneration: number, requestWorkspaceId: string, currentGeneration: number, currentWorkspaceId: string | null): boolean {
  return requestGeneration === currentGeneration && requestWorkspaceId === currentWorkspaceId;
}

export function projectEngineeringData(input: EngineeringDataInput): EngineeringRecordProjection[] {
  const rows: EngineeringRecordProjection[] = [
    ...input.modelSpecs.map((record) => ({
      kind: "model-spec" as const,
      id: record.id,
      workspaceId: record.workspace_id,
      primary: record.title,
      secondary: record.engineering_question,
      status: record.status,
      searchText: searchable([record.title, record.engineering_question, record.scope, record.status, record.maturity_status, record.id]),
      record,
    })),
    ...input.assumptions.map((record) => ({
      kind: "assumption" as const,
      id: record.id,
      workspaceId: record.workspace_id,
      primary: record.statement,
      secondary: record.status,
      status: record.status,
      searchText: searchable([record.statement, record.status, record.id]),
      record,
    })),
    ...input.parameters.map((record) => ({
      kind: "parameter" as const,
      id: record.id,
      workspaceId: record.workspace_id,
      primary: record.name,
      secondary: [record.symbol, record.value, record.unit].filter(Boolean).join(" · "),
      status: record.lifecycle_state,
      searchText: searchable([record.name, record.symbol, record.value, record.unit, record.lifecycle_state, record.status, record.value_status, record.id]),
      record,
    })),
    ...input.decisions.map((record) => ({
      kind: "decision" as const,
      id: record.id,
      workspaceId: record.workspace_id,
      primary: record.title,
      secondary: record.decision_text,
      status: record.status,
      searchText: searchable([record.title, record.decision_text, record.status, record.id]),
      record,
    })),
  ];

  return rows.sort((left, right) => {
    const kind = KIND_ORDER[left.kind] - KIND_ORDER[right.kind];
    if (kind !== 0) return kind;
    const leftPrimary = fold(left.primary);
    const rightPrimary = fold(right.primary);
    if (leftPrimary < rightPrimary) return -1;
    if (leftPrimary > rightPrimary) return 1;
    return left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
  });
}

export function visibleEngineeringRecords(rows: readonly EngineeringRecordProjection[], query: string, kinds: readonly EngineeringKind[]): EngineeringRecordProjection[] {
  const needle = fold(query);
  const enabled = new Set(kinds);
  return rows.filter((row) => enabled.has(row.kind) && (!needle || fold(row.searchText).includes(needle)));
}

export function chooseEngineeringSelection(currentKey: string | null, visible: readonly EngineeringRecordProjection[]): string | null {
  if (currentKey && visible.some((row) => recordKey(row) === currentKey)) return currentKey;
  return visible[0] ? recordKey(visible[0]) : null;
}
