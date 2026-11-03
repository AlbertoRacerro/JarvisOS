# Material / Tool / MCP Boundary (Stage 5-PRE)

Status: **design contract**. None of the layers below are integrated. This document
defines the boundary *before* any of them is built, so that ingestion, memory graph,
tools, MCP, or a product backend cannot become a leakage channel.

No integration of Graphify, DeerFlow, Supabase, MarkItDown, MCP, LiteLLM, or vLLM occurs
in this or any PRE slice. They are **conceptual references** only.

## Future layers and their boundary

| Layer | Reference (conceptual) | Boundary rule |
|---|---|---|
| document ingestion | MarkItDown | local-only conversion; output inherits source sensitivity |
| codebase graph | Graphify | nodes carry `sensitivity_class`; graph is not a downgrade path |
| vault / structured memory | Obsidian workflow | retrieval respects class; S3 never indexed |
| MCP resource loading | DeerFlow MCP boundary | MCP output is **untrusted until classified** |
| tool execution | DeerFlow skills/sandbox | tool input gated by `tool_policy`; output classified before reuse |
| agent harness / subagents | DeerFlow | subagent inherits caller's sensitivity ceiling; cannot raise its own egress rights |
| product backend | Supabase | NOT core memory; non-sensitive / default-redacted only; RLS mandatory if ever used |
| local training/eval | nanochat | training data must be classified; S2+ never used for cloud training |

## Required principles

1. **Local ingestion first.** Documents are converted/ingested locally; no raw material
   is sent to an external tool for conversion by default.
2. **No sensitive raw material to external tools by default.** S2+ requires sanitization
   + provenance + (per class) confirmation before any tool/MCP path.
3. **Classify before use.** A tool/MCP resource is classified *before* it is allowed into
   prompt context, retrieval, or another tool. Unclassified ⇒ treated as `S2`/untrusted.
4. **Sensitivity is monotonic upward through retrieval.** Retrieved/derived material
   inherits the **maximum** sensitivity of its sources; it can be raised, never silently
   lowered. Lowering requires an explicit sanitization step that records provenance.
5. **Provenance survives derivation.** A redacted derivative keeps a link to its origin
   class (see [SENSITIVITY_OBJECT_MODEL.md](SENSITIVITY_OBJECT_MODEL.md)).
6. **Product backend is not core memory.** A Supabase-like store, if ever used, holds only
   non-sensitive or default-redacted material, behind a strict boundary, with RLS always
   enabled. It is never the authority for sensitivity decisions.
7. **Provider responses are material too.** A model response that echoes or infers
   sensitive content is classified before it is stored, indexed, or fed back into history.
8. **MCP/tool outputs are untrusted.** They cannot self-declare a lower sensitivity or
   request their own egress rights; the harness enforces the caller's ceiling.

## Why this matters now (grounded)

The current system already has the seam that makes this real: `dev_local_chat` filters
history turns (`smoke_adapter.filter_clean_history` / `scan_history_turn_for_context`)
before they enter a prompt. That is exactly the place where a future retrieved note or
MCP resource would also enter context. If those new sources are added without inheriting
this filtering, the Stage 2-R1 golden findings (history bypass included, Italian IP
missed) would extend silently into the retrieval/MCP layer.

So: the boundary is defined now, while the only "context source" is in-request history,
to ensure later sources plug into the *same* sensitivity gate rather than around it.
