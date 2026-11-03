// Spec 216: pure model of the CFD axial-section field slice (no React, no DOM). Never invents data.
export type FieldName = "p" | "Umag";
export type FieldSlice = {
  time: string; association: "point" | "cell"; points: number[][]; polygons: number[][]; cellCount: number;
  normal: number[]; p: { values: number[]; min: number; max: number; units: string };
  U: { values: number[][]; min: number; max: number; units: string };
};
export type ParsedField =
  | { kind: "available"; slice: FieldSlice }
  | { kind: "unavailable"; reason: string }
  | { kind: "too_large"; reason: string; time?: string }
  | { kind: "invalid"; error: string };

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const vec3 = (v: unknown): v is number[] => Array.isArray(v) && v.length === 3 && v.every(finite);
const invalid = (error: string): ParsedField => ({ kind: "invalid", error: `Malformed field payload: ${error}` });

/** Validate a server payload; unavailable/too_large pass their reason through verbatim. */
export function parseFieldPayload(raw: unknown): ParsedField {
  if (!isObj(raw)) return invalid("not an object");
  if (raw.status === "unavailable") return typeof raw.reason === "string" && raw.reason ? { kind: "unavailable", reason: raw.reason } : invalid("unavailable without a reason");
  if (raw.status === "too_large") {
    return typeof raw.reason === "string" && raw.reason
      ? { kind: "too_large", reason: raw.reason, ...(typeof raw.time === "string" ? { time: raw.time } : {}) } : invalid("too_large without a reason");
  }
  if (raw.status !== "available") return invalid("unknown status");
  const association = raw.association;
  if (association !== "point" && association !== "cell") return invalid("association must be point or cell");
  if (typeof raw.time !== "string") return invalid("time missing");
  const points = raw.points, polygons = raw.polygons;
  if (!Array.isArray(points) || !points.every(vec3)) return invalid("points must be [x, y, z] triples of finite numbers");
  if (!Array.isArray(polygons) || !polygons.length) return invalid("polygons missing");
  for (const polygon of polygons) {
    if (!Array.isArray(polygon) || polygon.length < 3 || !polygon.every((i) => Number.isInteger(i) && i >= 0 && i < points.length)) {
      return invalid("polygon index out of range or fewer than 3 vertices");
    }
  }
  const expected = association === "point" ? points.length : polygons.length;
  const fields = raw.fields;
  if (!isObj(fields) || !isObj(fields.p) || !isObj(fields.U)) return invalid("fields p and U missing");
  const p = fields.p, U = fields.U;
  if (!Array.isArray(p.values) || !p.values.every(finite)) return invalid("p values must be finite numbers");
  if (p.values.length !== expected) return invalid(`p has ${p.values.length} values, expected ${expected} (${association})`);
  if (!Array.isArray(U.values) || !U.values.every(vec3)) return invalid("U values must be finite triples");
  if (U.values.length !== expected) return invalid(`U has ${U.values.length} values, expected ${expected} (${association})`);
  if (!finite(p.min) || !finite(p.max) || !finite(U.magnitude_min) || !finite(U.magnitude_max)) return invalid("field ranges must be finite");
  if (p.min > p.max || U.magnitude_min > U.magnitude_max) return invalid("field range min exceeds max");
  const plane = isObj(raw.plane) ? raw.plane : {};
  const normal = vec3(plane.normal) ? plane.normal : [0, 0, 1];
  if (normal.filter((c) => c !== 0).length !== 1) return invalid("plane normal must be axis aligned");
  return { kind: "available", slice: {
    time: raw.time, association, points: points as number[][], polygons: polygons as number[][], cellCount: polygons.length, normal,
    p: { values: p.values as number[], min: p.min, max: p.max, units: String(p.units ?? "") },
    U: { values: U.values as number[][], min: U.magnitude_min, max: U.magnitude_max, units: String(U.units ?? "") } } };
}

/** Per-sample scalar (point or cell) of the chosen field; |U| is computed from the vector. */
export function sampleValues(slice: FieldSlice, field: FieldName): number[] {
  return field === "p" ? slice.p.values : slice.U.values.map((v) => Math.hypot(v[0]!, v[1]!, v[2]!));
}
export const fieldRange = (slice: FieldSlice, field: FieldName) => field === "p" ? { min: slice.p.min, max: slice.p.max } : { min: slice.U.min, max: slice.U.max };
export const fieldUnits = (slice: FieldSlice, field: FieldName) => field === "p" ? slice.p.units : slice.U.units;

/** One scalar per polygon: mean of vertex values for point data, the cell value for cell data. */
export function polygonValues(slice: FieldSlice, field: FieldName): number[] {
  const values = sampleValues(slice, field);
  if (slice.association === "cell") return values.slice();
  return slice.polygons.map((polygon) => polygon.reduce((sum, i) => sum + values[i]!, 0) / polygon.length);
}

/** Axis the projection drops: the declared normal's, unless the points are not flat along it but are flat along another
 * axis (spec 229: the 216 C1 runs declare normal z while their points lie on y = 0). The points decide; no value changes. */
export function droppedAxis(slice: Pick<FieldSlice, "normal" | "points">): number {
  const declared = slice.normal.findIndex((c) => c !== 0);
  const spans = [0, 1, 2].map((axis) => {
    let min = Infinity, max = -Infinity;
    for (const point of slice.points) { min = Math.min(min, point[axis]!); max = Math.max(max, point[axis]!); }
    return slice.points.length ? max - min : 0;
  });
  const flat = (axis: number) => spans[axis]! <= 1e-9 * Math.max(...spans, 1e-300);
  if (flat(declared)) return declared;
  const other = [0, 1, 2].find((axis) => axis !== declared && flat(axis));
  return other ?? declared;
}

/** In-plane 2D coordinates: the axis the section is flat along is dropped (z for a z = 0 section -> x, y). */
export function projectPoints(slice: FieldSlice): [number, number][] {
  const drop = droppedAxis(slice);
  const [a, b] = [0, 1, 2].filter((axis) => axis !== drop) as [number, number];
  return slice.points.map((pt) => [pt[a]!, pt[b]!]);
}

export type Fit = {
  scale: number; scaleX: number; scaleY: number; offsetX: number; offsetY: number; width: number; height: number;
  /** Display-only stretch of one in-plane axis (1 = none); spec 229 requires it to be declared on screen. */
  stretch: number; stretchAxis: "x" | "y" | null;
};
/** Aspect-preserving fit into width x height with a margin; y is flipped for screen space. Degenerate extent never yields NaN.
 * With minFill > 0 (spec 229), an axis drawn thinner than that fraction of the viewport is stretched up to it, so a long
 * thin pipe section stays legible; the factor is returned so the caller can declare it. Data values are never changed. */
export function fitTransform(points2d: [number, number][], width: number, height: number, margin = 8, minFill = 0): Fit {
  if (!points2d.length) return { scale: 1, scaleX: 1, scaleY: 1, offsetX: 0, offsetY: 0, width, height, stretch: 1, stretchAxis: null };
  // A loop, not Math.min(...xs): the slice may hold 80k points, past some engines' argument limits.
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const [x, y] of points2d) { minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); }
  const spanX = maxX - minX, spanY = maxY - minY;
  const availX = Math.max(width - 2 * margin, 1), availY = Math.max(height - 2 * margin, 1);
  const candidates = [spanX > 0 ? availX / spanX : Infinity, spanY > 0 ? availY / spanY : Infinity];
  const best = Math.min(...candidates);
  const scale = Number.isFinite(best) ? best : 1;
  let scaleX = scale, scaleY = scale, stretchAxis: Fit["stretchAxis"] = null;
  if (minFill > 0 && spanX > 0 && spanY > 0) {
    if (spanY * scale < minFill * availY) { scaleY = minFill * availY / spanY; stretchAxis = "y"; }
    else if (spanX * scale < minFill * availX) { scaleX = minFill * availX / spanX; stretchAxis = "x"; }
  }
  return { scale, scaleX, scaleY, stretch: Math.max(scaleX, scaleY) / scale, stretchAxis,
    offsetX: (width - spanX * scaleX) / 2 - minX * scaleX, offsetY: (height + spanY * scaleY) / 2 + minY * scaleY, width, height };
}
export const toScreen = (fit: Fit, p: [number, number]): [number, number] => [p[0] * fit.scaleX + fit.offsetX, fit.offsetY - p[1] * fit.scaleY];

/** Viridis-like sequential stops (t = 0 .. 1). */
export const COLOR_STOPS: [number, number, number][] = [[68, 1, 84], [59, 82, 139], [33, 144, 141], [93, 201, 99], [253, 231, 37]];
/** Colour for value within [min, max]; min == max maps to the mid colour, values clamp, non-finite -> neutral grey. */
export function colorFor(value: number, min: number, max: number): string {
  if (!Number.isFinite(value)) return "rgb(128,128,128)";
  const span = max - min;
  const t = span > 0 ? Math.min(1, Math.max(0, (value - min) / span)) : 0.5;
  const x = t * (COLOR_STOPS.length - 1);
  const i = Math.min(Math.floor(x), COLOR_STOPS.length - 2), f = x - i;
  const [c0, c1] = [COLOR_STOPS[i]!, COLOR_STOPS[i + 1]!];
  return `rgb(${[0, 1, 2].map((k) => Math.round(c0[k]! + (c1[k]! - c0[k]!) * f)).join(",")})`;
}
export const legendGradient = () => `linear-gradient(to right, ${COLOR_STOPS.map((c, i) => `rgb(${c.join(",")}) ${(i / (COLOR_STOPS.length - 1)) * 100}%`).join(", ")})`;

export type DrawnPolygon = { points: string; fill: string };
export function buildPolygons(slice: FieldSlice, field: FieldName, width: number, height: number, minFill = 0): { polygons: DrawnPolygon[]; fit: Fit } {
  const p2 = projectPoints(slice);
  const fit = fitTransform(p2, width, height, 8, minFill);
  const screen = p2.map((p) => toScreen(fit, p));
  const { min, max } = fieldRange(slice, field);
  const values = polygonValues(slice, field);
  return { fit, polygons: slice.polygons.map((polygon, k) => ({
    points: polygon.map((i) => `${screen[i]![0].toFixed(2)},${screen[i]![1].toFixed(2)}`).join(" "), fill: colorFor(values[k]!, min, max) })) };
}
