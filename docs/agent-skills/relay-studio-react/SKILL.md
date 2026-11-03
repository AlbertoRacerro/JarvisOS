---
name: relay-studio-react
description: Apply a small set of audited React patterns when editing JarvisOS Engineering Studio operator UI. Use for Studio, Process, or BLUECAD React views; exclude backend and product agent behavior.
---

# Relay Studio React

Build the operator flow around the current owner API and accepted spec. Keep the user's physical model, units, assumptions, source freshness, and Test/Publish authority visible. Prefer an existing component and route before creating another state owner.

Two static rules are adapted from Vercel's `react-best-practices` at commit `063bee94c3f4df8453406c830b0a7df0f2860278` (MIT):

- **Independent reads can run together.** When data calls have no ordering dependency and share the same workspace snapshot, initiate them together with `Promise.all`. Preserve separate error handling when one result may fail independently. Do not parallelize writes or revision-sensitive reads with their mutation. Source: `rules/async-parallel.md`.
- **Update from current state with a functional setter.** If a handler's next value depends on the previous value, use `setValue(previous => ...)`. This avoids stale closures during delayed browser interactions. A direct setter is clearer when the next value is independent. Source: `rules/rerender-functional-setstate.md`.

Use these only where they improve the actual task. Before claiming UI acceptance, run the relevant build and, for browser-observable criteria, invoke `docs/agent-skills/relay-browser-proof/SKILL.md`. The source files are guidance, not executable dependencies; this skill installs nothing.
