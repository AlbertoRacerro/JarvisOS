# 353 — SEARCH-HOST-SERVICES-1: Local metasearch and query guard as managed host services

Status: merged
Depends on: 352

## Goal

The local SearXNG metasearch engine and a generic query guard in front of it run as user systemd services that the
host-operations executor installs, starts, restarts and reports, instead of processes started by hand. The guard is
public JarvisOS code; the private term groups it applies come from the loaded pack, never from the repository.

## Why

Directive `L20261124012919-7d88` item 4. The agent bench that justified the local default used a SearXNG instance on
127.0.0.1:8888 and a guard proxy on 127.0.0.1:8889, both started by hand from private bench scripts. After a reboot
or an outage nothing restarts them, and the guard lives only in a private bench directory. Research tools need one
stable, guarded local search endpoint.

## Code facts checked at `5a7189d0`

- `scripts/host_ops.py::UNIT_TEMPLATES` holds the user-unit bodies the executor may install.
- `scripts/host_ops.py::op_service_install` writes an allowed template to the user unit directory, backs up a
  differing unit, runs `daemon-reload` and `enable --now`, and fails unless the unit is active.
- `scripts/host_ops.py::op_service_control` starts, stops or restarts a policy-listed unit.
- `scripts/host_ops.py::op_service_status` reports the state of a policy-listed unit.
- `scripts/host_ops.py::DEFAULT_POLICY` lists, per operation, the units and templates a fresh host policy allows.
- `backend/app/core/packs.py::read_pack` parses a pack manifest and rejects unknown top-level keys.
- `backend/app/core/packs.py::mentions_pack_term` reports whether text contains a loaded pack's sensitivity term.
- `backend/app/modules/agents/hermes/ip_boundary.py::sanitize_text` and
  `backend/app/modules/agents/hermes/ip_boundary.py::check_sensitivity_floor` are the egress checks
  `backend/app/modules/agents/hermes/paper_search_tool.py::execute_paper_search` already applies to outgoing queries.

## Scope

In scope:
- **Pack schema.** An optional `[search_guard]` table in the pack manifest: `groups` (list of lists of non-empty
  strings; each inner list is one group of synonyms) and `min_groups` (positive integer, default 4). Parsed and
  validated by `read_pack`; exposed through a small accessor in `backend/app/core/packs.py`. The fake test pack gets
  a generic example table.
- **Guard proxy.** A stdlib HTTP proxy (new file, for example `scripts/search_guard.py`) listening on 127.0.0.1 only
  (default port 8889) that forwards `GET` requests to SearXNG on 127.0.0.1 (default 8888) and blocks a `/search`
  query when either: it mentions a pack sensitivity term (`mentions_pack_term`); it matches at least `min_groups`
  of the pack's `search_guard.groups` (case-insensitive, a group matches when any of its terms occurs); or it fails
  the same egress checks `execute_paper_search` applies (`sanitize_text` changes it, or `check_sensitivity_floor`
  returns S2 or above); reuse those functions, do not write new detection. A blocked query returns
  HTTP 200 with an empty result list (JSON when `format=json`, minimal HTML otherwise). Upstream failure returns 502.
  Only `GET /search`, `GET /healthz` and `GET /config` are forwarded; anything else is 404. Redirects from upstream are
  not followed. Upstream response body is bounded.
- **Fail closed.** When `JARVISOS_PACK_PATH` is set but the pack fails to load, every `/search` is blocked and
  `/healthz` reports the error. Without a pack, only the secret check applies (the core runs without a pack).
- **Logging.** One line per query to a log under the JarvisOS data root (path from `backend/app/core/paths.py`):
  timestamp, `ok`/`blocked`, and a SHA-256 prefix of the query. Never the query text.
- **Unit templates.** Two entries in `UNIT_TEMPLATES`:
  - `jarvisos-searxng`: `WorkingDirectory=%h/tools/searxng`, `Environment=SEARXNG_SETTINGS_PATH=%h/.config/jarvisos-hostops/searxng/settings.yml`,
    `ExecStart=%h/tools/searxng/.venv/bin/python -m searx.webapp`, restart on failure, memory and CPU bounds.
  - `jarvisos-search-guard`: runs the guard from the installed release (same pattern as `jarvisos-relay-watch`)
    with the backend virtualenv interpreter (`%h/src/JarvisOS/backend/.venv/bin/python`, as `bench.run` already
    uses) and `PYTHONPATH` set to the release's `backend` directory, with
    `EnvironmentFile=-%h/.config/jarvisos-hostops/search-guard.env` for `JARVISOS_PACK_PATH` and
    `JARVISOS_DATA_ROOT`, `After=`/`Wants=` the SearXNG unit, restart on failure, `NoNewPrivileges=yes`.
- **Policy defaults.** `DEFAULT_POLICY` allows both templates in `service.install` and both units in
  `service.status` and `service.control`; `http.smoke` gets a `search-guard` target on the guard's `/healthz`.

Out of scope (binding non-goals):
- Installing SearXNG itself, editing its `settings.yml`, or creating the env file: host prerequisites, already present
  on the maintainer's host.
- Changing an existing host `policy.json`: a host session adds the new entries (live-activation step below).
- Wiring a `web_search` tool into Hermes or chat (separate item); `paper_search` is unchanged.
- Moving or editing the private bench scripts.

## Files likely touched

- `backend/app/core/packs.py`
- `backend/tests/fixtures/packs/zephyr/` (manifest)
- `scripts/search_guard.py` (new)
- `scripts/host_ops.py`
- `backend/tests/test_search_guard_353.py` (new), `backend/tests/test_packs.py`, host-ops tests
- `docs/MODULES.md` (regenerated)

## Design constraints

- Loopback only: the guard binds 127.0.0.1 and forwards only to a 127.0.0.1 upstream; both ports are configurable
  only by arguments or environment, never by request content.
- No private terms, query text or pack content in code, tests, logs or unit files.
- Stdlib only; no new dependency.
- High risk (egress guard and host-operations code): coordinator full-diff review, independent second-session
  full-diff review, green CI, and live proof.

## Acceptance criteria

1. A pack with `[search_guard]` parses; malformed `groups` or `min_groups` raise `PackError`; a pack without it is
   unchanged.
2. The guard blocks a query that mentions a pack sensitivity term, one that matches `min_groups` groups, and one that
   fails the existing egress checks; it forwards a query matching fewer groups; blocked queries return an empty result list and
   never reach the upstream.
3. With a pack path set but unloadable, every `/search` is blocked and `/healthz` reports not ready.
4. Only the three listed paths are forwarded; non-loopback upstream configuration is refused at start.
5. The query log contains no query text.
6. `UNIT_TEMPLATES` and `DEFAULT_POLICY` contain both units with the properties listed in Scope; `service.install`
   refuses any other template as before.
7. Live activation, after merge and release install: a host session adds the two templates and units to the host
   policy; `service.install` for both templates returns active units; `http.smoke search-guard` returns 200; a
   generic query through 127.0.0.1:8889 returns results; one pack-term query returns an empty list. Receipts are
   recorded as proof.

## Required tests

- Offline tests with a fake upstream (a local stub server or an injected opener) covering criteria 1–6.
- Existing host-ops and pack tests stay green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria 1–6 met with coordinator and independent full-diff reviews,
STATUS updated; criterion 7 recorded as live proof (or as the owed live activation if a host session is not
available).
