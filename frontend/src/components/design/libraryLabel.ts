// Library rows read as a short name plus its version; the full published name stays in the tooltip.
type Entry = { name: string; version_label: string };
export type LibraryLabel = { kind: string | null; name: string; version: string; full: string };

const KINDS: [RegExp, string][] = [[/^CFD reduced model\s+/i, "CFD"], [/^Reduced model\s+/i, "Reduced"]];

/** "CFD reduced model c1_tube.darcy.smooth_pipe" + "… v1" -> CFD · "Darcy · smooth pipe" · v1. */
export function libraryLabel(entry: Entry): LibraryLabel {
  let name = entry.name.trim();
  let kind: string | null = null;
  for (const [prefix, label] of KINDS) if (prefix.test(name)) { kind = label; name = name.replace(prefix, ""); break; }
  // A dotted model id keeps its last two parts: the family and the case.
  name = name.replace(/\b[\w-]+(?:\.[\w-]+)+\b/g, (id) => id.split(".").slice(-2).map((part, index) => {
    const words = part.replace(/[_-]+/g, " ").trim();
    return index === 0 ? words.charAt(0).toUpperCase() + words.slice(1) : words;
  }).join(" · "));
  if (!name) name = entry.name;
  const version = /\bv\d+$/i.exec(entry.version_label.trim())?.[0] ?? entry.version_label;
  return { kind, name, version, full: `${entry.name} · ${entry.version_label}` };
}
