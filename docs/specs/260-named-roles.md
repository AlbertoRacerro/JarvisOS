# 260 — NAMED-ROLES-1: named local roles over the Hermes runtime

Status: planned (draft for coordinator review)
Depends on: 259 (Hermes v0.23.0); absorbs what remains valid of 034

## Goal

The Jarvis chat can run a Hermes conversation under a named role: **Isaac** (scientific reasoning colleague, with a
**Sheldon** devil's-advocate mode of the same role), **Gregor** (scientific internet researcher), **Q** (tool-using
agent) and **Alfred** (mail, calendar, documents). A role is a name, a purpose, presentation instructions, a toolset
subset and a swappable model setting; it is not a model and it gains no authority.

## Why

Maintainer directives `L20261109220309-dfe1` and `L20261109220757-0951` item 2: move the local-model R&D into Jarvis
and give the team names, reusing the frozen July roster. A name is a role, so the model under it changes when a
better one is measured (model-agnostic harness, `L20261109190005-6773`).

## Scope

In scope:
- A role registry in the Hermes agent module: `isaac`, `gregor`, `q`, `alfred`, each with display name, one-line
  purpose, presentation instructions (Sheldon is Isaac with a devil's-advocate instruction block, selected per turn) and
  a toolset list that is a subset of the current `ENABLED_TOOLSETS` (259). The registry is code, versioned with
  JarvisOS; this is the part of 034 that survives (policy metadata, capability labels, advisory constraints).
- A per-role model setting: a local inference route and model id. Defaults come from the loaded pack when it declares
  them (candidates under test stay in the private pack) and otherwise fall back to the current
  `JARVIS_HERMES_INFERENCE_ROUTE`. The maintainer can change a role's model from settings without a code change.
  Only `local:` routes are accepted, as today.
- Session binding: a chat thread chooses a role when its Hermes session starts; `AgentSessionRef.profile_id` carries the
  role id and the worker builds the agent with that role's instructions, toolsets and route. Changing role starts a new
  Hermes session (no mid-session swap).
- Frontend: a role chooser in the Hermes chat composer showing name and purpose, and the active role's name on its
  replies. A role whose model is not available is shown unavailable with the reason, never silently replaced.

Out of scope (binding non-goals):
- New tools, toolsets, connectors, egress or permissions for any role. Gregor's web research and Alfred's mail and
  calendar need capabilities JarvisOS does not broker yet; until then those roles run with the existing broker tools
  and say so in their purpose line.
- Hard-coding today's candidate models as final; benchmark content or scores (private pack).
- Mascots and the live sub-agent view (Spec 261, item 3); multi-persona sessions (026), review panels (011, 032).
- Loading or switching the llama.cpp model from a role choice beyond what Spec 250's switch already offers.

## Design constraints

- Roles own no memory, permission, provider, sensitivity or promotion decision (AGENTS.md Never 5 and 7).
- `route_class="auto"` and paid providers stay unreachable from any role.
- Role instructions are presentation and reasoning style only; they never widen the admitted tool set
  (`withhold_tools` stays fail-closed).

## Acceptance criteria

1. The registry lists exactly the four roles plus the Sheldon mode of Isaac; each role's toolsets are a subset of
   `ENABLED_TOOLSETS` (test).
2. A role's model setting accepts only `local:` routes; a pack default is used when present, else the environment
   route (tests with the fake pack and with no pack).
3. Starting a Hermes session with role R builds the agent with R's instructions, toolsets and route; switching role
   starts a new session (tests with a fake worker).
4. The chat shows the chooser with the four names and purposes and labels replies with the active role; an
   unavailable role model is shown as unavailable (frontend `.mjs` test and browser walk from `/`).
5. No pack content appears in the repository; CI leak guard and gates pass.

## Open points for coordinator review

- Whether the per-role model setting lives in the existing settings store or the pack only (prefer settings store
  with pack defaults).
- Whether Sheldon is chosen per turn or per session (draft: per turn, same session).
