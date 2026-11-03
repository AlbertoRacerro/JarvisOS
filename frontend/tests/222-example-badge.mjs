// Spec 222 contract 3: the Example badge predicate, tooltip and wiring.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { caseStudyLabel, caseStudyName, exampleTooltip, homeProject, isExampleWorkspace, workspaceOptionLabel } from "../src/app/exampleWorkspace.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

assert.equal(isExampleWorkspace({ slug: "example-c1-smooth-tube" }), true);
assert.equal(isExampleWorkspace({ slug: "engineering-studio-example" }), true);
for (const slug of ["project-c1", "my-example-project", "examples", "Example-x", "", "engineering-studio-example-2"]) {
  assert.equal(isExampleWorkspace({ slug }), false, slug);
}
assert.equal(isExampleWorkspace(null), false);
assert.equal(isExampleWorkspace(undefined), false);

assert.equal(exampleTooltip({ slug: "example-a", description: "Development example of C1. Created today. More." }), "Development example of C1.");
assert.equal(exampleTooltip({ slug: "example-a", description: "No terminator" }), "No terminator");
assert.equal(exampleTooltip({ slug: "example-a", description: "Version 1.5 is fine. Next." }), "Version 1.5 is fine.");
assert.equal(exampleTooltip({ slug: "example-a", description: null }), "Example workspace");
assert.equal(exampleTooltip({ slug: "example-a", description: "  " }), "Example workspace");

assert.equal(workspaceOptionLabel({ slug: "example-a", name: "Ex" }), "Ex (Example)");
assert.equal(workspaceOptionLabel({ slug: "plain", name: "Plain" }), "Plain");

// Examples are presented as case studies of the home project (the first non-example workspace).
assert.equal(caseStudyName({ name: "Example · C1 smooth tube (development example)" }), "C1 smooth tube");
assert.equal(caseStudyLabel({ name: "Example · C1 smooth tube (development example)" }), "C1 · smooth tube");
assert.equal(caseStudyLabel({ name: "Engineering Studio example" }), "Engineering Studio");
assert.equal(caseStudyLabel({ name: "Example" }), "Example");
assert.equal(homeProject([{ slug: "example-a" }, { slug: "main" }, { slug: "other" }])?.slug, "main");
assert.equal(homeProject([{ slug: "example-a" }]), undefined);
const rail = read("src/components/shell/ProjectSwitcher.tsx");
assert.ok(rail.includes(">Case study</span>") && !rail.includes("Viewing example"), "the rail names an open example a case study");

const badge = read("src/app/ExampleBadge.tsx");
assert.match(badge, /className="example-badge" title=\{exampleTooltip\(workspace\)\}/);
assert.match(read("src/pages/DesignProject.tsx"), /<h1>\{projectName \|\| "Project"\}<ExampleBadge workspace=\{projectWorkspace\} \/><\/h1>/);
const css = read("src/styles/lumen.css");
assert.match(css, /\.example-badge \{[^}]*var\(--color-accent-subtle\)[^}]*\}/s);
assert.doesNotMatch(css.match(/\.example-badge \{[^}]*\}/s)[0], /#[0-9a-f]{3,8}\b/i);
assert.match(read("package.json"), /"test:222": "node tests\/222-example-badge\.mjs"/);
console.log("222 example badge frontend ok");
