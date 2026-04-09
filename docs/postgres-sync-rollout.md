# Postgres Sync Rollout

## Goal

Replace the current CSV-to-DB sync bottleneck without deleting the existing
`Course`, `Instructor`, `College`, or `Department` tables that the rest of the
app already depends on.

The durable direction is:

1. Keep the current canonical application tables.
2. Add sync history in `sync_runs` and `change_log`.
3. Replace the importer with a Postgres-first staging/upsert pipeline.
4. Roll out additively so the only required downtime is the app restart that
   loads the new code.

## What Does Not Need Migration

These existing tables and models should stay in place:

- `courses`
- `instructors`
- `colleges`
- `departments`

They are already referenced throughout `admin.py`, `main.py`, `questions.py`,
and `tracking.py`. Replacing them would create unnecessary application-wide
breakage.

## What Can Be Added Safely

These tables are additive and do not require rewriting current course data:

- `sync_runs`
- `change_log`

Because the application already calls `db.create_all()` on startup, these tables
can be introduced without dropping existing data. The helper script below lets
you validate that path before the app restart:

```bash
source venv/bin/activate
python scripts/prepare_sync_schema.py
```

## Low-Downtime Rollout

### 1. Take a live Postgres backup

`pg_dump` is the safest rollback point and requires only lightweight locks.

```bash
export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
./scripts/postgres-backup.sh
```

The script writes a custom-format dump under `backups/` by default.

### 2. Pre-create the additive sync history schema

Run this while the current app is still serving traffic:

```bash
source venv/bin/activate
python scripts/prepare_sync_schema.py
```

This verifies the core course tables are present and ensures `sync_runs` and
`change_log` exist. It does not delete or rewrite any course data.

### 3. Deploy the new importer code

The importer rewrite should target Postgres set-based operations:

- load CSVs into staging tables
- upsert `courses` with `INSERT ... ON CONFLICT DO UPDATE`
- refresh `instructors` for the sync window with delete + bulk insert
- recompute `student_count` with set-based `UPDATE ... FROM`
- write `change_log` from SQL diffs or from staged pre/post snapshots

The application tables stay the same; only the sync path changes.

### 4. Restart the app

This is the only expected downtime window:

```bash
sudo systemctl restart tce-admin.service
sudo systemctl status tce-admin.service --no-pager
```

### 5. Run and validate one sync

After restart:

1. Trigger `HANA to Datasource`.
2. Watch `/settings/sync-logs`.
3. Confirm `sync_runs` / `change_log` are being populated.
4. Validate `/verification/` against the imported term window.

## Rollback

If the new importer causes bad writes or cannot complete in time:

1. Stop the web app.
2. Restore the latest dump.
3. Restart the app on the previous code revision.

Restore command:

```bash
export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
./scripts/postgres-restore.sh /path/to/backup.dump --yes-i-understand
```

## Recommended Durable Importer Shape

For Postgres, the importer should be rewritten around staging tables, not ORM
row loops:

- `courses_stage`
- `instructors_stage`
- `student_counts_stage`

Recommended flow per sync:

1. Create temporary or per-run staging tables.
2. Bulk load CSV data into staging.
3. Upsert from staging into canonical tables.
4. Record row/field deltas into `change_log`.
5. Drop or truncate staging tables.

That keeps the canonical schema stable while moving the expensive sync work into
set-based SQL that Postgres handles efficiently.
