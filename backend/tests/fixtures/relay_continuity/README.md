# Relay Continuity Test Fixtures

Sanitized fixtures for offline relay continuity tests. Payload text, command arguments, and message/tool/wait identifiers are synthetic redactions/placeholders to keep private narratives, session IDs, user paths, and credentials completely out of the public repository. The event shapes (JSONL hierarchy and state structure) and the exact numeric usage and cumulative state token values are copied directly from verified retained real turns. The fixtures must not be claimed as entire literal copies of real JSONL files.

## Spec 277 Context Cap Fixtures

- `claude_context_over_cap/`:
  - `state.json`: Real Claude turn state shape written by Relay, carrying measured `metadata.context_used_tokens: 1583217` and `metadata.context_window_tokens: 200000`.
  - `output.jsonl`: Claude Code JSONL stream event shape with `message.usage`. The final assistant event carries the exact measured numeric triple from a retained over-cap turn: `input_tokens: 2`, `cache_creation_input_tokens: 1020`, and `cache_read_input_tokens: 198706`, yielding a measured per-call context size of 199,728 tokens (> 150,000 cap).
- `claude_context_under_cap/`:
  - `state.json`: Real Claude turn state shape written by Relay, carrying measured `metadata.context_used_tokens: 1080361` and `metadata.context_window_tokens: 200000`.
  - `output.jsonl`: Claude Code JSONL stream event shape with `message.usage`. The final assistant event carries the exact measured numeric triple from a retained under-cap turn: `input_tokens: 2`, `cache_creation_input_tokens: 209`, and `cache_read_input_tokens: 60189`, yielding a measured per-call context size of 60,400 tokens (<= 150,000 cap).
- `codex_context_cumulative/`:
  - `state.json`: Real Codex turn state shape with measured `metadata.context_used_tokens: 2901816`.
  - `output.jsonl`: Codex CLI turn event shape (`thread.started`, `turn.started`, `item.completed`, `turn.completed`), showing cumulative `turn.completed` usage with exact measured event values `input_tokens: 2891987` and `output_tokens: 9829` (totaling 2,901,816 tokens) without per-call assistant usage.
