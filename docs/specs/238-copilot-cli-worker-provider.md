# 238 — GitHub Copilot CLI delegated-worker qualification and routing

Status: ready
Depends on: none

## Goal and scope

Qualify GitHub Copilot CLI (`@github/copilot`) as a development-time delegated worker on this public repository, and add its premium-interactions quota as a fourth worker-routing pool. This does not enable a product AI provider. GitHub Models remains unenabled; product AI still requires `run_ai_task`, an `ai_jobs` row, and safe default fake mode with paid budget zero.

## Terms and observed readiness

Copilot CLI operates under GitHub Copilot Product Specific Terms. Prompts and outputs sent to CLI tools outside standard editors may be retained by GitHub to provide and maintain the service. Bounded developer-directed coding assistance on a local worktree is within this qualification; no customer, pack, secret, or other private data may enter a Copilot worker turn.

The no-model-turn `gh api copilot_internal/user` probe reports live entitlement and `quota_snapshots.premium_interactions` fields: `percent_remaining` (0–100), `unlimited`, `overage_permitted`, `has_quota`, `entitlement`, and `remaining`. The 2026-11-03 live Education plan qualification reports `access_type_sku: free_educational_quota` and premium interactions at 200/200 with `overage_permitted: false`. `@github/copilot` is installed under `~/.nvm`, and a dedicated fine-grained PAT with the `Copilot Requests` permission only is stored at `~/.local/share/jarvisos-sandbox/state/copilot/token`. On this plan the CLI accepts only `--model auto`, which selects GPT-5.6 Luna. These are point-in-time observations, not a claim of current readiness.

The reader and worker fail closed with `no_entitlement`, `copilot_missing`, or `auth_missing` if the entitlement, CLI, or dedicated worker auth is unavailable. Worker auth may be supplied at `~/.local/share/jarvisos-sandbox/state/copilot/token`, through `COPILOT_GITHUB_TOKEN`, or via linked `copilot-home` state. Quota at zero refuses work, including when paid overage is permitted; the wrapper never initiates paid overage.

## Credential and execution contract

The coordinator-side reader may call `gh api copilot_internal/user` with `JARVISOS_COPILOT_GH_TOKEN`, `COPILOT_GITHUB_TOKEN`, the dedicated token file, or the coordinator's existing gh environment. `JARVISOS_GH_BIN` is a test override. This reading creates no model turn. The worker child removes coordinator `GH_TOKEN`, `GITHUB_TOKEN`, `GH_*`, `GIT_*`, and `SSH_AUTH_SOCK`, uses an empty `GH_CONFIG_DIR`, and disables global and system Git configuration and terminal prompts. Only a dedicated Copilot token or linked `~/.copilot` state reaches the child. Its command denies `shell(gh:*)`, `shell(git push*)`, `shell(git remote*)`, and `shell(git config*)`.

The wrapper runs only inside the certified sandbox, in a clean linked worktree, with allowlisted model aliases. It records transcript, stderr, diff, and a typed result, kills the process group on timeout, and refuses changed Git config or hooks metadata. It makes no commit or ledger entry; the coordinator reviews the diff.

## Routing and acceptance

`worker_routing.py` adds the Copilot pool with a 10% reserve and priority `gemini`, `copilot`, `codex`, `claude`. It exposes one tier-1 `copilot-auto` alias, launched with `--model auto`. No tier-2 or tier-3 Copilot route is qualified. The quota column reports entitlement, CLI, auth, exhausted, or unreadable failures without selecting a worker. A stale snapshot cannot promote an unavailable Copilot pool.

Focused offline tests cover parsing, routing, credential isolation, refusal paths, worktree and metadata boundaries, timeout cleanup, and evidence. The ordinary risk-based review and CI gates apply. The recorded Education entitlement and installation are point-in-time host observations; recheck them before real Copilot delegation.
