import { CULTURE_FIELDS, type StudioPort, type StudioUnitCase, type StudioUnitContract, type StudioUnitStream, type StudioVariable } from "../../api/studio";

// Spec 190 process-unit authoring helpers. Pure so the build-gating test can import them.
export const MAX_PORTS = 4;
export const CULTURE_INPUTS = [...CULTURE_FIELDS, "ph"] as const;
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const KEYWORDS = new Set(["False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield"]);
export const blankPort = (): StudioPort => ({ name: "", label: "" });
export const defaultStream = (): StudioUnitStream => ({ mass_flow_kg_s: 1, temperature_K: 298.15, pressure_Pa: 101325, mass_fractions: {}, culture: {} });

export function blankUnitCase(contract: Pick<StudioUnitContract, "inlets" | "parameters">): StudioUnitCase {
  const first = (variable: StudioVariable) => variable.min != null && variable.min > 0 ? variable.min : variable.max != null && variable.max < 0 ? variable.max : 0;
  return {
    inlets: Object.fromEntries(contract.inlets.map((port) => [port.name, defaultStream()])),
    params: Object.fromEntries(contract.parameters.map((item) => [item.name, first(item)])),
    expected_outlets: null, tolerance: 1e-6
  };
}
export const fractionSum = (fractions: Record<string, number>) => Object.values(fractions).reduce((total, value) => total + value, 0);

/** Contract problems the backend would refuse, shown before Create/Save so the operator can fix them in place. */
export function contractIssues(contract: Pick<StudioUnitContract, "inlets" | "outlets" | "parameters">): string[] {
  const issues: string[] = [];
  for (const [title, ports] of [["inlet", contract.inlets], ["outlet", contract.outlets]] as const) {
    if (ports.length < 1 || ports.length > MAX_PORTS) issues.push(`Declare 1 to ${MAX_PORTS} ${title} ports.`);
    for (const port of ports) {
      if (!IDENTIFIER.test(port.name) || KEYWORDS.has(port.name)) issues.push(`${title[0].toUpperCase()}${title.slice(1)} port name "${port.name}" must be a Python identifier.`);
      if (!port.label.trim()) issues.push(`${title[0].toUpperCase()}${title.slice(1)} port ${port.name || "(unnamed)"} needs a label.`);
      if (/["'\\#]/.test(port.label)) issues.push(`Label of ${port.name} must not contain quotes, backslash or #.`);
    }
    if (new Set(ports.map((port) => port.name)).size !== ports.length) issues.push(`${title[0].toUpperCase()}${title.slice(1)} port names must be unique.`);
  }
  const names = contract.parameters.map((item) => item.name);
  if (new Set(names).size !== names.length) issues.push("Parameter names must be unique.");
  return issues;
}

/** An <img> source for a sanitized or pending SVG. An image never runs script, so previewing is safe. */
export const svgDataUri = (svg: string) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;

/** Plain meaning of each typed host-check code (backend studio/unit_checks.py). */
export const HOST_CHECK_HELP: Record<string, string> = {
  STUDIO_UNIT_STATE_INVALID: "An outlet stream is malformed: a missing field, a non-finite or negative value, mass fractions that do not sum to 1, or a compound that is not in the flowsheet.",
  STUDIO_UNIT_OUTLETS_MISMATCH: "The returned outlets differ from the declared outlet ports.",
  STUDIO_UNIT_MASS_IMBALANCE: "Total outlet mass flow differs from total inlet mass flow beyond 1e-9 relative.",
  STUDIO_UNIT_COMPONENT_IMBALANCE: "A compound's outlet mass flow differs from its inlet mass flow beyond 1e-9 relative.",
  STUDIO_UNIT_CULTURE_IMBALANCE: "A culture species is not conserved and no generation is declared for it.",
  STUDIO_UNIT_FAILED: "evaluate raised an error or the sandbox refused it.",
  STUDIO_UNIT_TIMEOUT: "evaluate exceeded the sandbox time limit.",
  STUDIO_RUNTIME_UNAVAILABLE: "The Studio runtime is disabled or unavailable on this host."
};
export const hostCheckHelp = (code: string) => HOST_CHECK_HELP[code] ?? "Typed code from the Studio unit host check.";

/** Align a case with the current contract: inlets and parameters follow the declared names, extras are dropped. */
export function alignCase(item: StudioUnitCase, contract: Pick<StudioUnitContract, "inlets" | "outlets" | "parameters">): StudioUnitCase {
  const fresh = blankUnitCase(contract);
  const expected = Object.fromEntries(Object.entries(item.expected_outlets ?? {}).filter(([port]) => contract.outlets.some((outlet) => outlet.name === port)));
  return {
    inlets: Object.fromEntries(contract.inlets.map((port) => [port.name, item.inlets[port.name] ?? fresh.inlets[port.name]])),
    params: Object.fromEntries(contract.parameters.map((variable) => [variable.name, item.params[variable.name] ?? fresh.params[variable.name]])),
    expected_outlets: Object.keys(expected).length ? expected : null, tolerance: item.tolerance ?? 1e-6
  };
}
