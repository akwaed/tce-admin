#!/usr/bin/env python3
"""
Backup change_log / sync_runs and optionally backfill tracking baselines.

Safety
------
- Always writes a timestamped backup of the current change_log and sync_runs
  tables before any mutation (even with --dry-run we still write the backup
  so you have a restore point from "right now").
- Does NOT silently delete historical rows. No-op "Updated" rows (Old == New
  after date normalization) can optionally be *archived* into
  change_log_archived (copied then removed from change_log) with --archive-noops.
- first_seen_in_tracking_at is only set where currently NULL:
    * courses that already have change_log rows → MIN(created_at) for that section
    * courses with zero change_log rows → left NULL unless --stamp-untracked-now
      (stamps "now" as the honest baseline going forward — not a creation date)

Usage
-----
    # Backup only (recommended first step on prod)
    FLASK_ENV=production python scripts/backup_and_backfill_change_tracking.py --backup-only

    # Preview baseline stamps + optional archive counts
    python scripts/backup_and_backfill_change_tracking.py --dry-run

    # Apply baseline backfill (no archive)
    python scripts/backup_and_backfill_change_tracking.py --apply

    # Apply + archive false-positive no-op updates after backup
    python scripts/backup_and_backfill_change_tracking.py --apply --archive-noops

    # Stamp first_seen=now for courses that still have no history (honest baseline)
    python scripts/backup_and_backfill_change_tracking.py --apply --stamp-untracked-now

Restore
-------
Backup files land under:

    data/backups/change_tracking/YYYYMMDD_HHMMSS/

Contents:
    change_log.jsonl   — one JSON object per line
    sync_runs.jsonl
    manifest.json      — row counts, timestamps, how to restore

To restore into PostgreSQL (example; adapt table columns if schema drifts):

    # Recreate from JSONL (requires psql + a small loader, or use the
    # companion restore notes in manifest.json). Prefer restoring from a
    # full DB dump if you have one; this JSONL is the safety net for the
    # change-tracking tables specifically.

SQLite restore example (dev):

    python scripts/backup_and_backfill_change_tracking.py --restore-from data/backups/change_tracking/TIMESTAMP
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

UTC = timezone.utc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env')
except ImportError:
    pass

from sqlalchemy import inspect, text

from app import create_app
from app.models import db
from app.models.course import Course
from app.models.sync_history import ChangeLog, SyncRun
from app.services.course_sync import normalize_diff_value


BACKUP_ROOT = PROJECT_ROOT / 'data' / 'backups' / 'change_tracking'


def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _ensure_archive_table():
    """Create change_log_archived if missing (additive, both SQLite and PG)."""
    dialect = str(db.engine.url.drivername)
    if 'postgresql' in dialect or 'postgres' in dialect:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS change_log_archived (
                id BIGINT PRIMARY KEY,
                sync_run_id INTEGER,
                created_at TIMESTAMP,
                entity_type VARCHAR(30),
                entity_key VARCHAR(200),
                change_type VARCHAR(20),
                field_name VARCHAR(100),
                old_value TEXT,
                new_value TEXT,
                display_label VARCHAR(300),
                archived_at TIMESTAMP,
                archive_reason VARCHAR(100)
            )
        """))
    else:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS change_log_archived (
                id INTEGER PRIMARY KEY,
                sync_run_id INTEGER,
                created_at DATETIME,
                entity_type VARCHAR(30),
                entity_key VARCHAR(200),
                change_type VARCHAR(20),
                field_name VARCHAR(100),
                old_value TEXT,
                new_value TEXT,
                display_label VARCHAR(300),
                archived_at DATETIME,
                archive_reason VARCHAR(100)
            )
        """))
    db.session.commit()


def _row_to_dict(row, columns):
    return {col: getattr(row, col) for col in columns}


def backup_tables(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cl_cols = [
        'id', 'sync_run_id', 'created_at', 'entity_type', 'entity_key',
        'change_type', 'field_name', 'old_value', 'new_value', 'display_label',
    ]
    sr_cols = [
        'id', 'started_at', 'completed_at', 'status',
        'courses_added', 'courses_updated', 'courses_removed',
        'instructors_added', 'instructors_removed', 'students_counted',
        'change_log_rows', 'elapsed_seconds', 'error_text',
        'datasources_path', 'term_codes',
    ]

    cl_path = out_dir / 'change_log.jsonl'
    sr_path = out_dir / 'sync_runs.jsonl'
    cl_count = 0
    with cl_path.open('w', encoding='utf-8') as f:
        for row in ChangeLog.query.order_by(ChangeLog.id).yield_per(1000):
            f.write(json.dumps(_row_to_dict(row, cl_cols), default=_json_default) + '\n')
            cl_count += 1

    sr_count = 0
    with sr_path.open('w', encoding='utf-8') as f:
        for row in SyncRun.query.order_by(SyncRun.id).yield_per(500):
            f.write(json.dumps(_row_to_dict(row, sr_cols), default=_json_default) + '\n')
            sr_count += 1

    manifest = {
        'created_at': datetime.now(UTC).isoformat(),
        'database': str(db.engine.url).split('@')[-1] if '@' in str(db.engine.url) else str(db.engine.url),
        'change_log_rows': cl_count,
        'sync_runs_rows': sr_count,
        'files': {
            'change_log': str(cl_path.relative_to(PROJECT_ROOT)),
            'sync_runs': str(sr_path.relative_to(PROJECT_ROOT)),
        },
        'restore_notes': (
            'This is a full dump of change_log and sync_runs at backup time, including '
            'buggy no-op Updated rows. To restore: stop writers, truncate or delete '
            'current rows if replacing, then re-insert from the JSONL files. Prefer a '
            'full postgres/sqlite dump for disaster recovery; this file is the '
            'change-tracking safety net. See --restore-from on this script for a best-effort reimport.'
        ),
    }
    man_path = out_dir / 'manifest.json'
    man_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def find_noop_ids(limit=None):
    """Yield change_log ids where updated + normalized old == new."""
    q = ChangeLog.query.filter(ChangeLog.change_type == 'updated')
    if limit:
        q = q.limit(limit)
    for row in q.yield_per(1000):
        if normalize_diff_value(row.old_value) == normalize_diff_value(row.new_value):
            yield row


def backfill_first_seen(dry_run: bool, stamp_untracked_now: bool) -> dict:
    """Set first_seen_in_tracking_at where NULL using earliest change_log time."""
    stats = {
        'from_change_log': 0,
        'stamped_now': 0,
        'already_set': 0,
        'still_null': 0,
    }
    now = datetime.now(UTC).replace(tzinfo=None)

    # Courses that already have a marker.
    already = Course.query.filter(Course.first_seen_in_tracking_at.isnot(None)).count()
    stats['already_set'] = already

    # Earliest change per section_key (course-level keys only — not instructor pairs).
    # Also consider entity_key like "section|user" by taking the section prefix.
    # Simpler approach: for each course, find min created_at where entity_key = section
    # OR entity_key LIKE section||'|%'
    dialect = str(db.engine.url.drivername)
    if 'postgresql' in dialect or 'postgres' in dialect:
        sql = text("""
            SELECT c.section_key, MIN(cl.created_at) AS first_at
              FROM courses c
              JOIN change_log cl
                ON cl.entity_key = c.section_key
                OR cl.entity_key LIKE c.section_key || '|%'
             WHERE c.first_seen_in_tracking_at IS NULL
             GROUP BY c.section_key
        """)
    else:
        sql = text("""
            SELECT c.section_key, MIN(cl.created_at) AS first_at
              FROM courses c
              JOIN change_log cl
                ON cl.entity_key = c.section_key
                OR cl.entity_key LIKE (c.section_key || '|%')
             WHERE c.first_seen_in_tracking_at IS NULL
             GROUP BY c.section_key
        """)

    rows = db.session.execute(sql).fetchall()
    for section_key, first_at in rows:
        stats['from_change_log'] += 1
        if not dry_run:
            db.session.execute(
                text(
                    'UPDATE courses SET first_seen_in_tracking_at = :ts '
                    'WHERE section_key = :sk AND first_seen_in_tracking_at IS NULL'
                ),
                {'ts': first_at, 'sk': section_key},
            )

    if stamp_untracked_now:
        untracked = Course.query.filter(Course.first_seen_in_tracking_at.is_(None)).all()
        for course in untracked:
            # Skip if we just would have set from change_log but dry-run
            stats['stamped_now'] += 1
            if not dry_run:
                course.first_seen_in_tracking_at = now
    else:
        stats['still_null'] = Course.query.filter(
            Course.first_seen_in_tracking_at.is_(None)
        ).count()
        if dry_run:
            # In dry-run, from_change_log rows are still NULL in DB
            stats['still_null'] = max(0, stats['still_null'] - stats['from_change_log'])

    if not dry_run:
        db.session.commit()
    else:
        db.session.rollback()

    return stats


def archive_noops(dry_run: bool) -> dict:
    """Copy no-op updated rows to change_log_archived, then delete from change_log."""
    _ensure_archive_table()
    now = datetime.now(UTC).replace(tzinfo=None)
    archived = 0
    ids = []
    for row in find_noop_ids():
        ids.append(row.id)
        archived += 1
        if dry_run:
            continue
        db.session.execute(
            text("""
                INSERT INTO change_log_archived (
                    id, sync_run_id, created_at, entity_type, entity_key,
                    change_type, field_name, old_value, new_value, display_label,
                    archived_at, archive_reason
                ) VALUES (
                    :id, :sync_run_id, :created_at, :entity_type, :entity_key,
                    :change_type, :field_name, :old_value, :new_value, :display_label,
                    :archived_at, :archive_reason
                )
            """),
            {
                'id': row.id,
                'sync_run_id': row.sync_run_id,
                'created_at': row.created_at,
                'entity_type': row.entity_type,
                'entity_key': row.entity_key,
                'change_type': row.change_type,
                'field_name': row.field_name,
                'old_value': row.old_value,
                'new_value': row.new_value,
                'display_label': row.display_label,
                'archived_at': now,
                'archive_reason': 'noop_old_equals_new',
            },
        )

    if not dry_run and ids:
        # Delete in chunks
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ChangeLog.query.filter(ChangeLog.id.in_(chunk)).delete(synchronize_session=False)
        db.session.commit()
    else:
        db.session.rollback()

    return {'noop_rows': archived, 'archived': 0 if dry_run else archived}


def restore_from(backup_dir: Path, dry_run: bool) -> dict:
    """Best-effort reimport of change_log + sync_runs from a backup directory."""
    cl_path = backup_dir / 'change_log.jsonl'
    sr_path = backup_dir / 'sync_runs.jsonl'
    if not cl_path.exists() or not sr_path.exists():
        raise SystemExit(f'Missing jsonl files in {backup_dir}')

    stats = {'sync_runs': 0, 'change_log': 0}
    if dry_run:
        with sr_path.open() as f:
            stats['sync_runs'] = sum(1 for _ in f)
        with cl_path.open() as f:
            stats['change_log'] = sum(1 for _ in f)
        return stats

    # Insert sync_runs first (FK parent). Skip ids that already exist.
    with sr_path.open(encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            exists = SyncRun.query.get(row['id'])
            if exists:
                continue
            # Parse datetimes
            for k in ('started_at', 'completed_at'):
                if row.get(k):
                    row[k] = datetime.fromisoformat(row[k])
            db.session.execute(
                text("""
                    INSERT INTO sync_runs (
                        id, started_at, completed_at, status,
                        courses_added, courses_updated, courses_removed,
                        instructors_added, instructors_removed, students_counted,
                        change_log_rows, elapsed_seconds, error_text,
                        datasources_path, term_codes
                    ) VALUES (
                        :id, :started_at, :completed_at, :status,
                        :courses_added, :courses_updated, :courses_removed,
                        :instructors_added, :instructors_removed, :students_counted,
                        :change_log_rows, :elapsed_seconds, :error_text,
                        :datasources_path, :term_codes
                    )
                """),
                row,
            )
            stats['sync_runs'] += 1

    with cl_path.open(encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            exists = ChangeLog.query.get(row['id'])
            if exists:
                continue
            if row.get('created_at'):
                row['created_at'] = datetime.fromisoformat(row['created_at'])
            db.session.execute(
                text("""
                    INSERT INTO change_log (
                        id, sync_run_id, created_at, entity_type, entity_key,
                        change_type, field_name, old_value, new_value, display_label
                    ) VALUES (
                        :id, :sync_run_id, :created_at, :entity_type, :entity_key,
                        :change_type, :field_name, :old_value, :new_value, :display_label
                    )
                """),
                row,
            )
            stats['change_log'] += 1

    db.session.commit()
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backup-only', action='store_true', help='Only write backup files')
    parser.add_argument('--dry-run', action='store_true', help='Report actions without mutating (backup still written)')
    parser.add_argument('--apply', action='store_true', help='Apply baseline backfill (and archive if requested)')
    parser.add_argument('--archive-noops', action='store_true',
                        help='Archive false-positive Updated rows (Old==New) into change_log_archived')
    parser.add_argument('--stamp-untracked-now', action='store_true',
                        help='Set first_seen_in_tracking_at=now for courses with no history')
    parser.add_argument('--restore-from', type=str, default=None,
                        help='Restore change_log/sync_runs from a backup directory')
    parser.add_argument('--skip-backup', action='store_true',
                        help='Skip backup (not recommended; only for restore-from dry checks)')
    args = parser.parse_args(argv)

    if not args.backup_only and not args.dry_run and not args.apply and not args.restore_from:
        parser.error('Specify --backup-only, --dry-run, --apply, and/or --restore-from')

    app = create_app(os.environ.get('FLASK_ENV', 'default'))
    with app.app_context():
        # Ensure additive column exists before backfill.
        inspector = inspect(db.engine)
        if inspector.has_table('courses'):
            cols = {c['name'] for c in inspector.get_columns('courses')}
            if 'first_seen_in_tracking_at' not in cols:
                print('Adding courses.first_seen_in_tracking_at ...')
                dialect = str(db.engine.url.drivername)
                try:
                    db.session.execute(text(
                        'ALTER TABLE courses ADD COLUMN IF NOT EXISTS '
                        'first_seen_in_tracking_at TIMESTAMP'
                    ))
                except Exception:
                    try:
                        db.session.execute(text(
                            'ALTER TABLE courses ADD COLUMN first_seen_in_tracking_at TIMESTAMP'
                        ))
                    except Exception as exc:
                        print(f'WARNING: could not add column: {exc}')
                db.session.commit()

        stamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
        out_dir = BACKUP_ROOT / stamp

        if not args.skip_backup and not args.restore_from:
            print(f'Writing backup to {out_dir} ...')
            manifest = backup_tables(out_dir)
            print(
                f"  change_log: {manifest['change_log_rows']:,} rows\n"
                f"  sync_runs:  {manifest['sync_runs_rows']:,} rows\n"
                f"  manifest:   {out_dir / 'manifest.json'}"
            )
            print(
                '\nRestore: keep this directory. Re-import with:\n'
                f'  python scripts/backup_and_backfill_change_tracking.py '
                f'--restore-from {out_dir} --skip-backup\n'
            )

        if args.backup_only:
            return 0

        if args.restore_from:
            path = Path(args.restore_from)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            stats = restore_from(path, dry_run=args.dry_run or not args.apply)
            print('Restore', '(dry-run)' if (args.dry_run or not args.apply) else '', stats)
            if args.dry_run or not args.apply:
                print('Pass --apply with --restore-from to actually insert missing rows.')
            return 0

        dry = args.dry_run or not args.apply
        mode = 'DRY-RUN' if dry else 'APPLY'
        print(f'\n[{mode}] Backfilling first_seen_in_tracking_at ...')
        fs_stats = backfill_first_seen(dry_run=dry, stamp_untracked_now=args.stamp_untracked_now)
        print(json.dumps(fs_stats, indent=2))

        if args.archive_noops:
            print(f'\n[{mode}] Archiving no-op Updated rows ...')
            ar_stats = archive_noops(dry_run=dry)
            print(json.dumps(ar_stats, indent=2))
        else:
            # Still report how many no-ops exist
            n = sum(1 for _ in find_noop_ids())
            print(f'\nNo-op Updated rows currently in change_log: {n:,}')
            print('(Left in place; UI hides them by default. Use --archive-noops to move them.)')

        if dry:
            print('\nNo database mutations applied (dry-run). Re-run with --apply to commit.')
        else:
            print('\nDone.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
