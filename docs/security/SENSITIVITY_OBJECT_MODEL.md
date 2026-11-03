# Sensitivity Object Model (Stage 5-PRE)

Status: **design contract**. No runtime implementation. Defines the material types
JarvisOS handles and the metadata each must eventually carry before it can be
saved, retrieved, sent to a tool/MCP, or sent to a provider.

This document distinguishes **CURRENT** (what the code does today, from the Stage 2 /
Stage 2-R1 golden tests) from **TARGET** (what consolidation must guarantee).

## Why an object model

Today, sensitivity decisions are made per-call-site on bare strings, with divergent
marker lists (see [STAGE_5_CONSOLIDATION_PLAN.md](STAGE_5_CONSOLIDATION_PLAN.md) drift
table). There is no notion of a *material object* that carries its classification with
it. That is why the same token (`"token"`) gets four different verdicts across sites,
and why a future Graphify/Obsidian/MCP layer could silently become a leakage channel:
material would cross a boundary without its sensitivity travelling with it.

The object model fixes the *vocabulary*, not the code. Implementation is Stage 5.x.

## Material types

| Object type | Exists today? | Where (current) |
|---|---|---|
| `chat_message` | yes (dev) | `dev_message_route` request body |
| `history_turn` | yes (dev) | `dev_local_chat` history, scanned by `smoke_adapter.scan_history_turn_for_context` |
| `uploaded_file` | no | future ingestion (MarkItDown-style) |
| `repo_file` | partial | runner reads hash-pinned script under data root |
| `retrieved_note` | no | future retrieval layer (does not exist yet) |
| `codebase_graph_node` | no | future Graphify-style memory |
| `tool_output` | no | future tool/MCP layer |
| `mcp_resource` | no | future MCP layer |
| `secret_or_api_key` | yes | `secrets/storage.py` (runtime, not persisted in cleartext) |
| `provider_response` | yes | AI gateway / smoke paths |
| `audit_report` | yes | `events` table (redacted by `events/service.py`) |
| `generated_artifact` | yes | `files/service.py` artifacts (absolute `stored_path`) |

Only objects that already exist are in scope for Stage 5 migration. The rest are
**boundary contracts** so that, when those layers are built, they cannot bypass policy.

## Required metadata (TARGET)

Every material object, once the model is implemented, must carry:

| Field | Meaning | Default when unknown |
|---|---|---|
| `sensitivity_class` | one of S0..S4 (see egress policy) | `S2` (fail closed) |
| `origin` | where it came from (user, provider, tool, file, graph) | `unknown` |
| `owner_context` | workspace / session it belongs to | required |
| `contains_secret` | credential/API key present | `true` if unknown |
| `contains_private_ip` | project/proprietary/patent material | `true` if unknown |
| `contains_external_provider_intent` | "send to X" / upload intent | `false` |
| `contains_prompt_injection_or_bypass` | jailbreak / "ignore previous" | `false` (CURRENT gap, see below) |
| `storage_policy` | may it be saved, and where | derived from class |
| `retrieval_policy` | may it be indexed / recalled | derived from class |
| `egress_policy` | may it leave the machine, to whom | derived from class |
| `tool_policy` | may it be passed to a tool/MCP | derived from class |
| `provider_policy` | which provider classes are eligible | derived from class |
| `retention_policy` | how long it may persist | derived from class |
| `redaction_policy` | what must be masked before reuse | derived from class |
| `confirmation_required` | user must explicitly approve | `true` for S2+ egress |
| `provenance` | link to source object(s) if derived | required for sanitized derivatives |

`provenance` is mandatory: a sanitized S1 derivative of an S3 secret must remain linked
to its S3 origin, so a later step cannot treat the derivative as freely shareable.

## Fail-closed default

When classification is unknown or ambiguous, the object defaults to **S2** and to the
most restrictive policy for any *real external* path. This is the opposite of the
current FAST_DEV behavior (`privacy.py:144` maps unknown → `internal` → `external_allowed=True`),
which is preserved only as a dev/local convenience and is **not** sufficient for real
provider egress in the TARGET model.

## Anchored to current decision sites

The model must be expressible as adapters over today's sites without changing behavior
in Stage 5.0/5.1:

- `ai/privacy.py` → produces `sensitivity_class` + `egress_policy` for `chat_message`.
- `events/service.py` → enforces `redaction_policy` for `audit_report`.
- `runner/safety.py` → enforces `tool_policy` for `repo_file` (script execution).
- `dev_message_route/smoke_adapter.py` → enforces `retrieval_policy`/history inclusion
  for `history_turn` (`scan_history_turn_for_context`) and response shaping.

See [SENSITIVITY_EGRESS_RETRIEVAL_POLICY_PRE.md](SENSITIVITY_EGRESS_RETRIEVAL_POLICY_PRE.md)
for the class definitions and [MATERIAL_TOOL_MCP_BOUNDARY_PRE.md](MATERIAL_TOOL_MCP_BOUNDARY_PRE.md)
for the future-layer boundaries.
