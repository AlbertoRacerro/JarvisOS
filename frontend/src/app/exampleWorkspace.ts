// Spec 222: example workspaces are labelled so they are never mistaken for user projects.
export type ExampleCandidate = Readonly<{ slug: string; description?: string | null }>;

export const STUDIO_EXAMPLE_SLUG = "engineering-studio-example";
export const EXAMPLE_SLUG_PREFIX = "example-";
/** Spec 229: the C1 smooth-tube Example offered from an empty Design project. */
export const C1_EXAMPLE_SLUG = "example-c1-smooth-tube";

export function isExampleWorkspace(workspace: Pick<ExampleCandidate, "slug"> | null | undefined): boolean {
  const slug = workspace?.slug ?? "";
  return slug === STUDIO_EXAMPLE_SLUG || slug.startsWith(EXAMPLE_SLUG_PREFIX);
}

/** First sentence of the description (provenance one-liner), or a fixed fallback. */
export function exampleTooltip(workspace: ExampleCandidate): string {
  const text = (workspace.description ?? "").trim().replace(/\s+/g, " ");
  if (!text) return "Example workspace";
  const end = text.search(/[.!?](\s|$)/);
  return end === -1 ? text : text.slice(0, end + 1);
}

/** The home project a case study is shown in: the first workspace that is not an example (as in the rail). */
export function homeProject<T extends Pick<ExampleCandidate, "slug">>(workspaces: readonly T[]): T | undefined {
  return workspaces.find((item) => !isExampleWorkspace(item));
}

/** An example presented as a case study of the home project, named without the "Example ·" prefix and the
 * trailing note: "Example · C1 smooth tube (development example)" → "C1 smooth tube". */
export function caseStudyName(workspace: Readonly<{ name: string }>): string {
  const name = workspace.name.replace(/^\s*example\s*[·:–-]\s*/i, "").replace(/\s*\([^)]*\)\s*$/, "").replace(/\s+example\s*$/i, "").trim();
  return name || workspace.name;
}

/** Picker label of a case study: a leading case code is set apart, "C1 smooth tube" → "C1 · smooth tube". */
export function caseStudyLabel(workspace: Readonly<{ name: string }>): string {
  const name = caseStudyName(workspace);
  const coded = /^([A-Z]+\d+\w*)\s+(.+)$/.exec(name);
  return coded ? `${coded[1]} · ${coded[2]}` : name;
}

/** Plain-text label for <option> elements, which cannot hold a badge element. */
export function workspaceOptionLabel(workspace: ExampleCandidate & { name: string }): string {
  return isExampleWorkspace(workspace) ? `${workspace.name} (Example)` : workspace.name;
}
