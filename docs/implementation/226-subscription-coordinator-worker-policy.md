# 226 maintenance — subscription coordinators and Codex workers

## Authority and scope

Maintainer directive `L20261030135147-e6c4`, 2026-10-30, explicitly requests
Claude Code Opus 5.5 / Antigravity Argon / Antigravity Opus 5.5 coordination,
with Codex Terra 6.1 and Luna 6 primarily doing delegated work. The dated
amendments in specs 219 and 226 and the Generic Frontier Builder Contract
record this current subscription allocation. The constitutional rule that
model brands do not grant authority remains unchanged.

Starting master: `9e19b8f6026d0488766404a6cd219c2281fb78af`, clean.
Implementation branch: `codex/subscription-coordinator-worker-policy`.
This changes the existing repository-development owner, not product AI.

## Delivered behavior

- Preferred coordinators rotate `claude` → `argon` → `agy_opus` → `claude`
  when a new mission follows a completed mission. The selected available
  preferred coordinator stays with its current mission. Unavailable routes
  are skipped. Codex coordinates only when all preferred routes are unavailable,
  and yields when a preferred route recovers.
- Rotation reuses the existing mission state; resumed missions, historical
  sessions, holds, pending nonces and completion verification are preserved.
  No new daemon, queue, state store or credential profile is introduced.
- `agy_opus` launches `claude-opus-5-5-medium` by default. The explicitly
  allowlisted low/medium/high variants use `JARVISOS_AGY_OPUS_MODEL` and require
  the actual `3p-5h` and `3p-weekly` buckets. Argon retains its existing model
  family and Gemini common/WTU buckets. Probe caches are separate by family;
  immediate launch preflight uses the same model family as execution.
- Existing subscription-only, no-overage, certified-sandbox, coordinator lease
  and fail-closed quota checks remain. Opus has its own actual agent identity
  in state, launch metadata and classified reports. Old native Argon metadata
  remains readable.
- Codex delegation uses the existing CLI, isolated worktrees and durable jobs:
  `gpt-6.1-terra` for implementation/tests/fixes, `gpt-6-luna` for small
  mechanical work. This is the coordinator's default task-allocation contract,
  not a new automatic per-task worker dispatcher. A genuine quota refusal
  disables that route until its reported reset; an existing qualified worker
  may be used without buying credits or repeatedly retrying Codex.
- Real native proof exposed an existing classifier defect: the CLI can prefix
  its executor exit header with a fixed long-output truncation envelope.
  The parser now accepts only that exact optional envelope followed immediately
  by the anchored exit header. It never searches arbitrary stdout for exit text.
  Asynchronous acknowledgements remain pending; nonzero exits remain abnormal.
  `read_file` is recognized as read-only; unknown tools remain external.
  Native prompt guidance also requires waiting for the executor's actual exit
  before ending a turn. Guidance does not replace deterministic verification.

## Actual environment evidence

Host and certified-sandbox `agy models` catalog inspections found
`gemini-4-argon-{low,medium,high}`, `claude-opus-5-5-{low,medium,high}` and
`claude-sonnet-5-5-{low,medium,high}`. Sonnet is recorded, not enabled here.
Sandbox discovery used the existing `prepare_login()` and subscription settings
check; a plain probe before this preparation reported sign-in required.
No token or credential contents were extracted.

The existing Claude Code transcript at session `20261030-132609-69378e`,
`turn-001`, reports actual model `claude-opus-5-5` (Relay config alias `opus`).
The sandbox native Codex app-server `model/list` reports exact worker IDs
`gpt-6.1-terra` and `gpt-6-luna`. Inventory is not proof of quota room.

Actual Terra and Luna delegated sessions produced partial source/tests and
documentation respectively, then both reached the account usage limit. Their
delivery was recovered and every diff inspected by the coordinator; they are
not reported as fully successful workers. Existing supervisor quota evidence
reports Codex unavailable until `2026-11-04T06:59:59Z`. No paid API or purchased
credit fallback was used. Ledger receipt: `L20261030143817-0764`.

Native Opus evidence root:
`/home/user/jarvis-control/work/evidence/coordinator-worker-policy-20261030`.
Raw streams and final replies remain under the candidate worktree's ignored
`.agent-relay/sessions/<session>/turns/turn-001/`.

1. Session `20261030-142922-510100`: actual `agy_opus`,
   `claude-opus-5-5-medium`, launched true, process exit 0, no refusal.
   Independent source/test review returned PASS and actually executed a source
   hash command. Reviewed native adapter SHA-256:
   `7b527bbd88128d3906bf8d5c304809184a16c3acead2ea36f26f9303ef4d7948`.
   Start receipt records base and diff digest. The original classifier initially
   returned unknown because of the real truncation prefix; that failed
   observation is preserved. After the bounded parser repair, the same unchanged
   raw transcript classifies success with zero bad lines and no pending calls.
2. Session `20261030-143531-aa4026`: actual same native Opus model, exit 0,
   no refusal. Independent focused review of the classifier repair returned
   PASS, including spoofed stdout, asynchronous acknowledgements, nonzero exits
   and unknown-tool handling. It actually hashed the controller source as
   `ec340c0a7e52672f398c22321cd42a1d6ef1c81345f6f534eacc0d442d2f3aff`.
   Its unchanged raw transcript also classifies success, zero bad lines,
   no pending calls. Both peers explicitly did not run tests. Subsequent changes
   are prompt waiting guidance, truthful report labels and documentation;
   they do not alter the independently reviewed classifier mechanism.

At the second proof's preflight the actual Opus buckets reported remaining
fractions `0.8809351921081543` weekly and `0.7618703842163086` five-hour.
These are point-in-time readings, not current availability promises. The
temporary proof-process timeout was bounded (240/180 seconds); the repository
runner's existing 1800-second limit was not changed.

## Checks and failures

Commands run from the candidate worktree, using the existing backend venv:

```bash
jarvis-sandbox --cwd "$WORKTREE/backend" "$VENV/bin/python" -m pytest -q tests/test_relay_continuity.py tests/test_relay_argon_223.py tests/test_relay_observe.py tests/test_relay_jobs_recovery.py
# 590 passed, 1 existing Starlette/AnyIO BlockingPortal deprecation warning; 23.73 s.
jarvis-sandbox --cwd "$WORKTREE/backend" "$VENV/bin/python" -m pytest -q tests/test_relay_argon_223.py
# Final native prompt + report-identity coverage: 70 passed, same warning.
cd "$WORKTREE/backend"
"$VENV/bin/python" -m ruff check app tests ../scripts/relay_argon.py ../scripts/relay_continuity.py
# PASS.
"$VENV/bin/python" ../scripts/check_typecheck_ratchet.py --base-sha 9e19b8f6026d0488766404a6cd219c2281fb78af
# PASS: current_errors=301, baseline_errors=310.
cd "$WORKTREE"
python3 scripts/check_spec_status.py
# PASS: 250 rows.
python3 scripts/check_architecture_enforcement.py
# PASS.
git diff --check
# PASS.
```

`WORKTREE=/home/user/src/worktrees/codex-subscription-coordinator-worker-policy`;
`VENV=/home/user/src/JarvisOS/backend/.venv`.

Failed exploratory invocations were corrected, not suppressed:

- Initial pytest command named nonexistent mission/roadmap test files, exit 4;
  corrected to the actual existing modules above.
- Initial host run had four failures: three old coordinator-order assertions
  were updated to the expressly requested preference; the sandbox-entrypoint
  check correctly rejected the WSL host's `/mnt/c`. The unchanged boundary test
  passes inside the certified sandbox. No frozen conformance test was edited.
- Ruff invoked from repository root selected unrelated configuration and
  reported nine existing issues. Correct backend-directory invocation passes;
  unrelated source was not modified to satisfy the wrong configuration.
- Two automation tool discovery calls used unsupported list/view arguments;
  no automation changed. Both known host automation directories are absent.
  The actual mission owner is the existing systemd service, stopped gracefully
  under `L20261030135917-0a63`, with no competing shared writer observed.

Frontend build/browser tests are not run for this change: no frontend/product
surface changes. Full backend suite is left to exact-head sharded CI under the
delivery protocol. Native tests mock providers; live proof is separate above.
No token saving, general model reliability or scientific correctness claim is
inferred from the allocation policy or the bounded native proof.

## Activation boundary and remaining work

At candidate preparation the existing service is inactive after graceful STOP,
not crashed. Last roadmap mission `m-20261030T135911Z-1d54ad` remains incomplete,
agent Argon, with no hold/pending attempt; no completion was invented. Existing
historical data and STOP are preserved until exact-head integration. After
merge, resume this owner at the proven safe boundary and retain the activation
receipt in the existing ledger/evidence location. A successful isolated Opus
proof does not by itself establish production owner activation.

Unrelated qualification jobs `j-20261030T134927Z-49859c` (224) and
`j-20261030T135409Z-041a4b` (230) exited 1 before this change. Their logs and
roadmap references remain; no green browser qualification is claimed. Recover
these existing jobs/fronts before duplicating work. The small conservative
`sed -n` command-effect parser classification observed during review is not
broadened here; completed commands still retain their actual executor status.

Boundary: no product backend/frontend/routes/API/schema/memory/retrieval change;
no launcher change or external source vendoring; the sole new provider route is
the explicitly authorized native subscription repository coordinator.
