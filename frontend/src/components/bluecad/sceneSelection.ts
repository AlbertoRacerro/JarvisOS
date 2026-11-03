import { resolveScenePart, type ResolvedScenePart } from "./sceneBinding";

const MANIFEST_ROLE = "attempt.manifest_artifact_id";
const SHA256 = /^[0-9a-f]{64}$/;

export type SceneArtifactRef = Readonly<{
  id: string;
  roles: readonly string[];
  sha256: string;
}>;

export type SceneSelectionPreconditions = Readonly<{
  workspaceId: string;
  candidateId: string;
  artifactId: string;
  viewerSessionId: string;
  meshKey: string;
  semanticKey: string;
}>;

export type SceneSelectionResolution =
  | Readonly<{ state: "resolved"; part: ResolvedScenePart }>
  | Readonly<{ state: "unresolved" }>
  | Readonly<{ state: "ambiguous" }>;

/**
 * Candidate aggregate manifests are explicit attempt-owned artifacts. Their array
 * position, creation order and display filename are not authority.
 */
export function candidateManifestArtifactIds(artifacts: readonly SceneArtifactRef[]): string[] {
  return [...new Set(
    artifacts
      .filter((artifact) => artifact.roles.includes(MANIFEST_ROLE))
      .map((artifact) => artifact.id)
  )].sort();
}

/**
 * Resolve the current GLB artifact by the candidate-owned artifact id, never by
 * filename, array position or any other presentation detail.
 */
export function currentGlbArtifact(
  artifacts: readonly SceneArtifactRef[],
  artifactId: string | null
): SceneArtifactRef | null {
  if (!artifactId) return null;
  const matches = artifacts.filter((artifact) => artifact.id === artifactId && SHA256.test(artifact.sha256));
  return matches.length === 1 ? matches[0] : null;
}

/**
 * Resolve one current viewer semantic hit against all candidate-owned accessible
 * manifests. Exactly one valid current binding is required. No renderer-order,
 * name, material, colour or bounds fallback is permitted.
 */
export function resolveCandidateSceneHit(
  manifestValues: readonly unknown[],
  semanticKey: string | null,
  currentGlbSha256: string
): SceneSelectionResolution {
  const matches = manifestValues.flatMap((manifest) => {
    const resolved = resolveScenePart(manifest, semanticKey, currentGlbSha256);
    return resolved ? [resolved] : [];
  });
  if (matches.length === 0) return { state: "unresolved" };
  if (matches.length !== 1) return { state: "ambiguous" };
  return { state: "resolved", part: matches[0] };
}

/**
 * Load only manifest artifacts identified by their explicit candidate role. A
 * failed/missing artifact read is fail-closed: the hit stays inspectable but is
 * not promoted to semantic engineering identity.
 */
export async function resolveCandidateSceneHitFromArtifacts(
  artifacts: readonly SceneArtifactRef[],
  semanticKey: string | null,
  currentGlbSha256: string,
  loadManifest: (artifactId: string) => Promise<unknown>
): Promise<SceneSelectionResolution> {
  const manifestIds = candidateManifestArtifactIds(artifacts);
  if (!semanticKey || manifestIds.length === 0 || !SHA256.test(currentGlbSha256)) {
    return { state: "unresolved" };
  }
  try {
    const manifests = await Promise.all(manifestIds.map((artifactId) => loadManifest(artifactId)));
    return resolveCandidateSceneHit(manifests, semanticKey, currentGlbSha256);
  } catch {
    return { state: "unresolved" };
  }
}

/**
 * Async manifest resolution may publish only while every scene precondition still
 * names the same workspace/candidate/artifact/viewer/hit.
 */
export function acceptsSceneSelectionResolution(
  current: SceneSelectionPreconditions | null,
  captured: SceneSelectionPreconditions
): boolean {
  return current !== null
    && current.workspaceId === captured.workspaceId
    && current.candidateId === captured.candidateId
    && current.artifactId === captured.artifactId
    && current.viewerSessionId === captured.viewerSessionId
    && current.meshKey === captured.meshKey
    && current.semanticKey === captured.semanticKey;
}
