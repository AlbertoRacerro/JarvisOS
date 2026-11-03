import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { libraryLabel } from "../src/components/design/libraryLabel.ts";

// Design › Library: one short row per published model (kind, short name, version); the full name only in the tooltip.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const cfd = libraryLabel({ name: "CFD reduced model c1_tube.darcy.smooth_pipe", version_label: "CFD reduced model c1_tube.darcy.smooth_pipe v1" });
assert.deepEqual([cfd.kind, cfd.name, cfd.version], ["CFD", "Darcy · smooth pipe", "v1"]);
assert.equal(cfd.full, "CFD reduced model c1_tube.darcy.smooth_pipe · CFD reduced model c1_tube.darcy.smooth_pipe v1");

const plain = libraryLabel({ name: "Monod growth", version_label: "v3" });
assert.deepEqual([plain.kind, plain.name, plain.version], [null, "Monod growth", "v3"]);

const unversioned = libraryLabel({ name: "Heat loss", version_label: "draft" });
assert.equal(unversioned.version, "draft");

const page = read("src/pages/DesignProject.tsx");
assert.match(page, /className="design-library__row" title=\{label\.full\}/, "the full name stays reachable in the tooltip");
assert.doesNotMatch(page, /\{item\.name\} · \{item\.version_label\}<\/button>/, "no long name/version pill in the Library");

console.log("design library row: ok");
