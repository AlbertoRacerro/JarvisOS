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
