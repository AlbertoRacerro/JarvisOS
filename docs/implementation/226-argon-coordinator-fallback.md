# 226 — Argon coordinator transport and fallback evidence

The branch, test filename and external proof directories retain the original
223 label. During the interrupted delivery window, fresh master allocated 223
to a separately accepted conversation-intake contract and 224–225 to other
work. This change was reconciled as **226**, preserving those canonical rows
and their authority. No existing contract was replaced.

## Scope and authority

The maintainer requested the third coordinator after the successful real 221
Flash proof (`L20261027074542-f501`). The existing single mission owner remains
the scheduler and authority. This integration does not change product AI
execution, the database, frontend, launcher, host-ops operations, worker merge
permissions or the active engineering example queue.

The native Antigravity coordinator transport is separate from the constrained
221 worker. The latter still strips Git/GitHub credentials and forbids native
approval bypass. A full coordinator runs only under the mission profile inside
the existing OS sandbox, sharing the coordinator lease and existing independent
mission completion checks. This mirrors the already-authorized Claude/Codex
coordinator roles rather than granting a worker new authority.

## Actual CLI findings

The real sandbox sign-in persists through the existing state root. The sandbox's
ephemeral `~/.gemini` can link to its already-bound dedicated `state/agy-home`;
no launcher edit, copied host credentials or second login is needed. An existing
conflicting path must be refused rather than replaced.

The live model list exposes `gemini-3.8-flash-low` and
`gemini-4-argon-{low,medium,high}`. Bare aliases from the original 221 contract
were not confirmed by that list. Argon quota requires the common `gemini-5h`
and `gemini-weekly` buckets plus `gemini-argon-wtu-weekly`, rather than an
assumed `argon-` prefix.

Native `stream-json` records initialization, indexed tool steps and a final
result. The stream, not model prose, is execution evidence. The first live
`pwd` probe with `accept-edits` produced exit 0 and `status: SUCCESS` even
though `denied_actions` recorded a refused command and the response was empty.
The `auto` probe also refused the command because that mode was unavailable
for the account. These are failed command proofs, not successful coordinator
qualification. Their original transcripts remain under
`/home/user/jarvis-control/work/evidence/223-argon-stream-probe/`.

The official [CLI reference](https://antigravity.google/docs/cli/reference)
documents the native settings, model selection and account-dependent modes.
The [permission reference](https://antigravity.google/docs/permissions?tab=cli)
distinguishes workspace edit approval from terminal command permission. The
integration therefore uses an explicit mission-only invocation rather than
changing the global settings shared by workers. Subscription-only execution
must refuse enabled credit overage or an API-provider configuration.

## Acceptance evidence

The frozen implementation passed the following combined command from the real
certified sandbox, with the isolated worktree as cwd:

```bash
jarvis-sandbox --cwd /home/user/src/worktrees/codex-223-argon-fallback -- bash -c 'cd backend && /home/user/src/JarvisOS/backend/.venv/bin/python -m pytest -q tests/test_relay_continuity.py tests/test_relay_argon_223.py tests/test_relay_jobs_recovery.py tests/test_relay_observe.py tests/test_agy_worker_221.py'
```

Result: **596 passed**, 36.08 s, one pre-existing Starlette/anyio
`BlockingPortal` deprecation warning. New native tests cover strict protocol
and tool identity, malformed/missing output, quota/settings refusals, primary
provider recovery, owner lease lifetime, actual command completion evidence,
nonce recovery and independent mission completion checks. Monitor tests also
cover redaction, denied actions and failed native command output.

Early host-side regression runs failed the existing mission entrypoint test
because `/mnt/c` is present outside the sandbox; the boundary refusal is
correct. An early adapter environment compatibility defect was repaired.
An early durable-job lifecycle check also failed outside the relevant PID
environment. The combined certified-sandbox run above passed every case;
no boundary or frozen conformance test was weakened or skipped.

Ruff passed for all changed Python modules and tests, using
`--config backend/pyproject.toml`; `python3 scripts/check_spec_status.py`
passed with 243 registry rows, and `git diff --check` passed. CI now includes
the native runner in its existing explicit Ruff list. Frontend build/tests
were not run: no frontend source or contract changed. Full backend coverage
is delegated to exact-head CI rather than repeated locally.

The real native transport qualification includes session
`20261027-081232-153c08`, model `gemini-4-argon-low`, one turn lasting
39.331267462 s. Native initialization, tool execution output with exit code
0, final `SUCCESS` and canonical JSON `RELAY_STATUS` were retained. The
independent checks verified native classifier success with no bad records,
final `propose_done`, and nonce-based session recovery. The artifact fidelity
check initially used the wrong expected bytes: the model wrote literal
backslash+n instead of the newline requested by the task. Independent review
caught this discrepancy. The artifact and original incorrect attestation were
preserved; corrected `verification.json` records `FAIL_ARTIFACT`. Evidence:

- `/home/user/jarvis-control/work/evidence/223-argon-stream-probe/final-qualified-session/`
- `/home/user/jarvis-control/work/evidence/223-argon-stream-probe/ARGON_FINAL_PROOF.txt`

A bounded repeat with no escape characters passed in session
`20261027-081534-7f8853`: actual command execution, native exit 0,
classifier `success` with zero bad records, canonical `propose_done`, nonce
recovery and exactly 12 requested ASCII bytes `ARGON_223_OK`, with no newline.
Artifact SHA-256:
`f71dd6ec509fb83db9bd9f16fa21d718b27eeb59b2f02279b5acbd3a521e05b8`.
The repeat did not change source. Its raw artifacts and independent byte
verification are in `223-argon-stream-probe/exact-qualified-session/`, next
to `ARGON_EXACT_PROOF.txt` in the same external evidence root.

Independent source review passed with no remaining blocking defect; the
reviewer independently ran native/observer/worker tests, **109 passed**, and
identified the artifact discrepancy above before delivery. The final real
proof's script hashes match the frozen source used by the 596-test run.

Earlier transport probes with plain `RELAY_STATUS propose_done` did not prove
mission completion because the existing parser requires its canonical JSON
format. The native prompt now supplies that exact protocol; the parser was
not relaxed. The short delayed-command probe did not demonstrate a genuinely
outstanding asynchronous command handoff. Missing native executor completion
evidence therefore remains pending and cannot advance a mission.

Native transport qualification is distinct from real automatic takeover
caused by both primary providers becoming unavailable. No primary quota was
deliberately exhausted and no live cooldown was cleared for a proof. Source
integration is also distinct from activation: the existing long-lived owner
must reach a safe restart boundary after its active registered jobs finish.
The active 222 scientific/importer work is never interrupted for activation.

## CI repair and expanded verification

The first exact-head CI at `08efc59a7a0c7cabc057527aae4f4f003c5d4e2d`
failed `backend-tests (core-d)`: `test_isolated_copied_scripts` and
`test_copied_release_discovers_repo_from_cwd` copied the old script set but
omitted the newly required `relay_argon.py`, so isolated imports failed with
`FileNotFoundError`. The real release installer extracts the complete scripts
tree. Its three test-package lists now include the new module; the isolation,
entrypoint and repository-discovery assertions remain unchanged. No required
check was skipped or relabelled.

The expanded certified-sandbox command adds `tests/test_host_ops.py` to the
combined command above: **674 passed**, 31.06 s, one existing AnyIO warning.
The exact expanded command is:

```bash
jarvis-sandbox --cwd /home/user/src/worktrees/codex-223-argon-fallback -- bash -c 'cd backend && /home/user/src/JarvisOS/backend/.venv/bin/python -m pytest -q tests/test_host_ops.py tests/test_relay_continuity.py tests/test_relay_argon_223.py tests/test_relay_jobs_recovery.py tests/test_relay_observe.py tests/test_agy_worker_221.py'
```

Source adapter hashes still match the real native proof. The subsequent
registry reconciliation preserves master specs 223–225 and assigns 226 to
this already-authorized Argon slice.

## Remote delivery

PR #957 merged as
`867ae42329e36b7731f0905caf6bb2f4e86102cf` after all **13/13** checks passed
on head `2b4e295081fb7c60725eaf3f2ba2f786a10ee577` (CI run
`40570746895`, plus PR Attention Evidence). Backend, six core shards, two
BlueCad shards, frontend build, geometry canary and classification passed.
The previously failed copied-release cases passed in core-d.

At the original delivery, the existing owner was not restarted: registered spec 222 retry
`j-20261027T193410Z-f1a798` was active at delivery. That historical process had
not loaded the new fallback. Activate the merged owner only after registered
jobs and the current turn reach a safe boundary; preserve their results.
No manual primary cooldown reset or live automatic Argon takeover was tested.


## Live owner activation, 2026-10-28

Under maintainer directive `L20261028072214-302d`, the coordinator stopped the
previous idle owner at a safe boundary and started the existing
`jarvisos-mission-owner.service` with merged source
`0f3a063eea1594934491c474174d8b12a86b0f4f`. This source includes the native
226 fallback and the goal-first continuation correction from PR
#972.

Systemd reports MainPID `3461948`, active since `2026-10-28 08:38:20 CET`
(`07:38:20Z`). The own STOP marker was preserved as
`~/jarvis-control/work/continuity/diagnostics/STOP-resumed-goal-first-20261028T073819Z`.
The registered live 222 importer `j-20261028T072610Z-47f981` continued independently
and completed; its solver work was not killed or restarted by owner activation.
The new owner advanced through cycles 217–219, integrated C2 readiness PR
#973, then continued the
C2 source/capability audit while the coordinator finished the live Example UI.

This discharges the owner source-activation debt. No live automatic takeover,
exhaustion of either primary quota, reset of a cooldown, or new provider reliability
claim was used as activation evidence. The earlier native transport qualification
remains the evidence for that separate boundary.
