# Settings beta — approved visual reference — 2026-08-26

Status: maintainer-approved visual/product reference; not runtime implementation authority.

Approved local HTML identity:

- `settings_beta_mockup_v2.html`
- SHA-256: `f30a0937f9e8cb1a189ade226a004ac4206597d1130433748b87d4c61043e5de`

Approved rendered identity:

- `settings_beta_mockup_v2.png`
- SHA-256: `63863bd3c68142fc85f06cbcb9a003a6eafe20c14b4d4868796c1a333f236154`

The canonical product semantics are already frozen in `docs/product-direction/05-settings-contract.md` (not carried into this repository). This reference freezes the approved visual composition used during the maintainer design session.

Required composition:

- global primary rail remains `Design | Memory | Development | Coding | Settings`;
- Settings owns exactly `Appearance | AI | System`;
- no Jarvis right-side inspector is required in Settings;
- no giant dashboard cards; use wide, restrained rows/dividers and compact controls;
- Appearance owns theme and accent selection;
- AI provider credentials are provider-scoped, never model-scoped;
- provider rows expose connection state, masked credential, endpoint, available models and bounded actions such as update/test/refresh/disconnect;
- Codex/Claude Code integrations remain semantically distinct from ordinary provider API credentials;
- orchestration/policy is compact and separates provider availability from policy authority and deterministic validation;
- System exposes app/runtime/data/service state with advanced diagnostics subordinate/collapsed;
- visual language follows the same warm limestone / near-white shell, compact radii and restrained chlorophyll-green accent used by the approved operator references.

The approved HTML is stronger layout evidence than any screenshot affected by rendering/tooling error.

This file does not modify `docs/specs/STATUS.md`, release the post-100 hold, or authorize runtime implementation.
