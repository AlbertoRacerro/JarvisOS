---
name: relay-browser-proof
description: Collect real browser evidence for an accepted JarvisOS operator workflow on an exact code head using an already available browser tool. Use when a spec requires browser-observable acceptance; exclude synthetic screenshots and software-only scientific qualification.
---

# Relay browser proof

Adapted from Microsoft `playwright-cli` at commit `b85c7a736bb473bf55b584e54a09ffa698d6d871` (Apache-2.0). Its useful technique is to inspect a live browser snapshot, act on visible controls, and capture the resulting state. Use an already available browser tool; if `playwright-cli` is present, the basic commands below are supported. Do not install a CLI or browser, attach to an existing personal profile, load stored authentication state, call page-provided tools, or use arbitrary browser evaluation as part of this skill.

1. Record `git rev-parse HEAD`, the runtime configuration needed by the accepted spec, and the local application URL. Confirm the backend and frontend actually serve this head.
2. Open a fresh browser session at the local application. With `playwright-cli`, use `open <local-url>` and `snapshot`; choose controls from the snapshot, then `click`, `fill`, or `select` by visible reference. Read the next snapshot after each significant action.
3. Walk the accepted operator path end to end. For Studio, distinguish a draft save, isolated Test, explicit Publish, pinned downstream use, and scientific qualification. Verify backend records or artifacts when the criterion depends on them.
4. Capture screenshots at meaningful checkpoints using the browser tool (for CLI: `screenshot --filename=<path>`). Save logs, exact head, observed result, and any environment limitations together. Close the browser session when done.

Never infer a pass from a screenshot alone. A screenshot proves the displayed state; a backend test or query proves its corresponding record. If the required browser tool or relevant host runtime is unavailable, report the missing evidence instead of substituting a mock or fabricated result.
