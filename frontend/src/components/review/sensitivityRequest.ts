export type SensitivityRequest = { generation: number; key: string };
export function sensitivityIdentity(workspaceId: string, filter: string, kind: string, id: string): string {
  return JSON.stringify([workspaceId, filter, kind, id]);
}
export function acceptsSensitivityRequest(current: SensitivityRequest | null, request: SensitivityRequest): boolean {
  return current !== null && current.generation === request.generation && current.key === request.key;
}
