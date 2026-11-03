export type AppearancePreference = "system" | "light" | "dark";
export type ResolvedAppearance = "light" | "dark";
// Spec 213: a curated accent set designed per appearance replaces the free custom HEX picker.
export type AccentPreset = "microalgae" | "leaf-chlorophyll" | "lagoon" | "glacier" | "iris" | "ember";

export type AccentPreference = Readonly<{
  preset: AccentPreset;
}>;

export const APPEARANCE_STORAGE_KEY = "jarvisos:appearance:v1";
export const APPEARANCE_OPTIONS: readonly AppearancePreference[] = ["system", "light", "dark"];
export const ACCENT_STORAGE_KEY = "jarvisos:accent:v1";
export const VISUAL_PREFERENCE_EVENT = "jarvisos:visual-preference-change";
export const DEFAULT_ACCENT_PRESET: AccentPreset = "microalgae";
// Lumen Dark keeps the approved chlorophyll seed; Lumen Light uses a cooler pine/fern green of the same family.
export const DEFAULT_ACCENT_HEX = "#8DB943";
export const DEFAULT_LIGHT_ACCENT_HEX = "#357D4D";
// Light seeds sit near OKLCH L 0.53 / C 0.10 (white text >= 4.5:1); dark seeds near L 0.77 / C 0.09-0.12.
export const ACCENT_PRESETS: Readonly<Record<AccentPreset, Readonly<Record<ResolvedAppearance, string>>>> = {
  microalgae: { light: DEFAULT_LIGHT_ACCENT_HEX, dark: DEFAULT_ACCENT_HEX },
  "leaf-chlorophyll": { light: "#4B763B", dark: "#8EC374" },
  lagoon: { light: "#1E7C72", dark: "#69C4B6" },
  glacier: { light: "#3770A3", dark: "#81BCE5" },
  iris: { light: "#7159A0", dark: "#BAA9E4" },
  ember: { light: "#AF5A21", dark: "#EAA76B" }
};
export const ACCENT_OPTIONS: readonly AccentPreset[] = [
  "microalgae",
  "leaf-chlorophyll",
  "lagoon",
  "glacier",
  "iris",
  "ember"
];

const ACCENT_FOREGROUND_LIGHT = "#FFFFFF";
const ACCENT_FOREGROUND_DARK = "#000000";
// Spec 198 Lumen: accent text must stay readable on the deepest light tint (panel top) and the raised dark surface.
const ACCENT_TEXT_LIGHT_SURFACE = "#D7F0DC";
const ACCENT_TEXT_DARK_SURFACE = "#142318";
const MIN_TEXT_CONTRAST = 4.5;

function isAppearancePreference(value: unknown): value is AppearancePreference {
  return value === "system" || value === "light" || value === "dark";
}

function isAccentPreset(value: unknown): value is AccentPreset {
  return ACCENT_OPTIONS.includes(value as AccentPreset);
}

export function normalizeAccentHex(value: string): string | null {
  const normalized = value.trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(normalized) ? normalized : null;
}

function srgbChannelToLinear(channel: number): number {
  const normalized = channel / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(hex: string): number {
  const normalized = normalizeAccentHex(hex) ?? DEFAULT_ACCENT_HEX;
  const red = srgbChannelToLinear(Number.parseInt(normalized.slice(1, 3), 16));
  const green = srgbChannelToLinear(Number.parseInt(normalized.slice(3, 5), 16));
  const blue = srgbChannelToLinear(Number.parseInt(normalized.slice(5, 7), 16));
  return (0.2126 * red) + (0.7152 * green) + (0.0722 * blue);
}

function contrastRatio(firstHex: string, secondHex: string): number {
  const first = relativeLuminance(firstHex);
  const second = relativeLuminance(secondHex);
  const lighter = Math.max(first, second);
  const darker = Math.min(first, second);
  return (lighter + 0.05) / (darker + 0.05);
}

function mixHex(fromHex: string, toHex: string, amount: number): string {
  const channel = (hex: string, offset: number) => Number.parseInt(hex.slice(offset, offset + 2), 16);
  const mixed = [1, 3, 5].map((offset) => Math.round(channel(fromHex, offset) + ((channel(toHex, offset) - channel(fromHex, offset)) * amount)));
  return `#${mixed.map((value) => value.toString(16).padStart(2, "0")).join("").toUpperCase()}`;
}

export function accentForegroundHex(seedHex: string): string {
  const normalized = normalizeAccentHex(seedHex) ?? DEFAULT_ACCENT_HEX;
  const lightContrast = contrastRatio(normalized, ACCENT_FOREGROUND_LIGHT);
  const darkContrast = contrastRatio(normalized, ACCENT_FOREGROUND_DARK);
  return lightContrast >= darkContrast ? ACCENT_FOREGROUND_LIGHT : ACCENT_FOREGROUND_DARK;
}

export function accentTextForegroundHex(seedHex: string, appearance: ResolvedAppearance): string {
  const normalized = normalizeAccentHex(seedHex) ?? DEFAULT_ACCENT_HEX;
  const surface = appearance === "dark" ? ACCENT_TEXT_DARK_SURFACE : ACCENT_TEXT_LIGHT_SURFACE;
  if (contrastRatio(normalized, surface) >= MIN_TEXT_CONTRAST) return normalized;
  const lightContrast = contrastRatio(surface, ACCENT_FOREGROUND_LIGHT);
  const darkContrast = contrastRatio(surface, ACCENT_FOREGROUND_DARK);
  const target = lightContrast >= darkContrast ? ACCENT_FOREGROUND_LIGHT : ACCENT_FOREGROUND_DARK;
  // Keep the accent's hue: shade the seed toward the readable extreme only as far as AA text contrast requires.
  for (let step = 1; step < 20; step += 1) {
    const shaded = mixHex(normalized, target, step / 20);
    if (contrastRatio(shaded, surface) >= MIN_TEXT_CONTRAST) return shaded;
  }
  return target;
}

function safeStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function notifyVisualPreferenceChange(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(VISUAL_PREFERENCE_EVENT));
}

function safeColorSchemeQuery(): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  try {
    return window.matchMedia("(prefers-color-scheme: dark)");
  } catch {
    return null;
  }
}

export function readAppearancePreference(): AppearancePreference {
  const storage = safeStorage();
  if (!storage) return "system";
  try {
    const value = storage.getItem(APPEARANCE_STORAGE_KEY);
    return isAppearancePreference(value) ? value : "system";
  } catch {
    return "system";
  }
}

export function writeAppearancePreference(preference: AppearancePreference): void {
  const storage = safeStorage();
  if (storage) {
    try {
      storage.setItem(APPEARANCE_STORAGE_KEY, preference);
    } catch {
      // Appearance persistence is best-effort and carries no application authority.
    }
  }
  notifyVisualPreferenceChange();
}

function srgbToOklab(hex: string): readonly [number, number, number] {
  const normalized = normalizeAccentHex(hex) ?? DEFAULT_ACCENT_HEX;
  const [red, green, blue] = [1, 3, 5].map((offset) => srgbChannelToLinear(Number.parseInt(normalized.slice(offset, offset + 2), 16)));
  const l = Math.cbrt((0.4122214708 * red) + (0.5363325363 * green) + (0.0514459929 * blue));
  const m = Math.cbrt((0.2119034982 * red) + (0.6806995451 * green) + (0.1073969566 * blue));
  const s = Math.cbrt((0.0883024619 * red) + (0.2817188376 * green) + (0.6299787005 * blue));
  return [
    (0.2104542553 * l) + (0.793617785 * m) - (0.0040720468 * s),
    (1.9779984951 * l) - (2.428592205 * m) + (0.4505937099 * s),
    (0.0259040371 * l) + (0.7827717662 * m) - (0.808675766 * s)
  ];
}

// A stored pre-213 custom HEX migrates to the perceptually nearest curated preset (OKLab, either appearance).
export function nearestAccentPreset(hex: string): AccentPreset {
  const normalized = normalizeAccentHex(hex);
  if (!normalized) return DEFAULT_ACCENT_PRESET;
  const target = srgbToOklab(normalized);
  let best: AccentPreset = DEFAULT_ACCENT_PRESET;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const preset of ACCENT_OPTIONS) {
    for (const seed of Object.values(ACCENT_PRESETS[preset])) {
      const candidate = srgbToOklab(seed);
      const distance = Math.hypot(target[0] - candidate[0], target[1] - candidate[1], target[2] - candidate[2]);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = preset;
      }
    }
  }
  return best;
}

export function safeAccentPreference(value: unknown): AccentPreference {
  if (!value || typeof value !== "object") return { preset: DEFAULT_ACCENT_PRESET };
  const { preset, customHex } = value as { preset?: unknown; customHex?: unknown };
  if (isAccentPreset(preset)) return { preset };
  if (preset === "custom" && typeof customHex === "string") return { preset: nearestAccentPreset(customHex) };
  return { preset: DEFAULT_ACCENT_PRESET };
}

export function readAccentPreference(): AccentPreference {
  const storage = safeStorage();
  if (!storage) return { preset: DEFAULT_ACCENT_PRESET };
  try {
    const raw = storage.getItem(ACCENT_STORAGE_KEY);
    return raw ? safeAccentPreference(JSON.parse(raw)) : { preset: DEFAULT_ACCENT_PRESET };
  } catch {
    return { preset: DEFAULT_ACCENT_PRESET };
  }
}

export function writeAccentPreference(preference: AccentPreference): AccentPreference {
  const safePreference = safeAccentPreference(preference);
  const storage = safeStorage();
  if (storage) {
    try {
      storage.setItem(ACCENT_STORAGE_KEY, JSON.stringify(safePreference));
    } catch {
      // Accent persistence is visual-only best effort.
    }
  }
  notifyVisualPreferenceChange();
  return safePreference;
}

export function accentHex(preference: AccentPreference, appearance: ResolvedAppearance): string {
  return ACCENT_PRESETS[safeAccentPreference(preference).preset][appearance];
}

function applyAccentSeed(root: HTMLElement, preference: AccentPreference, appearance: ResolvedAppearance): void {
  const seed = accentHex(preference, appearance);
  root.dataset.accent = preference.preset;
  root.style.setProperty("--accent-seed", seed);
  root.style.setProperty("--color-accent-on", accentForegroundHex(seed));
  root.style.setProperty("--color-accent-foreground", accentTextForegroundHex(seed, appearance));
}

export function applyAccentPreference(preference: AccentPreference): AccentPreference {
  const safePreference = safeAccentPreference(preference);
  if (typeof document === "undefined") return safePreference;
  const root = document.documentElement;
  applyAccentSeed(root, safePreference, root.dataset.theme === "dark" ? "dark" : "light");
  return safePreference;
}

export function applyStoredAccent(): AccentPreference {
  const preference = readAccentPreference();
  applyAccentPreference(preference);
  return preference;
}

export function resolveAppearance(preference: AppearancePreference): ResolvedAppearance {
  if (preference === "light" || preference === "dark") return preference;
  return safeColorSchemeQuery()?.matches ? "dark" : "light";
}

export function applyResolvedAppearance(resolved: ResolvedAppearance): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.style.colorScheme = resolved;
  // Each curated accent has its own seed per appearance.
  applyAccentSeed(root, safeAccentPreference({ preset: root.dataset.accent ?? readAccentPreference().preset }), resolved);
}

export function applyAppearancePreference(preference: AppearancePreference): ResolvedAppearance {
  const resolved = resolveAppearance(preference);
  applyResolvedAppearance(resolved);
  return resolved;
}

export function applyStoredAppearance(): AppearancePreference {
  const preference = readAppearancePreference();
  applyAppearancePreference(preference);
  return preference;
}

export function applyStoredVisualPreferences(): void {
  applyStoredAppearance();
  applyStoredAccent();
}

export function subscribeToVisualPreferenceUpdates(listener: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  window.addEventListener(VISUAL_PREFERENCE_EVENT, listener);
  return () => window.removeEventListener(VISUAL_PREFERENCE_EVENT, listener);
}

export function subscribeToSystemAppearance(
  preference: AppearancePreference,
  onChange: (resolved: ResolvedAppearance) => void
): () => void {
  if (preference !== "system") return () => undefined;
  const query = safeColorSchemeQuery();
  if (!query) return () => undefined;

  const listener = (event: MediaQueryListEvent | MediaQueryList) => {
    onChange(event.matches ? "dark" : "light");
  };

  if (typeof query.addEventListener === "function") {
    query.addEventListener("change", listener);
    return () => query.removeEventListener("change", listener);
  }

  query.addListener(listener);
  return () => query.removeListener(listener);
}
