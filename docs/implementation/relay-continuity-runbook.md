# Relay continuity supervisor: operational runbook (spec 195)

`scripts/relay_continuity.py` continues a Relay coordinator session chain after a failed turn, and sends sanitized Slack notices. It never modifies the repository, Relay's configuration, permissions or session journals. Durable state is under `~/jarvis-control/work/continuity/` (override it with `RELAY_CONTINUITY_DIR`).

## When to use it

Run the supervisor when you step away from an interactive Relay REPL session and want failures to be handled:

| Failure | What the supervisor does |
| --- | --- |
| Quota exhaustion | Switches to the other agent at once; when both are out, waits for the earliest reset and resumes. |
| DNS/API blips, login failures | Backs that agent off (short backoff, or 30 min to 4 h for a login problem) and continues on the other. |
| Interrupted turns | Stops what the turn left running and continues, retrying a crash on the same agent; 8 consecutive crashes hold. |
| Interrupted operations | Local ones go to the successor to verify. External ones are probed (push, merge, PR creation); an external one no probe settles holds. |

After a successful turn the chain goes **idle** and waits for you. The supervisor never invents new work.

**Only one coordinator at a time.** The supervisor refuses to launch while a Relay REPL (or another `relay run`) is live in the same repository, or while the legacy autopilot scripts run. Close the REPL first, and start the supervisor outside it (for example in tmux).

## Commands

```bash
cd ~/src/JarvisOS
S=$(ls -t .agent-relay/sessions | head -1)        # the session to supervise (normally the REPL's)

# Supervise: continue the chain if its last turn failed; exit idle on success.
tmux new -d -s relay-continuity "python3 scripts/relay_continuity.py run --session $S"

# Hand the chain a new maintainer task (runs on the current agent while it works, else the other):
python3 scripts/relay_continuity.py run --session $S --task-file /path/to/task.md

python3 scripts/relay_continuity.py status        # phase, head session, cooldowns, hold, counters
python3 scripts/relay_continuity.py stop          # graceful: exits at the next safe point, never kills a turn
python3 scripts/relay_continuity.py classify .agent-relay/sessions/$S/turns/turn-004   # one turn's outcome

# Host-side observer (spec 205): owner incidents, Italian digest, REPL turn failures, master merges.
tmux new -d -s relay-watch "python3 scripts/relay_continuity.py watch --digest-hours 2"
python3 scripts/relay_continuity.py notify-test   # see "Observability (spec 205)" for --digest / --alert
```

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Idle, stopped, or waiting for another coordinator to finish |
| 3 | Another supervisor, the legacy autopilot or a lock blocks the start |
| 4 | Held |
| 2 | Refused input (a secret pattern in a task or note), or the kernel refused the subreaper role |

## Holds

A hold means automatic continuation was refused and needs your review. Slack shows `JarvisOS continuity HOLD` with a reason.

| Reason | Meaning and what to check |
| --- | --- |
| `effects_uncertain` | The turn was interrupted during an **external** operation that no probe can settle: a PR comment, a `gh api` write, a `curl` POST, an MCP tool, a notification, or a detached external job. Pushes, merges and PR creations are probed automatically. Check with `gh pr view`/`git ls-remote`/`python3 scripts/devctx.py recover` whether it took effect, then `release`. Interrupted local work (scripts, builds, file writes, servers) never holds: the successor verifies it. |
| `output_unparseable` | The turn transcript is corrupt or torn. |
| `output_missing` | The turn left no output of its own: Relay's "No response captured" turns keep a copy of the previous turn's output, and the agent may still have worked. Check `git log`, `gh pr list`, `devctx.py recover` for what it did. |
| `attempt_unresolved` | A launched attempt left no identifiable Relay session, even if the repository looks unchanged (a merge or a PR comment leaves no local trace). Or a restart could not find it, or the journal records a launch the state does not account for. Establish what happened, then use `release --drop-pending`. |
| `circuit_failures` | 8 consecutive launched turns crashed or failed for an unknown reason (for example an invalid model). Provider limits, outages and login failures never count: they only make that provider wait. |
| `no_provider_available`, `wait_exceeded` | Both agents are spend-capped (no reset), or a wait exceeded 8 days. |
| `permissions_changed` | The authority-bearing parts of `.agent-relay/permissions.toml` changed. |
| `state_corrupt` | The state file is unreadable. Inspect it, then move it aside to reset. |
| `task_check_failed` | A mission task with no completion check claimed it was done. Attach a `contract`, then `release`: the owner re-checks before relaunching. (A failed or unreadable check is a checkpoint, not a hold.) |
| `task_blocked` | A mission task ended `RELAY_STATUS blocked`. Read its `devctx` blocker or question; `release` runs the task again. |
| `task_checkpoint_limit` | A mission task checkpointed 12 times without finishing. Review its progress; `release` allows a fresh bound. |

After review:

```bash
python3 scripts/relay_continuity.py release --note "push landed on origin; safe to continue"
#   --clear-cooldown claude|codex  also clears a provider cooldown (e.g. a spend cap you raised)
#   --drop-pending                 forgets an unresolved launched attempt
python3 scripts/relay_continuity.py run --session $S
```

A release is recorded in the attempt journal and the control ledger. Every hold names the turn it is about (`status` shows `hold.turn`). A release acknowledges exactly that turn: a later turn on the same session (for example, one you ran by hand in the REPL) is gated afresh. A release waives only the review it answers: the released turn is not re-gated. Provider cooldowns and the failure circuit still apply, and a new failure is gated again. Holds raised by the earlier controller for reasons that no longer hold (`live_work`, `reconcile_*`, `auth_failure`, `unknown_failure`, `crash_repeated`, `circuit_attempts`, `repl_active`, `task_report_unverified`, `effects_uncertain`) are decided again automatically on the next run of that chain, without a release. Releasing `permissions_changed` accepts the new settings as the pinned authority.

## Behaviour notes

- **Failover is execution-driven.** A quota, outage or login failure makes only that provider wait (its reset, a short backoff, or a 30 min to 4 h estimate). The other agent continues at once. A crash is retried on the same agent. The chain stays on whichever agent works; a new mission starts on Claude. When both are out, the supervisor sleeps until the earliest reset and resumes by itself.
- **Process ownership.** `run` and `serve` are child subreapers. When a turn ends, everything it left running (dev servers, `nohup` jobs, double-forked daemons) is stopped, so nothing overlaps the next turn. The next turn is told which processes were stopped and restarts what it needs. A server you want to keep across turns is not supported: start it outside the supervised chain.
- **Provider resets.** Reset times come from `agent-relay --json limits --refresh` when the reading is fresh and plausible. Otherwise they come from the provider's own message ("resets 4:20pm (Europe/Rome)", "try again at 11:38 PM"). If neither is usable, the supervisor rechecks after 30 minutes. A Claude "monthly spend limit" message that prints a session reset is treated as a session quota. A spend cap without a reset needs `release --clear-cooldown`.
- **Headless authority is narrower than the REPL's.** Relay applies `permissions.toml` backend modes only in the REPL. Headless continuations run Claude in `--permission-mode default` with Relay's approval hook, so tool calls that need approval are refused, and the agent will report itself blocked. Codex runs with Relay's headless default, which allows workspace writes. The supervisor never widens this.
- **Claude REPL permissions.** On this workstation, `.agent-relay/permissions.toml` selects the REPL launch mode. Keep `[claude.backend].mode = "auto"` for bounded Claude approval and do not set `bypassPermissions` in `.claude/settings.local.json`. That local file allows only `Bash(git status)` and `Bash(python3 scripts/devctx.py recover)` as recovery fallbacks. Relay v0.7.7 runs Claude with `-p`; a `default` mode request outside those rules was denied without an interactive approval prompt in the 2026-10-19 live check. Inspect a fresh turn's `system/init.permissionMode` and its tool results before assuming a mode change worked. The notify-only watcher alerts once per completed REPL turn if the effective mode is `dontAsk`.
- **Restart safety.** Every launch writes an intent record with a nonce before Relay starts. If the supervisor dies, the Relay turn keeps running in its own session. On restart the supervisor waits for that process (it never relaunches it), then finds the session by nonce and classifies it.
- **Slack.** Messages use fixed templates with validated fields only. They never include transcripts, prompts, tool output, paths or secrets. The webhook is read from `~/.config/jarvis/slack_webhook_url`, which must be mode 600. Delivery failures are counted and retried once, and never interrupt supervision.

## Rollback

1. `python3 scripts/relay_continuity.py stop` (or kill the supervisor by exact PID; the running turn is unaffected), and stop `relay-watch`.
2. Restore the Relay REPL handoff order: `cp ~/jarvis-control/work/continuity/backup-20261018/relay-user-config.toml ~/.config/relay/config.toml`. Check it with `sha256sum` against `SHA256SUMS` in that directory.
3. Optionally remove `~/jarvis-control/work/continuity/`. Nothing else reads it.
4. The legacy autopilot is preserved in `~/jarvis-control/autopilot/retired-20261018/` (see its README). Moving the scripts back restores it.

## Interrupted operations (spec 195 gate, amended 2026-10-23)

When a turn ends with operations still in flight, the supervisor first stops every process the turn left running. Then:

1. **Local operations** (scripts, builds, file writes, servers, subagents) are listed to the successor as `INTERRUPTED`. The successor, on whichever provider is available, inspects their effects and redoes them only when safe. If one may have had an external effect it cannot verify, it records a blocker and ends `RELAY_STATUS blocked`, which holds the mission for you.
2. **External operations** are probed read-only by the supervisor:
   - a one-branch `git push`: is the remote at the local commit?
   - `gh pr merge <n>`: is the PR merged, or still open?
   - `gh pr create`: is there an open PR for the head?

   A settled operation is reported to the successor as COMPLETED (do not repeat it) or NOT STARTED. Anything else holds `effects_uncertain`.
3. Every probe result is in `attempts.jsonl` (`"event": "reconcile"`) and in the control ledger.

There is no separate read-only diagnosis any more: in its history it settled only local background launches, which process ownership now handles, and it could not see remote state. A pending diagnosis left by the earlier controller is journalled `abandoned` on the next run.

## Supervised missions (spec 201)

A mission is an ordered list of tasks under one recorded maintainer directive. You submit it and leave. One owner, `serve`, executes queued missions one at a time in submission order. It reuses the supervisor above for every task: classification, failover, adoption after restart, the side-effect gate and holds. Mission state lives in `~/jarvis-control/work/continuity/missions/<id>/`, separate from the `run` chain, so the historical `effects_uncertain` hold on `20261018-215555-4f193a` is untouched.

```bash
RC="python3 scripts/relay_continuity.py"
cat > /tmp/mission.json <<'JSON'
{"authority": "L20261020085928-0431",
 "tasks": [{"text": "Fix the flaky test in ...; open a PR.", "check": {"kind": "pr_merged", "pr": 900}},
           {"text": "Write docs/... and verify it.", "check": {"kind": "path_exists", "path": "docs/..."}}]}
JSON
$RC submit --mission-file /tmp/mission.json   # durable before anything launches; refuses unknown directive ids
$RC missions                                  # every mission: queued | active | waiting | held | complete | cancelled
$RC stop                                      # the owner exits at its next safe point; a running turn is never killed
$RC cancel ID --note "..."                    # only while the owner is stopped and nothing is in flight
```

- **Advancement (spec 204).** A successful provider turn is execution, not delivery. Its final `RELAY_STATUS` only routes it. `continue` is a durable checkpoint: the task stays pending and is resumed on the same chain, at most 12 times across owner restarts (`task_checkpoint_limit`). `blocked` holds (`task_blocked`). A missing or unknown status, and a `propose_done` whose independent read-only check fails or cannot read its evidence (GitHub down, `gh` unauthenticated), are checkpoints too. `propose_done` advances only after the check passes. A task without a check holds with `task_check_failed` when it claims to be done. A held or corrupt mission blocks the missions behind it.
- **Completion contracts.** `pr_merged {"pr": N}` and `path_exists {"path": "rel/path"}` as before; `spec_merged {"spec": "204"}` when the PR number is not known in advance. It reads `docs/specs/STATUS.md` from GitHub master, never the local checkout, and needs the row `merged` with every PR linked in it merged. `coordinator_cycle` is reserved for roadmap cycles (below); `submit` refuses it.
- **Outcomes.** `$RC missions` shows, per mission, the outcome of each recorded task: `verified` (its check passed), `cycle_settled` (a roadmap cycle ended; its work is not thereby verified), `maintainer_attested` (`release --advance`), or `legacy_unverified` (counted by the owner before spec 204 on a provider return alone). `verified` is true only when every task is `verified`. A finished mission's record is never rewritten; the spec 204 audit judges it.
- **Legacy missions.** Definitions are immutable. When the owner next runs an unfinished mission, it reopens every task from the first `legacy_unverified` one on (journal `mission_reopened`; the history stays under `reopened`) and resumes there instead of reporting the mission complete. A task with no check holds when it claims to be done. Give it a contract, with the owner stopped, and release it:

  ```bash
  $RC contract --mission ID --task 1 --check '{"kind": "spec_merged", "spec": "205"}' --note "why this evidence proves the task"
  $RC release --mission ID --note "contract attached"
  ```

  After a release, the owner first re-checks a task whose last turn already reported `propose_done`, and records it complete without a relaunch when the evidence is now there, for example when the PR merged after review. Otherwise the task runs again. `contract` refuses a task that already has a check in its definition, or that is recorded complete. It refuses `coordinator_cycle` except on a recorded roadmap cycle mission.
- **Never a concurrent writer.** While a Relay REPL or other coordinator runs on the repository, the owner waits (`waiting:<id>:repl_active` or `legacy_autopilot`). It never types into or adopts a REPL. Each `jarvis-sandbox` instance has its own PID namespace, so a REPL in another instance is invisible in the owner's process table. `relay` (`scripts/relay_safe.py`) therefore holds a shared flock, `.agent-relay/locks/coordinator.lease`, for as long as any coordinator invocation runs. Daemons, hooks and the owner's own turns are exempt. The owner treats a held lease as `repl_active`. A REPL started by a `relay` installed before this change holds no lease, so restart open REPLs after updating it.
- **Where it runs.** `serve` refuses to start outside the `jarvis-sandbox` boundary (spec 201 D1). Inside, Claude runs in `bypassPermissions` through `scripts/relay_mission_bin/claude`, and Codex runs `danger-full-access`; `bwrap` is the containment. The owner pins `CLAUDE_CONFIG_DIR` and `CODEX_HOME` to the sandbox login state, whatever its launcher exported, and also `GH_CONFIG_DIR` and `GIT_CONFIG_GLOBAL` when the project-scoped GitHub login is installed there. It also links Relay's fixed Claude credential path (`~/.claude/.credentials.json`) inside the sandbox's private home: without a Claude limits reading, a return to Claude after a Codex takeover can never be confirmed.
- **Provider return.** After a Claude quota switch to Codex, Claude is preferred again once its reset has passed, a fresh reading shows room and 20 minutes have elapsed since the switch.

**Roadmap continuation (spec 203).** One sentence from the maintainer, "Continue developing JarvisOS according to the roadmap", is enough. The coordinator in the Relay REPL records it and starts roadmap mode:

```bash
python3 scripts/devctx.py directive "<the maintainer's message>" --source "relay <session>"   # prints L...
$RC roadmap start --authority L...
```

The maintainer then exits the REPL: the owner starts only once the coordinator lease is free. While roadmap mode is on and every mission has finished, the owner queues one cycle at a time. Each cycle is a one-task mission, and its turn runs the Generic Frontier Builder Contract against fresh GitHub, `STATUS.md` and the ledger. Missions you submit yourself go first. A cycle carries the `coordinator_cycle` contract. It settles when its turn ends `propose_done` or `blocked` while the remote heads are readable, and is recorded `cycle_settled`. That says the cycle ran, not that the work it chose is delivered: a later cycle recovers that work from fresh PR and `STATUS.md` state. A cycle that checkpoints (`continue`) stays pending.

- **Pauses.** Roadmap mode pauses, and sends a Slack `roadmap_paused` notification, in three cases:
  - a cycle ends with `RELAY_STATUS blocked`: the coordinator needs you, or no authorized work remains, and it records why with `devctx.py note question` or `blocker`;
  - three cycles in a row leave the remote branch heads unchanged (`no_progress`);
  - a cycle is cancelled (`cycle_cancelled`).

  While `git ls-remote` fails, the owner submits nothing. A held mission blocks the queue as usual.
- **Commands.** `$RC roadmap status` (also shown by `$RC missions`) reports the state. `$RC roadmap stop --note ...` pauses after the running cycle; to stop that turn too, use `$RC stop`. To resume, run `roadmap start` again.
- **Single writer.** Spec 079's `JARVISOS_CONTINUATION_MODE` must stay `OFF` while roadmap mode is on. `EXECUTE_NO_MERGE` would add a second writer on the same PR.

**Persistent owner (D2).** The user unit runs the owner through the launcher. Create it from a host shell, not from inside the sandbox: there, `~/.config` is sandbox state and the user bus is hidden. Check that `jarvis-sandbox` forwards a command line this way (`jarvis-sandbox python3 -c 'import socket; print(socket.gethostname())'` prints `jarvis-sandbox`) before you enable it. `~/.config/systemd/user/jarvisos-mission-owner.service`:

```ini
[Unit]
Description=JarvisOS supervised mission owner (spec 201)
[Service]
WorkingDirectory=%h/src/JarvisOS
ExecStart=%h/.local/bin/jarvis-sandbox python3 scripts/relay_continuity.py serve --interval 60
KillMode=process
Restart=on-failure
RestartPreventExitStatus=2 3
RestartSec=120
[Install]
WantedBy=default.target
```

`systemctl --user daemon-reload && systemctl --user enable --now jarvisos-mission-owner`. Exit 2 (outside the sandbox, no Claude CLI, no subreaper role) and 3 (another owner) are not restarted. `KillMode=process` does **not** keep a running provider turn alive: the owner runs in `jarvis-sandbox`'s PID namespace (`--die-with-parent`), and stopping the main process ends every process in it. The next owner settles the killed attempt as `crash` and passes it through the side-effect gate before any relaunch. To restart without killing a turn, run `$RC stop`, and wait until `systemctl --user status jarvisos-mission-owner` shows it exited at a task boundary (status 0, so `Restart=on-failure` leaves it inactive). Then `rm ~/jarvis-control/work/continuity/STOP && systemctl --user start jarvisos-mission-owner`; while STOP exists, a started owner exits at once. With `Linger=no` the unit runs only while a login session exists. Enable lingering (`loginctl enable-linger $USER`) only if unattended operation without a login is needed. WSL start after a Windows reboot is verified separately.

**Rollback.** `systemctl --user disable --now jarvisos-mission-owner`, then remove the unit file and `daemon-reload`. Missions, journals and Relay sessions stay in `~/jarvis-control/work/continuity/missions/` and `.agent-relay/sessions/`. A later owner resumes queued missions from them. `$RC stop` without systemd has the same effect on a foreground owner.

## Observability (spec 205)

The mission owner runs in the sandbox, which cannot read the Slack webhook. Slack delivery therefore comes from `watch`, which runs on the **host**. It reads the owner's state (missions, `roadmap.json`, `STOP`, the kernel lock table for `serve.lock`, Relay turn `state.json`) and writes only `~/jarvis-control/work/continuity/watch/`. It takes no owner lock and calls no provider. The same adapter, `scripts/relay_observe.py`, backs the terminal monitor.

- **Digest:** every 2 hours by default (`watch --digest-hours 1..4`). It is in Italian and covers *In corso* (state, goal, current claim, why), *Fatto dall'ultimo aggiornamento* (task outcomes labelled verified, settled cycle or unverified; PRs merged to master; the last turn's report and verification), *Prossimo* and *Serve intervento*. The state is one of: running, queued, checkpointed, verified complete, held, paused, waiting, idle, stopped, unknown. A running state is never inferred from GitHub.
- **Alerts:** sent on detection, at the watcher's polling interval (`--interval`, default 60 s). They cover a held mission, a paused roadmap (with the recorded question when blocked), an owner that is not running while work is pending, a wait longer than 6 hours and unreadable owner state. Each incident is announced once until it clears, including across watcher restarts. When Slack is down nothing blocks: the message is retried on the next poll, and the failure count appears in the next delivered digest.
- **Test commands (host):** `python3 scripts/relay_continuity.py notify-test --digest` sends the current digest, and `notify-test --alert` sends an alert sample marked as a test. Each prints the delivered text and never prints the URL. To preview without sending, run `python3 scripts/relay_observe.py digest`; `relay_observe.py status` prints the snapshot and incidents as JSON.
- **Terminal monitor:** `python3 scripts/relay_observe.py follow` (`--no-replay` starts at the live end). It follows the active mission's latest turn, announces task, phase, provider and session changes, and prints messages, tool calls and redacted, bounded results. It never shows hidden reasoning. Ctrl-C only closes the viewer.
- **Host unit (spec 207):** `~/.config/systemd/user/jarvisos-relay-watch.service` is written by `host_ops.py install --sha <merged master sha> --with-watch` from a host shell. See host installation and rollback (not carried into this repository). Its template is in the installed release:

```ini
[Unit]
Description=JarvisOS Relay observer: Slack digest and incident alerts (spec 205)
[Service]
WorkingDirectory=%h/src/JarvisOS
ExecStart=/usr/bin/python3 -I %h/.local/share/jarvisos-hostops/current/scripts/relay_continuity.py watch --digest-hours 2
Restart=on-failure
RestartPreventExitStatus=2 3
RestartSec=60
[Install]
WantedBy=default.target
```

The installer reloads user units and starts the watcher only with `--with-watch`. After installation the sandbox may request `host_ops.py request service.install --args '{"template":"jarvisos-relay-watch"}' --wait 60` for a signed installation receipt. Run either this unit or a tmux `watch`, never both: the second one exits 3 on `watch.lock`, which the unit does not restart. The watcher is a separate process, so it reports an owner crash or stop while work is pending (`owner_down`) without an `OnFailure=` hook. A watcher that is itself down cannot report anything: check `systemctl --user status jarvisos-relay-watch`. Rollback: `systemctl --user disable --now jarvisos-relay-watch`.
