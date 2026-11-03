# 225 — SCOPED-COMPUTER-USE-1 (future candidate)

Registry authority: `docs/specs/STATUS.md`. Issue #963. Maintainer goal: **Jarvis may operate on an interface for the user**, not merely narrate, under narrowly approved authority. First useful capability is 224 Guided Operator Walkthrough (not carried into this repository), whose observed semantic targeting and verification must be reusable.

## Shared architecture discovery gate — 2026-10-27

The maintainer clarified that **computer use and real-time learning walkthrough are co-primary product goals**; an architecture treating computer use as a simple future DOM-click add-on to 224 is not accepted. A prior-art review of existing computer-use products (not carried into this repository) informed the architecture; no third-party code or assets are used.

Before readiness, jointly qualify the observer/grounding/step verification contract with 224 and independently qualify stricter privileges for action execution. In particular, *a plausible position, user click, screenshot change, model confidence or API 200 is not a verified completed action*. Permits must be scoped and non-idempotent actions never replayed after helper recovery without evidence. No silent elevation from guiding to execution.

The tier definitions below remain provisional examples, not approval of any pointer simulator, browser extension or Windows agent.

## Minimum viable computer use

- **Tier 0: Explain/point/watch** — 224 only; default.
- **Tier 1: In-app NAVIGATE and reversible UI interaction** — typed app-specific actions after explicit operator activation: open known route/sidecar, choose non-destructive tabs/inspector panels, focus known control. Use canonical `navigate`, React event and domain state; never generate arbitrary DOM click selectors or JS.
- **Tier 2: Existing domain actions** — for editing parameters, running CFD, committing project changes, repository work etc., use existing 111 and domain capability owners and exact current revision/grant/preview/confirmation. A visual click is **not** a way to bypass a protected backend operation.
- **Tier 3: Browser / Windows desktop control** — optional and separately qualified, for applications outside JarvisOS. Start with grounded browser DOM/accessibility targets via a tightly scoped browser adapter. Windows 11 UIAutomation/accessibility tree and screen+vision fallback only where DOM is unavailable. Every action is bound to `session_id`, app/window, origin/process allowlist, observation epoch, target identity, requested action and expiry. No silent always-on desktop daemon, keylogging, global mouse takeover, protected-window capture, arbitrary file dialogs or unsolicited computer use.

## Control/permission contract
`observe -> propose -> show target/consequence -> approve if required -> reobserve -> perform one bounded action -> verify -> stop/next`.
- `READ/NAVIGATE` cannot acquire `PROPOSE/COMMIT/EXECUTE` by model output alone.
- Grant is user-initiated, narrow by app/workspace/session/action, revocable immediately. User input overrides and cancels planned steps; stopping future steps is not rollback of completed ones.
- Sensitive content is excluded at capture and egress boundaries; use the existing Jarvis policy gateway, not raw provider API or sidecar-owned privileged desktop process.
- Focus/target stale, window changed, wrong workspace, permission denied, protected screen, deadline, absent accessibility target => fail closed and invite operator to continue manually. No coordinate-only blind clicks; fallback screenshot guidance must remain informational unless separately authorized and independently verified.
- Distinct event evidence of requested vs executed action, target state, outcome and source timestamp; do not record passwords or secret-bearing screenshots.
- Consider an optional non-intercepting native overlay only after precise in-app 224 marker is proven; it must be click-through, DPI/multi-monitor and accessibility correct.

## Evidence before promoting status from planned
1. 224 running in a real Jarvis frontend with resilient target register and owner-backed verification.
2. One exact browser route interaction with real UI state and no duplicate write, tested on isolated local data with Playwright.
3. Windows native host observation feasible without modifying existing security policies; sanctioned helper available with no hidden admin privileges and explicit per-session grant broker.
4. Demonstrate adversarial click-target swap, prompt injection through page content, focus hijack, consent expiration, protected field, multi-monitor coordinate drift and denial of unsupported COMMIT/EXECUTE.
5. Independently assess computer-use frameworks and up-to-date Windows UIAutomation/CDP options; the prior-art review of existing products (not carried into this repository) is a reference, not a dependency, and grants no cloning rights.

## Non-goals
No second Hermes agent, full-screen permanent visibility monitor, duplicate chat/thread/memory, new model route, uncontrolled shell, browser extension with blanket permissions, remote-control service or direct cloud screenshot streaming. No fake success text on failed UI actions. Implementation requires independent readiness/evidence for the materially broader security surface.
