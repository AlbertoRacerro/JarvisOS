# JarvisOS Data-Root Recovery Runbook

This runbook covers the local, operator-triggered recovery boundary implemented
by spec 021b-B. It does not add cloud backup, a scheduler, a daemon, encryption,
compression, or automatic destructive recovery.

## Scope

A snapshot contains:

- the SQLite database, created through `sqlite3.Connection.backup()`;
- `workspaces/`;
- `artifacts/`.

It intentionally excludes `logs/`. A snapshot is usable only when both
`manifest.json` and `COMPLETE` are present and `verify` succeeds.

## Create a snapshot

From the repository root:

```powershell
python scripts/jarvisos_data_root.py snapshot `
  --source-root C:\JarvisOS `
  --destination D:\JarvisOS-Backups
```

The source root may be omitted when the current JarvisOS settings resolve the
correct data root. The destination must be outside the source root.

Optional bounded retention:

```powershell
python scripts/jarvisos_data_root.py snapshot `
  --source-root C:\JarvisOS `
  --destination D:\JarvisOS-Backups `
  --keep-last 5
```

Retention runs only after the new snapshot verifies successfully. It counts and
removes only complete verified `snapshot-*` directories. Partial or corrupt
folders are left untouched for diagnosis.

A successful command prints JSON containing the final snapshot directory,
snapshot id, and manifest SHA-256.

## Verify a snapshot

```powershell
python scripts/jarvisos_data_root.py verify `
  D:\JarvisOS-Backups\snapshot-<snapshot-id>
```

Verification is read-only and fails nonzero for incomplete metadata, unsupported
manifest versions, unexpected or missing files, path escapes, size/hash drift,
SQLite integrity failure, migration mismatch, or row-count mismatch.

Do not restore a snapshot that fails verification. Do not rename a
`.partial-*` directory to make it appear complete.

## Restore to a new root

Stop the JarvisOS backend before restoring. Restore first to an absent or empty
target:

```powershell
python scripts/jarvisos_data_root.py restore `
  D:\JarvisOS-Backups\snapshot-<snapshot-id> `
  --target-root E:\JarvisOS-Restored
```

Restore verifies the source snapshot, builds an operation-owned partial target,
rebases registered root-bound database paths, checks SQLite integrity and
foreign keys, reads back registered artifacts by SHA-256, and publishes the
target by atomic rename only after all checks pass.

The source snapshot is never deleted or modified by restore.

## Replace a non-empty target

The default is fail-closed. To replace an existing non-empty target, the operator
must supply the explicit destructive flag:

```powershell
python scripts/jarvisos_data_root.py restore `
  D:\JarvisOS-Backups\snapshot-<snapshot-id> `
  --target-root C:\JarvisOS `
  --allow-nonempty-target
```

Before using this flag:

1. stop the backend and every process writing the target root;
2. verify the snapshot separately;
3. confirm the target path exactly;
4. preserve an independent backup when the existing target has value.

The restore stages the new root before moving the previous target. If publication
fails, it attempts to restore the previous directory and never removes the
source snapshot.

## Activate the restored root

Set the data-root environment variable before starting the backend:

```powershell
$env:JARVISOS_DATA_ROOT = "E:\JarvisOS-Restored"
.\scripts\start-backend.ps1
```

Then verify:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/system/info
```

The restored root must not depend on the original source path. Runner paths,
registered artifacts, and BLUECAD geometry paths are rebased transactionally;
an unresolved old-root occurrence aborts restore.

## Failure handling

- A failed snapshot never publishes a complete `snapshot-*` directory.
- A failed restore never modifies the source snapshot.
- Symlinks, special files, destination-under-source layouts, concurrent source
  file changes, and concurrent SQLite data-version changes fail closed.
- Preserve any unexpected partial directory and command output long enough to
  diagnose the cause, but do not treat it as a valid backup.

## Move the data root

`move` copies the **whole** data root (every top-level entry, `logs/` included) to a new place, for example into the
private vault (spec 378). Snapshot and restore copy only the database, `workspaces/` and `artifacts/`; do not use them
for a move. The source is never modified, renamed or deleted.

1. Stop the app and every unit that writes the data root (backend, search guard, workers). Nothing may write the
   source while it is copied; a change during the copy aborts the move.
2. Run, from the repository root:

   ```bash
   python3 scripts/jarvisos_data_root.py move --source-root <old-root> --target-root <new-root>
   ```

   The target must be absent or empty and must not contain the source or lie inside it. The command copies the
   database with SQLite's backup API and every other file byte for byte, keeps relative symlinks that stay inside the
   root (model caches use them), rebases the paths the database stores, and verifies sizes, SHA-256 hashes and SQLite
   integrity. It refuses absolute symlinks, symlinks that leave the root and special files, and it aborts when a
   configuration file (`.json`, `.toml`, `.env`, `.ini`, `.yaml`, `.yml`) outside `logs/`, `artifacts/` and
   `workspaces/` still names the old root: edit that file in the source and run again. Mentions in logs, artifacts,
   workspace files and other databases are history; they are listed as `old_root_mentions` and not changed. On
   success it publishes the target in one rename and writes `MOVED_FROM.json` there. On failure no target is
   published; a `.partial-move-*` directory next to it is kept for diagnosis. Delete it by hand once understood.
3. Point every environment file that names the data root at the new root: `~/.config/jarvisos/operator.env`, the
   search guard's env file, and any other that `grep -rl JARVISOS_DATA_ROOT ~/.config` finds. When the new root is in
   the private vault, also set `JARVISOS_VAULT_PATH` to the vault directory.
4. Start the app. Check `/health` and the system status, then open a recent workspace and a recent result.
5. Run the read-only comparison:

   ```bash
   python3 scripts/jarvisos_data_root.py verify-move --source-root <old-root> --target-root <new-root>
   ```

   It fails only when a source file is missing from the target. Files the app changed or added since the move are
   listed as `changed` and `added`.
6. Keep the old root until the maintainer confirms in writing that the new one works. Removing it is the maintainer's
   own step, never an agent's.

**Rollback.** Stop the app, put the old values back in every environment file changed in step 3, and start the app
on the old root. Keep the new root for diagnosis. Anything written only to the new root after the move is not in the
old one; `verify-move` lists it as `changed` or `added`.
