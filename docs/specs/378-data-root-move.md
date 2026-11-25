# 378 — DATA-ROOT-MOVE-1: Move the whole data root to a new place, verified, with the old copy kept

Status: ready
Depends on: none

## Goal

The operator can move the live data root to a new directory (the private vault) with one command that copies every
file, rebases the paths the database stores, verifies the copy against the source, and never touches the source. A
runbook covers stopping the app, the move, switching every environment file that names the data root, the checks after
start, keeping the old copy until the maintainer confirms, and rollback.

## Why

Directive `L20261125114331-7ab2` (storage split), item 2: "Data root moves into the vault … copy, verify, switch
`JARVISOS_DATA_ROOT`, keep the old copy until the maintainer confirms. Write a runbook with rollback." The existing
snapshot/restore (spec 021b-B) is not enough: it copies only the database, `workspaces/` and `artifacts/`, so a move
built on it would drop `settings/`, `secrets/`, `models/`, `bluecad/`, the retrieval index and every other top-level
entry.

## Code facts checked at `9fac1e57`

- `scripts/data_root_recovery/common.py`: `INCLUDED_ROOTS = ("workspaces", "artifacts")`, `EXCLUDED_ROOTS = ("logs",)`;
  `_inventory_tree` and `_copy_inventory` walk and copy without following symlinks and fail closed on special files;
  `rebase_absolute_path` and `_string_mentions_source_root` handle POSIX and Windows path strings.
- `scripts/data_root_recovery/restore.py::_rebase_database` rebases runner command metadata, environment metadata,
  simulation payloads, registered artifacts and BLUECAD geometry paths; `_assert_old_root_absent` and
  `_verify_restored_database` (integrity, foreign keys, artifact SHA-256 read-back) check the result.
- `backend/app/core/paths.py::JarvisPaths` places, among others, `settings/`, `secrets/`, `retrieval-index.sqlite3`,
  `bluecad/freecad/…` and the search-guard log under the data root.
- `scripts/jarvisos_launcher.py` reads `JARVISOS_DATA_ROOT` from `~/.config/jarvisos/operator.env` and otherwise
  defaults it. The search guard unit has its own env file (`scripts/host_ops.py`, `search-guard.env`).
- `scripts/vault_guard.py` (spec 377) refuses proof and job paths inside `JARVISOS_VAULT_PATH`.

## Scope

1. **`move` command.** `python3 scripts/jarvisos_data_root.py move --source-root S --target-root T`:
   - refuses when `T` is inside `S`, `S` is inside `T`, `T` exists and is not empty, or the database is being written
     (the SQLite data version changes during the copy, as snapshot already detects);
   - copies the database with `sqlite3.Connection.backup()` and every other top-level entry, `logs/` included, into an
     operation-owned partial directory next to `T`, never following links. A relative symlink whose resolved target
     stays inside `S` is recreated as the same relative link (model caches use them); an absolute symlink, one that
     leaves `S`, or a special file aborts and names its relative path;
   - rebases the database with `_rebase_database`, then runs `_assert_old_root_absent` and the restore verification;
   - also scans every other `.sqlite3` file and every UTF-8 text or JSON file under the copy for the old root. Any
     mention aborts the move and lists the relative file paths, never the content. No text file is rewritten;
   - verifies the copy: the same relative file set as the source (database excepted), the same size and SHA-256 per
     file, SQLite integrity on every database;
   - publishes `T` by atomic rename only after every check passes, and writes `T/MOVED_FROM.json` with the source path,
     time, file count, total bytes and a manifest digest;
   - never modifies, renames or deletes `S`. It prints JSON with the target, file count, bytes and the manifest digest.
2. **`verify-move` command.** `verify-move --source-root S --target-root T` repeats the read-only comparison for use
   after the app has been restarted on `T` and before the old copy is removed. Files the app changed since the move are
   listed as changed, not as failures.
3. **Runbook.** A new section in `docs/DATA_ROOT_RECOVERY.md`, "Move the data root", in this order: stop the app and
   every unit that writes the root; run `move`; edit every env file that names the data root (`operator.env`, the search
   guard env, any other found by `grep -rl JARVISOS_DATA_ROOT ~/.config`); set `JARVISOS_VAULT_PATH` if the target is
   in the vault; start; check `/health` and the system status; open a recent workspace; run `verify-move`; keep `S`
   until the maintainer confirms in writing. Rollback: stop the app, restore the old env values, start on `S`, and
   keep `T` for diagnosis. Removing `S` is the maintainer's own step, never an agent's.

Out of scope (binding non-goals):

- Running the move on the maintainer's live data root. Agents never touch the live root; the maintainer runs it from
  the runbook, or asks a host session to.
- Backups and encryption (storage-split item 5), the vault overlay (spec 376), path guards (spec 377).
- Changing snapshot/restore behaviour or their manifest format.

## Files likely touched

- `scripts/data_root_recovery/move.py` (new), `scripts/data_root_recovery/cli.py`, `scripts/data_root_recovery/__init__.py`
- `docs/DATA_ROOT_RECOVERY.md`
- `backend/tests/test_data_root_move_378.py` (new)

## Design constraints

- The source is read-only to this command in every path, including failures and interruptions.
- A failed or interrupted move leaves no published `T`; the partial directory is kept for diagnosis and never renamed
  to look complete.
- Output and errors carry relative paths only inside the root; no file content.
- Standard library only.

## Acceptance criteria

1. A fake data root with a database (runner, artifact and BLUECAD rows naming the old root), `workspaces/`,
   `artifacts/`, `settings/`, `secrets/`, `models/`, `logs/` and a retrieval index moves completely: every file is
   present in `T` with the same hash, an in-root relative symlink is still a link to the same relative target, the
   database rows name `T`, and `S` is byte-identical before and after.
2. `T` inside `S`, `S` inside `T`, a non-empty `T`, a symlink leaving `S`, and a settings JSON that names the old root each
   abort with no published `T`, and `S` is unchanged.
3. A failure injected after the copy (for example in verification) leaves no `T` and keeps the partial directory.
4. `verify-move` passes right after a move and lists a file changed afterwards as changed.
5. Lint, focused tests and CI pass.

## Required tests

- `backend/tests/test_data_root_move_378.py`: offline with `tmp_path` roots built by the existing data-root test
  helpers where they fit; covers criteria 1–4 and that no output contains file content.

## Definition of done

Merged with green CI on the exact head after one severe coordinator review and an independent second review (high risk:
data loss), and proven by a move of a temporary copy-shaped root on the host (never the live root). The live move is the
maintainer's runbook step.
