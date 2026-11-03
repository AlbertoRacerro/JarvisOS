# Sensitivity / Egress / Retrieval Policy (Stage 5-PRE)

Status: **design contract**. No runtime change. Defines the canonical sensitivity
classes and the default handling rules per class, covering not only egress
("can this leave the machine?") but also **saving, indexing/retrieval, history/context
inclusion, tool/MCP, and provider eligibility**.

## Canonical classes

Names chosen to subsume the current ad-hoc classes in `ai/privacy.py`
(`public / internal / confidential / sensitive_ip / secret / unknown`).

| Class | Meaning | Maps from current `classify()` |
|---|---|---|
| `S0_PUBLIC` | public, already published / general knowledge | `public` |
| `S1_INTERNAL_LOW_RISK` | internal notes, non-identifying, low harm | `internal` |
| `S2_PRIVATE_OR_PROJECT_IP` | project/proprietary/patent, private project data | `confidential`, `sensitive_ip` |
| `S3_SECRET_OR_CREDENTIAL` | API keys, passwords, tokens, private keys | `secret` |
| `S4_DANGEROUS_OR_POLICY_CRITICAL` | prompt-injection/bypass, destructive intent, policy-critical | (currently scattered: bypass markers, runner blocklist) |
| `unknown` → treated as `S2` | ambiguous / unclassified | `unknown` (CURRENT: FAST_DEV maps to `internal`) |

## Default handling matrix (TARGET)

`Y` = allowed by default, `N` = denied by default, `R` = allowed only on redacted/sanitized
derivative (with provenance), `C` = allowed only after explicit user confirmation.

| Action | S0 | S1 | S2 | S3 | S4 |
|---|---|---|---|---|---|
| `save_to_events` (redacted audit) | Y | Y | Y | R | Y(metadata only) |
| `save_to_long_term_memory` | Y | Y | C | N | N |
| `index_for_retrieval` | Y | Y | C | N | N |
| `include_in_history` | Y | Y | Y | N | N |
| `include_in_prompt_context` | Y | Y | Y | R | N |
| `send_to_local_model` | Y | Y | Y | R | N |
| `send_to_cloud_model` | Y | C | R+C | N | N |
| `send_to_tool_or_MCP` | Y | C | R+C | N | N |
| `redaction_required` | N | N | Y(for egress) | Y(always) | Y |
| `user_confirmation_required` | N | N | Y(egress) | Y(any reuse) | Y |

## Encoded rules (non-negotiable in TARGET)

1. **S2/S3/S4 never leave automatically.** Any cloud/tool/MCP egress of S2+ requires a
   sanitized derivative (`R`) and/or explicit confirmation (`C`). This formalizes the
   frozen rule from the routing handoff: *"S2/S3/S4 non escono automaticamente."*
2. **Difficulty never lowers sensitivity.** A hard-but-sensitive task is solved by
   producing an S1 sanitized derivative deliberately — not by reclassifying the original
   downward.
3. **Sanitized derivatives preserve provenance.** An S1 derivative of an S3/S2 object
   keeps a link to its origin (`provenance` in the object model). Losing provenance is a
   policy violation.
4. **Unknown/ambiguous fails closed for real external paths.** This *reverses* the
   current FAST_DEV behavior for real providers. FAST_DEV may stay as a local/dev mode but
   is **not sufficient** for real provider egress.
5. **History and retrieval contamination are first-class egress risks.** Including an S3
   secret in history/context, or indexing it for retrieval, is treated as severe as
   sending it to a provider — because both end in a prompt that may reach a model.
6. **Bilingual markers.** IP/sensitivity detection must consider both English and Italian
   markers. CURRENT `scan_history_turn_for_context` catches English `proprietary/patent`
   but misses Italian `riservata Zephyr` (Stage 2-R1 golden) — TARGET must close this.

## CURRENT vs TARGET gaps (must be carried, not hidden)

| Gap | CURRENT (golden-locked) | TARGET |
|---|---|---|
| FAST_DEV ambiguous | `internal` + `external_allowed=True` | `S2`, external denied, dev-only convenience |
| `token` token | 4-way divergence across sites | single class, consistent verdict |
| `api key` vs `api_key` | space-sensitive in `classify()` | normalized, both `S3` |
| history bypass | `None` (included) | `S4`, excluded |
| Italian IP | `None` (missed) | `S2` |
| runner obfuscated import | passes preflight | still bounded by hash-pin; blocklist not a sandbox |

These gaps are **intentionally preserved today** and are scheduled in the Stage 5
consolidation plan, not fixed in this slice.
