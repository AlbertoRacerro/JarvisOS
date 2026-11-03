const SCENE_BINDING_VERSION = "bluecad_scene_binding_v0_1";
const SCENE_BINDING_ARTIFACT = "model.glb";
const SEMANTIC_KEY = /^bluecad-part-sha256-[0-9a-f]{64}$/;
const SHA256 = /^[0-9a-f]{64}$/;

export type ResolvedScenePart = Readonly<{
  semanticKey: string;
  partId: string;
  partKind: string | null;
}>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function artifactDigest(manifest: Record<string, unknown>): string | null {
  const artifacts = manifest.artifacts;
  if (!isRecord(artifacts)) return null;
  const glb = artifacts[SCENE_BINDING_ARTIFACT];
  if (!isRecord(glb) || typeof glb.sha256 !== "string" || !SHA256.test(glb.sha256)) return null;
  return glb.sha256;
}

function validBindingObjects(
  objects: unknown,
  parts: Record<string, unknown>
): objects is Record<string, { part_id: string }> {
  if (!isRecord(objects) || Object.keys(objects).length === 0) return false;
  if (Object.keys(objects).length !== Object.keys(parts).length) return false;
  const seenPartIds = new Set<string>();
  for (const [key, value] of Object.entries(objects)) {
    if (!SEMANTIC_KEY.test(key) || !isRecord(value) || Object.keys(value).length !== 1) return false;
    if (typeof value.part_id !== "string" || !isRecord(parts[value.part_id])) return false;
    if (seenPartIds.has(value.part_id)) return false;
    seenPartIds.add(value.part_id);
  }
  return seenPartIds.size === Object.keys(parts).length;
}

/**
 * Resolve one exporter-owned GLTF semantic key against the exact current manifest.
 *
 * This is deliberately fail-closed. Renderer order, display names, materials,
 * bounds and other inspection facts are never accepted as engineering identity.
 */
export function resolveScenePart(
  manifestValue: unknown,
  semanticKey: string | null,
  currentGlbSha256: string
): ResolvedScenePart | null {
  if (!semanticKey || !SEMANTIC_KEY.test(semanticKey) || !SHA256.test(currentGlbSha256)) return null;
  if (!isRecord(manifestValue)) return null;

  const binding = manifestValue.scene_binding;
  const parts = manifestValue.parts;
  const manifestSpecId = manifestValue.spec_id;
  if (!isRecord(binding) || !isRecord(parts)) return null;
  if (typeof manifestSpecId !== "string" || !manifestSpecId) return null;
  if (
    binding.version !== SCENE_BINDING_VERSION ||
    binding.artifact !== SCENE_BINDING_ARTIFACT ||
    binding.spec_id !== manifestSpecId
  ) return null;
  if (artifactDigest(manifestValue) !== currentGlbSha256) return null;

  const objects = binding.objects;
  if (!validBindingObjects(objects, parts)) return null;
  const target = objects[semanticKey];
  if (!target) return null;

  const part = parts[target.part_id];
  if (!isRecord(part)) return null;
  const partKind = typeof part.kind === "string" ? part.kind : null;
  return { semanticKey, partId: target.part_id, partKind };
}
