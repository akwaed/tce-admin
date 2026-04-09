# Postgres Sync Rollout

## Goal

Replace the current CSV-to-DB sync bottleneck without deleting the existing
`Course`, `Instructor`, `College`, or `Department` tables that the rest of the
app already depends on.

Important current state: the live server is presently using SQLite because
`tce-admin.service` does not set `DATABASE_URL`. A Postgres cutover is a real
database migration, not just an importer change.

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

## SQLite To Postgres Migration

### 1. Take a live SQLite backup

Use the backup API instead of copying the `.db` file directly:

```bash
./scripts/sqlite-backup.sh
```

This writes a timestamped SQLite backup under `backups/`.

### 2. Create the target PostgreSQL database

Create an empty Postgres database and user before touching the app service.

Example target URL format:

```bash
postgresql+psycopg2://user:pass@host:5432/tce_admin
```

### 3. Install a PostgreSQL driver in the venv

The repo currently does not declare one. Install the driver you intend to use
for SQLAlchemy on the server before migration.

Example:

```bash
source venv/bin/activate
pip install psycopg2-binary
```

### 4. Pre-seed PostgreSQL from SQLite

Run the one-time migration into a fresh Postgres database while the app is still
serving traffic from SQLite:

```bash
source venv/bin/activate
python scripts/migrate_sqlite_to_postgres.py \
  --source-sqlite instance/tce_admin.db \
  --target-url 'postgresql+psycopg2://user:pass@host:5432/tce_admin'
```

This creates the target schema from SQLAlchemy metadata, copies all known app
tables in dependency order, and resets Postgres sequences afterward.

### 5. Validate the new database

Before cutover, confirm the core counts are sane in Postgres:

```bash
DATABASE_URL='postgresql+psycopg2://user:pass@host:5432/tce_admin' \
python scripts/prepare_sync_schema.py
```

This should report the expected counts and confirm that no destructive schema
changes were needed.

## Low-Downtime Cutover

### 1. Freeze writes briefly

Stop the app and take one final SQLite backup immediately before switching:

```bash
sudo systemctl stop tce-admin.service
./scripts/sqlite-backup.sh
```

### 2. Re-run the SQLite -> Postgres copy

Repeat the migration against the final backup so Postgres has the last writes
from SQLite:

```bash
source venv/bin/activate
python scripts/migrate_sqlite_to_postgres.py \
  --source-sqlite backups/<final-backup>.db \
  --target-url 'postgresql+psycopg2://user:pass@host:5432/tce_admin'
```

Use a fresh Postgres database for this final pass, or explicitly wipe and
recreate the target before rerunning.

### 3. Keep a Postgres rollback snapshot too

`pg_dump` is the safest rollback point and requires only lightweight locks.

```bash
export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
./scripts/postgres-backup.sh
```

The script writes a custom-format dump under `backups/` by default.

### 4. Point the service at Postgres

Prefer an env file over embedding credentials directly in the unit:

```bash
sudo tee /etc/tce-admin.env >/dev/null <<'EOF'
DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/tce_admin
EOF
sudo chmod 600 /etc/tce-admin.env
sudo systemctl edit --full tce-admin.service
```

Add this line under `[Service]`:

```ini
EnvironmentFile=/etc/tce-admin.env
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart tce-admin.service
sudo systemctl status tce-admin.service --no-pager
```

### 5. Validate the live app

After restart:

1. Load `/settings/` and `/verification/`.
2. Confirm admins can log in.
3. Trigger one sync and watch `/settings/sync-logs`.
4. Confirm `sync_runs` / `change_log` are being populated if the new sync code
   is deployed.

## Postgres-Only Additive Schema Prep

Once the service is pointing at Postgres, or in a shell with `DATABASE_URL`
set to the Postgres target, you can safely run:

```bash
source venv/bin/activate
python scripts/prepare_sync_schema.py
```

This verifies the core course tables are present and ensures `sync_runs` and
`change_log` exist. It does not delete or rewrite any course data.

## Deploy the Postgres Importer

The importer rewrite should target Postgres set-based operations:

- load CSVs into staging tables
- upsert `courses` with `INSERT ... ON CONFLICT DO UPDATE`
- refresh `instructors` for the sync window with delete + bulk insert
- recompute `student_count` with set-based `UPDATE ... FROM`
- write `change_log` from SQL diffs or from staged pre/post snapshots

The application tables stay the same; only the sync path changes.

Do not cut the live app to Postgres while `app/services/course_sync.py` still
contains the SQLite-only `FastCourseSync` implementation. Replace or revert that
first, then switch the service over.

## Rollback

If the new importer causes bad writes or cannot complete in time:

1. Stop the web app.
2. Empty `/etc/tce-admin.env` or remove the `EnvironmentFile` line from
   `tce-admin.service` so the app falls back to SQLite again.
3. Restore the latest SQLite backup if needed.
4. Restart the app on the previous code revision.

Restore command:

```bash
sudo cp /dev/null /etc/tce-admin.env
sudo systemctl daemon-reload
sudo systemctl restart tce-admin.service
```

If you need to restore the Postgres target for another attempt:

```bash
export DATABASE_URL='postgresql+psycopg2://user:pass@host:5432/dbname'
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
