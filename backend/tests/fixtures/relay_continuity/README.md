# Relay Continuity Test Fixtures

Sanitized fixtures for offline relay continuity tests. All private narratives, user paths, and credentials have been removed or replaced with synthetic placeholders in accordance with repository policies.

## Spec 277 Context Cap Fixtures

- `claude_context_over_cap/`:
  - `state.json`: Real Claude turn state shape written by Relay, carrying `metadata.context_used_tokens: 2166425` and `metadata.context_window_tokens: 200000` (from the 2026-11-15 Claude turn documented in Spec 277).
  - `output.jsonl`: Real Claude Code JSONL stream events with `message.usage`. The final assistant event records `input_tokens: 158420`, `cache_creation_input_tokens: 2100`, and `cache_read_input_tokens: 1480` for a measured context size of 162,000 tokens (> 150,000 cap).
- `claude_context_under_cap/`:
  - `state.json`: Real Claude turn state shape written by Relay, carrying `metadata.context_used_tokens: 2166425` and `metadata.context_window_tokens: 200000`.
  - `output.jsonl`: Real Claude Code JSONL stream events with `message.usage`. The final assistant event records `input_tokens: 58342`, `cache_creation_input_tokens: 1210`, and `cache_read_input_tokens: 848` for a measured context size of 60,400 tokens (matching the median 60,400 of the 181 real Claude turns corpus in Spec 277).
- `codex_context_cumulative/`:
  - `state.json`: Real Codex turn state shape with `metadata.context_used_tokens: 2166425`.
  - `output.jsonl`: Real Codex CLI turn events (`thread.started`, `turn.started`, `item.completed`, `turn.completed`), showing cumulative `turn.completed` usage but no per-call assistant usage.
