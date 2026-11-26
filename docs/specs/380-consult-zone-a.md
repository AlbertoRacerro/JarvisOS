# 380 — CONSULT-ZONE-A-1: First usable expert consult in `ask` mode

Status: merged
Depends on: 376

## Goal

The local executor (Inkling) can ask a trusted cloud consultant (Claude on the maintainer's subscription) for ideas,
mechanisms, physics, literature or critique through one tool, `jarvis_consult`. Inkling writes the question and an
indicative context; deterministic code only passes or blocks that text, sends it, stores the answer and pastes it
verbatim. Specific content (any number produced or stored by Jarvis, geometry, files, verbatim tool output) is blocked
before anything leaves the machine. In this first step every consult waits for the maintainer to see the exact packet
and approve or edit it (`ask` mode); `auto` exists but only the maintainer can switch it on.

## Why

Directive `L20261125114331-2bd1` (consult design v3), §9 step 1: "the zone config and the ADR amendment; the brief as a
context release; the consult tool, the zone-A gate (§3.1–3.3, 3.6, 3.7), the Relay transport with the guarded MCP,
verbatim insertion and the ledger". Its precondition ("after the bench-recipe port is merged and measured") holds:
spec 372 merged and was measured (`L20261125154544-9ec2`), and its follow-ups 374 and 375 merged.

## Code facts checked at `1d83e27b`

- `backend/app/modules/ai/cloud_escalation.py::screen_outbound_text` returns `refused`/`secret_detected` for an S4
  floor and `edit_required` for S2/S3 floors (`deterministic_floor_trigger`).
- `backend/app/modules/relay_gateway/service.py::draft_relay_escalation` / `escalate_with_relay` are the existing
  operator draft-then-approve flow (exact text and `text_digest`, replay guard, `_submit_run` with `max_turns=1`).
  `_submit_run` mounts the repository or a derivative workspace (`access_mode_for`), materialises context releases and
  ingests `jarvis-actions` blocks; none of that is wanted for a consult. Relay runs write no `ai_jobs` row.
- `backend/app/modules/relay_gateway/service.py::create_context_release` accepts only approved S0/S1 derivatives in a workspace.
- `configs/relay_gateway.json` `repository.note` says "JarvisOS v1 is open source".
- `backend/app/modules/ai/egress_sanitizer.py::_ALLOWED_SANITIZER_KINDS` is `{"deterministic", "model_local"}`;
  `create_prompt_derivative` validates the final content against S0/S1 cloud levels.
- `backend/app/modules/ai/execution.py::run_ai_task` takes `route_class`, `adapters`, `bindings` and `metadata`; a
  binding with `requires_network` goes to `_run_external_network_task` (egress packet, budget). Its level and budget
  checks were not read line by line for this spec: the implementer opens them first.
- `backend/app/core/vault.py::load_vault` reads `vault.toml` under `JARVISOS_VAULT_PATH` (`protected_dimensions`,
  `local_only`, `protected_propositions`); `vault_protected_dimensions` merges with the pack's.
- `backend/app/modules/agents/hermes/worker_shim.py` lists the Jarvis MCP tools Hermes may see;
  `backend/app/modules/agents/hermes/supervisor.py::_TOOL_CAPABILITIES` maps tool names to capability ids and the dispatcher calls one `dispatch_*`
  function per capability with the turn's grant.
- `backend/app/modules/agents/hermes/calc_run_tool.py::_log_run` stores only metadata of a `jarvis_python` run (no
  output values). Stored specific numbers live in `parameters`, `simulation_result_values`,
  `simulation_run_scalar_results`, `design_component_geometry`, `design_component_units`, `studio_procedure_runs` and
  BLUECAD candidate rows.
- `paper_search_tool.py`, `browser_page_tool.py` already refuse S2–S4 queries via `check_sensitivity_floor`;
  `scripts/search_guard.py` (spec 364) is the guarded search proxy.
- The pack's brief draft has a nucleus section and modules M1–M7, each a paragraph that starts with `**M<n> — `.

Differences from the directive, kept to its intent:

- "Calc runs … in the workspace DB": calc outputs are not stored, so the taint check reads this turn's tool outputs
  (which include every calc result of the turn) plus the stored tables above.
- "Reuse the derivative / `cloud_escalations` provenance": those tables enforce S0/S1, which the amendment keeps for every
  other route. The consult ledger records the same vocabulary (`sanitizer_kind = "model_local"`, approval `operator` or
  `policy_auto`) in its own table instead of loosening them.
- §4.3 exchanges (consultant questions answered through the gate) and §4.4 fallback need the Sage path: they move to the
  step-2 spec. In this spec the consultant's questions appear in its answer and Inkling may open a new consult.

## Scope

1. **Zone and mode config** (new `backend/app/modules/consult/config.py`). Read `consult.toml` next to `vault.toml`
   in `JARVISOS_VAULT_PATH` (the vault is maintainer-only and never mounted into a sandbox):
   - `consult_mode = "off" | "ask" | "auto"`; missing vault, missing file, a parse error or an unknown value is `off`;
   - `brief_sha256` (the maintainer's approval of the brief, see 2);
   - `[[trusted_accounts]]` with `id`, `agent` (a key of `relay_gateway.json` `agents`), `model` and
     `training_off_verified_on` (ISO date). An entry without a valid date is zone C and is never used for a consult.
   No API route, tool or model output writes this file. `GET /ai/consult/status` returns the mode, whether the brief is
   approved, and the trusted account ids with their dates; nothing else.
2. **Brief** (`consult/brief.py`). The pack manifest gains an optional `[consult]` table: `brief` (path relative to the
   pack) and `nucleus_heading` (the level-2 heading of the nucleus section). The nucleus is that section up to the next
   level-2 heading; a module is a paragraph block starting with `**M<n> — ` up to the next module or level-2 heading.
   The brief is approved only while `sha256(file bytes) == brief_sha256`; any edit to the file disables consults until
   the maintainer writes the new digest. Without a pack brief consults are `off`. The core holds no brief text.
3. **Tool** `jarvis_consult(kind, question, modules, context, local_only=false)`, capability `jarvis.consult`:
   - `kind` in `ideas | physics | literature | critique`; `question` ≤ 1,500 characters; `modules` ≤ 2 ids present in the
     approved brief; `context` ≤ 12,000 characters; no other argument (no attachments);
   - `local_only=true` returns `sage_unavailable` (the Sage path is step 2) with a plain reason, and Inkling answers itself;
   - at most 3 consults per turn (loop guard), counted on the turn, refused after that;
   - a companion `jarvis_consult_read(id, offset)` returns a stored answer in chunks of about 1,500 tokens.
   The packet is built by code from: the approved nucleus and chosen modules, a fixed server template per `kind` (in
   `consult/templates.py`), and the question and context fields. Tool description and the system prompt line: "Consult
   the expert for ideas, mechanisms, physical explanations, literature and critique. Every number comes from Jarvis
   tools."
4. **Zone-A gate** (`consult/gate.py`, deterministic, fail-closed, in this order, over the packet without the brief):
   1. `screen_outbound_text` S4 plus a personal-identity pattern check (Italian tax code, birth date, street address,
      phone, ID-document numbers, e-mail). A hit is `refused` and never retried.
   2. **Number taint.** Extract every number with its unit. Compare each with the project's specific numbers: this
      turn's tool outputs, the stored tables listed in the code facts for the current workspace, and pack plus vault
      `protected_dimensions`. A match within 2 % relative (exact for integers that are counts) is a hit. Physical
      constants from a fixed list in `gate.py`, and numbers Inkling tags as literature with a source
      (`<number> <unit> {lit:<source>}`), pass even when they match, and are logged. A hit returns
      `rewrite_required` with the offending numbers and the rule "write an order of magnitude or a range spanning at
      least ×2".
   3. **No files, no verbatim output.** Refuse paths under the vault, the data root or the pack, CAD/mesh/CFD file
      names (`.step .stp .stl .3mf .iges .igs .fcstd .msh .vtk .vtu .foam .inp .obj .glb`), and any span longer than 200
      characters (whitespace-normalised) that also occurs in this turn's tool outputs or in text files of the vault
      outside its data root (rolling-hash shingle index, cached by path, size and modification time).
   4. At most 2 rewrites per consult; after that `sage_unavailable` (step 2 routes it to Sage).
   5. **Mode.** `off`: refused `consult_off`. `ask`: the attempt is stored `awaiting_approval` and the tool returns
      `pending` with the consult id; Inkling ends its answer with `{{consult:<id>}}`. `auto`: sent at once; one attempt
      in five (chosen from the packet hash) is marked for the maintainer's audit list.
5. **Approval** (`ask`). The thread shows a pending-consult card with the exact packet (brief version, modules, template,
   question, context), and three buttons: Approva, Modifica, Rifiuta. An edit re-runs gate steps 1–3 on the edited
   text; approval binds the exact packet digest (the `escalate_with_relay` pattern: `text_digest`, replay guard).
6. **Transport.** An approved packet goes through `run_ai_task` (one `ai_jobs` row, invariant 2) with an explicit
   route class `consult_zone_a` (never `auto`, invariant 1) bound to a provider binding for the trusted account, whose
   adapter starts one Relay session:
   - a fresh empty working directory, no repository, no derivative workspace, no context releases, no `jarvis-actions`
     ingestion, a fresh agent home holding only the agent's credentials (no memory of earlier consults);
   - highest reasoning effort, a 5-minute timeout, output capped at 24,000 characters;
   - built-in web tools disabled; one MCP server from Jarvis with `web_search`, `paper_search` and `read_page` only,
     each through the existing search guard and sensitivity floor; if the sandbox cannot reach that server the session
     runs with no tools at all, never with built-in ones;
   - egress limited to the agent's own hosts (no `common_egress`);
   - accounting basis `subscription`, cost 0. The egress level rule admits S2/S3 content only on this route and only for
     a packet whose digest matches a consult attempt that passed the gate; every other route keeps S0/S1; S4 nowhere.
7. **Answer and verbatim insertion.** The answer is stored as a `consult_answer` with its sha256. When the turn text is
   rendered, `{{consult:<id>}}` becomes a block labelled "<Modello> dice · <ora>" holding the full answer unchanged and a
   collapsible "domanda inviata" with the exact packet; the block is appended when the marker is missing; a pending
   consult shows the approval card in its place. A byte-identity check compares the rendered answer with the stored
   bytes and, on mismatch, shows only the stored answer by id. Inkling receives an answer (in the turn for `auto`, at the
   next turn for `ask`) wrapped as untrusted data, at most about 1,500 tokens, with its id for `jarvis_consult_read`.
   The answer never triggers an outbound call, a setting change or a write; numbers from it carry the existing
   "no tool" provenance badge.
8. **Ledger** (migration `0038_consult_ledger`): append-only `consult_attempts` (every attempt, sent or not: packet and
   its hash, brief digest and modules, each gate step's result and reason, rewrite count, mode, account and model,
   `sanitizer_kind = "model_local"`, approval `operator` or `policy_auto`, audit flag, times, `ai_job_id`) and
   `consult_answers` (attempt id, answer text and hash, model, time). Rows are never updated except an attempt's state
   (`awaiting_approval → approved | rejected | sent | failed`).
9. **Settings.** Settings > AI gains a read-only "Consulente" card: mode, brief approved yes/no with its current digest,
   trusted accounts with their training-off date, and in `auto` the audit list. It states in plain Italian what the
   maintainer must do (write `consult.toml` in the vault) and links the runbook.
10. **ADR and docs.** ADR-064 in `docs/DECISIONS.md` amends ADR-059 for zone A as the directive states (indicative
    S2/S3 to zone A through the consult path only; specific content local; S4 blocked everywhere), citing the directive.
    `docs/RUNBOOKS.md` gains the `consult.toml` steps. `configs/relay_gateway.json` note: "source-available, all rights
    reserved".

Out of scope (binding non-goals):

- The `local_only` judge (§3.4), the Sage path and batching (§6), exchanges and fallback (§4.3–4.4): step 2.
- Zone-C hardening, the mosaic check and the leak benchmark (§5): step 3. Objective triggers (§8): step 4.
- Writing or approving the brief, the trusted-account date or the `local_only` list: the maintainer's.
- Any learned router or classifier deciding whether to consult.

## Files likely touched

- `backend/app/modules/consult/` (new: `module.toml`, `config.py`, `brief.py`, `templates.py`, `gate.py`, `service.py`,
  `routes.py`), `backend/app/core/consult_schema.py` (new), `backend/app/core/schema.py`
- `backend/app/core/packs.py` (`[consult]` table), `backend/tests/fixtures/packs/zephyr/` (fake brief)
- `backend/app/modules/ai/execution.py` or the provider binding/egress level check for the `consult_zone_a` route
- `backend/app/modules/relay_gateway/service.py` (consult session mode), `configs/relay_gateway.json`
- `backend/app/modules/agents/hermes/{worker_shim,supervisor,broker_mcp}.py`, a `consult_tool.py`
- `backend/app/modules/ai/thread_service.py` (marker rendering), `frontend/src/` chat card and Settings card,
  `frontend/tests/`, `frontend/src/api/generated` (regenerated)
- `docs/DECISIONS.md`, `docs/RUNBOOKS.md`, `backend/tests/test_consult_380*.py`, `backend/tests/test_migration_discipline.py`,
  an `e2e/` walk

## Delivery

Two PRs are allowed, each green and reviewed on its own: (a) config, brief, gate, ledger, the tool and `ask`/`auto` with a
fake transport; (b) the Relay consult session with the guarded MCP, the `run_ai_task` route, rendering and the UI.
Until (b) merges, `consult_mode` other than `off` refuses with `transport_unavailable`.

## Design constraints

- Code never writes consult text: the harness assembles fixed parts and Inkling's fields, and passes or blocks.
- Every check fails closed; an exception in the gate is a refusal with code `gate_error`.
- Tests use the fake `zephyr` pack, a fake vault and a fake Relay transport; CI never reaches a provider.
- The ledger stores the packet but never an S4 hit: a refused S4 attempt stores the reason and the hash only.
- No new dependency.

## Acceptance criteria

1. With no vault, no `consult.toml`, an unknown mode, an unapproved or edited brief, or a trusted account without a
   date, `jarvis_consult` refuses with the matching code and nothing is sent.
2. A packet containing a number from this turn's tool output, from a stored parameter or result, or from
   `protected_dimensions` (within 2 %) returns `rewrite_required` naming it; the same packet with the value as a ×2
   range passes; a tagged literature value and a listed constant pass and are logged.
3. An S4 secret or personal-identity pattern refuses without retry and stores only hash and reason.
4. A vault path, a CAD/mesh file name, or a 201-character span copied from a tool output or vault text file refuses.
5. After 2 rewrites, and for `local_only=true`, the result is `sage_unavailable`; a fourth consult in one turn refuses.
6. In `ask` the attempt waits; approval of an edited packet re-runs the gate; a digest mismatch refuses; rejection sends
   nothing. In `auto` the packet is sent and one in five is flagged for audit.
7. A sent consult creates one `ai_jobs` row on route `consult_zone_a`, a Relay session with an empty workspace, no
   repository, no built-in web tools and only the guarded MCP tools; other routes still refuse S2/S3 content.
8. The stored answer is rendered byte-identical in the block, with "domanda inviata" showing the exact packet; the
   marker is replaced or the block appended; Inkling receives it wrapped as untrusted data within the size limit.
9. Every attempt, sent or not, has a `consult_attempts` row with each gate step's result.
10. Lint, focused tests, the frontend build, the contract and migration gates, and CI pass; a browser walk from a clean
    profile shows the Settings card and a pending consult approved into an answer block (fake transport).

## Required tests

- `backend/tests/test_consult_380_gate.py`: each gate step and its order, tolerance edges, literature tags, constants,
  shingle spans, rewrite cap, loop guard.
- `backend/tests/test_consult_380_flow.py`: config and brief states, `ask`/`auto`, approval and edit, replay and digest
  checks, ledger rows, `ai_jobs` row, fake Relay session manifest (no repository, no built-in tools, egress), egress
  level rule for other routes, marker rendering and byte identity.
- `backend/tests/test_migration_discipline.py` digest and baseline for `0038_consult_ledger`.
- Frontend `.mjs` tests for the card labels; an `e2e/` walk over Settings and a pending consult.

## Definition of done

Merged with green CI on the exact head after one severe coordinator review and an independent second review of the full
diff (high risk: egress), the browser walk evidence attached, and a host proof: on a temporary data root and a fake vault
with `consult_mode = "ask"`, one consult is drafted, approved and answered through a real Relay session on the
subscription, and its ledger row, `ai_jobs` row and answer block are inspected. Activation on the live app waits for the
maintainer's three items: the training-off date, the brief approval and the `local_only` list.
