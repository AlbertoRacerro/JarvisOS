import type { Site } from "./types";

import NumericInput from "../ui/NumericInput";
export type SiteDraft = Omit<Site, "latitude" | "longitude" | "elevation_m"> & {
  latitude: string;
  longitude: string;
  elevation_m: string;
};

export function toSiteDraft(site: Site | null): SiteDraft {
  return {
    name: site?.name ?? "",
    latitude: site ? String(site.latitude) : "",
    longitude: site ? String(site.longitude) : "",
    elevation_m: site ? String(site.elevation_m) : "",
    timezone: site?.timezone ?? "UTC",
    water_body: site?.water_body ?? "",
    revision: site?.revision ?? 0,
  };
}

export function sitePayload(draft: SiteDraft): Site {
  return {
    ...draft,
    latitude: Number(draft.latitude),
    longitude: Number(draft.longitude),
    elevation_m: Number(draft.elevation_m),
    water_body: draft.water_body || null,
  };
}

type Props = {
  value: SiteDraft;
  onChange: (site: SiteDraft) => void;
  onSave: () => void;
  disabled: boolean;
};

export function SiteForm({ value, onChange, onSave, disabled }: Props) {
  const update = (key: keyof SiteDraft, next: string) => onChange({ ...value, [key]: next });
  return (
    <section className="environment-card">
      <h3>Site</h3>
      <div className="environment-site-grid">
        <label>Site name<input value={value.name} maxLength={120} onChange={(e) => update("name", e.target.value)} /></label>
        <label>Latitude (°)<NumericInput min={-90} max={90}
          value={value.latitude} onValueChange={(text) => update("latitude", text)} /></label>
        <label>Longitude (°)<NumericInput min={-180} max={180}
          value={value.longitude} onValueChange={(text) => update("longitude", text)} /></label>
        <label>Elevation (m)<NumericInput min={-500} max={9000}
          value={value.elevation_m} onValueChange={(text) => update("elevation_m", text)} /></label>
        <label>IANA timezone<input value={value.timezone} onChange={(e) => update("timezone", e.target.value)} /></label>
        <label>Water body<input value={value.water_body ?? ""} maxLength={120}
          onChange={(e) => update("water_body", e.target.value)} /></label>
      </div>
      <button type="button" disabled={disabled} onClick={onSave}>Save site</button>
    </section>
  );
}
