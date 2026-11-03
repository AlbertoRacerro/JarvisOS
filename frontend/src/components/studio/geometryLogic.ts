import type { StudioGeometryArtifact, StudioGeometryCase, StudioGeometryFacts } from "../../api/studio";

// Pure helpers of the spec 189 geometry surface; GeometryAuthoring.tsx renders them.
export const GEOMETRY_NOTICE = "Geometry runs build123d in the isolated runtime with the geometry profile: 120 s wall time, 4 GiB memory, 64 MiB per file, 128 MiB for all three files. The STEP, STL and GLB files are untrusted output; they are never executed by JarvisOS. Scientifically unqualified.";
export const blankGeometryCase = (): StudioGeometryCase => ({ inputs: {} });

export const optional = (value: string): number | null => (value.trim() === "" || !Number.isFinite(Number(value)) ? null : Number(value));
export const size = (bytes: number) => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MiB` : `${(bytes / 1024).toFixed(1)} KiB`;
const LIMIT_LABELS: Record<string, string> = { wall_seconds: "wall time (s)", cpu_seconds: "CPU time (s)", memory_bytes: "memory (bytes)", file_bytes: "per-file size (bytes)", scratch_bytes: "scratch space (bytes)", artifact_bytes: "combined artifact size (bytes)" };
/** The limit a run hit and its configured value, as recorded by the backend. */
export const limitText = (limit: string | null, value: number | null) => limit ? `Stopped by the ${LIMIT_LABELS[limit] ?? limit} limit: ${value ?? "unknown"}.` : null;

export type GeometryRunOutput = { geometry: StudioGeometryFacts; artifacts: StudioGeometryArtifact[] };
/** A succeeded geometry generation's facts and artifacts, or null for any other payload. */
export const geometryRunOutput = (payload: string | null): GeometryRunOutput | null => {
  try {
    const value = JSON.parse(payload ?? "null") as Partial<GeometryRunOutput> | null;
    return value?.geometry && Array.isArray(value.artifacts) ? value as GeometryRunOutput : null;
  } catch { return null; }
};
/** The error message of a failed run, which names any resource limit it hit. */
export const runFailure = (payload: string | null): string | null => {
  try { return (JSON.parse(payload ?? "null") as { error?: { message?: string } } | null)?.error?.message ?? null; } catch { return null; }
};
