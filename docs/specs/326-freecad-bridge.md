# 326 — FREECAD-BRIDGE-1: Read the open FreeCAD document and set a property from the CAD screen

Status: ready
Depends on: none

## Goal

The CAD screen gets a "FreeCAD" panel that connects to the FreeCAD the user has open (a separate, user-installed program),
lists its open documents and their objects with typed properties, and changes one property through a typed operation the
user previews and confirms. FreeCAD applies the edit in one transaction and rolls back an edit that fails or leaves an
invalid object, so a bad change never reaches the document. Jarvis never saves, deletes or runs code in FreeCAD.

## Why

Directive `L20261119070313-84a6`, step 1 (the bridge), after the front-end plan of spec 298 and the guided-path work were
merged. A part modelled by hand lives in FreeCAD; the engineer needs to see and adjust it from Jarvis without copying
numbers by hand, and the assistant needs a typed, checkable way to propose a change. It reuses an existing add-on
instead of writing a FreeCAD plug-in (reuse before writing).

## Survey of existing bridges (2026-11-19)

| Project | Licence | Activity | What it exposes | Fit |
| --- | --- | --- | --- | --- |
| `neka-nat/freecad-mcp` (reviewed at `246f608d5de4`) | MIT | about 3.4k stars, pushed 2026-11-16 | FreeCAD add-on with an XML-RPC server bound to `127.0.0.1:9875` by default (remote mode and token optional); typed methods `ping`, `list_documents`, `get_objects`, `get_object`, `create_object`, `edit_object`, `delete_object`, `reload_document`, `run_fem_analysis`; also `execute_code` | **Chosen.** `edit_object` applies properties in one transaction and rolls back failed or invalid edits (`rpc_server/object_factory.py::edit_object_gui`); `create_object` likewise. `delete_object` has no rollback and `execute_code` runs arbitrary code: Jarvis never calls either. |
| `spkane/freecad-addon-robust-mcp-server` | Apache-2.0 | about 275 stars, pushed 2026-11-19 | XML-RPC surface is one generic `execute` of Python with an in-FreeCAD review dialog; typed tools live in its own MCP server on top of that | Not chosen: every operation is code at the transport, which the directive excludes. |
| `LBurny/cadpilot` and other forks | MIT / unverified | small | MCP servers over the same port-9875 add-on pattern | Not chosen: same transport, less maintained. |

The add-on is installed by the user into FreeCAD (Addon Manager or the `Mod` folder) and started from its toolbar; it is
never bundled, vendored or imported by Jarvis. FreeCAD itself is LGPL and stays a separate program.

## Code facts checked at `b49ce9e4`

- `backend/app/modules/process_stack/dwsim.py::_configured_runtime` reads a Windows executable path and its pinned SHA-256
  from environment variables and returns a reason code (`..._UNAVAILABLE`, `..._DIGEST_UNCONFIGURED`,
  `..._DIGEST_MISMATCH`) instead of raising; `backend/app/modules/process_stack/dwsim_mcp.py::DwsimMcpClient` runs it over stdio through WSL
  interop with bounded timeouts. This is the pattern to copy for reaching a Windows program from the backend.
- WSL2 in its default NAT networking mode cannot reach a listener bound to Windows `127.0.0.1`; the add-on binds there by
  default. A relay run on the Windows side through interop reaches it without opening the port to the network.
- `backend/app/modules/bluecad/print_handoff.py` already launches a Windows program (`explorer.exe`) through interop and
  converts paths with `_to_windows_path`.
- The CAD screen is `frontend/src/components/bluecad/BluecadWorkbench.tsx`; BlueCAD routes are in
  `backend/app/modules/bluecad/routes.py` under `/workspaces/{id}/bluecad`.
- `backend/tests/bluecad/test_registry.py::CI_FORBIDDEN_IMPORT_RE` already forbids `import FreeCAD` in the core.

## Source of truth (directive split)

A part modelled by hand lives in FreeCAD as `.FCStd`; AI-generated variants compared against calculations stay in
build123d candidates. This slice only reads and edits the live document. The reference from BlueCAD to a FreeCAD part is
defined here and built in the next slice: a FreeCAD reference is (document file name, object name, SHA-256 of the saved
`.FCStd`, SHA-256 of a STEP export made from it), attached to a candidate like a Studio reference
(`backend/app/modules/bluecad/studio_references.py`); exports (STEP for exchange, STL/3MF for printing) are produced by the user's
`FreeCADCmd` from the saved file with a fixed Jarvis-owned script, not from the live session.

## Scope

In scope:
- `backend/app/modules/bluecad/freecad_bridge.py`: configuration from `JARVISOS_FREECAD_PYTHON_PATH` and
  `JARVISOS_FREECAD_PYTHON_SHA256` (FreeCAD's bundled Windows `python.exe`), same reason codes as DWSIM
  (`FREECAD_BRIDGE_UNAVAILABLE`, `FREECAD_BRIDGE_DIGEST_UNCONFIGURED`, `FREECAD_BRIDGE_DIGEST_MISMATCH`), plus
  `FREECAD_ADDON_NOT_RUNNING` when the relay cannot reach the add-on.
- `backend/app/modules/bluecad/freecad_relay.py`: standard-library script run by that `python.exe`, one JSON request per
  line on stdin, one JSON response per line on stdout, calling `xmlrpc.client` on `127.0.0.1:9875` only. It refuses any
  method outside the allowlist `ping`, `list_documents`, `get_objects`, `get_object`, `edit_object`. The backend enforces
  the same allowlist before sending.
- Typed edit: `{document, object, property, value}` where the property already exists on the object and the value has its
  current type (number, with unit kept as FreeCAD reports it; boolean; string). Unknown objects, unknown properties,
  read-only properties and type changes are refused before anything is sent.
- Routes under `/workspaces/{id}/bluecad/freecad`: `GET status`, `GET documents`, `GET documents/{doc}/objects`,
  `POST edit/preview` (returns old and new value, no call to FreeCAD's edit), `POST edit/apply` (requires the preview's
  old value; refuses with `FREECAD_STALE_PREVIEW` if the live value changed meanwhile; returns the add-on outcome and, on
  failure, that the document was left unchanged).
- A "FreeCAD" panel in `BluecadWorkbench.tsx`: connection state in plain Italian with the next action when not connected
  (install the add-on, start its server, configure the path), the documents and objects with their properties, and an
  edit form whose "Anteprima" shows old → new and whose "Applica" asks for confirmation.
- Each applied edit is recorded as a workspace event (document, object, property, old, new, outcome); no file paths.

Out of scope: create, delete, save, reload, FEM, screenshots and `execute_code`; exports and the BlueCAD reference (next
slice); the assistant proposing edits (it may later produce the same typed request, still previewed and confirmed); window
docking (directive step 2); the overlay shell (step 3); remote mode of the add-on.

## Files likely touched

`backend/app/modules/bluecad/freecad_bridge.py`, `freecad_relay.py`, `routes.py`, `backend/app/modules/bluecad/module.toml`
(entrypoints), `backend/tests/test_bluecad_freecad_bridge_326.py` with a fake relay peer,
`frontend/src/components/bluecad/BluecadWorkbench.tsx` (or a new `FreecadPanel.tsx` beside it), `frontend/src/api/client.ts`,
generated contracts, `frontend/tests/freecadPanel.test.mjs` and its entry in `frontend/package.json`, an `e2e/` walk using
the fake peer.

## Design constraints

- No new dependency: `xmlrpc.client`, `json`, `subprocess` only. Nothing FreeCAD is imported by the core.
- Every subprocess call is bounded (connect and request timeouts); the relay exits when stdin closes.
- The backend never sends model text to FreeCAD: only validated identifiers and typed values.
- Jarvis never calls save; the user saves in FreeCAD and can undo there. This keeps every Jarvis action reversible.
- Default off: with no path configured the panel says the bridge is not configured and nothing is spawned.
- No Windows launcher or `.ps1` change in this slice.

## Acceptance criteria

1. Backend tests with a fake relay peer: allowlist enforced on both sides (`execute_code`, `delete_object` refused
   without reaching the peer); preview never calls `edit_object`; apply with a stale old value is refused; an add-on
   failure is reported as "document unchanged"; digest mismatch and missing add-on give their reason codes.
2. Frontend static test asserts the Italian labels, the confirm step and the not-configured state; contracts current.
3. AI browser walk from `/` with visible controls only, against the fake peer: open CAD, see the documents, preview and
   apply one numeric change, see a refused edit reported as unchanged. Evidence attached to the PR.
4. Live proof owed after merge on the maintainer's machine once the add-on is installed: read his open document and apply
   and undo one edit. Until then the capability is recorded as engine and interface present, not walked live.
5. Risk medium: CI green on the exact head and one severe review of the diff.

## Required tests

`backend/tests/test_bluecad_freecad_bridge_326.py`, `frontend/tests/freecadPanel.test.mjs`, the e2e walk.

## Definition of done

Merged with the walk evidence; STATUS row reconciled; the live proof of criterion 4 recorded as owed in the ledger.
