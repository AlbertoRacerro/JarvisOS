import { basename, join, resolve, sep } from "node:path";

const FIXTURE_REGISTRY = Object.freeze({
  "model-version-selection": Object.freeze({
    script: "model_version_selection.py",
    phases: Object.freeze(["workspace", "versions"]),
  }),
  "literature-knowledge": Object.freeze({
    script: "literature_knowledge.py",
    phases: Object.freeze(["workspace", "sources"]),
  }),
  "project-search": Object.freeze({
    script: "project_search.py",
    phases: Object.freeze(["workspace", "records"]),
  }),
  "engineering-workspace": Object.freeze({
    script: "engineering_workspace.py",
    phases: Object.freeze(["workspace"]),
  }),
  "dwsim-editor-unavailable": Object.freeze({
    script: "dwsim_editor_unavailable.py",
    phases: Object.freeze(["workspace", "case"]),
  }),
});

export const FIXTURE_IDS = new Set(["none", ...Object.keys(FIXTURE_REGISTRY)]);

export function trustedFixturePaths(fixtureDir) {
  const root = resolve(fixtureDir);
  return Object.values(FIXTURE_REGISTRY).map(({ script }) => {
    const path = resolve(join(root, script));
    if (!path.startsWith(`${root}${sep}`) || basename(path) !== script) {
      throw new Error(`trusted fixture path escaped registry root for ${script}`);
    }
    return path;
  });
}

export function fixturePhaseAllowed(fixture, phase) {
  if (fixture === "none") return false;
  const spec = FIXTURE_REGISTRY[fixture];
  return Boolean(spec && spec.phases.includes(phase));
}

export function resolveTrustedFixture(fixture, phase, fixtureDir) {
  if (!fixturePhaseAllowed(fixture, phase)) {
    throw new Error(`unsupported trusted fixture phase ${fixture}:${phase}`);
  }
  const spec = FIXTURE_REGISTRY[fixture];
  const root = resolve(fixtureDir);
  const path = resolve(join(root, spec.script));
  if (!path.startsWith(`${root}${sep}`) || basename(path) !== spec.script) {
    throw new Error(`trusted fixture path escaped registry root for ${fixture}`);
  }
  return path;
}
