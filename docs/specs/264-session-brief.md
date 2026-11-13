# 264 — SESSION-BRIEF-1: operator brief and standing directive text at session start

Status: ready
Depends on: none

## Goal

When the mission owner opens a coordinator turn in a **new model context**, the prompt carries (a) the operator's
mission brief, when the operator has configured one, and (b) the full text of the directives that stand behind the
current mission. A turn that resumes the same agent's context gets neither again. The public repository never contains
the brief, its path or any directive text.

## Why

Maintainer directives `L20261113011123-36ed` and `L20261113093830-1603` (item 3). The recovery packet shows the latest
four directives cut to 160 characters (`devctx.py` `render`); everything older is behind "`+N older`". The operator's
statement of what the work is for lives in a private file a coordinator reads only if it remembers to. Sessions on
2026-11-13 left the directed order because neither text reached them. Putting both texts in the prompt removes that
dependency on memory.

## Code facts (verified at `2ef5d2b6`)

- `scripts/relay_continuity.py` `Supervisor._launch`: builds the turn text, appends the mission task under
  `MISSION_HEADER`, then appends `self.host.worker_routing()` inside a `try` so a failure only logs. It computes
  `parent` (the Relay session to continue; `None` starts a new one) from `self.state["agent"]` and `NATIVE_AGENTS`
  before overwriting `self.state["agent"] = agent`, and journals a `launch` event from `pending`.
- `Host` is a dataclass of injectable callables (`worker_routing: Callable[[], str] | None = None`); `make_host`
  wires the real ones, so tests inject fakes.
- The owner's state root is `control_root()` (`RELAY_CONTINUITY_DIR`, default outside the repository); it is writable
  by the owner and outside Git.
- A mission carries `authority`, a directive ledger id (`validate_mission`, `submit_mission`).
- Directive ledger entries (`devctx.record_directive`) carry `directive.path` and `directive.sha256`;
  `devctx.directive_provenance` already treats a hash mismatch as "modified after recording". Entries named in another
  entry's `supersedes` are inactive (`devctx.build_packet`).
- `ROADMAP_TASK` is the cycle text a roadmap mission is submitted with.
- `.gitignore` ignores `.agent-relay/`, where Relay stores each turn's `prompt.md`.

## Scope

In scope:

1. **New-context rule.** A turn is in a new context when `parent is None` or the launching agent differs from the
   agent of the previous turn (`self.state["agent"]` before it is overwritten). Only such turns get the block.
2. **Brief.** The brief's location is one absolute path written in a file named `mission-brief` in the owner's state
   root (`control_root()`); no default path exists in code. Missing setting file: nothing changes, no log line.
   Brief readable and at most 24 000 bytes: a block with a fixed header (operator's private brief; read it before the
   recovery packet; nothing from it may be copied into the repository, PRs, commits, issues or public notes; it grants
   no authority beyond the recorded directives) followed by the text. Configured but missing, unreadable, not absolute
   or over the limit: one line saying the brief could not be attached and why (no path), one log line, launch proceeds.
3. **Standing directives.** For a mission turn: the full text of the mission's `authority` directive and of every
   directive recorded after it that is not superseded, oldest first, each under its ledger id. A directive whose file is
   missing or whose SHA-256 differs from the ledger is replaced by one line naming its id and the reason. Total directive
   text is capped at 24 000 bytes; when the cap is reached, the newest directives are kept and the omitted ids are listed
   on one line. No mission (plain continuation): no directive block.
4. **Journal.** The `launch` event gains `brief` (first 12 hex characters of the attached brief's SHA-256, or null) and
   `directives` (list of attached ledger ids). Never text, never a path.
5. **Fail-open.** Any exception while building either block logs its type and launches without it, like the
   worker-routing block.
6. **Cycle text.** `ROADMAP_TASK` gains one generic sentence: when an operator brief is attached it states what the
   work is for; judge value against it, and say in the cycle note when you depart from its suggested order.

Out of scope (binding non-goals):

- No copy of the brief or directive text into the repository, the ledger, notifications or worker task files.
- No templating, summarising or per-agent variants; no new store or mission type.
- No change to `devctx.py recover` output.
- Worker (`agy_worker.py`, `relay_jobs.py`) prompts are unchanged.

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_continuity.py`
- `docs/specs/STATUS.md`, this file

## Design constraints

- Resumed same-agent turns are byte-for-byte unchanged when no brief is configured and there is no mission.
- With no `mission-brief` setting and no mission, every prompt is byte-for-byte what it is today.
- The block is appended after the mission task and before the worker-routing block.
- Reading happens in `make_host` callables (or one new `Host` callable); `Supervisor` stays testable with fakes.

## Acceptance criteria

1. Setting absent, no mission: launched text identical to today's for new and resumed turns.
2. Setting present, new context: the brief block appears exactly once; resumed same-agent turn: absent.
3. Agent switch with a non-null `parent`: the block is attached.
4. Oversize, missing and unreadable brief: a one-line notice without the path; the launch proceeds.
5. Mission turn in a new context: authority directive and later non-superseded directives attached in order; a
   superseded one is absent; a hash-mismatched one is replaced by its notice line; over the cap the newest are kept and
   omitted ids listed.
6. The `launch` journal record carries `brief` and `directives` and never the text or path.
7. A raising brief or directive reader logs and still launches.
8. A test asserts `.agent-relay/` stays in `.gitignore`.

## Required tests

Offline unit tests in `backend/tests/test_relay_continuity.py` with a fake `Host` and a temporary ledger/control root
covering criteria 1–8.

## Definition of done

Test gate green, acceptance criteria met, full diff reviewed by a second session (risk high: supervisor code), row
`merged`. Live activation follows the owner's next release install and writing the `mission-brief` setting in the
owner's state root; the first new-context cycle after it shows the block in its stored prompt.
