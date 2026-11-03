# ICONOGRAPHY — JarvisOS 100

## Decision

Generic application icon family for spec 100: **Phosphor Icons** via `@phosphor-icons/react`.

Maintainer preference order is:

**Phosphor > Lucide > Tabler**

but consistency/coverage outranks preference. The rule is: choose the first family in that order that covers the complete generic application vocabulary; do not fill random gaps from another family.

The 2026-08-25 audit of the current JarvisOS UI and expected 100 interaction vocabulary found sufficient Phosphor coverage. Therefore:

- 100 should use **Phosphor only** for generic application icons;
- do not add Lucide or Tabler alongside it;
- use the maintained package `@phosphor-icons/react`, not the legacy `phosphor-react` package;
- Phosphor is MIT-licensed and supports tree-shaking, six weights and custom-icon extension.

Upstream:

- `https://github.com/phosphor-icons/react`
- `https://github.com/phosphor-icons/core`

## Weight/style policy

Consistency includes weight, not only family.

- `regular` is the normal application weight.
- `light` may be used where a larger icon would otherwise appear too heavy, but not randomly inside the same control class.
- `bold`/`fill` are reserved for state emphasis where a filled-state convention is semantically useful.
- `duotone` is **not** the default. It may be used sparingly for top-level identity/Jarvis/floating emphasis if the 100 proof shows it improves hierarchy without making the app illustrative.
- icons inherit `currentColor`; do not hard-code brand green into every glyph.

Indicative sizes:

- dense inline/action controls: 16px;
- normal controls/list navigation: 18px;
- primary rail/workbench navigation: 20px;
- rare identity/empty-state illustration: 24px+ only when appropriate.

An icon-only button still requires an accessible name/tooltip as appropriate.

## Current coverage inventory

The table records the semantic slots that 100 can reasonably need from the current routes/components plus common controls already present in JarvisOS. Exact glyph choice may change within Phosphor if browser proof reveals a clearer sibling, but the family decision does not.

### Primary navigation and workbench stages

| JarvisOS semantic slot | Phosphor candidate |
| --- | --- |
| Home | `HouseIcon` / `HouseLineIcon` |
| Design | `CubeFocusIcon` |
| Runs | `PlayCircleIcon` |
| Engineering Data | `DatabaseIcon` |
| Review | `ClipboardTextIcon` or `ShieldCheckIcon` depending local meaning |
| Settings | `GearSixIcon` |
| Model / geometry | `CubeIcon` |
| Process scaffold | `FlowArrowIcon` |
| Results / analytics | `ChartLineIcon` |
| Lineage / provenance | `GraphIcon` or `TreeStructureIcon` |
| AI Threads / Jarvis conversation | `ChatCircleDotsIcon` |

`FlowArrowIcon` represents only the **Process workspace/stage** in 100. It does not invent unit operations, streams or process-model semantics.

### Shell, navigation and generic commands

| Semantic slot | Phosphor candidate |
| --- | --- |
| Search / command | `MagnifyingGlassIcon` |
| Add/create | `PlusIcon` |
| Edit | `PencilSimpleIcon` |
| Duplicate/copy | `CopyIcon` |
| Archive | `ArchiveIcon` |
| Delete | `TrashSimpleIcon` |
| Save where an explicit save affordance really exists | `FloppyDiskIcon` |
| Undo | `ArrowCounterClockwiseIcon` |
| Redo | `ArrowClockwiseIcon` |
| Refresh/retry | `ArrowsClockwiseIcon` |
| Filter | `FunnelSimpleIcon` |
| Sort | `SortAscendingIcon` / corresponding sibling |
| Toggle sidebar/panel | `SidebarIcon` / `SidebarSimpleIcon` |
| Expand/collapse | `Caret*Icon` / `ArrowsOut*Icon` |
| Close/dismiss | `XIcon` |
| Overflow | `DotsThreeIcon` / vertical sibling |
| External link/export navigation | `ArrowSquareOutIcon` |
| Link | `LinkIcon` / simple sibling |
| Upload | `UploadSimpleIcon` |
| Download | `DownloadSimpleIcon` |
| Folder/open project | `FolderOpenIcon` |
| Multiple files/artifacts | `FilesIcon` |

### Authority, state and validation

| Semantic slot | Phosphor candidate |
| --- | --- |
| Locked / protected | `LockIcon` / `LockSimpleIcon` |
| Visibility | `EyeIcon` / `EyeSlashIcon` |
| Information | `InfoIcon` |
| Warning | `WarningIcon` / `WarningCircleIcon` |
| Valid/pass | `CheckCircleIcon` |
| Security/verified | `ShieldCheckIcon` |
| History | `ClockCounterClockwiseIcon` |
| Pending/loading | `SpinnerGapIcon` or CSS loader |
| Activity | `PulseIcon` |
| Stop run | `StopIcon` |
| Pause where semantically supported | `PauseIcon` |

Status color remains independently defined; icons must not make a semantic state depend on user accent.

### Engineering / inspection vocabulary

| Semantic slot | Phosphor candidate |
| --- | --- |
| 3D object | `CubeIcon` / `CubeTransparentIcon` |
| Inspect/focus object | `CubeFocusIcon` |
| Selection | `Selection*Icon` / `CursorClickIcon` |
| Bounds | `BoundingBoxIcon` |
| Measure | `RulerIcon` |
| Angle | `AngleIcon` |
| Origin/target/coordinate focus | `CrosshairIcon` |
| Parameters/tuning | `SlidersHorizontalIcon` |
| Tool/repair | `WrenchIcon` |
| Calculation | `CalculatorIcon` |
| Engineering table | `TableIcon` |
| Console/log | `TerminalWindowIcon` |
| Line chart | `ChartLineIcon` |
| Bar/histogram | `ChartBarIcon` |
| Scatter data | `ChartScatterIcon` |
| Dependency graph | `GraphIcon` |
| Hierarchy/tree | `TreeStructureIcon` |
| Branch/version relationship | `GitBranchIcon` / `GitDiffIcon` |

There is no literal `AxisIcon` in the audited core catalog; this is **not a coverage failure** because the current UI need is semantic “origin/coordinate/focus”, for which `CrosshairIcon`/`BoundingBoxIcon` are clearer generic UI symbols. A future true CAD axis gizmo belongs in the viewport/tool layer and may be custom geometry rather than a generic app icon.

## Future process/PFD symbols are a separate boundary

The future editable process workspace will need domain symbols such as pumps, valves, heat exchangers, vessels/reactors, mixers, splitters, compressors, streams, instrumentation and potentially P&ID/PFD variants.

Those are **engineering notation**, not generic application icons. Their absence from a generic library must not cause an ad-hoc switch from Phosphor to Lucide/Tabler.

When process semantics are authorized by future backend/evaluator contracts:

1. define the required engineering symbol vocabulary from the actual process domain contract;
2. prefer standard-recognizable PFD/P&ID geometry over decorative icons;
3. adapt stroke, optical weight and grid to the JarvisOS icon language;
4. use Phosphor's documented `IconBase`/custom-icon mechanism where practical so custom engineering symbols inherit size/color/weight conventions;
5. keep custom symbols versioned and semantically named;
6. do not invent unit-operation semantics during spec 100.

## Future generic-icon fallback rule

If a later product slice genuinely needs a **generic application** icon for which Phosphor has no acceptable semantic representation:

1. verify the gap against current upstream Phosphor;
2. test whether a custom icon consistent with the Phosphor grid is the smaller coherent solution;
3. if a broad family change is justified, audit Lucide coverage as the next complete-family candidate;
4. only if Lucide also fails, audit Tabler;
5. never silently mix general-purpose icon families one glyph at a time.

That later decision is independent of process/PFD engineering symbols.
