// Spec 213: user-resizable shell panels. Widths are visual-only browser preferences (no API or persistence change).
export type ShellPanel = "navigator" | "sidecar";

export const PANEL_WIDTH_STORAGE_KEY = "jarvisos:panel-widths:v1";
export const PANEL_WIDTH_BOUNDS: Readonly<Record<ShellPanel, Readonly<{ min: number; max: number }>>> = {
  navigator: { min: 200, max: 480 },
  sidecar: { min: 280, max: 640 }
};
export const PANEL_WIDTH_KEYBOARD_STEP = 16;
export const PANEL_WIDTH_KEYBOARD_LARGE_STEP = 64;

export type PanelWidths = Partial<Record<ShellPanel, number>>;

export function clampPanelWidth(panel: ShellPanel, width: number): number {
  const { min, max } = PANEL_WIDTH_BOUNDS[panel];
  if (!Number.isFinite(width)) return min;
  return Math.round(Math.min(max, Math.max(min, width)));
}

export function parsePanelWidths(raw: string | null): PanelWidths {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const widths: PanelWidths = {};
    for (const panel of Object.keys(PANEL_WIDTH_BOUNDS) as ShellPanel[]) {
      const value = parsed?.[panel];
      if (typeof value === "number" && Number.isFinite(value)) widths[panel] = clampPanelWidth(panel, value);
    }
    return widths;
  } catch {
    return {};
  }
}

export function readPanelWidths(): PanelWidths {
  try {
    return parsePanelWidths(window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY));
  } catch {
    return {};
  }
}

export function writePanelWidths(widths: PanelWidths): void {
  try {
    window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, JSON.stringify(widths));
  } catch {
    // Panel widths are a best-effort visual preference.
  }
}

/** Width after a keyboard command on a splitter, or null when the key is not a splitter command. */
export function keyboardPanelWidth(panel: ShellPanel, current: number, key: string, shiftKey: boolean): number | null {
  const step = shiftKey ? PANEL_WIDTH_KEYBOARD_LARGE_STEP : PANEL_WIDTH_KEYBOARD_STEP;
  // The navigator grows to the right; the sidecar grows to the left.
  const grow = panel === "navigator" ? "ArrowRight" : "ArrowLeft";
  const shrink = panel === "navigator" ? "ArrowLeft" : "ArrowRight";
  if (key === grow) return clampPanelWidth(panel, current + step);
  if (key === shrink) return clampPanelWidth(panel, current - step);
  if (key === "Home") return PANEL_WIDTH_BOUNDS[panel].min;
  if (key === "End") return PANEL_WIDTH_BOUNDS[panel].max;
  return null;
}
