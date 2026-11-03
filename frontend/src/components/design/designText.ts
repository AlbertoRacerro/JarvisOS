// Readable names for internal keys shown in Design panels (no raw snake_case on screen).
import type { CfdRun } from "../../api/designProject";

/** "tube_inner_diameter" -> "Tube inner diameter". */
export function readableKey(key: string): string {
  const text = key.replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Server prose may name keys ("tube_inner_diameter and tube_length"); show them as words. */
export const readableProse = (text: string): string =>
  text.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, (word) => word.replace(/_/g, " "));

export const regimeLabel = (regime: string): string =>
  regime === "turbulent_rans" ? "turbulent" : regime === "laminar" ? "laminar" : readableKey(regime).toLowerCase();

/** "3 Oct, 14:05" for a stored timestamp; the raw text when it is not a date. */
export function shortWhen(when: string): string {
  const date = new Date(when);
  return Number.isNaN(date.getTime()) ? when : date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** A CFD run as people read it: regime, Reynolds number and when; the run id stays in the tooltip. */
export function runLabel(run: CfdRun): string {
  const regime = regimeLabel(run.regime);
  const re = run.measurement?.status === "measured" && typeof run.measurement.reynolds === "number" ? ` · Re ${Math.round(run.measurement.reynolds)}` : "";
  return `${regime.charAt(0).toUpperCase()}${regime.slice(1)}${re} · ${shortWhen(run.finished_at ?? run.queued_at)}`;
}
