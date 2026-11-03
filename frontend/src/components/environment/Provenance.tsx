import type { Profile } from "./types";
import { CHANNEL_LABELS, displayUnit } from "./types";

function printable(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(printable).join("; ");
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key.replace(/_/g, " ")}: ${printable(item)}`)
      .join(" · ");
  }
  return String(value);
}

type Props = { profile: Profile; onChooseParent: (digest: string) => void };

export function Provenance({ profile, onChooseParent }: Props) {
  const provenance = profile.provenance;
  const parent = profile.parent_digest ?? (typeof provenance.source_digest === "string" ? provenance.source_digest : null);
  const rows: Array<[string, unknown]> = [
    ["Source kind", provenance.kind],
    ["Filename", provenance.filename],
    ["Parser", provenance.parser],
    ["Parser or generator version", provenance.pvlib_version ?? provenance.parser],
    ["Column mapping", provenance.column_mapping],
    ["Units", Object.fromEntries(Object.entries(profile.channels).map(([channel, unit]) => [
      CHANNEL_LABELS[channel] ?? channel,
      displayUnit(channel, unit),
    ]))],
    ["Selected TMY months and years", provenance.selected_month_year_pairs ?? provenance.source_month_year_pairs],
    ["Profile label", profile.label],
    ["Year normalization", provenance.year_normalization],
    ["Irradiance time offset", provenance.irradiance_time_offset],
    ["Irradiance semantics", provenance.irradiance_semantics],
    ["Operation", provenance.label ?? provenance.summary],
    ["Parent source kind", provenance.parent_source_kind],
    ["Parent parser version", provenance.parent_parser_version],
    ["Parent generator version", provenance.parent_generator_version],
    ["Conversion factor (µmol/J)", provenance.factor_umol_per_j],
    ["Factor provenance", provenance.factor_provenance],
    ["Generator parameters", provenance.parameters],
    ["Site snapshot", provenance.site_snapshot],
    ["Original file SHA-256", provenance.original_sha256],
    ["Profile digest", profile.digest],
  ];
  return (
    <section className="environment-prov" aria-label="Profile provenance">
      <h4>Provenance</h4>
      {parent && <p>{profile.provenance.kind === "edit" ? "Edited from" : "Derived from"}{" "}
        <button type="button" className="environment-parent-link" onClick={() => onChooseParent(parent)}>{parent}</button>
      </p>}
      <dl>{rows.filter(([, value]) => value !== undefined && value !== null && value !== "").map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{printable(value)}</dd></div>
      ))}</dl>
    </section>
  );
}
