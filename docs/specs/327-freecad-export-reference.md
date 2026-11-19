# 327 — FREECAD-EXPORT-REFERENCE-1: Export a saved FreeCAD part and place it in the BlueCAD view

Status: merged
Depends on: 326

## Goal

From the CAD screen the engineer picks a saved FreeCAD file (`.FCStd`) and one object in it. Jarvis keeps a copy of the
file, has the user-installed FreeCAD export that object to STEP with a fixed Jarvis script, makes STL, 3MF and GLB from
that STEP with the BlueCAD kernel, and attaches the result to a BlueCAD candidate as a pinned, visual-only "FreeCAD part"
shown in the assembly view beside the candidate, with STEP, STL and 3MF downloadable. The pin is the slice-1 definition:
(document file name, object name, SHA-256 of the `.FCStd`, SHA-256 of the STEP).

## Why

Directive `L20261119070313-84a6`, step 1, second slice: spec 326 defined the reference and the exports and left them to
this slice. A hand-modelled part lives in FreeCAD; without this the engineer cannot put it next to the generated variants
or send it to a slicer from Jarvis. It reuses the Studio reference mechanism and the BlueCAD exporters instead of adding a
second viewer or mesher.

## Code facts checked at `397d7aee`

- `backend/app/modules/bluecad/freecad_bridge.py::_configured_runtime` already resolves and SHA-256-pins FreeCAD's
  bundled Windows `python.exe` (`JARVISOS_FREECAD_PYTHON_PATH`, `JARVISOS_FREECAD_PYTHON_SHA256`) and returns reason codes;
  `_relay_path` converts a script path for a `/mnt/...` `.exe`; `FreecadBridge.request` shows the bounded `Popen` pattern.
- `backend/app/modules/bluecad/print_handoff.py::_to_windows_path` converts any WSL path (including the data root on the
  Linux file system) through `wslpath -w` and rejects unusable results.
- `backend/app/modules/bluecad/studio_references.py` stores visual-only references in `bluecad_studio_references`
  (owner `backend/app/core/bluecad_studio_schema.py`), re-verifies digests on every read (`_present`) and when serving the
  GLB (`reference_glb`), and uses the frame `mm_z_up_rotation_RxRyRz_deg` with a revisioned transform.
- `frontend/src/components/bluecad/BluecadWorkbench.tsx` passes `references` (`id`, `url`, `translation_mm`,
  `rotation_deg`) to `BluecadGlbViewer`; FreeCAD parts are appended to that list, no viewer change.
- `backend/app/modules/bluecad/export.py` writes STEP/STL/GLB/3MF from build123d shapes (`bd.export_stl`,
  `bd.export_gltf`, `_export_3mf`); `build123d==0.11.1` is a backend requirement and can import STEP (`bd.import_step`).
- No route in the backend takes multipart uploads; `python-multipart` must not be added. The upload is a raw
  `application/octet-stream` body.
- `CURRENT_SCHEMA_MIGRATION_ID` is `0036_decision_case_verdict` in `backend/app/core/schema.py`.

## Scope

In scope:
1. **Upload.** `POST /workspaces/{id}/bluecad/freecad/files` with a raw body (limit 64 MiB, refused above with
   `FREECAD_FILE_TOO_LARGE`) and the original file name in a header (`X-Filename`, base name only, must end in `.FCStd`,
   at most 120 characters, no path separators). The body must be a ZIP containing `Document.xml` (an `.FCStd` is a ZIP);
   otherwise `FREECAD_FILE_INVALID`. Stored content-addressed under the data root at
   `bluecad/freecad/<workspace_id>/<sha256>/source.FCStd` through a new `paths.py` method; the same bytes uploaded twice
   reuse the copy. Response: `{file_sha256, file_name, objects}` where `objects` comes from step 2 in list mode.
2. **Fixed export script.** `backend/app/modules/bluecad/freecad_export.py`, standard library plus FreeCAD modules only,
   run by the pinned `python.exe` with two arguments (Windows paths of the stored copy and of a fresh output directory)
   and an optional third (object name). It makes FreeCAD importable by putting the executable's own directory first on
   `sys.path`, opens the document read-only in memory (never saves), and either lists the objects that have a non-null
   `Shape` (name, label, type id) as JSON on stdout, or exports the named object's shape with `Import.export` to
   `model.step` in the output directory. It takes no other input: no code, no expression, no module name from the request.
   Bounded by a 120 s timeout; on timeout or non-zero exit nothing is recorded.
3. **Kernel exports.** The backend reads `model.step` with build123d and writes `model.stl`, `model.3mf` and `model.glb`
   beside it with the same deflection settings as `export.py` (reuse its helpers; do not copy them). The result directory
   is `bluecad/freecad/<workspace_id>/<file_sha256>/<object_name_digest>/`.
4. **Reference.** New table `bluecad_freecad_references` (additive migration `0037_bluecad_freecad_references`, owner
   `bluecad_studio_schema.py` or a sibling `*_schema.py`): id, workspace, candidate, document file name, object name and
   label, `fcstd_sha256`, `step_sha256`, `stl_sha256`, `three_mf_sha256`, `glb_sha256`, transform JSON, revision,
   timestamps; unique on (candidate, `fcstd_sha256`, object name). Routes under
   `/workspaces/{id}/bluecad/candidates/{candidate_id}/freecad-references`: list, add (`{file_sha256, object_name,
   translation_mm?, rotation_deg?}`; runs the export), move (expected revision), remove (expected revision),
   `model.glb`, and `model.step` / `model.stl` / `model.3mf` downloads. Every read and every served file re-verifies
   the stored digests exactly as `studio_references.py` does; a changed or missing file makes the reference `valid: false`
   with a finding and nothing is served. Responses never contain file-system paths.
5. **Panel.** In the FreeCAD panel of spec 326 (`FreecadPanel.tsx`): a "Parte FreeCAD nella vista" section with a file
   chooser ("Scegli file .FCStd"), the object list from the upload, "Aggiungi alla vista" for the selected candidate, and
   one card per reference showing file name, object, short digests, validity, the STEP/STL/3MF download links and
   "Rimuovi". The parts appear in `BluecadGlbViewer` through the existing `references` prop. It works without the live
   add-on (only the configured `python.exe` is needed); when not configured it says so in plain Italian, as slice 1 does.
6. One workspace event per added or removed reference (file name, object, digests, outcome); no paths.

Out of scope: exporting from the live session; editing the uploaded copy; using a FreeCAD part in BlueCAD port checks,
FEM, CFD, volume or combined exports (visual only, as Studio references); re-pinning when the user saves a newer file
(the user uploads again and removes the old card); docking (directive step 2); FreeCAD's own STL/3MF writers.

## Source of truth

The `.FCStd` in the user's FreeCAD folders stays the master. Jarvis keeps an immutable copy only to make the pin
reproducible. A newer save is a new upload with a new digest; the old reference stays valid for what it pinned.

## Design constraints

- No new dependency (no `python-multipart`). The core never imports `freecad_export.py`; it only passes its path to
  the pinned `python.exe`, as with `freecad_relay.py`. Inside the script FreeCAD modules are loaded with
  `importlib.import_module` after the `sys.path` change, so ruff and the mypy ratchet (`scripts/check_typecheck_ratchet.py`)
  see no unresolved import, and no `import FreeCAD` line appears in the backend (the pattern
  `backend/tests/bluecad/test_registry.py::CI_FORBIDDEN_IMPORT_RE` describes).
- Subprocess arguments are only the two converted paths and an object name matching `^[A-Za-z_][A-Za-z0-9_]{0,63}$`
  (FreeCAD internal names); `shell=False`.
- Default off: no configured `python.exe`, no spawn.
- No Windows launcher or `.ps1` change.

## Acceptance criteria

1. Backend tests with a fake `python.exe` (a small script that writes a fixed STEP made by build123d in the test, or a
   fixed object list): size and name limits, ZIP/`Document.xml` check, dedup of the same bytes, object-name pattern,
   export timeout leaves no reference, digests re-verified on read and on serve (tampered GLB or STEP gives
   `valid: false` and 404 on serve), path-free responses, duplicate add refused, stale revision refused on move/remove.
2. Migration discipline test updated (new id, digest/baseline) following `docs/MIGRATIONS.md`.
3. Frontend static test (`frontend/tests/freecadPanel.test.mjs` extended) asserts the Italian labels and that FreeCAD
   parts are passed to the viewer's `references`; contracts current.
4. AI browser walk from `/` with visible controls only, against the fake `python.exe`: open CAD, choose a file, choose an
   object, add it to the view, see the card valid with download links, remove it. Evidence attached to the PR.
5. Live proof owed after merge on the maintainer's machine: one real `.FCStd` exported and shown. **Not verified and to
   be checked first live**: that FreeCAD's bundled `python.exe` can `import FreeCAD` with its own directory on
   `sys.path`. If it cannot, the fallback is `FreeCADCmd.exe` from the same folder with its own pinned digest, passing
   the arguments through a `WSLENV`-exported variable; that is a follow-up change, not part of this slice.
6. Risk high (additive migration; subprocess to a Windows program on user files): CI green on the exact head, severe
   review of the diff, and a second-session review.

## Required tests

`backend/tests/test_bluecad_freecad_reference_327.py`, `backend/tests/test_migration_discipline.py`,
`frontend/tests/freecadPanel.test.mjs`, the e2e walk.

## Definition of done

Merged with the walk evidence; STATUS row reconciled; live proof of criterion 5 recorded as owed in the ledger.
