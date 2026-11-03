import { useState } from "react";
import type { ResultProperty } from "../../api/processDraft";

// Presentation-only grouping of the DWSIM-reported property set (spec 158). Values and
// units are shown verbatim as DWSIM reported them at solve time; nothing is computed here.
const GROUPS: [string, string][] = [
  ["conditions", "Conditions & flows"],
  ["phases", "Phases"],
  ["composition", "Composition"],
  ["thermodynamic", "Thermodynamic"],
  ["transport", "Transport"],
  ["equilibrium", "Equilibrium"],
  ["other", "Other"],
];
const KNOWN = new Set(GROUPS.map(([key]) => key));

const shown = (value: ResultProperty["value"]) => {
  if (value == null || value === "") return "—";
  if (typeof value === "number") return Number.isFinite(value) ? String(Number(value.toPrecision(6))) : String(value);
  return String(value);
};

export default function ResultProperties({
  properties,
  stale,
  label,
}: Readonly<{ properties: ResultProperty[]; stale: boolean; label: string }>) {
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const rows = needle
    ? properties.filter((row) => `${row.name} ${row.id} ${row.unit}`.toLowerCase().includes(needle))
    : properties;
  return (
    <section className={`draft-result-props${stale ? " is-stale" : ""}`} aria-label={label}>
      <input
        type="search"
        className="draft-result-props__filter"
        aria-label={`Filter ${label.toLowerCase()}`}
        placeholder="Filter properties…"
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      {GROUPS.map(([group, heading]) => {
        const members = rows.filter((row) => (KNOWN.has(row.group) ? row.group : "other") === group);
        if (!members.length) return null;
        return (
          <details key={group} open={group === "conditions" || Boolean(needle)} className="draft-result-props__group">
            <summary>
              {heading} <span>({members.length})</span>
            </summary>
            <table>
              <tbody>
                {members.map((row) => (
                  <tr key={row.id} className={row.specification ? "is-spec" : "is-result"}>
                    <th scope="row" title={row.id}>{row.name}</th>
                    <td>{shown(row.value)}</td>
                    <td>{row.unit}</td>
                    <td><span className="draft-result-props__kind">{row.specification ? "spec" : "result"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        );
      })}
      {!rows.length && <p className="draft-hint">No property matches the filter.</p>}
    </section>
  );
}
