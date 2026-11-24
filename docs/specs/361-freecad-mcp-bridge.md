# 361 — FREECAD-MCP-BRIDGE-1: Drive the user's FreeCAD through the MCP Bridge add-on with fixed Jarvis scripts

Status: ready
Depends on: 326

## Goal

The CAD screen's FreeCAD panel (spec 326) works against the FreeCAD add-on the maintainer actually installed: the MIT
"MCP Bridge" add-on (`CREATeNG/freecad-mcp-bridge`), which serves MCP over Streamable HTTP on `127.0.0.1:39280/mcp`
behind a bearer token. The same five typed operations (ping, list documents, list objects, read one object, set one
existing property) are carried out by fixed, Jarvis-authored Python scripts sent through the add-on's
`execute_python` tool. The XML-RPC relay for the uninstalled `neka-nat/freecad-mcp` add-on is removed.

## Why

Directive L20261124084154-fbd8: the maintainer installed this add-on and prefers that Jarvis adapts to it. The panel
today says "FreeCAD non configurato" and the live proof owed by spec 326 cannot happen. The add-on stays a separately
installed program; Jarvis adds only the interface (mission rule: interface, not a copy of the tool).

## Code facts checked at `7d3bb6cf`

- `backend/app/modules/bluecad/freecad_bridge.py::FreecadBridge.request` enforces `FREECAD_ALLOWLIST` and runs
  `freecad_relay.py` with the hash-pinned FreeCAD `python.exe` from `_configured_runtime`, one JSON line in and out.
- `backend/app/modules/bluecad/freecad_bridge.py::_configured_runtime` reads `JARVISOS_FREECAD_PYTHON_PATH` and
  `JARVISOS_FREECAD_PYTHON_SHA256`; `backend/app/modules/bluecad/freecad_references.py` also uses it for spec 327 exports.
- `backend/app/modules/bluecad/freecad_bridge.py::validate_edit` refuses unknown, read-only, type-changing and
  unit-changing edits before anything is sent.
- `backend/app/modules/bluecad/freecad_relay.py::_call` speaks XML-RPC to `127.0.0.1:9875` (the add-on not installed).
- `backend/app/modules/bluecad/routes.py` builds `FreecadBridge()` for the `/workspaces/{id}/bluecad/freecad` routes.

Add-on facts read from its source (`freecad/mcp_bridge/mcp_protocol.py`, `http_server.py`, `USER_GUIDE.md`) on
2026-11-24; the implementer re-checks them against the installed version:
- Tools: `execute_python(code)`, `execute_python_file(filepath)`, `get_output_page(job_token, page_no)`.
- No MCP sessions; replies are `application/json` or `text/event-stream`. A tool result is one text block holding JSON
  `{has_more, [page_no], [job_token], [error], page}`; `isError` on failure. A run still waiting (for example on the
  user's approval dialog) returns a `job_token` to poll with `get_output_page`.
- Permission mode "Ask" (default) shows each script in a FreeCAD dialog before it runs; "Don't run" returns an error.
  Each run is one FreeCAD undo step "MCP Bridge: run".

## Scope

In scope:
- `freecad_relay.py` rewritten (standard library only) as an MCP Streamable HTTP client to `http://127.0.0.1:39280/mcp`
  run, as today, by the hash-pinned FreeCAD `python.exe` (so it reaches the Windows loopback). It accepts only the five
  operation names, builds the script from a fixed template per operation, calls `execute_python`, follows
  `get_output_page` while `has_more`/`job_token` within a bounded total wait, and returns the parsed result.
- Fixed templates: parameters enter the script only as one JSON string literal produced by `json.dumps` and decoded by
  `json.loads` inside the script; identifiers are validated (`^[A-Za-z_][A-Za-z0-9_]*$` for document and object names,
  property names from the object's own property list). The script prints exactly one result line with a fixed prefix;
  the relay ignores every other output line. No template saves, closes, opens, deletes or recomputes documents.
- Edit template: reads the live value, refuses with `FREECAD_STALE_PREVIEW` when it differs from the preview's old
  value, sets the property, and on any exception restores the old value and reports "document unchanged".
- Token: the backend reads it from `JARVISOS_FREECAD_MCP_TOKEN_FILE` (default `~/.config/jarvis/freecad_mcp_token`),
  refuses a file readable by group or others (`FREECAD_MCP_TOKEN_UNSAFE`) or missing (`FREECAD_MCP_TOKEN_MISSING`), and
  passes it to the relay inside the stdin JSON request, never in argv, environment, logs, events or responses.
- Reason codes in plain Italian in the panel: add-on off (`FREECAD_ADDON_NOT_RUNNING`), token refused
  (`FREECAD_MCP_UNAUTHORIZED`), script declined in FreeCAD (`FREECAD_MCP_DECLINED`), approval still pending at timeout
  (`FREECAD_MCP_PENDING`). The not-configured state lists the steps: install the add-on, generate the token, save it in
  the token file, switch MCP On, set the two `python.exe` variables. It also says that in the add-on's "Ask" mode
  FreeCAD asks to confirm each Jarvis script, and shows the script name so the user recognises it.
- Removal of the XML-RPC code path and of its tests' XML-RPC fake; the fake relay peer becomes a fake MCP server.

Out of scope (binding non-goals):
- Any free-form or model-written code: the assistant cannot reach `execute_python`; only the five templates exist.
- `execute_python_file`, HTTPS/certificates, listeners beyond loopback, changing the add-on's permission mode.
- New operations (create, delete, save, export through MCP); spec 327's export path is unchanged.
- Windows launchers and `.ps1` files.

## Files likely touched

- `backend/app/modules/bluecad/freecad_bridge.py`, `backend/app/modules/bluecad/freecad_relay.py`
- `backend/tests/test_bluecad_freecad_bridge_326.py` (or a new `test_freecad_mcp_bridge_361.py`) with a fake MCP peer
- `frontend/src/components/bluecad/` FreeCAD panel copy for the new states, its `frontend/tests/` static test
- `configs/architecture_enforcement.json` only if the loopback HTTP call needs an accepted-owner row

## Design constraints

- No new dependency. Loopback only: the relay refuses any host other than `127.0.0.1`, no proxies, no redirects,
  bounded connect, read and total wait (approval included), bounded response size.
- The backend never sends model text to FreeCAD; templates are constants in the repository.
- Every edit stays reversible: one undo step in FreeCAD, Jarvis never saves.
- Default off: without the `python.exe` pair or the token file nothing is spawned.

## Acceptance criteria

1. Offline tests with a fake MCP peer: each operation sends only its template with the JSON-encoded parameters;
   an identifier with quotes, newlines or Python syntax is refused before sending; a non-allowlisted operation never
   reaches the peer; JSON and SSE replies, `job_token` paging, `isError`, decline, 401 and timeout map to their codes;
   the token never appears in argv, logs, events or HTTP responses of Jarvis; a group-readable token file is refused.
2. Edit: stale preview refused without a set; a failing set restores the old value and reports unchanged.
3. The XML-RPC path is gone (`xmlrpc` no longer imported by the bluecad module).
4. Frontend static test covers the new Italian states; the existing FreeCAD walk passes against the fake peer.
5. Live proof on the maintainer's machine after merge: with the add-on On, the panel lists his open document, previews
   and applies one property change, and the change is undone with Ctrl+Z in FreeCAD.
6. Risk high (local code execution bridge, credential): CI green on the exact head, full diff review by a second
   qualified session, and the live proof of criterion 5.

## Required tests

The backend test file with the fake MCP peer, the frontend static test, `e2e/freecad_bridge_walk.mjs`.

## Definition of done

Acceptance criteria met, STATUS row reconciled, live proof recorded in the ledger.
