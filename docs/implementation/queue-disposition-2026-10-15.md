# Open queue disposition (2026-10-15)

Status: **planning record**. Written under the 2026-10-15 maintainer directive "continue until all the queue is done", after the Engineering Studio program (187–192) merged. Reviewed against master `af053d31`. `STATUS.md` remains the only live registry. This record explains the row changes made with it and carries no implementation authority beyond what `STATUS.md` grants.

Method: every `planned` or `blocked` row was checked against its spec file (where one exists), its row note, and the code on master. The dispositions follow the execution protocol §3: IMPLEMENT, ABSORB, CANCEL, TRIGGER-DEFER. A TRIGGER-DEFER row stays `planned` and names its trigger. A row whose trigger would cross a hard boundary (external egress, provider credentials, spending, heavy external installs, self-update or terminal authority) needs explicit maintainer authorization when it fires. The coordinator does not pull it forward.

## Definition-only umbrellas (unchanged)

059, 060, 061, 081, 095 and 100c are definition or re-derivation authorities that must never receive an implementation PR. They are not queue work.

## IMPLEMENT

| Row | Why now | Contract |
|---|---|---|
| 028 | The policy is followed in code but written nowhere, and the global fresh-versus-upgraded check is missing (`test_sqlite_legacy_upgrade.py` names it out of scope). Schema growth from 187–191 makes drift costlier. | 028 (not carried into this repository), ready |
| 165 | BLUECAD exports STL/STEP only. 189 user geometry now produces printable parts with no print path. | 165 (not carried into this repository), ready |
| 173 | The 172 dynamic engine (#789) has no general GUI. Only the 191 procedure bench reaches it. The Studio architecture record keeps 173 as a general capability. | 173 (not carried into this repository), ready |
| 174 | Rescoped. Validated tube and array interception geometry (sun vector from the installed pvlib, beam/diffuse/sea-reflected interception, Fresnel wall, inter-tube shading) is specialist computation that a user correlation cannot replace (directive §40). A user-supplied optical correlation stays a Studio model, not part of 174. | readiness after 173 |
| 175 | Rescoped to the general solver part: a dynamic lumped energy state and algebraic carbonate pH in the 172 engine. 190 units are steady with no energy ports, so this is not user-authorable today. The Degasser rate law and gas-transfer correlations become user models once 175 provides the states. | readiness after 174 |

## ABSORB (closed as `cancelled`, absorbed)

| Row | Owner |
|---|---|
| 012 | 187 (#806) isolated runtime and 188 (#808) authoring deliver proven OS-isolated, non-bundled user execution, which was the row's own reopen condition. 189 (#818) carries geometry through the same runtime. The disabled `bluecad_l2_v0` runner path stays disabled. |
| 036 | Honest advisory identity, route and authority display is delivered by the 091 sidecar provenance (#281), 161 escalation disclosure (#752), 166 workspace action cards (#760) and 146 Hermes status (#683). Its persona dependencies (034, 068) are deferred or cancelled. |

## TRIGGER-DEFER

| Row | Trigger | Hard boundary when it fires |
|---|---|---|
| 011, 026, 032, 034 | A real review need that 161 independent-agent escalation cannot meet. Any panel builds on 146 Hermes, not a JarvisOS persona store. | no |
| 046 | A failed FEM result where the operator wants a generated alternative. The building blocks are now 189 geometry and 038 FEM, not the review panel. | no |
| 013 | A project check that a Studio model (188) or unit with host checks (190) cannot express. | yes (plugin code execution) |
| 025 | Graded multi-route traffic exists to promote from. 147 owns local selection. | no (actuation would be) |
| 031, 033 | A live interaction surface needs a vocabulary report (031), or recurring 189 user geometry is worth promoting into `builders.py` through a human PR (033). | no |
| 039, 065 | The operator chooses and funds a frontier provider, or a second external provider is in use. | **yes** (egress, credentials, spending) |
| 053 | A thesis, advisor or grant deliverable needs an exported dossier. 113 (#548) is the read view it would export from. | low (sensitivity on export) |
| 062 | A re-derived secondary Evaluation/Audit UI. Stays `blocked` per the 2026-08-17 maintainer design. | no |
| 080 | Repair rounds that the manual and Relay paths cannot satisfy. | **yes** (writes to PR branches, spending) |
| 122 | The operator asks Jarvis to act on Roadmap or Brainstorm. | no |
| 125 | An update failure that the 154a launcher fast-forward cannot recover. | **yes** (self-update and rollback authority) |
| 126 | An explicit maintainer request for terminal authority. Directive §41 forbids terminal access as a shortcut. | **yes** (PTY, secret exposure) |
| 027 | A project decision that needs modal or thermal FEM. | no |
| 093 | The process bakeoff and typed process-design → CAD handoff (109, #690) show a topology need that a 189 geometry script cannot meet. | no |
| 176 | The parametric PBR array template is absorbed by the 189 geometry category (a user CAD script). The remainder, two-way staleness and derived-measurement findings over the 109 handoff, waits for a real array design loop. | no |
| 177 | Separators and storage are user models over 190. The general part, energy ports and recycle accumulation in the solver, waits for a real harvest question. | no |
| 178 | An economics study after 177. | **yes** (BioSTEAM out-of-process install) |
| 179 | 175 and 176 done, and a decision needs a Tier-3 correlation. | **yes** (OpenFOAM, GPL, heavy) |

## Order

028 ∥ 165 ∥ 173 run now; their write sets are disjoint. Readiness for 174 and then 175 is written after 173 merges.
