# Development beta — approved visual and interaction reference — 2026-08-27

Status: maintainer-approved visual/product reference; not runtime implementation authority.

This file freezes the approved `Development` workspace composition reached during the 2026-08-26/27 maintainer design session. It supplements `docs/product-direction/03-project-memory-and-development-contract.md` (not carried into this repository) and later accepted product-direction/spec authority. `docs/specs/STATUS.md` remains the source of live implementation authority.

## Shared Development shell

Primary navigation remains exactly:

`Design | Memory | Development | Coding | Settings`

`Development` owns exactly:

`Roadmap | Brainstorm`

The approved warm limestone / near-white operator shell is reused. Normal interface typography follows the same Inter / Inter Display direction already approved elsewhere in the operator workspace, using regular/medium weights rather than heavy bold treatment. The narrow, tall, light condensed type treatment is reserved for the colored Roadmap workstream blocks only.

## Roadmap — Timeline

Final approved local HTML identity:

- `development_roadmap_beta_mockup_v9_timeline_execution.html`
- SHA-256: `eb71ddab8cd829d041319fca7a6b08d4e7f60ae57a51f96310ed9ea11d20dc70`

Approved rendered reference identity:

- `development_roadmap_beta_mockup_v9_timeline_execution.png`
- SHA-256: `bf37f0df84af967d9264a030ae2283c6ebf121ebc038cbc31620a6482afd8f83`

The repository freezes the approved semantics plus exact local artifact identities/hashes. The local HTML fixture remains the maintainer-approved composition reference for this session; any future repository-embedded copy must match the hash above exactly.

Approved composition and interaction rules:

- `Roadmap` is the dominant Development planning surface and keeps a large central timeline/Gantt area;
- Roadmap exposes only `Timeline | Calendar`; the formerly considered standalone `Board` destination is removed;
- the lower part of Timeline contains a collapsible `Execution status` snapshot using the same Roadmap item identities, normally emphasizing `Ready | In progress | Blocked`;
- `Planned` and `Completed` remain available through secondary controls/filters rather than permanently occupying primary board columns;
- the execution snapshot may expand/collapse without becoming a separate page;
- moving an item between operational states mutates the same Roadmap item and therefore remains synchronized with Timeline and Calendar;
- a transition to `Done` must not bypass deterministic acceptance/done-when checks. Where a known condition fails, the UI blocks or requires explicit acknowledged override according to later authority;
- Roadmap bars use a modern restrained bevel/depth treatment: technical/full colors rather than pastel, front-face depth/highlight rather than a simple floating card shadow, tighter rounded corners, and the approved narrow/light workstream type treatment;
- normal application chrome, headings, controls, Jarvis and filters do not use the condensed workstream typeface and avoid heavy bold typography;
- the right column remains `Jarvis` above `Focus & filters`, preserving maximum width for the Roadmap itself;
- filters are presentation-only and may include status, priority, domain, model, blocked/dependency/critical-path focus;
- Jarvis may propose scheduling/roadmap changes but changes require explicit acceptance;
- manual `+ Add work item` remains first-class.

### Manual Roadmap work item

The approved editor supports a fast base form plus optional advanced fields. Useful fields include:

- title;
- type: `Task | Work package | Milestone | Investigation | Validation | Decision | Procurement | Manufacturing | Meeting/Review`;
- start/end dates (single date for milestone where appropriate);
- status: `Planned | Ready | In progress | Blocked | Done | Cancelled`;
- priority: `Critical | High | Normal | Opportunity`;
- description;
- domain, with selectable predefined domains plus future extensibility;
- links to Project Basis, model/version, acceptance criterion, Literature, Process/BLUECAD objects and Brainstorm records;
- dependencies (`Depends on` / `Blocks`);
- scheduling constraints such as cannot-start-before / must-finish-before;
- acceptance / `Done when` condition;
- tags with autocomplete;
- owner;
- effort estimate;
- progress, preferably derived from subtasks/evidence where possible;
- subtasks;
- resources/cost;
- notes/attachments.

Every existing Roadmap item is user-manageable. Clicking a work item exposes at minimum `Open details | Edit | Delete`; edit reuses the same editor with current values loaded.

Manual and Jarvis-proposed items share one Roadmap entity model but keep provenance. A manually created item may enter the roadmap directly; a Jarvis proposal remains a proposal until explicitly accepted.

## Roadmap — Calendar

Final approved local HTML identity:

- `development_calendar_beta_mockup_v1.html`
- SHA-256: `3dc94b861478007080a3d9658ec81bc3c46394d0bf73f9a5aa4aa669024b2737`

Approved rendered reference identities:

- default Week view: `092df609ab6974e0f9abaa0271991ca098869389d0710cee905b1b2ad00553cd`
- add-event dialog: `5f362dd6fd8ce761253754400dc72430640ada9f39a2dcd042919d8cbde12cd8`
- selected-event actions: `97e1697d8730ffdeadaa17790d9ccb94cdee4c07ae385f17506dbc0f3e6d68e0`

The repository freezes the approved semantics plus exact local artifact identities/hashes. The local HTML fixture remains the maintainer-approved composition reference for this session; any future repository-embedded copy must match the hash above exactly.

Calendar is not a duplicate rendering of Roadmap windows. Its distinct purpose is actual time management: day/week scheduling down to hours and minutes.

Approved semantics:

- default useful view is `Week`, with `Day | Week | Month | Agenda` available;
- Calendar supports hour-by-hour blocks such as a call from `15:00–16:00`, focused engineering work sessions, review sessions, experiments/lab sessions, reminders, deadlines and unavailable/personal time where useful;
- a Roadmap work item may have zero, one or many Calendar blocks linked to it;
- Roadmap duration/window and Calendar scheduled effort are distinct concepts. A ten-day Roadmap window does not imply ten occupied Calendar days;
- linked work sessions can expose planned/completed/remaining effort for the parent Roadmap item in future implementations;
- creating a Calendar block from a Roadmap item prelinks that work item; creating from a calendar cell may prefill the selected date/time;
- clicking an event exposes at minimum `Open details | Open roadmap item` where linked, `Edit | Delete`;
- `+ Add event` supports title, event type, date, start/end time, priority, description, linked Roadmap item, domain, location/meeting link, participants, reminder, done-when, tags and optional attachments/notes;
- Jarvis may answer availability/scheduling questions and propose a schedule, but does not commit calendar changes without explicit acceptance;
- Month remains overview-oriented; Week/Day are the real execution-planning views.

## Brainstorm

Final approved local HTML identity:

- `development_brainstorm_beta_mockup_v2_phosphor.html`
- SHA-256: `2b30f8d558045becf3c79b7d9a7bfcfd186a42a6278d92f53a0150be61f82631`

Approved rendered reference identity:

- `development_brainstorm_beta_mockup_v2_phosphor.png`
- SHA-256: `faa74483fb5a7f084cdf8b9761c99c75f6d0cddeac78c864cc575cbd3691b9cd`

Earlier approved multi-context behavior reference:

- SHA-256: `4838750050dd0d18f03efc9d21f706ff319c30096d82dcd02baaa86fc8dddcf1`

The repository freezes the approved semantics plus exact local artifact identities/hashes. The local HTML fixture remains the maintainer-approved composition reference for this session; any future repository-embedded copy must match the hash above exactly.

Brainstorm is not a Kanban lifecycle such as Inbox/Exploring/Candidate. Its primary model is:

`Raw -> discussion/reconciliation with Jarvis -> Reconciled -> explicit promotion`

### Raw

Raw is intentionally unstructured capture:

- a free-form text box accepts rough ideas, questions, observations and notes without forcing taxonomy before thought;
- original Raw text and attachments are preserved rather than silently rewritten by Jarvis;
- file attachment is first-class;
- future local speech-to-text capture is explicitly anticipated, with the UI reserving a record-speech affordance for a locally optimized transcription path;
- approved generic controls use official Phosphor icons, including a paperclip for attachment and microphone for speech capture;
- Raw records may use truthful lifecycle labels such as `NEW`, `DISCUSSED`, `RECONCILED`, `SUPERSEDED`;
- `DISCUSSED` means it has been talked through but not necessarily consolidated; `RECONCILED` means useful content has been incorporated into one or more reconciled records; `SUPERSEDED` means later understanding replaces it;
- reconciliation/supersession never destroys original provenance.

### Reconciled

Reconciled is Jarvis-maintained structured understanding produced from Raw notes, linked files and the actual discussion with the operator.

At rest, Reconciled is deliberately compact and shows only the information needed to identify the topic at a glance: idea ID, title, short takeaway/status/domain/update metadata as appropriate. It must scale to many ideas without becoming a wall of expanded cards.

Clicking a reconciled idea expands it inline; multiple reconciled ideas may remain open. Expanded detail includes:

- a variable-length `Discussion synthesis` that preserves all materially relevant conclusions from Raw plus conversation, rather than merely restating the original note;
- why an idea is useful/not useful, accepted/rejected constraints, trade-offs, chosen direction and open questions when relevant;
- structured bullets/tables/decision summaries where useful;
- inline SVG diagrams when a schematic communicates the reconciled understanding better than prose;
- compact/collapsible provenance linking Raw records, relevant past discussions, files and exact project/Coding/Design context;
- actions such as `Edit`, `Supersede`, `Add to Roadmap`, and explicit `Promote -> Design` or `Promote -> Coding` according to the idea's domain.

A reconciled record is maintainable/revisable rather than an immutable one-shot summary. Later discussions may propose an update to an existing IDEA record, preserving lineage, instead of accumulating near-duplicate memory records.

### Jarvis active context basket

Jarvis remains persistently on the right instead of being embedded separately inside each reconciled card.

Opening an idea for reading does **not** silently change conversational context. Deeper reasoning is intentional:

- each idea can be explicitly added to Jarvis context;
- the operator may also request the same action naturally, e.g. `recover context of IDEA-030 and IDEA-034 and continue reasoning about ...`;
- active context is visibly represented with removable chips/records;
- multiple reconciled ideas can be loaded simultaneously for comparison/merging;
- the context basket may also include linked Raw records, relevant past discussions, attachments, model/version references, Literature, Design or Coding objects;
- Jarvis can propose reconciliation updates, supersession, creation of a new derived IDEA, or source-state changes, but those mutations require explicit approval.

Promotion from Brainstorm does not directly mutate authoritative Design or executable Coding state. It creates the appropriate explicit proposal/development item in the target workspace, where that workspace's own review/approval lifecycle applies.

## Board decision

The standalone `Board` Roadmap view is intentionally removed. Its useful capability survives as the collapsible `Execution status` section under Timeline. This prevents a low-information duplicate page while retaining fast answers to `what is ready`, `what is active`, and `what is blocked`.

## Authority and implementation boundary

These references freeze approved composition and interaction intent only. They do not:

- modify `docs/specs/STATUS.md`;
- release the post-100 maintainer visual-inspection hold;
- authorize 100a/100b or later runtime implementation;
- create a new source of canonical Roadmap/Calendar/Brainstorm state;
- authorize Jarvis to commit proposals without explicit acceptance;
- override later accepted ADR/spec authority.

The post-100 visual-inspection hold remains active until the maintainer explicitly releases it after the remaining Coding visual pass. When an approved HTML artifact and an incorrectly rendered screenshot disagree, the approved HTML/composition contract is authoritative.
