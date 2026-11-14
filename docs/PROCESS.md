# How work is done

This is the one process document for people and agents working on JarvisOS. `AGENTS.md` holds the commands, the
conventions the code does not show, and the "never" rules; this file holds how a piece of work moves from idea to merge.
It states goals and the evidence that must exist, not a script: the acting model chooses the method.

If this file disagrees with the code or with `docs/specs/STATUS.md` about a fact, the code or the registry wins and this
file is fixed in the same change.

## 1. Sources of truth

| Question | Authority |
| --- | --- |
| What does the system do? | The code, the tests, fresh runtime evidence |
| Spec state, hard dependencies, implementation PR | `docs/specs/STATUS.md`, and only that |
| What must the current slice deliver? | The active spec (`docs/specs/NNN-*.md`) |
| What may an agent do, and what never? | `AGENTS.md` |
| Durable architecture decisions | `docs/DECISIONS.md` |

READMEs, chat handoffs, old comments, branch names and model summaries are context, not authority. A current explicit
maintainer directive sets priorities, goals and process rules; `docs/specs/STATUS.md` remains the live registry of spec
state, dependencies and implementation PRs.

## 2. Roles

Roles follow granted capability, never model or vendor name. Do not write a model order or a model id into documents or
prompts; the router and the configuration own them.

| Role | Who | May | Limits are enforced by |
| --- | --- | --- | --- |
| Coordinator | A capable model with the tools to own one front | Plan, decide the method, implement, delegate, review, merge, reconcile | Granted tools, credentials and branch rules |
| Peer | Another capable session | Independent review, disjoint implementation, research | Its own worktree and the permissions it was given |
| Worker | A cheaper or restricted model | One bounded task on a given worktree and paths; proposal-only if so configured | The sandbox and the narrow paths and tools it receives, not prose |

**Delegating.** The coordinator classifies a delegable task by difficulty and by risk (what it costs if the worker is wrong:
low = docs, tests, copy; medium = feature code covered by tests; high = security, egress, credentials, migrations, data
loss, supervisor/relay/sandbox, irreversible operations), then runs

```bash
python3 scripts/worker_routing.py pick --difficulty D --risk R
```

and delegates to the worker it returns: the cheapest adequate one whose subscription pool has headroom. `no_worker`
means the coordinator does the work or waits. The router only reads quota; it launches nothing and grants no authority.
The coordinator reviews the worker's diff in proportion to risk before adopting it, and runs the checks itself.
Record every worker outcome with `python3 scripts/worker_routing.py record` (pass `--evidence <dir>` for each agy worker run). On a model failure, escalate with salvage (the next worker starts from the
failed branch or diff plus the findings, unless the coordinator records why the approach is wrong).

Worker routing rules (ADR-062): Gemini first for workers at every tier, with effort from difficulty times risk (Flash
low, medium or high; Argon medium or high when its own bucket has room). Claude and GPT workers are used only when the
gemini pool is below its reserve, when the measured record says Flash is not adequate for that kind of task, or as the
escalation after a Flash attempt on the same task failed its checks; never loop cheap attempts, and waiting is an
acceptable outcome. There is no model-identity bias: the gates (CI, spec proofs, browser walk, the coordinator's review,
risk rules) are the quality guarantee for every model. An independent review prefers another family (`pick --review-of
FAMILY`) but never blocks on it. Coordinators are not changed by these rules.

A coordinator owns ordinary reversible technical decisions: method, touch set, refactors, tests, docs, planning
compression, technical merge. It does not own branch protection, rulesets, required checks, credentials, spending or
anything in section 7.

## 3. Specs

A spec is an outcome contract. It freezes the user-visible outcome, scope and non-goals, hard boundaries, acceptance
criteria, and any evidence that cannot be inferred from the code. It does not freeze the implementation method unless the
mechanism is part of the outcome. Start from `docs/specs/SPEC_TEMPLATE.md`.

`docs/specs/STATUS.md` lists the open specs with status, dependencies and implementation PR:

| Status | Meaning |
| --- | --- |
| `planned` | Identified; **never** authorizes implementation |
| `blocked` | An explicit dependency or maintainer decision stops it |
| `ready` | Accepted contract; implementation may start once hard dependencies are merged |
| `in_progress` | Implementation active, no PR yet |
| `in_review` | An implementation PR is open |
| `merged` | Merged (the completing implementation PR may write this itself); prune the row and the spec file in a later change |
| `cancelled` | Will not be done under this identity |

Rules:

1. **The coordinator owns spec acceptance.** It drafts or takes the spec, reviews it critically against overengineering
   (section 6: is every part needed for the outcome?), and if it agrees moves the row to `ready` and implements it
   without a maintainer approval step. Implementation starts only from `ready` with every hard dependency merged (or no
   longer listed). The review checks the spec's code facts against the code first; from spec 270 they are
   `path::Symbol` references that `scripts/check_code_facts.py` keeps resolving while the row is `ready`.
2. The implementation PR carries `**Spec gate:** implementation NNN` in its body; the row must be `in_review` with this
   PR's number, or `merged` with it when this PR completes the spec (the row reaches the base branch only by that merge, so
   it is never early there). A slice that leaves work open keeps `in_review`. `definition NNN` or `N/A` is used for
   planning and process work. `scripts/check_spec_status.py` enforces it.
3. Definition, spec, acceptance (`ready`) and implementation may be combined or split as risk warrants; keep a separate
   planning checkpoint only when destructive authority, credentials, migrations, security boundaries or an open question
   to the maintainer (section 7) require it.
4. Do not open a lifecycle PR that only repeats accepted information. Do not copy spec state into READMEs, specs, chat
   handoffs or prompts.
5. A coordinator may close a `planned` row without implementing it when fresh evidence shows it adds nothing: it is
   already delivered by another owner (absorb), obsolete (cancel), or waits for a real trigger (defer). Say which, in the
   row.

## 4. Pull requests, evidence and merge

1. **One task, one worktree, one branch.** Recover existing work (branches, PRs, `devctx.py recover`) before opening new
   work. Disjoint work may run in parallel worktrees; shared GitHub writes stay serialized.
2. **Run the checks that causally cover your change** before pushing. Leave the full suite to the sharded CI run on the
   exact head unless a local-only proof is missing.
3. **Evidence is proportional to risk** (`AGENTS.md`, "Evidence by risk"). Review findings are evidence, not orders:
   reproduce them against the exact head and the accepted contract, fix the real ones, rebut the false ones precisely,
   PARK the non-blocking ones.
4. **Severity** is by impact. P0: secret exposure, data loss, catastrophic authority failure. P1: an accepted criterion
   fails or a main workflow breaks. P2: a real weakness; it blocks only if it materially affects correctness, security,
   usability or evidence. P3: polish. P0 and P1 block.
5. **One repair wave per causal family.** When a defect is found, sweep its bounded family before patching the first
   symptom, repair the family together, add the minimum causal test, and do not widen into unrelated architecture. While a
   wave is active, other reviewers do not restate recorded findings.
6. **Freeze, then converge.** When the outcome is implemented and no known P0, P1 or blocking P2 remains, the head is
   frozen: no polish, no speculative robustness. A new material defect unfreezes it; nothing else does.
7. **Required checks** are the CI jobs `backend`, `gen-check`, `doc-paths` and `leak-guard`, plus the frontend build when
   the frontend changed. Environment evidence (browser walk, hardware, live integration) is separate and cannot be
   replaced by a review.
8. **Merge** is an explicit act by the coordinator, never GitHub auto-merge. Immediately before it, check only what can
   invalidate it: the head SHA is the one that was checked and reviewed, required checks are green on it, no P0 or P1 is
   open, the registry row is truthful, and the PR is mergeable against the current base. Then merge, verify the base
   branch; the registry already says `merged` when the PR wrote it; otherwise update it in the next PR that touches the
   registry (no PR only for that). Continue with the next authorized goal. Do not launch one more review after every
   condition is met.
9. **A head change invalidates only the evidence it affects.** Reuse the rest.

## 5. Supervisor and Relay

Relay runs the agent CLIs. `scripts/relay_continuity.py` supervises coordinator sessions so that work continues when you
are away. Operations manual: `docs/implementation/relay-continuity-runbook.md`.

- **Failover.** A quota, outage or login failure makes only that provider wait; the next agent continues. After a clean
  turn the chain goes idle and waits.
- **Missions and roadmap mode.** A mission is an ordered task list queued under a recorded maintainer directive and run
  one task at a time. In roadmap mode the supervisor keeps one coordinator cycle queued; the cycle reads `docs/specs/STATUS.md` and
  the fresh GitHub state, picks the highest-value authorized work, and drives it to a merged and reconciled result or to a
  durable checkpoint (a pushed branch or PR plus a ledger note).
- Every coordinator turn records a maintainer update with `python3 scripts/devctx.py note note --lane maintainer "Sto … . Prossimo: …"`: one or two plain Italian sentences about what the maintainer sees, naming a feature by its visible name or a spec by its subject, without ids, SHAs, paths or codenames.
- **Recovery packet.** Start every resumed or taken-over session with `python3 scripts/devctx.py recover`. It prints a
  freshness-checked picture of Git, GitHub, `docs/specs/STATUS.md`, worktrees and the local control ledger. Record what the code
  cannot show with `devctx.py note` (kinds include `question` and `blocker`) and `devctx.py directive`, not in chat
  handoffs. Maintainer directives and holds shown there bind you.
- **Long work** (solvers, studies, benchmarks) runs as a registered durable job: `python3 scripts/relay_jobs.py start ...`.
  Never keep a turn open waiting for a job or for CI: checkpoint, end the turn, and a later turn collects the result.
  A running job blocks only the work that depends on it.
- **Ending a supervised turn.** End with `RELAY_STATUS propose_done` when the work is merged or checkpointed,
  `RELAY_STATUS continue` after a durable checkpoint, or `RELAY_STATUS blocked` after recording a `question` or `blocker`
  note and only when no authorized useful work remains.
- **Holds.** The supervisor stops and asks for review when it cannot tell whether an external operation (a push, a merge,
  a comment) took effect. Check the effect, then `python3 scripts/relay_continuity.py release --note "..."`. A turn that
  was interrupted may have done part of its work: inspect before repeating, and never repeat an external operation that
  must not happen twice.

## 6. Convergence habits

Before building a new capability, look for a mature existing library or tool and prefer depending on it, recording its name, version and licence (spec 239 inventory); keep author and licence attribution on any code copied on purpose, and never copy code of unknown provenance or with a licence incompatible with this all-rights-reserved repository.

Finish rather than keep finding reasons not to finish. Before adding infrastructure, a workflow, a state store, a
governance rule or a general abstraction, answer: is it needed for the accepted criterion, can the criterion be reached
without it, and what concrete risk justifies the new surface? If the criterion is reachable without it, do not build it.
A slow or unavailable reviewer is not a reason to build review machinery; use another qualified session or, for a risk
tier that does not require independence, the coordinator review plus the required execution evidence.

## 7. When to contact the maintainer

Only for these cases (outside a requested status report):

1. real spending is required, or a budget limit is at risk;
2. a credential, account, repository or human-controlled permission is missing;
3. a security issue, a secret exposure or a material destructive or data-loss risk needs human awareness;
4. no two practicable safe routes forward exist;
5. the maintainer explicitly asked to be shown alternatives to choose from;
6. the coordinator has a real product doubt that the code and evidence cannot settle.

**Question format.** Every question to the maintainer is pre-structured so it can be answered with one click or one
word: one short question, 2 to 4 options with the recommended one first and marked `(consigliata)`, one line per option
on its consequence, and `altro` always allowed. Write it in Italian, plain language, without internal acronyms. Record
it with `devctx.py note question` and continue with any authorized work that does not depend on the answer.

A technical obstacle opens engineering work; it is not by itself a block.

## 8. Changing the rules

`AGENTS.md` and this file change on the maintainer's request, or when repeated measured delivery failure shows a rule is
the problem. Prefer deleting duplicated rules to adding new ones, and keep one source for each fact. Rules that must hold
every time belong in CI or in tool permissions, not in prose.

Backlog: an informative (non-blocking) report of dead code, unused dependencies and cross-module imports, to be read by
the coordinator periodically; promote a check to blocking only if the same problem keeps coming back.
