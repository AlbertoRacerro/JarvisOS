# 263 — WORKER-RESULT-GUARDS-1: headless worker result classification

Status: ready
Depends on: none

## Goal

The delegated headless worker wrapper (`scripts/agy_worker.py`) treats an empty model response as a harness failure
even when the worker changed files, and preserves a non-empty plain-text response when JSON parsing fails. A review
or report response can succeed without changing source files. Existing sandbox, credential, permission-denial and
stream-status guards remain the baseline for these result checks.

## Why

Maintainer directives `L20261110070734-85cb` and `L20261113011123-36ed`: prevent silent worker failures where permission
prompts, unhandled tool denials, stream interruptions, or blank model turns are falsely recorded as successful runs. Under
directive `L20261109090114-5e79`, a harness failure is never a model capability verdict: accurate classification allows
the supervisor to retry or recover without burning provider diversity or misjudging model competence.

## Code facts (verified at `0adcb5d6`)

- `scripts/agy_worker.py` `in_sandbox`: verifies the certified sandbox environment boundary (spec 201 D1) by checking
  that `socket.gethostname() == SANDBOX_HOSTNAME` (`"jarvis-sandbox"`) and asserting that neither Windows nor WSL mount
  paths exist (`not Path("/mnt/c").exists()` and `not Path("/run/WSL").exists()`).
- `scripts/agy_worker.py` `child_env`: strips GitHub and Git credential tokens from the subprocess environment
  (`STRIPPED_ENV`: `GH_TOKEN`, `GITHUB_TOKEN`, `GH_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`, `SSH_AUTH_SOCK`, `GH_ENTERPRISE_TOKEN`,
  `GITHUB_PERSONAL_ACCESS_TOKEN`, and `STRIPPED_PREFIXES`: `GH_`, `GIT_`). It points `GIT_CONFIG_GLOBAL` to `/dev/null`, sets
  `GIT_CONFIG_NOSYSTEM="1"`, `GIT_TERMINAL_PROMPT="0"`, and isolates `GH_CONFIG_DIR` to an empty subdirectory under evidence.
- `scripts/agy_worker.py` `run_argv`: constructs argument vector for headless worker execution. At `0adcb5d6` (which includes
  merged PR #66 / commit `9a319903`), `run_argv` passes `[agy, "-p", task, "--model", model, "--output-format", "json",
  "--mode", "accept-edits", "--dangerously-skip-permissions", "--sandbox", "--print-timeout", f"{timeout}s"]`, and
  `FORBIDDEN_FLAGS` is `("--remote-control",)`. *(Code-fact check note: prior to PR #66, `--dangerously-skip-permissions` was
  listed under `FORBIDDEN_FLAGS`; PR #66 permitted it for sandboxed execution).*
- `scripts/agy_worker.py` `_run`: initializes `status, reason = "succeeded", None`. It checks execution return code
  (`proc.returncode != 0`), subprocess timeout (`subprocess.TimeoutExpired`), and git metadata tampering
  (`git_fingerprint(git_dir, common)` comparison). It writes stdout to `transcript.json` (if valid JSON) or `transcript.txt`,
  writes stderr to `stderr.txt`, and independently captures working tree diffs to `diff.patch` via `collect_diff`.
- `scripts/agy_worker.py` `_run` result classification: examines parsed JSON `document` from stdout. If `denied_actions` is
  present and non-empty, it sets `status="failed"`, `reason="permission_denied: <actions>"`. If `document.get("status") != "SUCCESS"`,
  it sets `status="failed"`, `reason="stream_status_<status>"`.
- `scripts/agy_worker.py` `_run` empty result vs diff check: lines 338-341 evaluate `elif not str(document.get("response") or "").strip() and not diff: status, reason = "failed", "empty_result"`, and on non-JSON output evaluate `status, reason = "failed", "empty_result" if not diff else "stream_status_missing"`. This logic partially conflates an empty diff with an empty result, treating empty model output as success if diff is non-empty, and failing unparsed non-empty text if diff is empty.
- `scripts/agy_worker.py` `failure_class`: classifies non-succeeded runs into `"policy"` (`git_metadata_modified`),
  `"harness"` (`empty_result`, `permission_denied:*`, `stream_status_*`, `timeout`, `spawn_failed:*`, or runner errors mapped via
  `relay_argon.harness_cause`), `"provider_quota"`, or `"model_or_unknown"`.
- `scripts/agy_worker.py` `default_evidence_dir`: generates unique run evidence directories under
  `~/jarvis-control/work/evidence/` using an ISO timestamp and random hex suffix (`secrets.token_hex(3)`).
- `backend/tests/test_agy_worker_221.py`: exercises `agy_worker` against a mock executable (`FAKE` script via `Env` fixture).
  Existing tests cover argument passing, credential stripping, sandbox check refusal, denied actions, stream error statuses,
  and failure classification.

## Scope

In scope:

1. **Empty output with a diff.** Classification in `_run` treats empty model output as a harness failure
   (`status="failed"`, `reason="empty_result"`, `failure_class="harness"`) even if the worker changed source files.
2. **Decoupling empty diff from empty model output.** A review or report task may legitimately produce no repository file
   modifications (`diff_bytes == 0`). Such runs succeed when non-empty model response text is produced. Conversely, runs
   where the model emits empty output (`response` is empty or whitespace-only) are classified as `empty_result` harness
   failures regardless of whether source files were modified.
3. **Preservation of unparsed model output.** Non-JSON plain text model output on stdout is written to `transcript.txt` and
   is not classified as `empty_result` when genuine text content is present and the process exits successfully. A nonzero
   exit or a known runner error still fails.
4. **High-risk evidence qualification.** Verification requires a second-session full diff review and real-environment proof
   inside the certified sandbox.

Out of scope (binding non-goals):

- Quota retry scheduling, quota reset tracking, and provider fallback sequencing (owned by later slices under directives
  `L20261110070734-85cb` and `L20261113011123-36ed`).
- Timeout resumption policies, conversation continuation management (`--resume-conversation` multi-turn continuity) in
  relay/supervisor (owned by later slices).
- Multi-pool worktree allocation, clean worktree provisioning, or router policy alterations (`scripts/worker_routing.py`).
- Repeating or modifying already-merged baseline changes from PR #66.
- Modifications to host-ops supervisor daemons, systemd unit templates, or host-level deployment policies.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `scripts/agy_worker.py`
- `backend/tests/test_agy_worker_221.py`

## Design constraints

- **High-risk boundary:** Execution involves permission elevation (`--dangerously-skip-permissions`), credential scrubbing,
  and subprocess containment. Any changes fall under AGENTS.md High Risk: requires green tests, green CI on exact head, a full
  diff review by a second qualified session, and mutation proof in the real environment.
- **Fail-closed sandbox guard:** `in_sandbox()` must pass before `--dangerously-skip-permissions` can reach a subprocess.
  Execution must refuse before invoking the binary if the hostname does not match `SANDBOX_HOSTNAME` or if WSL/Windows mounts exist.
- **Credential isolation:** Child process environment must never inherit GitHub tokens, personal access tokens, Git global/system
  credentials, or SSH sockets (AGENTS.md Never 6).
- **Harness vs capability separation:** Harness failures (permission denials, stream errors, timeouts, empty model outputs) must
  map strictly to `failure_class="harness"`. They must never be classified as successful turns or recorded as model failures.
- **Decoupled diff semantics:** Review/report tasks legitimately produce zero diff. Diff presence or absence must never be used
  as a proxy for model response existence.
- **Stdlib-only implementation:** `scripts/agy_worker.py` must remain standard-library-only.
- **Public repository hygiene:** No private project or customer names, internal file paths, machine hostnames, or confidential
  tokens in code, comments, docstrings, tests, fixtures, or commits.

## Acceptance criteria

Checkable statements. Each one must be objectively verifiable from the diff or a test.

1. `run_argv` includes `--dangerously-skip-permissions`, `--mode accept-edits`, and `--sandbox` when running inside the
   certified sandbox (`in_sandbox() == True`); execution refuses with `not_sandbox` before spawning when `in_sandbox()` is False.
2. `child_env` removes `GH_TOKEN`, `GITHUB_TOKEN`, `GH_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`, `SSH_AUTH_SOCK`, `GH_ENTERPRISE_TOKEN`,
   `GITHUB_PERSONAL_ACCESS_TOKEN`, and all `GH_*`/`GIT_*` variables, isolates `GH_CONFIG_DIR` to an empty directory, and sets
   `GIT_CONFIG_GLOBAL="/dev/null"`, `GIT_CONFIG_NOSYSTEM="1"`, and `GIT_TERMINAL_PROMPT="0"`.
3. If the worker transcript reports non-empty `denied_actions`, `_run` sets `status="failed"`, `reason="permission_denied: <actions>"`,
   and `failure_class="harness"`.
4. If the worker transcript reports stream `status` other than `"SUCCESS"`, `_run` sets `status="failed"`,
   `reason="stream_status_<status>"`, and `failure_class="harness"`.
5. If the model produces empty output (`response` is empty or whitespace), `_run` sets `status="failed"`, `reason="empty_result"`,
   and `failure_class="harness"`, regardless of whether `diff` is empty or non-empty.
6. A review/report task with a non-empty model response and zero source changes (`diff_bytes == 0`) completes with
   `status="succeeded"`, `reason=None`, and `failure_class=None`.
7. A zero-exit run with non-JSON stdout containing non-empty plain text saves it to `transcript.txt` and succeeds;
   a nonzero exit or known runner error still fails.
8. **High-risk evidence qualification:**
   - Full diff review completed and documented by a second qualified session.
   - Real-environment sandbox proof: verified execution inside the certified sandbox (`jarvis-sandbox`) confirming full
     permissions execute without interactive permission prompts, credentials remain stripped, and transcript/diff isolation holds.

## Required tests

Concrete offline-only tests in `backend/tests/test_agy_worker_221.py` using the fake runner fixture:

- `test_sandboxed_full_permissions_and_stripped_credentials`: confirms `--dangerously-skip-permissions` is passed and credentials
  are purged in the child process.
- `test_not_sandbox_refusal`: confirms execution aborts with `not_sandbox` without spawning `agy`.
- `test_permission_denials_classified_as_harness`: simulates transcript with `denied_actions`; asserts `status="failed"`,
  `reason="permission_denied: ..."`, and `failure_class="harness"`.
- `test_stream_error_status_classified_as_harness`: simulates transcript with `status="ERROR"` and `status="FAILED"`; asserts
  `status="failed"`, `reason="stream_status_ERROR"`, and `failure_class="harness"`.
- `test_empty_model_response_classified_as_harness`: simulates empty `response` with both zero and non-zero git diffs; asserts
  `status="failed"`, `reason="empty_result"`, and `failure_class="harness"`.
- `test_review_task_without_diff_succeeds`: simulates review/report output with non-empty `response` and zero git diff; asserts
  `status="succeeded"`, `reason=None`, and `failure_class=None`.
- `test_unparsed_plain_text_output_not_empty_result`: simulates a zero-exit non-JSON plain text response; asserts text is
  stored in `transcript.txt` and the run succeeds, then verifies a nonzero exit still fails.
- `test_failure_class_mapping_all_guards`: verifies `failure_class()` maps all permission denial, stream status, and empty result
  reasons to `"harness"`.

## Definition of done

Test gate green (`backend/tests/test_agy_worker_221.py` and lint passing), acceptance criteria 1–7 verified by offline tests,
high-risk criterion 8 proven (second-session diff review and real sandbox execution receipt attached), spec status updated in
`docs/specs/STATUS.md`, and brief summary delivered.
