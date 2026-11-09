# Architecture Decision Records

## ADR-001: Use FastAPI Backend

Status: Accepted

JarvisOS uses FastAPI for the backend because it is lightweight, typed, Python-native, and fits future engineering workflows that will rely on Python services and model execution.

## ADR-002: Use React + Vite + TypeScript Frontend

Status: Accepted

JarvisOS uses React, Vite, and TypeScript for the frontend. This keeps the local UI fast to develop while preserving type safety and a clean path to richer workflow screens later.

## ADR-003: Use SQLite Initially

Status: Accepted

JarvisOS starts with SQLite because the product is local-first and Windows-first. The database layer must remain small and avoid SQLite-only assumptions that would block a future PostgreSQL migration.

## ADR-004: Use Local Windows Data Root

Status: Accepted

Runtime data defaults to:

```text
C:\JarvisOS
```

The repository may live separately from the data root. Filesystem paths must be centralized through the backend paths layer.

## ADR-005: Route AI Access Through AI Gateway

Status: Accepted

All AI provider access must go through the AI Gateway. No feature module should call OpenAI, Gemini, local models, or future providers directly.

## ADR-006: Treat Old JarvisOS As Reference Only

Status: Accepted

JarvisOS v2/v3 is a clean new codebase. Prior implementations can inform decisions but should not be refactored as the foundation or copied as monolithic patterns.

## ADR-007: Keep V1 Architecture-Strong And Feature-Thin

Status: Accepted

JarvisOS V1 should establish strong architecture before broad features. Early tools must be placed inside the architecture spine and should remain migration-friendly.

## ADR-008: Introduce Minimal Persistent Domain Foundation Before Advanced Tools

Status: Accepted

JarvisOS needs persistent records for workspaces, model specs, assumptions, parameters, runs, decisions, files, links, and events before adding runners or AI workflows. This keeps future tools inside the architecture spine instead of creating isolated utilities.

## ADR-009: Use Migration-Friendly Metadata For Early Rough Records

Status: Accepted

Early engineering records may be incomplete or messy. Core tables include stable IDs, status, timestamps, schema version fields, notes, and raw payload fields where useful so later migrations and cleanup remain possible.

## ADR-010: Keep First APIs Simple And Workflow-Supporting

Status: Accepted

Milestone 0B exposes only the useful early APIs: workspaces, model specs, assumptions, parameters, simulation runs, and decisions. Lower-level entities, links, artifacts, and events remain internal until a concrete workflow needs direct API access.

## ADR-011: AI Calls Must Go Through The AI Gateway Provider Interface

Status: Accepted

All AI-assisted behavior must pass through `app/modules/ai/gateway.py`. Routes, modeling services, frontend helpers, and future utilities must not call paid providers directly.

## ADR-012: First AI Feature Is Structured Modeling Draft, Not Chat

Status: Accepted

The first AI Co-Engineering feature transforms an informal engineering idea into a structured draft for review. It is not a generic chatbot, streaming chat UI, autonomous agent loop, or Python execution workflow.

## ADR-013: Fake Provider Is Required For Tests And No-Cost Development

Status: Accepted

The fake provider is deterministic, requires no API key, spends no money, and is used by default. Backend tests must not require real provider calls.

## ADR-014: Default Monthly API Budget Is 0 USD

Status: Accepted

Paid AI is blocked by default. The user must explicitly change budget/settings before any future paid provider may be considered.

## ADR-015: Budget Guard Must Wrap All Real Provider Calls

Status: Accepted

Before any future real provider call, the gateway must check provider mode, API key availability, paid AI enablement, monthly budget, spend month-to-date, and provider-specific token caps where applicable. If cost is uncertain, the guard must be conservative.

## ADR-016: Scaleway EU Is The First Real Smoke-Test Provider

Status: Accepted

Scaleway EU was selected as the first real-provider smoke candidate because JarvisOS needed an EU-hosted diagnostic path before considering broader provider work. Milestone 0C-B implemented only the boundary and smoke-test controls; the Scaleway provider remained a no-network stub until a later milestone explicitly added real smoke calls. Later D6B/D6C decisions clarify that Scaleway is not the local privacy classifier, local gatekeeper, core router, or required future provider.

## ADR-017: Token Cap Must Stay Below Free-Tier Reference During Smoke Testing

Status: Accepted

Scaleway smoke tests default to a `500000` monthly token cap and an `800000` hard stop against a `1000000` token free-tier reference. This keeps smoke testing conservative and forces the gateway to prove cap behavior before any real provider usage is introduced.

## ADR-018: JarvisOS Enforces Routing Locally

Status: Accepted

Providers may recommend privacy or sensitivity classifications in future milestones, but they cannot authorize their own unrestricted access. JarvisOS uses local policy to decide whether content may be sent externally. In the current early-stage `FAST_DEV` mode, that policy protects structural secrets and provider credentials without broadly blocking normal public/internal technical prompts. Future `STRICT_IP` mode may block `sensitive_ip`, `confidential`, and `unknown` content more aggressively when real proprietary data enters the system.

## ADR-019: Smoke Tests Use Synthetic Data Only

Status: Accepted

AI smoke tests must use synthetic examples rather than real project secrets, proprietary geometry, or sensitive project data. The purpose is to validate gateway controls, token guards, policy decisions, and event logging without exposing actual engineering IP.

## ADR-020: Live Scaleway Calls Are Smoke-Test Only

Status: Accepted

The first real external AI call is limited to manually triggered Scaleway EU smoke tests. It must go through the AI Gateway, require paid AI, explicit Scaleway mode, explicit live smoke enablement, an environment API key, local privacy approval, and token-cap approval. Automated tests mock the provider and must never call Scaleway.

## ADR-021: AI Smoke Console Is A Guarded Smoke Surface, Not Chat

Status: Accepted

Milestone 0C-D adds a narrow manual AI Smoke Console for short harmless live Scaleway checks such as greetings. The console must use the existing AI Gateway, local privacy policy, token guard, and Scaleway provider boundary. It must not add conversation history, memory, RAG, agents, arbitrary system prompts, project modeling behavior, or Python execution. The console uses existing monthly Scaleway token counters and displays a fixed `500000` token smoke threshold for operator visibility.

## ADR-022: Python Runner V0 Is Explicit Local Execution For Reviewed Scripts

Status: Accepted

Milestone 0D-B introduces a minimal local Python Runner for reviewed deterministic scripts only. `model_versions` acts as the first ModelImplementation record, `simulation_runs` remains the canonical run record, and runner-specific metadata lives in small `runner_jobs`, `run_logs`, and `run_artifacts` tables. Execution is synchronous and explicit through a run endpoint; creating a job does not execute code. V0 uses no shell invocation, no inherited secret environment, controlled data-root paths, timeout and output limits, script hashing, and simple preflight checks, but it is not a hostile-code sandbox.

## ADR-023: Provider-neutral AI Contracts Before New Providers

Status: Accepted

Milestone 0E-D1 adds provider-neutral AI contracts before integrating additional providers. JarvisOS should expose one future Supervisor AI interface while provider/model selection remains internal, policy-driven, gated, and audited. The new contracts define provider, model, task, request, response, usage, routing, gate, authority, and registry shapes only. They do not add OpenAI, Anthropic, DeepSeek, Ollama, or any other provider, and they do not change the existing Scaleway smoke-test behavior.

## ADR-024: Scaleway Adapter Migrates First Without Expanding Provider Scope

Status: Accepted

Milestone 0E-D2 wraps the existing Scaleway live smoke-test implementation behind the provider-neutral `AIProviderAdapter` contract. The adapter maps `AIRequest` into the existing Scaleway smoke-call boundary and maps the sanitized provider result into `AIResponse` and `AIUsage`. Key resolution, local gates, smoke-only scope, monthly token counters, and no-network automated tests remain unchanged. This is not provider routing, dynamic model discovery, Supervisor AI, or the start of OpenAI, Anthropic, DeepSeek, Ollama, or other provider support.

## ADR-025: Default AI Policy Mode Is FAST_DEV

Status: Accepted

JarvisOS currently contains mostly public physics, generic code, toy models, architecture notes, and non-proprietary design exploration. The default AI policy mode is therefore `FAST_DEV`: it allows ordinary public/internal technical prompts through approved AI paths while preserving budget, token, provider, credential, event-redaction, and no-plaintext-secret boundaries. Broad keyword blocking for terms such as `patent`, `geometry`, a project name, `Quick Coupler`, or `confidential` is intentionally not used in `FAST_DEV`. `STRICT_IP` remains a future mode for stricter classification and deterministic AuthorityPolicy behavior once real proprietary IP is present.

## ADR-026: Add DeepSeek As One Strong Smoke-only Provider

Status: Accepted

Milestone 0E-D4 adds exactly one strong provider adapter: DeepSeek through an OpenAI-compatible chat-completions request shape. The adapter is env-var-only through `DEEPSEEK_API_KEY`, supports only the narrow provider-smoke path, and does not add provider routing, Supervisor AI, project modeling, or a provider-specific bot UI. This validates the provider-neutral adapter boundary without expanding product scope. Automated tests mock the provider and must not call the network.

## ADR-027: First Supervisor AI Endpoint Is Public/Internal And Narrow

Status: Accepted

Milestone 0E-D5 adds `POST /ai/supervisor/public-test` as the first Supervisor AI vertical slice. It is backend-only, runs only in `FAST_DEV`, accepts bounded public/internal technical prompts, does not accept provider/model selection, and records redacted provider-neutral events. Temporary provider selection prefers the configured DeepSeek adapter and falls back to Scaleway only when explicitly configured for live smoke. This is not chat, routing, project modeling, file ingestion, runner execution, or an agent framework.

## ADR-028: Supervisor AI Uses Logical Provider Tiers, Not Provider Bots

Status: Accepted, refined by ADR-029

JarvisOS should evolve toward one stable user-facing Supervisor AI backed by logical provider tiers: `cheap`, `medium`, and `frontier`, with optional future local/offline handling. Users should not choose provider-specific bots such as DeepSeek, Grok, GPT, Gemini, Claude, or Scaleway in normal workflows. Provider/model details, tier assignments, fallback chains, credentials, and diagnostic smoke targets remain internal or admin/config-only. The current `provider_mode` field remains a compatibility control for existing smoke paths, but future Supervisor routing should use tier assignments and auditable route plans. ADR-029 corrects the ordering: local gate contracts must come before external tier contracts.

## ADR-029: Local Gatekeeper Comes Before External Provider Tiers

Status: Accepted, refined by ADR-030

The component deciding whether raw user input is safe to send externally must run locally. JarvisOS must not use Scaleway, DeepSeek, Grok, Gemini, GPT, Claude, or any other cloud provider as the first sensitivity classifier. Future Supervisor flow starts with a Local Gatekeeper that applies deterministic hard rules and, later, may use a local Gemma classifier. The gatekeeper emits a logical gate decision such as `LOCAL_ONLY`, `LOCAL_GEMMA`, `USER_CONFIRM_REQUIRED`, `CHEAP_GATE`, `CHEAP_PLUS_GATE`, `SCIENTIFIC_MEDIUM_GATE`, `FRONTIER_GATE`, or `BLOCKED`. Only after that decision may an allowed request reach an external provider adapter. Scaleway remains a smoke/simulation provider and adapter example, not the privacy classifier or core router. ADR-030 supersedes the original contract-first next step; the next milestone is `0E-D7 - Local Gemma Evaluation Harness and Golden Set`.

## ADR-030: Evaluate Local Gemma Foundation Before Gatekeeper Contracts

Status: Accepted

Before Gemma can support local gatekeeping, JarvisOS must prove that local Gemma can use context, memory, deterministic tool outputs, and structured output schemas correctly. A weak previous Jarvis/Gemma experience should not be attributed to the model alone until context packing, retrieval, tool contracts, memory design, orchestration, and evaluation are tested. The next milestone should be `0E-D7 - Local Gemma Evaluation Harness and Golden Set`, with no Gemma runtime, no routing, no external APIs, no chat UI, and no memory runtime. Local gate and external tier contracts should follow only after the evaluation foundation defines quality bars and failure categories.

## ADR-031: Local Gemma Evaluation Harness Comes Before Runtime Adapter

Status: Accepted

Milestone 0E-D7 adds a backend-local golden set and deterministic scoring harness for future Gemma evaluation. The harness currently validates 95 synthetic cases across conversation continuity, Codex log summarization, prompt drafting, decision/TODO extraction, sensitivity/complexity classification, local-only note handling, public technical Q&A, retrieval interpretation, tool grounding, hallucination resistance, and schema compliance. It defines a strict `GemmaEvalOutput` schema and critical safety failure rules, but it does not call Gemma, Ollama, llama.cpp, LiteLLM, external providers, or any model server. The next milestone may be `0E-D8 - Local Gemma Runtime Adapter Dry Run`, using this harness as the boundary.

## ADR-032: Local Operating Brain Must Request Bounded Context

Status: Accepted

Milestone 0E-D7B extends the D7 harness so future local Gemma is evaluated as a local operating brain, not only a model answering from already supplied context. Gemma should be able to identify missing context, request bounded context packages from a controlled vocabulary, interpret partial context, ask for more context when needed, and avoid hallucinating file/tool/database results. The schema adds state, context sufficiency, requested context packages, allowed/forbidden tool requests, external prompt, and external-call intent fields. External calls remain non-executable in this harness; `external_call_requested` must remain false unless a later explicit runtime/policy milestone permits execution.

## ADR-033: Harden Local Gemma Evaluation Before Runtime

Status: Accepted

Milestone 0E-D7C reviews and hardens the D7/D7B evaluation harness before any real Gemma runtime is connected. The harness must reject duplicate or invalid golden cases, require missing-context rationale for context-request states, require expected forbidden tool requests to be explicitly marked as forbidden, reject invalid prose or JSON-like output as schema-invalid, and treat premature external prompts/calls as critical failures. D8 may run a local Gemma adapter only as a dry-run producer of schema outputs for this scorer; it must not add chat, tool execution, memory runtime, retrieval runtime, local gate enforcement, routing, or external provider calls.

## ADR-034: D8 Local Gemma Runtime Is Evaluation-Only

Status: Accepted

Milestone 0E-D8 adds a local Gemma runtime adapter dry run that can call only explicitly configured localhost OpenAI-compatible endpoints. It requires no API key and rejects non-local URLs, HTTPS URLs, credentials in URLs, private LAN IPs, and external domains. The adapter may produce only schema outputs for the D7/D7B/D7C deterministic scorer and local report. D8 does not add chat, memory runtime, file/database retrieval, context broker runtime, local gatekeeper enforcement, provider routing, external APIs, frontend UI, autonomous tools, or project modeling.

## ADR-035: D9 Gemma Evaluation Does Not Approve Operating-Brain Use

Status: Accepted

Milestone 0E-D9 ran the first local Gemma evaluation through the D8 adapter and D7 scorer. `gemma4:12b-it-qat` completed only the Stage 1 smoke subset and produced zero schema-valid outputs. `gemma4:31b-it-qat` did not complete the Stage 1 subset and timed out on a single-case probe under the current local setup. These results do not approve Gemma for local chat, memory runtime, context broker runtime, local gatekeeping, provider routing, autonomous tools, or project modeling. Any follow-up must remain evaluation-only and focus on local runtime latency, prompt/protocol simplification, schema simplification, and JSON compliance before Gemma can be reconsidered as a local operating-brain candidate.

## ADR-036: D9R Requires Staged Local Gemma Schemas Before Full D7 Output

Status: Accepted

Milestone 0E-D9R diagnosed the D9 failure with local-only probes. Both `gemma4:12b-it-qat` and `gemma4:31b-it-qat` can emit tiny direct JSON objects. `gemma4:12b-it-qat` still failed a compact operating-brain schema in both OpenAI-compatible and native Ollama modes. `gemma4:31b-it-qat` passed the compact schema only through native Ollama structured output, but timed out on a full D7 one-case probe even with native schema output and a simplified prompt. JarvisOS should not proceed to full local operating-brain behavior. The next local-AI milestone, if pursued, must remain evaluation-only and use staged schemas and protocol simplification before reconsidering the full `GemmaEvalOutput` contract.

## ADR-037: Local Gemma Uses Micro-Contracts Before Runtime Orchestration

Status: Accepted

Milestone 0E-D10A replaces the first-runtime-contract strategy for local Gemma. Instead of asking Gemma for one large `GemmaEvalOutput`, JarvisOS should evaluate small, independent micro-contracts such as task classification, context request, sensitivity check, tool-call proposal, external prompt draft, TODO extraction, decision extraction, and evidence selection. Gemma may propose structured objects only; JarvisOS remains responsible for validation, policy, execution, persistence, audit, retrieval, memory, and external API calls. D10A adds isolated schema models and documentation only. It does not add chat, memory runtime, retrieval runtime, local gatekeeping, provider routing, frontend UI, autonomous tools, or project modeling.

## ADR-038: D10B Micro-Contract Probes Do Not Approve Gemma Runtime Orchestration

Status: Accepted

Milestone 0E-D10B tested `gemma4:31b-it-qat` against the D10A micro-contracts using local Ollama native structured output and the real Pydantic JSON schemas exported by each contract. All 16 probe cases returned empty content and failed as invalid JSON. This does not approve Gemma for staged local orchestration, local chat, memory runtime, retrieval runtime, local gatekeeping, provider routing, autonomous tools, or project modeling. Because D9R showed that a hand-written compact schema can work, the likely next local-AI diagnostic would be schema-compatibility testing for Pydantic `$defs`/`$ref` versus flattened schemas. Product work should not wait on that if the project Model Foundry progress is more important.

## ADR-039: D10B-R Separates Schema/Budget Issues From Gemma Model Judgement

Status: Accepted

Milestone 0E-D10B-R showed that empty Ollama `message.content` can occur while `message.thinking` is non-empty and `done_reason=length`, so D10B's empty-content failures were not sufficient evidence that Gemma cannot perform micro-contract tasks. With higher `num_predict` and flat/direct schemas, `gemma4:12b-it-qat` passed several lightweight contract probes and `gemma4:31b-it-qat` passed a limited comparison, but 31B remained too slow for routine orchestration. Future local-Gemma evaluation should prefer flat schemas, explicit thinking/output-budget diagnostics, and 12B as the lightweight candidate; 31B should be reserved for occasional heavy local expert probes. This still does not authorize chat, memory runtime, retrieval runtime, context broker runtime, local gatekeeping, provider routing, autonomous tools, frontend UI, or project modeling.

## ADR-040: D10C Limits Local Gemma To Classification-Style Utility Candidates

Status: Accepted

Milestone 0E-D10C replaced the misleading D10B probe path with flat schemas, thinking-aware diagnostics, 12B-first probing, repeatability checks, and a limited 31B comparison. `gemma4:12b-it-qat` passed and repeated only task classification and sensitivity classification; it failed context request, TODO extraction, decision extraction, and evidence selection due to thinking budget exhaustion. `gemma4:31b-it-qat` passed the limited comparison on task classification, context request, and sensitivity check but remained too slow for routine orchestration. JarvisOS should not proceed to local Gemma orchestration, local gatekeeping, memory runtime, retrieval runtime, context broker runtime, chat, provider routing, autonomous tools, frontend UI, or project modeling. If local Gemma work continues, it should be limited to classification-style utility design with JarvisOS policy remaining authoritative.

## ADR-041: 0F Cleanup Requires Source Protection Before Deleting Generated Files

Status: Accepted

Milestone 0F found that the active `JarvisOS` folder was small, but Git initially had no tracked source files. JarvisOS source must be protected by a baseline commit before generated folders such as `backend/.venv`, `frontend/node_modules`, Python caches, frontend build outputs, or local package caches are removed. Cleanup must be explicit and path-validated; broad destructive commands such as `git clean -fd` are not an acceptable first cleanup step while source protection is uncertain.

## ADR-042: Canonical Documentation Hierarchy Replaces Milestone-Doc Sprawl

Status: Accepted

Milestone 0F-E establishes the canonical documentation hierarchy: `docs/ARCHITECTURE.md` for stable architecture, `docs/DECISIONS.md` for durable ADRs, `docs/RUNBOOKS.md` for current operating instructions, `README.md` for top-level orientation, and the local Gemma evidence (not carried into this repository). Milestone docs remain historical evidence and must not override current canonical docs. D10C remains the current local Gemma conclusion: 12B is a classification-only utility candidate, 31B is an occasional heavy expert candidate, and project modeling remains paused until AI infrastructure, external API escalation, and Modeling Workbench support are ready.

## ADR-043: Classification Utility Requires Budget Diagnostics Before Broader Reliability Work

Status: Accepted

Milestone 1A added a backend-only classification utility for `gemma4:12b-it-qat`. Milestone 1B hardens that utility with explicit prompt/input/output budget policy, local-adapter latency and thinking diagnostics, schema/fallback metadata, and structured failure mapping. This remains a local utility only: no route, frontend UI, chat, provider routing, local gatekeeper runtime, memory runtime, retrieval runtime, Context Pack Broker runtime, autonomous tool path, or project modeling behavior is added. Gemma may propose labels, JarvisOS validates their structure, applies hard overrides, and JarvisOS decides. A manual live budget probe is deferred to a separate 1B-R diagnostic milestone so automated tests never call Ollama and 1B does not silently expand output budgets beyond the fixed classification policy.

## ADR-044: Form-Driven Local Intelligence Replaces Deterministic Semantic Claims

Status: Accepted

JarvisOS should be described as deterministic structure around a local semantic brain, not as a system that deeply understands Gemma outputs. Gemma performs semantic reasoning locally inside bounded forms and protocols. JarvisOS validates structure only: schemas, required fields, allowed enum values, path rules, source IDs, permissions, transitions, retries, persistence, promotion policy, and audit. JarvisOS must not claim that structural validity proves semantic fidelity, strategic correctness, summary quality, technical truth, memory completeness, or subtle sensitivity correctness. Future local AI work should use Gemma-facing showcase files and form protocols such as classification, context access, memory cards, source cards, decision cards, sensitivity assessments, tool intent, provider intent, and clarification requests. Tool and provider forms express intent only; JarvisOS constructs, blocks, or escalates concrete execution according to explicit policy. The next roadmap step remains the manual 1B-R-LIVE classification probe, followed by analysis and form/protocol design work before memory runtime, retrieval runtime, Context Pack Broker runtime, provider routing, tool execution, Workbench, Foundry, or Debate Mode work.

## ADR-045: Memory Intake Is Fast, Staged, And Enriched Later

Status: Accepted

JarvisOS memory ingestion must be computationally cheap at write time. The 1C-W and 1C-X classification diagnostics showed that local models can emit structurally valid JSON, but one-shot semantic agreement remains too weak for fine-grained memory classification. Initial memory intake should preserve raw text, source/input ID, timestamp, conversation/project reference when available, observable boolean signals, broad uncertain buckets, uncertainty flags, and enrichment status. `FastIntakeSignalForm` is a cheap intake envelope, not canonical truth and not a final `MemoryCard`, `KnowledgeCard`, `DecisionCard`, `AssumptionCard`, `EvidenceCard`, or `SourceCard`. Full contextual interpretation should be deferred until retrieval, decision use, conflict resolution, high-value promotion, sensitivity review, or full context-pack availability. JarvisOS owns validation, persistence, promotion, execution, audit, and policy; model output remains advisory and cannot authorize memory promotion, retrieval access, provider calls, tool execution, route selection, safety decisions, or the project assumption acceptance.

## ADR-046: Audit External Memory References Before 1D

Status: Accepted

Before the 1D design sequence, JarvisOS audited Cavemem and Caveman as external
reference implementations for memory write boundaries, compact retrieval,
compression safety, raw/original retention, hooks, workers, MCP, and viewer
patterns. The audit is accepted as design evidence only. Cavemem/Caveman code
is not vendored into JarvisOS in this milestone, and no runtime memory,
retrieval, compression, MCP server, hooks, worker, viewer, routes, frontend UI,
provider integrations, Context Pack Broker runtime, local gatekeeper runtime, or
model authority is added. JarvisOS will adapt useful patterns with
Python-native implementation unless a specific licensed snippet is explicitly
approved later with MIT license and copyright notices preserved.

## ADR-047: Local-model-facing showcase files are non-authoritative regenerable views

Status: Accepted

Local-model-facing showcase files are orientation and index artifacts. They are
synthetic, non-authoritative, regenerable views over canonical sources such as
`README.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, and source files.

Canonical docs and source files remain the source of truth. Showcase files help
local models orient themselves before requesting bounded source files or context
packs, but they cannot authorize runtime action, memory promotion, retrieval,
provider calls, tool calls, safety decisions, or project assumptions. They also
cannot make Gemma, Qwen, or any model runtime-approved.

## ADR-048: Micro-context is bounded orientation context, not memory or retrieval

Status: Accepted

Micro-context is a small bounded orientation payload for local-model-facing
work. It may support future fast intake and form filling by providing compact
workspace, project, milestone, decision, clarification, and safety context.

Micro-context is non-authoritative. Canonical docs and source files remain the
source of truth. Micro-context cannot authorize runtime action, memory
promotion, retrieval, provider calls, tool calls, routing, safety decisions, or
project assumptions. Full context packs remain separate and heavier.

Runtime generation and loading are deferred. Cavemem-inspired hooks/events are
future triggers only and must not write micro-context or memory directly. Any
future hook/event implementation must pass through controlled boundaries after
MemoryStore facade, storage schema, policy, scope, retention, and tests exist.

## ADR-049: MemoryStore facade is the single future memory write boundary

Status: Accepted

Future durable memory writes must pass through a `MemoryStore` facade. Models,
hooks, routes, workers, tools, providers, and UI must not write durable memory
directly.

MemoryStore owns future validation, source links, staged state transitions,
raw/original retention references, hard policy overrides, and audit records.
Model output remains advisory and cannot authorize memory writes, promotion,
final sensitivity, retrieval, canonical state changes, or project assumptions.

This milestone is design-only. It adds no MemoryStore runtime, database schema,
retrieval, hooks, worker, MCP, viewer, compression, provider calls, tool
execution, or project modeling. Cavemem is an architectural reference only, not
vendored code.

## ADR-050: Compression requires token-preservation tests before runtime use

Status: Accepted

Compression is optional and later. Raw/original evidence must survive, and
compressed text remains non-authoritative.

Before any runtime compression is allowed, JarvisOS must prove protected
technical tokens are preserved, including code, paths, URLs, DOIs, commit
hashes, versions, commands, formulas, numbers, units, enum values, source IDs,
artifact IDs, table values, chemical identifiers, engineering identifiers, and
project material/geometry/process terms.

Sensitive or secret inputs must be refused or gated before compression.
External compression providers are not approved for JarvisOS memory. This
milestone adds no compression runtime.

## ADR-051: Staged memory schema uses SQLite/FTS behind MemoryStore

Status: Accepted

Future SQLite/FTS memory tables sit behind MemoryStore. MemoryStore remains the
write boundary for staged memory records, source links, raw/original body
references, promotion, supersession, and audit.

FTS is for compact scoped candidate retrieval, not full evidence authority.
Raw/original body references remain required, and secret or sensitive content
must not be blindly indexed.

This milestone adds no migration, table, runtime query, retrieval API, memory
runtime, compression runtime, provider calls, tool execution, hooks, MCP,
worker, viewer, or project modeling.

## ADR-052: Progressive retrieval is scoped candidate discovery before full evidence

Status: Accepted

Progressive retrieval starts from orientation, then scoped compact candidates,
then full body by stable ID or source reference. Retrieval output is
non-authoritative until grounded in full evidence and source provenance.

FTS snippets, compressed text, showcase files, and micro-context cannot
authorize decisions. Retrieval cannot authorize memory promotion, provider
calls, tool calls, route selection, final sensitivity, project assumptions, or
canonical state.

Default retrieval source classes are conservative. `raw_input`,
`proposed_memory`, and `superseded` records are review-only retrieval targets
and require explicit purpose, scope, sensitivity checks, and audit.

Models may propose retrieval requests but cannot query storage or authorize
full-body access directly. External providers and tools cannot query retrieval
directly.

Context Pack Broker runtime, retrieval APIs, DB queries, memory runtime,
provider calls, and tool execution are not added in this milestone.

## ADR-053: Holdout intake generalization set precedes model testing

Status: Accepted

The holdout intake generalization set is docs/data-only evaluation evidence for
future staged-intake and progressive-retrieval form testing. It precedes model
testing and must remain stable input data rather than a generated or
model-written dataset.

This milestone adds no model calls, scorer, harness, memory runtime, retrieval
runtime, Context Pack Broker runtime, provider/tool execution, routes, APIs,
database migrations, runtime models, storage classes, hooks, MCP, workers,
viewers, project modeling, external reference audit, or vendored code.

## ADR-054: Form protocols separate model proposals from JarvisOS authority

Status: Accepted

Local models fill bounded forms only. Valid form structure does not prove
semantic truth.

JarvisOS owns validation, retry, policy, persistence, promotion, execution,
retrieval gates, provider/tool gates, audit, and final decisions. Model output
remains advisory until JarvisOS policy, source grounding, review, and future
implementation gates decide what may happen.

The form catalog is docs-only. It adds no Pydantic models, runtime validator,
retry loop, model calls, memory runtime, retrieval runtime, Context Pack Broker
runtime, provider calls, tool execution, or project modeling.

## ADR-055: Structural validation and retry loops enforce form structure, not semantic truth

Status: Accepted

Structural validation checks schemas, required fields, enum values, field
lengths, source refs, scope fields, confidence bounds, allowed effects, and
authority constraints.

Structural validation does not prove semantic fidelity, factual truth,
strategic correctness, sensitivity correctness, or project technical validity.

Retry loops are bounded and machine-readable. Retry failures must end in review,
clarification, `not_decided`, or block.

This milestone adds no validator runtime, Pydantic models, scorer, harness,
model calls, memory runtime, retrieval runtime, Context Pack Broker runtime,
provider calls, tool execution, or project modeling.

## ADR-056: Fast secretary structured output should become schema-first before runtime use

Status: Accepted

Qwen fast secretary should not rely only on prompt-only JSON generation for
future approval. The 1G-B2-E full-holdout smoke run improved with
`qwen_hybrid_parse_safe_v0_4`, but still produced 28/32 parse, 4 critical gate
failures, and significant hard-field misses.

Future fast secretary experiments must move toward schema-first structured
output before any runtime use, default queue decision, memory write path,
retrieval gate, provider route, tool route, or safety decision. JSON Schema or
equivalent constrained-output mechanisms should own shape, required fields,
enum values, booleans, and bounded arrays. JarvisOS must still own structural
validation, semantic scoring, policy gates, persistence decisions, promotion,
audit, and manual review.

Structured-output experiments must precede runtime/default queue decisions.
This milestone adds no runtime memory, retrieval runtime, Context Pack Broker
runtime, provider calls, tool execution, backend route, frontend UI, model call,
database migration, vendored structured-output library, or project modeling.

## ADR-057: Model economy — cheap external is the workhorse, local is the privacy fallback

Status: Accepted

"Local-first" in JarvisOS describes where authority and data live (state,
policy, execution records, audit — all local), not which models perform the
work. The intended routing hierarchy at steady state is:

1. Cheap external models (GLM / Kimi / DeepSeek class) carry the majority of
   compute.
2. Frontier models (Opus / GPT frontier class) handle review, strategic
   documents, and hard tasks.
3. Local models serve only the rare cases where redaction of outbound content
   is impossible or ambiguous; such cases fail closed to local, and the system
   should be designed so this path stays rare.

Any earlier phrasing implying "external APIs are only for review" or "local
models do most of the work" is superseded by this record. This ADR does not
change execution invariants: external calls remain gated by explicit user
confirmation and deterministic policy (`route_class="auto"` still never
executes external providers), and safe defaults remain safe.

## ADR-058: Digital twin is a rendering consumer of the data spine

Status: Accepted (principles); scene-graph and binding mechanics deliberately deferred

JarvisOS may grow a richer 3D "digital twin" surface (up to a walkable
first-person scene of a project design). This ADR freezes the architectural
principles now so that the data spine and the future twin cannot diverge,
while leaving unproven implementation choices to the owning specs.

Accepted principles:

1. Any twin surface is a view over the data spine. It introduces no second
   store: engineering values live in records and artifacts, never in the
   scene.
2. GLB remains the canonical rendering format (the spec 006 viewer path). No
   second geometry export pipeline.
3. Scene identity and record identity are distinct. Every selectable scene
   component must carry a deterministic, stable `scene_component_id`. A
   binding manifest associates each component with zero or more normalized
   record references `<kind>:<id>`, each with a typed relation (for example
   `represents`, `decided_by`, `validated_by`, `costed_by`). One physical
   component may bind to many records.
4. Accepted records render by default; proposals render only when explicitly
   labeled as proposals. Promotion authority stays in the proposal-review
   surface (spec 054); the twin never promotes.
5. AI changes the twin only by proposing record or GeometrySpec changes
   through the existing gated paths. There is no direct scene-mutation
   channel.

Deliberately deferred to specs 050/052/055 and their follow-ups:

- the exact scene-graph structure, including how the current single-Compound
  GLB export (`backend/app/modules/bluecad/export.py`) evolves to expose
  named, stable components;
- binding transport: glTF `extras` versus a sidecar manifest;
- the relation vocabulary and its cardinality rules;
- FEM/process overlays and stale-state rendering (spec 051 signals).

This ADR adds no runtime, no route, and no dependency. It does not constrain
the 047–049 process writers, which stay unaware of GLB structure:
geometry↔record binding is owned by spec 052, the `<kind>:<id>` resolver by
spec 050, and view assembly by spec 055. Walkable rendering remains
trigger-gated on 047 producing real process data.

## ADR-059: External policy autopilot with sampled sanitization audit

Status: Accepted

ADR-057's model-economy hierarchy remains the intended steady state: cheap
external providers are the normal workhorse after deterministic server-owned
gates, frontier models handle review and hard tasks, and local models remain
the privacy fallback when safe sanitization is not possible.

Hard invariant 1 remains unchanged: `route_class="auto"` is local-only and never
executes an external provider. Policy autopilot applies only after server-owned
policy resolves an explicit external route. Every external attempt, including
each fallback, must use `run_ai_task`, write an `ai_jobs` row, and bind the exact
outbound packet, concrete provider/model, current sensitivity and provenance,
credential availability, projected economic decision, and applicable policy
versions.

An exact packet whose every block is currently effective S0/S1, provenance-bound,
and deterministically secret-free may receive a silent server-owned allow when
no configured confirmation trigger fires. Models, frontend state, and
caller-supplied fields never authorize that allow.

S2/S3/unknown sources may be minimized and sanitized locally. Every model-backed
sanitizer, including local Ollama, must itself run through `run_ai_task` on an
explicit local route and write its own `ai_jobs` row. Sanitizer output remains
advisory: it cannot lower deterministic floors or authorize egress. Raw
S2/S3/unknown content never enters an external packet. A result that remains
above S1 pauses for review or resanitization, or remains local; human
confirmation cannot make it externally eligible.

Raw S4 and any final content that remains secret-bearing or effective S4 are
denied with no override. Consistently with the merged 059a substrate, a current,
provenance-bound, deterministically secret-free effective S0/S1 derivative
originating from an S4-labelled source may remain eligible when all other gates
pass.

Auto-approved derivatives receive deterministic human sampling, defaulting to
5% per calendar week. A sampled rejection revokes the derivative and records a
sanitizer failure. Context minimization is mandatory, including fail-closed
configured limits on both count and serialized size of
S2/S3/unknown-derived blocks in each packet.

Human confirmation is limited to configured trigger families. It cannot
override the monthly hard budget, raw or final S4/secrets, missing credentials,
stale provenance, unsupported mechanics, or a final effective level above S1.
The maintainer explicitly accepts the residual risk of imperfect sanitization
in exchange for prototype velocity.

This ADR supersedes only ADR-057's sentence requiring explicit user confirmation
for every external call. It preserves ADR-057's model-economy hierarchy and all
server-owned execution, audit, safe-default, and model-advisory invariants.

## ADR-060: Adopt pinned Hermes as the swappable operational orchestrator

Status: Accepted

JarvisOS adopts a pinned, fingerprinted build of NousResearch Hermes Agent as its
operational agent-loop engine. The decision follows the principle:

> buy the orchestrator; build the domain and the authority.

Hermes owns the replaceable conversation loop, session surface, bounded subagent
delegation, and tool orchestration. JarvisOS retains exclusive ownership of
canonical state, domain services, sensitivity, route policy, provider
credentials, projected and actual budget, egress decisions, execution ledger,
proposal validation, promotion, and final authority.

Integration is standards-only:

1. Hermes reaches models through a local OpenAI-compatible JarvisOS passthrough
   whose implementation remains inside `run_ai_task`.
2. Hermes reaches JarvisOS domain operations through bounded MCP tools that call
   existing services.
3. JarvisOS does not import Hermes internals, write Hermes databases, depend on
   private Python modules, or expose its repository, SQLite files, or data root to
   Hermes.
4. Hermes does not call JarvisOS adapters, storage, runners, or services except
   through those documented interfaces.

The standards-only boundary is a replacement and rollback requirement. A future
orchestrator may replace Hermes if it satisfies the same tested API/MCP contracts.
Hermes upgrades require a new immutable identity, fingerprint, effective-config
preflight, compatibility evidence, and explicit maintainer decision.

Hard authority lines:

- Provider API keys and provider OAuth credentials exist only in JarvisOS.
  Hermes receives at most scoped local credentials for the passthrough and MCP
  endpoints.
- Hermes' internal memory, skills, summaries, and session state are episodic and
  disposable. Canonical engineering truth enters SQLite only through JarvisOS
  proposal and promotion paths.
- Hermes is never an authority for promotion, sensitivity, route permission,
  egress, budget, or accepted engineering state.
- Model switching, personas, and subagents may select only policy aliases offered
  by the JarvisOS passthrough. They cannot create or widen provider permission.
- Every AI provider attempt remains gated and ledgered by JarvisOS. Every domain
  mutation remains a proposal or an existing bounded job.
- The ten `AGENTS.md` invariants remain unchanged. The passthrough preserves the
  invariant that all AI calls traverse `run_ai_task` and write `ai_jobs`.
- Merged spec 059a remains unchanged. External passthrough execution is blocked
  until 059b is merged and active.

Security consequences:

- A Hermes configuration file is not treated as a sandbox. The production profile
  requires host-level workspace isolation, no direct provider egress, no inherited
  provider credentials, and no access to the JarvisOS repository or data root.
- Browser, web search, computer use, cron, proactive triggers, messaging, broad
  terminal/filesystem access, MCP sampling, and unrelated MCP servers start
  disabled.
- Delegation receives explicit depth, concurrency, iteration, timeout, tool/model
  call, and economic bounds. JarvisOS remains the enforcing economic authority.
- MCP outputs retain server-verifiable provenance so model-visible text cannot
  self-declare sensitivity or authority.

Roadmap consequences:

- Spec 060 is re-scoped as the definition-only Hermes integration umbrella.
- `HERMES-PASSTHROUGH-0`, `JARVIS-MCP-0`, and `HERMES-CONFIG-0` are its
  implementation-owning slices.
- Specs 030, 031, 034, and 036 are superseded in part. Hermes supplies the
  conversation engine, persona/subagent execution, and multi-agent interaction.
  Thin JarvisOS residual contracts remain for proposal/approval handoff,
  capability-vocabulary truth, policy-owned persona metadata, and honest
  authority/status presentation.
- The model-bench repository qualifies local models for privacy, offline, and
  bounded background lanes. External-model orchestration is governed by the
  JarvisOS passthrough and is not qualified by that benchmark.
- `MEMORY-CONSOLIDATE-0` is the first Hermes dogfood job and remains
  proposal-only.

This ADR authorizes specification and compatibility work only. It does not
activate Hermes, add runtime routes, enable external execution, modify 059a,
install dependencies, expose MCP, or make any registry row implementation-ready.

## ADR-061: One observe/ground/teach/render-watch/act contract for guidance (224) and computer use (225)

Status: Accepted (2026-10-29, Frontier Coordinator; proposed in #992). This records the choice required by the
#962 mandate, step 4. Acceptance does not make guidance (224) or computer use (225) ready. The only readiness it
backs is the owner-surface slice (not carried into this repository), which builds gate 1's owner
facts for Design and Process and measures gate 2's owner predicate, with no guidance runtime. Guide UI,
overlay, step registry and all of 225 stay gated by the conditions listed below.

Evidence:

- A prior-art review of an existing commercial computer-use product (not carried into this repository). No code, assets or runtime behaviour of that product are used.
- Jarvis owners and grounding bake-off (not carried into this repository). It ran 39 valid fixtures on real Jarvis screens at exact head `142c4cb6` over an isolated data-root snapshot. The browser issued no writes. Raw evidence is at `/home/user/jarvis-control/work/evidence/068-grounding-real-142c4cb6/`.
- The 2026-10-27 guidance architecture audit (not carried into this repository).

### Measured result

Four candidates resolved the same observations. All of them shared one physical check: exactly one
match that is visible, enabled and topmost.

| Candidate | Expected decisions | Invented pointers | Median / p95 latency |
|---|---|---|---|
| semantic (registered target + owner binding) | 36/39 | 0 | 3.1 / 146 ms |
| hybrid (owner binding, then role/name) | 35/39 | 1 (P31A) | 7.3 / 159 ms |
| accessibility (role/name) | 25/39 | 8 | 18.9 / 145 ms |
| visible text (DOM proxy for OCR) | 23/39 | 3 | 3.9 / 153 ms |

The latencies are in-page DOM queries in headless Chromium. They are not costs of an end-to-end
step. No provider, model, OCR or vision ran, so there is no external cost to report.

The failures fall into five clusters:

1. **Stale state looks valid.** Role/name and text point at a control that survived a change of project, stage, component, Literature project or draft (R04, R08, R16, U40, M27, M28, P31B). Only a comparison with the owner binding refused these cases.
2. **Labels are not identities.** Duplicate project, component, source and candidate labels (R06, R18, M26, B36 tree) can be resolved only by an owner id. Where the screen shows none, refusing is the only safe answer. Mesh names are not part ids (`mesh_5` → `bend_14`).
3. **Pixels contradict the owner while loading.** During R12 the heading and the empty component list showed the previous project. Only the owner's `loading` state was correct. A pixel-first resolver would have taught a false fact.
4. **Owner facts are missing,** so even the owner-bound path fails or refuses for an incidental reason: Process unit targets (P29), draft revision (P31A), field-slice read state (R22), an off-screen state distinct from occlusion (R14), landing-route "not yet" (R07), owner-id targets for options and rows (R06, R18), the reason a control is disabled (U39), a Literature surface (M25–M28), and a BlueCad surface with a part screen anchor (B33, B36).
5. **Order matters inside the hybrid.** Resolving by role/name before the owner id refused a request the owner id could answer (M26R).

### Decision

Guidance (224) and computer use (225) share one contract with five separable slots. They share
perception; they do not share authority.

1. **Observe.** An observation is the owner-published surface: route, workspace, version, stage, selection, panel states and owner revisions. It has an epoch. Screenshots and accessibility trees are attached as evidence but never define state. Each surface Jarvis guides on must publish its own surface event, and Design, Process, Literature and BlueCad all fall under that rule.
2. **Ground.** The resolver returns one of `locate(target, epoch)`, `wait(owner_reason)`, `offscreen(container)`, or `refuse(reason)`. Its order is fixed:
   1. the current epoch must equal the observed one;
   2. the request's owner id, or a registered target composed with the owner selection, picks the element;
   3. role/name serves only as confirmation;
   4. the shared physical check runs last.
   
   A label, text match or pixel region may confirm a target; it never establishes one. Disagreement refuses.
3. **Teach.** A step is a plan item over owner state: target ref, expected owner predicate, and the reason. It may be generated dynamically for an unseen task (U37) only from targets that exist in the current owner state. A recipe whose target does not exist is refused (U37F), not approximated.
4. **Render/watch.** The first renderer is an in-browser overlay. It binds to the observation epoch and clears with a stated reason when the epoch changes. This is the Jarvis equivalent of a stale-watch on the rendered overlay. A step completes only when its executable owner predicate holds. A click, pixel change, URL substring or the prose in `holds` does not complete a step.
5. **Act (225 only).** A separate broker holds a one-step grant bound to the session, surface, target ref, epoch, action class and expiry. It acts through the existing domain action owner (111 classes; COMMIT/EXECUTE keep their own grants), then re-observes and checks the owner predicate. Background reasoning can only request an action, through a leased request. It can never actuate.

### Alternatives rejected or deferred

- **DOM role/name only** (the withdrawn 224 V0): rejected. It produced eight invented pointers, all on stale or replaced owner state.
- **Text or OCR as the primary grounding**: rejected for Jarvis surfaces. The proxy produced three invented pointers and eleven missed targets, and it failed on short real labels ("Design", "p", "Run"). Real pixel OCR was not measured.
- **Screenshot/vision-first grounding** (the approach of existing commercial products): deferred, not adopted, for Jarvis's own surfaces. No vision candidate ran, and R12 shows the frame can contradict the owner. It stays a slot for surfaces without owners, such as external apps, behind UIA corroboration and refuse-on-disagreement. It needs its own bake-off.
- **A registry without owner bindings**: insufficient. It cannot reach canvas units or composed targets without published owner identity.
- **A global real-pointer desktop agent** (`SendInput` drive with an input blocker): out of scope for 224. It is not the 225 V0, because Jarvis targets have domain APIs. Any use outside Jarvis waits for the isolated Windows runtime qualification (ledger question `L20261028214413-1de0`) and a separate decision.
- **URL-predicate completion** (`DoneWhenUrlContains`): too weak for Jarvis. A route does not show run, selection or revision state.

### Gates before 224 readiness

- The surface owners publish the nine owner facts in cluster 4, each with an epoch or revision.
- At least one owner predicate is executable and measured after a real operator action in the browser, with no false completion on spinner, no-op and failed-action cases. This bake-off did not measure that.
- The rerun bake-off shows zero invented pointers for the chosen resolver at the exact head that is proposed for readiness.
- Draft PR #966 is re-examined against this contract. Only what fits is adopted; it is not merged because an older CI run was green.

225 additionally needs actual 224 operator proof, adversarial grant tests (replay after stop or restart, cross-thread bleed, stale-ref refusal) and host security qualification. Any Windows UIA or vision tier needs the isolated VM run.

### What this does not settle

The following were not measured: pixel OCR or vision, Windows UIAutomation, the runtime
behaviour of existing products, mixed DPI or multiple monitors, other themes and viewports, resource use beyond DOM-query
latency, and cases 02, 11, 24, 34, 35 and 38. The audit gives the reason each of those cases cannot be
produced on the current product.

## ADR-062: Never idle while any pool can work

Status: Accepted (2026-11-09, coordinator, under maintainer directive `L20261109084309-afcd`; implemented by spec 255).
It supersedes the spec 221 rule that kept Argon a worker only under `--coordinator-fallback`, the fixed
claude-first coordinator order and the Flash-low-only worker entry.

Context: the supervisor sat paused about two days with Claude, Codex and Argon's own bucket exhausted while about 90%
of the general Gemini quota was unused. Flash was confined to tier 1 by assumption, not evidence.

Decision:

- Capability is per model and effort, from measured results. Until measured, Gemini 3.8 Flash and Gemini 4 Argon at
  high effort take tier 3 and at medium tier 2. A model loses a tier only on evidence.
- Coordinators: Claude, Codex, Argon, a Flash coordinator, and agy Sonnet and Opus on the third-party buckets. Workers:
  every family at every effort the router lists. Selection ranks pools by the quota that would expire unused (binding
  window's free fraction per hour to its reset). The reserve is 5%, and 0 for a window resetting within 24 hours. The
  gemini family goes first among equally adequate workers.
- Effort follows difficulty times risk, the same rule for every family.
- Independent review prefers another family but never blocks on it.
- agy turns get 3600 s. Long tests run in the foreground with their own timeout, and commit and push run as a separate
  short step early in the turn.

Consequences: quality rests on the gates (CI, spec proofs, browser walk, independent review, risk rules), which apply
unchanged to every model. A later slice records per-turn agent, model, effort, CI first pass, findings, rework and
merge, and recomputes capability from those numbers.
