export type RecordResource =
  | "workspace"
  | "model-spec"
  | "assumption"
  | "parameter"
  | "simulation-run"
  | "decision"
  | "bluecad-candidate";

export type RecordRef = Readonly<{
  resource: RecordResource;
  workspaceId: string;
  recordId: string;
}>;

export type StageSelection =
  | Readonly<{ kind: "record"; ref: RecordRef }>
  | Readonly<{
      kind: "geometry-hit";
      viewerSessionId: string;
      ephemeralObjectId: string;
      point?: readonly [number, number, number];
    }>
  | Readonly<{
      kind: "bluecad-binding-status";
      state: "resolving" | "unresolved" | "ambiguous";
      workspaceId: string;
      candidateId: string;
      artifactId: string;
      viewerSessionId: string;
      ephemeralObjectId: string;
      meshKey: string;
      semanticKey: string;
    }>
  | Readonly<{
      kind: "bluecad-part";
      workspaceId: string;
      candidateId: string;
      artifactId: string;
      viewerSessionId: string;
      ephemeralObjectId: string;
      meshKey: string;
      semanticKey: string;
      partId: string;
      partKind?: string | null;
    }>;