import type { CfdField3d, CfdField3dOperator, CfdField3dSurface } from "../api/designProject";

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const vec3 = (value: unknown): value is [number, number, number] => Array.isArray(value) && value.length === 3 && value.every(finite);
const sign3 = (value: unknown): value is [number, number, number] => vec3(value) && value.every((part) => part === 1 || part === -1);
const obj = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

/** The API type is compile-time only; reject malformed exports before allocating WebGL geometry. */
export function validField3d(raw: unknown): raw is Extract<CfdField3d, { status: "available" }> {
  if (!obj(raw) || raw.status !== "available" || !Array.isArray(raw.surfaces) || !raw.surfaces.length || !obj(raw.symmetry) || !obj(raw.reflection_operators)) return false;
  if (raw.symmetry.domain !== "quarter" || raw.symmetry.axis !== "x" || !Array.isArray(raw.symmetry.mirror_planes) || !raw.symmetry.mirror_planes.every((plane) => typeof plane === "string")) return false;
  for (const surface of raw.surfaces) {
    if (!obj(surface) || surface.provenance !== "computed" || !["wall", "symmetry_plane", "section"].includes(String(surface.kind)) || typeof surface.name !== "string") return false;
    if (!Array.isArray(surface.points) || !surface.points.length || !surface.points.every(vec3) || !Array.isArray(surface.polygons) || !obj(surface.fields) || !obj(surface.fields.p) || !obj(surface.fields.U)) return false;
    if (!Array.isArray(surface.fields.p.values) || surface.fields.p.values.length !== surface.points.length || !surface.fields.p.values.every(finite)) return false;
    if (!Array.isArray(surface.fields.U.values) || surface.fields.U.values.length !== surface.points.length || !surface.fields.U.values.every(vec3)) return false;
    if (!finite(surface.fields.p.min) || !finite(surface.fields.p.max) || !finite(surface.fields.U.magnitude_min) || !finite(surface.fields.U.magnitude_max) || surface.fields.p.min > surface.fields.p.max || surface.fields.U.magnitude_min > surface.fields.U.magnitude_max) return false;
    const pointCount = surface.points.length;
    if (!surface.polygons.every((polygon: unknown) => Array.isArray(polygon) && polygon.length >= 3 && polygon.every((index) => Number.isInteger(index) && index >= 0 && index < pointCount))) return false;
  }
  for (const plane of raw.symmetry.mirror_planes) {
    const operator = raw.reflection_operators[plane];
    if (!obj(operator) || !sign3(operator.position) || !sign3(operator.vector_field) || operator.scalar !== "unchanged") return false;
  }
  return true;
}

export function transforms(payload: Extract<CfdField3d, { status: "available" }>, full: boolean) {
  const identity: { planes: string[]; position: [number, number, number]; U: [number, number, number] } = { planes: [], position: [1, 1, 1], U: [1, 1, 1] };
  if (!full) return [identity];
  // Compose only operators explicitly supplied for the run. Never infer a missing plane.
  return payload.symmetry.mirror_planes.reduce<typeof identity[]>((copies, plane) => {
    const op: CfdField3dOperator = payload.reflection_operators[plane]!;
    return [...copies, ...copies.map((copy) => ({ planes: [...copy.planes, plane],
      position: copy.position.map((sign, axis) => sign * op.position[axis]!) as [number, number, number],
      U: copy.U.map((sign, axis) => sign * op.vector_field[axis]!) as [number, number, number] }))];
  }, [identity]);
}

export function reflectedSample(surface: CfdField3dSurface, index: number, transform: ReturnType<typeof transforms>[number]) {
  const point = surface.points[index]!;
  const U = surface.fields.U.values[index]!;
  return { position: point.map((n, axis) => n * transform.position[axis]!) as [number, number, number],
    U: U.map((n, axis) => n * transform.U[axis]!) as [number, number, number],
    source: point, p: surface.fields.p.values[index]!, planes: transform.planes };
}
