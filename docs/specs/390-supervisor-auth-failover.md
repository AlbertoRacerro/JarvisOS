# 390 — SUPERVISOR-AUTH-FAILOVER-1

Status: ready
Depends on: none

## Goal

Close the runtime authentication failover and credentials-divergence gap in the continuity supervisor (`scripts/relay_continuity_*.py`), quota reader (`backend/app/modules/coding/provider_quota.py`), and runbook (`docs/implementation/relay-continuity-runbook.md`): deterministically classify runtime expired-login/OAuth failures across all agents as `"auth"`, place only that agent in an `"auth"` cooldown and fail over immediately without tripping `circuit_failures`, notify the maintainer once in plain Italian (Slack + Jarvis UI) with the exact plain host login command, auto-recover via a minimal zero-effect in-sandbox login probe (periodically and immediately when the credentials file changes), and eliminate the diverging dual-copy Claude credentials trap.

## Why

Implements Maintainer Directives `L20261128091427-8297` and `L20261128092954-c169` (incident finding `L20261128091338-3ee4`). When Claude's OAuth session expired at runtime, 8 zero-token failures in 30 s were misclassified as `unknown` and tripped `circuit_failures`, halting autonomous work for ~11 hours instead of failing over to Neon/agy. Furthermore, because `jarvis-sandbox` bind-mounts `~/.claude/.credentials.json` over `~/.local/share/jarvisos-sandbox/state/claude/.credentials.json`, re-logging with `CLAUDE_CONFIG_DIR=.../state/claude` wrote to a diverging file invisible inside the sandbox.

## Code facts checked at `77f5b1a8`

- `scripts/relay_continuity_turns.py::AUTH_RE` matches `oauth token (has )?expired` but misses `oauth session (has )?expired`, `please log in`, `token refresh failed`, `invalid_grant`, `session expired`, and `agy`/`argon`/`codex` runtime auth errors.
- `scripts/relay_continuity_turns.py::classify_turn` and `scripts/relay_continuity_turns.py::classify_argon_turn` classify terminal turn events; when Claude returns a zero-token failure or non-error/incomplete terminal event whose stderr/diagnostics contain an auth error, `classify_turn` falls through to `unknown` instead of `"auth"`.
- `scripts/relay_continuity_turns.py::no_terminal_evidence` checks `diagnostics` and `turn_dir / "stderr.log"` via `classify_text` only when no terminal event was found.
- `scripts/relay_continuity_supervisor.py::Supervisor.available` checks `source == "auth"` cooldowns only by scanning the ledger for a human decision answering `question` with `"1"`; it has no periodic or credentials-change-triggered self-recovery probe.
- `scripts/relay_continuity_supervisor.py::Supervisor.set_cooldown` sets `"auth"` cooldowns, records a generic Italian question without the exact login command, and emits `provider_exhausted` (`reset_source="auth"`).
- `scripts/relay_continuity_supervisor.py::Host` declares injectable supervisor callbacks (`relay`, `now`, `ledger`, `reachable`, `read_ledger`, etc.).
- `scripts/relay_continuity_notify.py::EVENTS` and `scripts/relay_continuity_notify.py::render` define allowlisted fixed-template Italian notifications sent to Slack/webhooks with zero free-text leakage.
- `scripts/relay_continuity_roadmap.py::link_relay_claude_credentials` symlinks `~/.claude/.credentials.json` inside the sandbox home to `CLAUDE_CONFIG_DIR / ".credentials.json"`.
- `backend/app/modules/coding/provider_quota.py::_resolve_claude_credentials_path` prefers `~/.local/share/jarvisos-sandbox/state/claude/.credentials.json` over `~/.claude/.credentials.json` when no env override is set, reading a diverging copy on the host when `jarvis-sandbox` bind-mounts `~/.claude/.credentials.json`.

## Scope

In scope:
- **Deterministic runtime auth classification (`scripts/relay_continuity_turns.py`):**
  - Broaden `AUTH_RE` to deterministically match runtime authentication/OAuth/refresh/login-required failures across Claude, Codex, Argon, and agy (e.g. `oauth (token|session) (has )?expired`, `session expired`, `please log ?in`, `refresh token (expired|invalid|failed)`, `token refresh failed`, `invalid_grant`, `login required`, `re-?authenticate`, `sign in again`, `auth(?:entication)? expired`, `failed to refresh (oauth|access) token`).
  - In `classify_turn` and `classify_argon_turn`, when the turn is not `"success"` and has zero tokens (`total_tokens == 0` or usage absent/zero), also inspect `diagnostics`, `turn_dir / "stderr.log"`, and assistant/error text with `classify_text`: if it classifies as `"auth"`, set `report.outcome = "auth"` (with a safe detail string).
  - Add a deterministic helper `_is_zero_token_turn(report)` so zero-token runtime auth failures never fall through to `unknown`.
- **Auth cooldown, immediate failover, and circuit protection (`scripts/relay_continuity_supervisor.py`):**
  - Keep `"auth"` outcomes out of `consecutive_failures` (already excluded at line 1888) and ensure an `"auth"` outcome puts ONLY that agent in `"auth"` cooldown and fails over immediately via `_choose(failed_agent)` to the next available agent in `PREFERRED_AGENTS`, never tripping `circuit_failures`.
  - Keep `circuit_failures` hold strictly intact for genuinely `unknown` / `crash` consecutive failures.
  - On first entry into `"auth"` cooldown for `agent` (deduplicated while the cooldown lasts — no repeated alerts):
    - Record one plain-Italian Jarvis UI question (`self.record("question", ...)`) containing the exact login command for that agent:
      - `claude`: `claude` (senza `CLAUDE_CONFIG_DIR`), poi `/login`
      - `codex`: `codex login`
      - `neon`, `argon`, `agy_opus`, `hydrogen`: login `agy` sul sistema host
    - Emit one allowlisted plain-Italian Slack notification (`provider_auth_expired` in `scripts/relay_continuity_notify.py`) with `dedupe_key=f"auth_expired|{agent}"` that includes the fixed per-agent Italian login instruction (zero secrets or free text).
- **In-sandbox auto-recovery probe (`scripts/relay_continuity_supervisor.py`, `scripts/relay_continuity_roadmap.py`):**
  - Add optional injectable callbacks on `Host`:
    - `auth_probe: Callable[[str], bool] | None = None`
    - `auth_cred_fingerprint: Callable[[str], str | None] | None = None`
  - Add default in-sandbox implementations wired in `command_serve` (which already runs inside `jarvis-sandbox`, so the probe executes on the real in-sandbox path):
    - `default_auth_cred_fingerprint(agent)`: returns `f"{mtime_ns}:{size}:{sha256_hex}"` of the agent's credentials file (for Claude: `~/.claude/.credentials.json` inside the sandbox, which is the bind-mounted host file; for Codex: `$CODEX_HOME/auth.json` or `~/.codex/auth.json`) or `None` if absent.
    - `default_auth_probe(agent)`: minimal zero-effect in-sandbox check (for Claude: `claude -p "ok" --model haiku --max-turns 1 --no-session-persistence --tools ""` with bounded 20 s timeout; returns `True` iff exit code 0 and output does not match `AUTH_RE`).
  - Store `cred_fingerprint` and `last_probe_at` on the `"auth"` cooldown entry. In `Supervisor.available(agent)`, when `source == "auth"`:
    - First retain existing manual ledger-decision `"1"` clearing.
    - Otherwise, if `host.auth_probe` is configured and either (a) `auth_cred_fingerprint(agent)` changed since `cred_fingerprint` was recorded, OR (b) at least `auth_probe_interval` (default 300 s) has elapsed since `last_probe_at` (or `since`):
      - Update `last_probe_at` (and `cred_fingerprint`), run `host.auth_probe(agent)`.
      - If it returns `True`, clear `self.state["cooldowns"][agent]`, record a ledger `finding` (`relay-continuity: cleared auth cooldown for <agent> after in-sandbox login probe succeeded`), save state, and return `True` so the preferred agent returns at the next turn boundary without a manual release.
- **Single Claude credentials source of truth (`backend/app/modules/coding/provider_quota.py`, `scripts/relay_continuity_roadmap.py`, `docs/implementation/relay-continuity-runbook.md`):**
  - Update `_resolve_claude_credentials_path()` in `provider_quota.py` so when no explicit `JARVISOS_CLAUDE_CONFIG_DIR` / `CLAUDE_CONFIG_DIR` override is set, it reads the canonical host file `~/.claude/.credentials.json` first (matching what `jarvis-sandbox` bind-mounts into the sandbox), falling back to `~/.local/share/jarvisos-sandbox/state/claude/.credentials.json` only when `~/.claude/.credentials.json` is absent.
  - Ensure `docs/implementation/relay-continuity-runbook.md` documents the single correct Claude login procedure (`claude` on the host without `CLAUDE_CONFIG_DIR`, then `/login`, verified via `jarvis-sandbox claude -p`), warning against creating a diverging copy in `~/.local/share/jarvisos-sandbox/state/claude/.credentials.json`.

Out of scope (binding non-goals):
- No changes to host systemd units or `~/.local/bin/jarvis-sandbox` outside the repository (note in summary if host state symlink cleanup is needed).
- No changes to quota-cooldown logic, spend-cap logic, or `circuit_failures` threshold for non-auth unknown failures.
- No secrets, tokens, or raw credential contents in logs, notifications, ledger entries, or UI responses.

## Files likely touched

- `scripts/relay_continuity_turns.py`
- `scripts/relay_continuity_supervisor.py`
- `scripts/relay_continuity_notify.py`
- `scripts/relay_continuity_roadmap.py`
- `backend/app/modules/coding/provider_quota.py`
- `docs/implementation/relay-continuity-runbook.md`
- `backend/tests/test_relay_continuity.py`
- `backend/tests/test_provider_quota_340.py` (or existing quota test file, verified at implementation)

## Design constraints

- **Deterministic, zero-model-judgement classification:** Runtime auth detection uses only deterministic token count + regex/status checks.
- **No secret leakage:** `relay_continuity_notify.py` stays strictly allowlisted fixed templates; `auth_cred_fingerprint` hashes file bytes and never logs contents or tokens; `_safe_auth_detail` never emits raw stderr.
- **High-risk governance:** Supervisor/auth code requires coordinator full-diff review, a second qualified session review, and verification inside `jarvis-sandbox` before merge.
- **Existing tests stay green:** All existing quota, spend-limit, and `circuit_failures` tests in `backend/tests/test_relay_continuity.py` and `backend/tests/test_relay_notify.py` must pass unchanged.

## Acceptance criteria

1. A zero-token turn with an OAuth/session-expired, refresh-failed, or "please log in" error (in stdout/result JSON or `stderr.log`, for Claude, Codex, Argon, or agy) is deterministically classified as `outcome == "auth"`.
2. When an agent fails with `outcome == "auth"`, the supervisor puts only that agent in `"auth"` cooldown, fails over immediately to the next available agent in `PREFERRED_AGENTS`, never trips `circuit_failures`, and sends exactly one plain-Italian Slack notification and one Jarvis UI question containing the exact plain host login command (`claude`, poi `/login` for Claude) with no repeated alerts while the cooldown lasts.
3. When the credentials fingerprint changes (or after `auth_probe_interval`) and the in-sandbox `auth_probe(agent)` succeeds, `Supervisor.available(agent)` automatically clears the `"auth"` cooldown, records a ledger `finding`, and returns `True` so the preferred agent resumes at the next turn boundary without manual release.
4. `provider_quota._resolve_claude_credentials_path()` reads `~/.claude/.credentials.json` first when no explicit env override is set (matching the sandbox bind-mount), eliminating host/sandbox divergence, and `docs/implementation/relay-continuity-runbook.md` documents the single login procedure.
5. All offline tests pass (new auth-failover test, new credentials-change auto-recovery test, plus existing quota, notify, circuit-breaker, and provider_quota tests).

## Required tests

- Offline test in `backend/tests/test_relay_continuity.py`:
  - Fake Claude turn failing with zero tokens + `"OAuth session expired"` (both in `result` JSON and in `stderr.log`), verifying `outcome == "auth"`, immediate failover to `neon` (no `circuit_failures` hold), and exactly one Italian Slack notice + UI question mentioning plain `claude` + `/login` (no duplicate notice on subsequent ticks).
  - Fake `codex` / `agy` / `argon` zero-token auth failure classified as `"auth"`.
  - Credentials-change auto-recovery test: agent in `"auth"` cooldown stays unavailable while probe returns `False`, then credentials fingerprint changes and probe returns `True` -> cooldown clears automatically, ledger `finding` recorded, agent returns at next turn boundary.
  - Periodic probe auto-recovery test after `auth_probe_interval` elapses.
  - Existing quota and `circuit_failures` tests remain green.
- Offline test for `provider_quota._resolve_claude_credentials_path()` verifying `~/.claude/.credentials.json` is preferred over `~/.local/share/jarvisos-sandbox/state/claude/.credentials.json` when no env override is set.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, second-session review completed, in-sandbox proof recorded, spec status updated, summary written.
