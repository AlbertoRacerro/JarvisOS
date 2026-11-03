// Spec 158: orthogonal stream routing. Pure geometry (no imports) so node tests can execute it.
// A route runs from a start anchor to an end anchor through operator waypoints using
// horizontal and vertical segments only. Waypoints are layout-only draft state.

export type Point = { x: number; y: number };
export type Side = "left" | "right" | "top" | "bottom";
/** A route end: the port (or stream marker edge) and the side the wire leaves it towards. */
export type Anchor = { at: Point; side: Side };

export const STUB = 14;
export const BEND = 24;
export const MAX_WAYPOINTS = 12;

const DIR: Record<Side, Point> = { left: { x: -1, y: 0 }, right: { x: 1, y: 0 }, top: { x: 0, y: -1 }, bottom: { x: 0, y: 1 } };
const horizontal = (side: Side) => side === "left" || side === "right";
const same = (a: Point, b: Point) => a.x === b.x && a.y === b.y;
const stub = (anchor: Anchor): Point => ({ x: anchor.at.x + DIR[anchor.side].x * STUB, y: anchor.at.y + DIR[anchor.side].y * STUB });

function dedupe(points: Point[]): Point[] {
  return points.filter((point, index) => index === 0 || !same(point, points[index - 1]));
}

/** Interior default path between the two stubs when the operator set no waypoints. */
function defaultVia(a: Point, sideA: Side, b: Point, sideB: Side, clearance: number): Point[] {
  if (horizontal(sideA) && horizontal(sideB)) {
    const forward = sideA === "right" ? b.x >= a.x : b.x <= a.x;
    if (forward && sideA !== sideB) {
      const mx = Math.round((a.x + b.x) / 2);
      return [{ x: mx, y: a.y }, { x: mx, y: b.y }];
    }
    // Backward (recycle) or same-facing ports: leave, loop below both ends, come back.
    const loopY = Math.max(a.y, b.y) + clearance;
    return [{ x: a.x, y: loopY }, { x: b.x, y: loopY }];
  }
  if (!horizontal(sideA) && !horizontal(sideB)) {
    const my = Math.round((a.y + b.y) / 2);
    return [{ x: a.x, y: my }, { x: b.x, y: my }];
  }
  return horizontal(sideA) ? [{ x: b.x, y: a.y }] : [{ x: a.x, y: b.y }];
}

/**
 * Full orthogonal polyline [start, startStub, ...interior, endStub, end].
 * Consecutive anchors that are not aligned get one elbow, turning perpendicular to the
 * segment that arrived, so every segment is axis-parallel.
 */
export function routeStream(start: Anchor, end: Anchor, waypoints: Point[] = [], clearance = 40): Point[] {
  const a = stub(start);
  const b = stub(end);
  const via = waypoints.length ? waypoints : defaultVia(a, start.side, b, end.side, clearance);
  const anchors = [a, ...via, b];
  const out: Point[] = [start.at, a];
  let arrivedHorizontal = horizontal(start.side);
  for (let index = 1; index < anchors.length; index += 1) {
    const from = anchors[index - 1];
    const to = anchors[index];
    if (from.x !== to.x && from.y !== to.y) {
      const elbow = arrivedHorizontal ? { x: from.x, y: to.y } : { x: to.x, y: from.y };
      out.push(elbow);
      arrivedHorizontal = !arrivedHorizontal;
    } else if (!same(from, to)) {
      arrivedHorizontal = from.y === to.y;
    }
    out.push(to);
  }
  out.push(end.at);
  return dedupe(out);
}

export const isOrthogonal = (points: Point[]) =>
  points.every((point, index) => index === 0 || point.x === points[index - 1].x || point.y === points[index - 1].y);

export const pathData = (points: Point[]) => points.map((point, index) => `${index ? "L" : "M"}${point.x},${point.y}`).join(" ");

/** Point halfway along the polyline, used to place a connected stream's marker. */
export function midpoint(points: Point[]): Point {
  const lengths = points.slice(1).map((point, index) => Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y));
  let rest = lengths.reduce((sum, value) => sum + value, 0) / 2;
  for (let index = 0; index < lengths.length; index += 1) {
    if (rest <= lengths[index] && lengths[index] > 0) {
      const t = rest / lengths[index];
      return { x: Math.round(points[index].x + (points[index + 1].x - points[index].x) * t), y: Math.round(points[index].y + (points[index + 1].y - points[index].y) * t) };
    }
    rest -= lengths[index];
  }
  return points[0] ?? { x: 0, y: 0 };
}

/** Segments the operator may drag: everything except the fixed port stubs. */
export const editableSegment = (points: Point[], index: number) => index >= 1 && index <= points.length - 3;

/** Waypoints that reproduce an edited polyline: interior vertices, collinear ones dropped. */
function toWaypoints(full: Point[]): Point[] {
  const clean = dedupe(full);
  const kept: Point[] = [];
  for (let index = 2; index < clean.length - 2; index += 1) {
    const prev = kept[kept.length - 1] ?? clean[1];
    const point = clean[index];
    const next = clean[index + 1];
    const collinear = (prev.x === point.x && point.x === next.x) || (prev.y === point.y && point.y === next.y);
    if (!collinear) kept.push(point);
  }
  return kept.map((point) => ({ x: Math.round(point.x), y: Math.round(point.y) }));
}

/** Drag segment `index` perpendicular to itself by `offset`; returns the new waypoints. */
export function offsetSegment(points: Point[], index: number, offset: number): Point[] | null {
  if (!editableSegment(points, index)) return null;
  const a = points[index];
  const b = points[index + 1];
  const flat = (at: number) => points[at].y === points[at + 1].y;
  const shift = (point: Point): Point => (flat(index) ? { x: point.x, y: point.y + offset } : { x: point.x + offset, y: point.y });
  // Stub ends are fixed to their ports, and a vertex shared with a parallel neighbour
  // must stay: in both cases the moved segment is joined by a new perpendicular jog.
  const keepA = index === 1 || flat(index - 1) === flat(index);
  const keepB = index + 1 === points.length - 2 || flat(index + 1) === flat(index);
  const next = points.slice();
  if (keepB) next.splice(index + 1, 0, shift(b));
  else next[index + 1] = shift(b);
  if (keepA) next.splice(index + 1, 0, shift(a));
  else next[index] = shift(a);
  const waypoints = toWaypoints(next);
  return waypoints.length <= MAX_WAYPOINTS ? waypoints : null;
}

/** Double-click on a segment: the part after `at` steps aside by BEND, adding a bend. */
export function addBend(points: Point[], index: number, at: Point): Point[] | null {
  if (!editableSegment(points, index)) return null;
  const a = points[index];
  const b = points[index + 1];
  const split = a.y === b.y ? { x: Math.round(at.x), y: a.y } : { x: a.x, y: Math.round(at.y) };
  const withSplit = [...points.slice(0, index + 1), split, ...points.slice(index + 1)];
  return offsetSegment(withSplit, index + 1, BEND);
}

/** Remove one interior vertex (double-click a bend handle); neighbours re-join with an elbow. */
export function removeVertex(points: Point[], index: number): Point[] | null {
  if (index < 2 || index > points.length - 3) return null;
  return toWaypoints([...points.slice(0, index), ...points.slice(index + 1)]);
}

/** Index of the polyline segment nearest to `at` (within `tolerance`), or -1. */
export function hitSegment(points: Point[], at: Point, tolerance = 6): number {
  let best = -1;
  let bestDistance = tolerance;
  for (let index = 0; index < points.length - 1; index += 1) {
    const a = points[index];
    const b = points[index + 1];
    const distance =
      a.y === b.y
        ? at.x >= Math.min(a.x, b.x) - tolerance && at.x <= Math.max(a.x, b.x) + tolerance ? Math.abs(at.y - a.y) : Infinity
        : at.y >= Math.min(a.y, b.y) - tolerance && at.y <= Math.max(a.y, b.y) + tolerance ? Math.abs(at.x - a.x) : Infinity;
    if (distance <= bestDistance) {
      best = index;
      bestDistance = distance;
    }
  }
  return best;
}

/** The draft ops a route edit produces: exactly one layout-only set_route, never a process op. */
export const routeEditOps = (stream: string, points: Point[]) => [{ op: "set_route" as const, stream, points: points.map(({ x, y }) => ({ x, y })) }];
