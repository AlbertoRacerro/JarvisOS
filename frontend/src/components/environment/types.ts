export type Site = {
  name: string;
  latitude: number;
  longitude: number;
  elevation_m: number;
  timezone: string;
  water_body: string | null;
  revision: number;
};

export type Profile = {
  profile_id: string;
  digest: string;
  name: string;
  channels: Record<string, string>;
  start: string;
  end: string;
  resolution_minutes: number | null;
  provenance: Record<string, unknown>;
  parent_digest: string | null;
  label?: string | null;
  integrity_error?: string;
};

export type ProfileValues = Omit<Profile, "channels"> & {
  timestamps: string[];
  channels: Record<string, (number | null)[]>;
  units: Record<string, string>;
  total: number;
  indices: number[];
  offset: number;
};

export type CsvPreview = {
  columns: string[];
  rows: string[][];
  row_count: number;
  parser: string;
};

export type Preview = {
  format: string;
  preview?: CsvPreview;
  columns?: string[];
  rows?: string[][];
  row_count?: number;
  metadata?: Record<string, unknown>;
};

export type Mapping = {
  channel: string;
  column: string;
  unit: string;
};

export const CHANNEL_LABELS: Record<string, string> = {
  ghi: "Global horizontal (GHI)",
  dni: "Direct normal (DNI)",
  dhi: "Diffuse horizontal (DHI)",
  par: "PAR",
  air_temperature: "Air temperature",
  sea_temperature: "Sea temperature",
  wind_speed: "Wind speed",
  cloud_cover: "Cloud cover",
  wave_height: "Wave height",
  wave_period: "Wave period",
};

export function displayUnit(channel: string, unit: string): string {
  if (channel === "air_temperature" || channel === "sea_temperature") return "°C";
  if (["ghi", "dni", "dhi"].includes(channel)) return "W/m²";
  if (channel === "par") return "µmol/(m²·s)";
  return unit;
}

export function displayValue(channel: string, value: number | null): number | null {
  return value !== null && ["air_temperature", "sea_temperature"].includes(channel)
    ? value - 273.15
    : value;
}

export function storageValue(channel: string, value: number | null): number | null {
  return value !== null && ["air_temperature", "sea_temperature"].includes(channel)
    ? value + 273.15
    : value;
}
