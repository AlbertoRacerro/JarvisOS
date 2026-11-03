// Spec 213: magnitude-aware, range-respecting numeric stepping shared by every stepped numeric field.
// From 1 upward the step is 1 (2, 3, 4…); below 1 it is a tenth (0.9…0.1), then a hundredth (0.09…), and so on.
// Stepping toward zero shrinks the step, so a positive value never crosses zero by stepping; declared bounds
// (inclusive or exclusive minimum, maximum, whole numbers) are always respected.

export type NumericBounds = Readonly<{
  min?: number;
  max?: number;
  /** The minimum itself is not allowed (e.g. a positive-only physical quantity: min 0, exclusive). */
  minExclusive?: boolean;
  integer?: boolean;
}>;

const SMALLEST_MAGNITUDE = 1e-6;

function decimalsOf(step: number): number {
  return Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
}

function stepFor(magnitude: number, awayFromZero: boolean): number {
  if (magnitude === 0) return 1;
  if (awayFromZero) return magnitude >= 1 ? 1 : 10 ** Math.floor(Math.log10(magnitude) + 1e-9);
  return magnitude > 1 ? 1 : 10 ** (Math.ceil(Math.log10(magnitude) - 1e-9) - 1);
}

export function parseNumeric(text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

function allowed(value: number, bounds: NumericBounds): boolean {
  if (bounds.min !== undefined && (bounds.minExclusive ? value <= bounds.min : value < bounds.min)) return false;
  if (bounds.max !== undefined && value > bounds.max) return false;
  return true;
}

/** Next value for one step up (+1) or down (-1), or null when a step would leave the valid range. */
export function stepNumeric(text: string, direction: 1 | -1, bounds: NumericBounds = {}): number | null {
  const parsed = parseNumeric(text);
  // An empty field steps from zero, or from the minimum when zero is out of range.
  const value = parsed ?? (allowed(0, bounds) ? 0 : bounds.min ?? 0);
  if (parsed === null && !allowed(value, bounds)) return bounds.minExclusive ? stepNumeric(String(value), 1, bounds) : value;
  const magnitude = Math.abs(value);
  const sign = value === 0 ? direction : Math.sign(value);
  const awayFromZero = value === 0 || sign === direction;
  let step = stepFor(magnitude, awayFromZero);
  if (bounds.integer) step = Math.max(1, Math.round(step));
  const units = magnitude / step;
  let nextMagnitude = awayFromZero
    ? (Math.floor(units + 1e-9) + 1) * step
    : (Math.ceil(units - 1e-9) - 1) * step;
  if (!awayFromZero && nextMagnitude < SMALLEST_MAGNITUDE) nextMagnitude = 0;
  const next = Number((sign * nextMagnitude).toFixed(decimalsOf(step)));
  if (allowed(next, bounds)) return next;
  if (parsed === null) return value;
  // Clamp onto an inclusive bound when the step overshoots it; otherwise stay put.
  if (direction > 0 && bounds.max !== undefined && value < bounds.max) return bounds.max;
  if (direction < 0 && bounds.min !== undefined && !bounds.minExclusive && value > bounds.min) return bounds.min;
  return null;
}

/** Formats a stepped number without floating-point noise. */
export function formatStepped(value: number): string {
  return String(Number(value.toPrecision(12)));
}

/** Numeric owners store a number, or nothing while the text is empty or not yet a number. */
export function numericOrUndefined(text: string): number | undefined {
  return parseNumeric(text) ?? undefined;
}
