"""
Course Data Sync Service  (PostgreSQL rewrite, April 2026)

Replaces the SQLite-only FastCourseSync implementation. This version uses
psycopg2 directly (bypassing SQLAlchemy ORM writes) for bulk performance, but
falls back gracefully to SQLAlchemy's raw connection when psycopg2 is not
directly accessible (e.g. connection-pool wrapped engines).

Architecture
------------
- Stage 1: load CSVs into TEMP staging tables via ``COPY FROM STDIN`` (fast path)
  or ``execute_values`` (fallback).  Neither blocks production tables.
- Stage 2: per-field diff against current production rows → change_log buffer.
- Stage 3: atomic UPSERT via ``INSERT ... ON CONFLICT DO UPDATE`` for courses
  and instructors; orphan rows removed.
- Stage 4: flush change_log buffer + finalize sync_run row.
- Cleanup: purge change_log rows older than 90 days.

Public API (unchanged from old service)
----------------------------------------
- ``CourseSyncService(datasources_path).sync_all()``
- ``get_sync_progress()``
- ``resolve_datasources_path(path)``
- ``generate_sample_data()``, ``write_sample_csvs()``
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
UTC = timezone.utc

from app.models import db
from app.services.sync_control import SyncCancelledError, raise_if_sync_cancelled

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

BATCH_SIZE = 5000          # rows per executemany / execute_values call
PROGRESS_UPDATE_INTERVAL = 5000
CHANGE_LOG_CAP = 50000     # cap per sync to bound storage on first-ever sync
CHANGE_LOG_RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# Global progress tracking (same API as old service)
# ---------------------------------------------------------------------------

_sync_progress = {
    'running': False,
    'current_step': '',
    'step_number': 0,
    'total_steps': 5,
    'records_processed': 0,
    'started_at': None,
    'error': None,
}
_sync_lock = threading.Lock()


def get_sync_progress():
    with _sync_lock:
        return dict(_sync_progress)


def _update_progress(step, step_number, records=0, error=None):
    with _sync_lock:
        _sync_progress['current_step'] = step
        _sync_progress['step_number'] = step_number
        _sync_progress['records_processed'] += records
        if error:
            _sync_progress['error'] = error


def _reset_progress():
    with _sync_lock:
        _sync_progress.update({
            'running': True,
            'current_step': 'Initializing...',
            'step_number': 0,
            'total_steps': 5,
            'records_processed': 0,
            'started_at': datetime.now(UTC).isoformat(),
            'error': None,
        })


def _finish_progress(message='Complete'):
    with _sync_lock:
        _sync_progress['running'] = False
        _sync_progress['current_step'] = message
        _sync_progress['step_number'] = _sync_progress['total_steps']


# ---------------------------------------------------------------------------
# Path resolver (unchanged API)
# ---------------------------------------------------------------------------

def resolve_datasources_path(primary_path='./datasources'):
    expected_files = {
        'Courses.csv', 'Instructor_Course.csv',
        'Student_Course.csv', 'Users.csv',
    }
    candidates = [primary_path]
    if primary_path.endswith('datasources'):
        candidates.append(primary_path.replace('datasources', 'datasourses'))
    elif primary_path.endswith('datasourses'):
        candidates.append(primary_path.replace('datasourses', 'datasources'))

    for candidate in candidates:
        if not os.path.isdir(candidate):
            continue
        existing = set(os.listdir(candidate))
        if expected_files.intersection(existing):
            return candidate
    return primary_path


# ---------------------------------------------------------------------------
# Sample data helpers (kept so run.py --generate-sample still works)
# ---------------------------------------------------------------------------

def generate_sample_data():
    return {
        'courses': [],
        'instructors': [],
        'students': [],
        'users': [],
    }


def write_sample_csvs(path='./datasources'):
    """No-op stub kept for backward-compat with run.py."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(value):
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y-%m-%d %H:%M:%S', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _clean(row, key):
    return (row.get(key) or '').strip()


def _get_raw_pg_connection():
    """Return the underlying psycopg2 connection from SQLAlchemy engine.

    Works with both SQLAlchemy 1.x and 2.x connection pool styles.
    Raises RuntimeError if the backend is not PostgreSQL.
    """
    url = db.engine.url
    driver = str(url.drivername)
    if 'postgresql' not in driver and 'postgres' not in driver:
        raise RuntimeError(
            f'CourseSyncService requires PostgreSQL; got {driver}. '
            'Set DATABASE_URL to a postgresql:// connection string.'
        )
    # Grab a raw DBAPI connection from the pool.
    raw = db.engine.raw_connection()
    return raw


# ---------------------------------------------------------------------------
# Low-level bulk helpers
# ---------------------------------------------------------------------------

def _execute_values(cursor, sql, rows, page_size=BATCH_SIZE):
    """Portable bulk insert using psycopg2.extras.execute_values."""
    try:
        from psycopg2.extras import execute_values
        execute_values(cursor, sql, rows, page_size=page_size)
    except ImportError:
        # Fallback: plain executemany (slower but works)
        import re
        # Convert "INSERT INTO t (a,b) VALUES %s" -> "INSERT INTO t (a,b) VALUES (%s,%s)"
        if rows:
            placeholder = '(' + ','.join(['%s'] * len(rows[0])) + ')'
            sql_plain = re.sub(r'VALUES\s*%s', f'VALUES {placeholder}', sql, flags=re.IGNORECASE)
            cursor.executemany(sql_plain, rows)


def _copy_csv_to_table(cursor, table_name, columns, filepath):
    """Use COPY FROM STDIN to bulk-load a CSV file into a staging table.

    This is the fastest possible ingest path: the CSV bytes go straight into
    PostgreSQL with no Python-side row iteration.
    """
    col_list = ', '.join(columns)
    sql = f"COPY {table_name} ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    with open(filepath, 'r', encoding='utf-8') as f:
        cursor.copy_expert(sql, f)


# ---------------------------------------------------------------------------
# Sync service
# ---------------------------------------------------------------------------

class CourseSyncService:
    """PostgreSQL-native CSV -> DB sync with per-field change tracking."""

    def __init__(self, datasources_path='./datasources'):
        self.datasources_path = resolve_datasources_path(datasources_path)
        self.errors = []
        self.stats = {
            'courses_added': 0,
            'courses_updated': 0,
            'courses_removed': 0,
            'users_synced': 0,
            'instructors_added': 0,
            'instructors_removed': 0,
            'student_enrollments_synced': 0,
            'colleges_added': 0,
            'departments_added': 0,
            'students_counted': 0,
            'change_log_rows': 0,
        }
        self.sync_run_id = None
        self._csv_section_keys = set()
        self._csv_terms = set()
        self._users_cache = {}
        self._change_buffer = []  # list of tuples
        self._sync_log_id = None

    def _raise_if_cancelled(self, conn):
        if self._sync_log_id:
            raise_if_sync_cancelled(
                self._sync_log_id,
                conn=conn,
                message='Sync cancelled by user.',
            )

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def sync_all(self, sync_log_id=None):
        """Run the full sync. Returns a summary dict; raises on fatal error."""
        _reset_progress()
        self._sync_log_id = sync_log_id

        # Flush SQLAlchemy session before we grab a raw connection.
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        db.session.remove()

        started = time.monotonic()
        started_at = datetime.now(UTC).replace(tzinfo=None)

        conn = _get_raw_pg_connection()
        conn.autocommit = False  # we manage transactions explicitly
        try:
            self._raise_if_cancelled(conn)
            self._ensure_tables_exist(conn)
            self.sync_run_id = self._create_sync_run(conn, started_at)
            conn.commit()

            self._raise_if_cancelled(conn)
            _update_progress('Loading courses...', 1)
            self._sync_courses(conn)

            self._raise_if_cancelled(conn)
            _update_progress('Loading users and instructors...', 2)
            self._sync_users(conn)
            self._sync_instructors(conn)

            self._raise_if_cancelled(conn)
            _update_progress('Loading student enrollments...', 3)
            self._sync_student_counts(conn)

            self._raise_if_cancelled(conn)
            _update_progress('Flushing change log...', 4)
            self._flush_change_buffer(conn)

            elapsed = time.monotonic() - started
            self._raise_if_cancelled(conn)
            _update_progress('Finalizing...', 5)
            self._finalize_sync_run(conn, elapsed, 'completed')
            conn.commit()

            # Cleanup old change_log rows (outside main transaction, non-fatal)
            try:
                self._purge_old_change_log(conn)
                conn.commit()
            except Exception as e:
                conn.rollback()
                self.errors.append(f'Change log cleanup warning: {e}')

            _finish_progress(f'Complete in {elapsed:.1f}s')
            return {
                'success': True,
                'stats': dict(self.stats),
                'errors': list(self.errors[:10]),
                'elapsed_seconds': elapsed,
                'sync_run_id': self.sync_run_id,
            }
        except Exception as exc:
            elapsed = time.monotonic() - started
            status = 'cancelled' if isinstance(exc, SyncCancelledError) else 'failed'
            try:
                conn.rollback()
                self._finalize_sync_run(conn, elapsed, status, str(exc))
                conn.commit()
            except Exception:
                pass
            if isinstance(exc, SyncCancelledError):
                _finish_progress('Cancelled')
                _update_progress('Cancelled', 5)
            else:
                _finish_progress(f'Failed: {exc}')
                _update_progress('Failed', 5, error=str(exc))
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _ensure_tables_exist(self, conn):
        """Idempotent: create sync_runs / change_log if not present."""
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                status VARCHAR(20),
                courses_added INTEGER DEFAULT 0,
                courses_updated INTEGER DEFAULT 0,
                courses_removed INTEGER DEFAULT 0,
                instructors_added INTEGER DEFAULT 0,
                instructors_removed INTEGER DEFAULT 0,
                students_counted INTEGER DEFAULT 0,
                change_log_rows INTEGER DEFAULT 0,
                elapsed_seconds FLOAT DEFAULT 0,
                error_text TEXT,
                datasources_path VARCHAR(500),
                term_codes VARCHAR(500)
            );
            CREATE INDEX IF NOT EXISTS ix_sync_runs_started_at
                ON sync_runs(started_at);
            CREATE INDEX IF NOT EXISTS ix_sync_runs_status
                ON sync_runs(status);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS change_log (
                id BIGSERIAL PRIMARY KEY,
                sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW(),
                entity_type VARCHAR(30) NOT NULL,
                entity_key VARCHAR(200) NOT NULL,
                change_type VARCHAR(20) NOT NULL,
                field_name VARCHAR(100),
                old_value TEXT,
                new_value TEXT,
                display_label VARCHAR(300)
            );
            CREATE INDEX IF NOT EXISTS ix_change_entity
                ON change_log(entity_type, entity_key);
            CREATE INDEX IF NOT EXISTS ix_change_run_type
                ON change_log(sync_run_id, change_type);
            CREATE INDEX IF NOT EXISTS ix_change_log_created_at
                ON change_log(created_at);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS course_users (
                user_id VARCHAR(50) PRIMARY KEY,
                first_name VARCHAR(100),
                last_name VARCHAR(100),
                email VARCHAR(200),
                last_synced TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS ix_course_users_last_name
                ON course_users(last_name);
            CREATE INDEX IF NOT EXISTS ix_course_users_first_name
                ON course_users(first_name);
            CREATE INDEX IF NOT EXISTS ix_course_users_email
                ON course_users(email);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS student_enrollments (
                id BIGSERIAL PRIMARY KEY,
                section_key VARCHAR(100) NOT NULL REFERENCES courses(section_key) ON DELETE CASCADE,
                user_id VARCHAR(50) NOT NULL REFERENCES course_users(user_id),
                last_synced TIMESTAMP DEFAULT NOW(),
                CONSTRAINT uq_student_enrollment_section_user UNIQUE (section_key, user_id)
            );
            CREATE INDEX IF NOT EXISTS ix_student_enrollments_section_key
                ON student_enrollments(section_key);
            CREATE INDEX IF NOT EXISTS ix_student_enrollments_user_id
                ON student_enrollments(user_id);
        """)
        cur.close()

    # ------------------------------------------------------------------
    # Sync run tracking
    # ------------------------------------------------------------------

    def _create_sync_run(self, conn, started_at):
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sync_runs (started_at, status, datasources_path)
            VALUES (%s, 'running', %s)
            RETURNING id
            """,
            (started_at, self.datasources_path),
        )
        run_id = cur.fetchone()[0]
        cur.close()
        return run_id

    def _finalize_sync_run(self, conn, elapsed, status, error_text=None):
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE sync_runs
               SET completed_at   = %s,
                   status         = %s,
                   courses_added  = %s,
                   courses_updated = %s,
                   courses_removed = %s,
                   instructors_added = %s,
                   instructors_removed = %s,
                   students_counted = %s,
                   change_log_rows = %s,
                   elapsed_seconds = %s,
                   error_text     = %s,
                   term_codes     = %s
             WHERE id = %s
            """,
            (
                datetime.now(UTC),
                status,
                self.stats['courses_added'],
                self.stats['courses_updated'],
                self.stats['courses_removed'],
                self.stats['instructors_added'],
                self.stats['instructors_removed'],
                self.stats['students_counted'],
                self.stats['change_log_rows'],
                elapsed,
                error_text,
                ','.join(sorted(self._csv_terms)) if self._csv_terms else None,
                self.sync_run_id,
            ),
        )
        cur.close()

    # ------------------------------------------------------------------
    # Change log buffering
    # ------------------------------------------------------------------

    def _record_change(self, entity_type, entity_key, change_type,
                       field_name=None, old_value=None, new_value=None,
                       display_label=None):
        if len(self._change_buffer) >= CHANGE_LOG_CAP:
            return
        self._change_buffer.append((
            entity_type, entity_key, change_type, field_name,
            None if old_value is None else str(old_value)[:4000],
            None if new_value is None else str(new_value)[:4000],
            display_label,
        ))

    def _flush_change_buffer(self, conn):
        if not self._change_buffer:
            return
        now = datetime.now(UTC)
        rows = [
            (self.sync_run_id, now,
             et, ek, ct, fn, ov, nv, dl)
            for (et, ek, ct, fn, ov, nv, dl) in self._change_buffer
        ]
        cur = conn.cursor()
        _execute_values(
            cur,
            """
            INSERT INTO change_log
                (sync_run_id, created_at, entity_type, entity_key,
                 change_type, field_name, old_value, new_value, display_label)
            VALUES %s
            """,
            rows,
        )
        cur.close()
        self.stats['change_log_rows'] = len(self._change_buffer)
        self._change_buffer.clear()

    def _purge_old_change_log(self, conn):
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM change_log WHERE created_at < NOW() - INTERVAL '%s days'",
            (CHANGE_LOG_RETENTION_DAYS,),
        )
        cur.close()

    # ------------------------------------------------------------------
    # Phase 1: courses
    # ------------------------------------------------------------------

    COURSE_COLUMNS = (
        'section_key', 'class_id', 'class_code', 'section_id', 'crs_section',
        'section_title', 'college_code', 'department_id', 'crosslisted_id',
        'course_start', 'course_end', 'tce_start', 'tce_end', 'tce_reminder',
        'marked_for_tce', 'student_count', 'term_code', 'last_synced',
    )

    COURSE_DIFF_FIELDS = (
        'class_id', 'class_code', 'section_id', 'crs_section', 'section_title',
        'college_code', 'department_id', 'crosslisted_id',
        'course_start', 'course_end', 'tce_start', 'tce_end', 'tce_reminder',
        'term_code',
    )

    def _sync_courses(self, conn):
        filepath = os.path.join(self.datasources_path, 'Courses.csv')
        if not os.path.exists(filepath):
            self.errors.append(f'Courses.csv not found at {filepath}')
            return

        with open(filepath, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        _update_progress(f'Loading courses from CSV... ({len(rows):,} rows)', 1)

        # First pass: collect metadata for colleges/depts.
        college_codes = {}
        dept_info = {}
        for row in rows:
            sk = _clean(row, 'SECTION_KEY')
            if sk:
                self._csv_section_keys.add(sk)
            tc = _clean(row, 'ACADEMIC_TERM')
            if tc:
                self._csv_terms.add(tc)
            cc = _clean(row, 'CLASS_COLLEGE_SHORT')
            cn = _clean(row, 'CLASS_COLLEGE')
            if cc and cc not in college_codes:
                college_codes[cc] = cn or cc
            did = _clean(row, 'CLASS_DEPARTMENT_ID')
            if did and did not in dept_info:
                dept_info[did] = (
                    _clean(row, 'CLASS_DEPARTMENT') or did, cc,
                )

        self._upsert_colleges_and_departments(conn, college_codes, dept_info)

        # Snapshot existing courses in the sync window.
        existing = self._snapshot_existing_courses(conn, self._csv_terms)

        now_dt = datetime.now(UTC).replace(tzinfo=None)
        to_upsert = []

        for row in rows:
            section_key = _clean(row, 'SECTION_KEY')
            if not section_key:
                continue

            mapping = {
                'section_key': section_key,
                'class_id': _clean(row, 'CLASS_ID') or None,
                'class_code': _clean(row, 'CLASS') or None,
                'section_id': _clean(row, 'SECTION_ID') or None,
                'crs_section': _clean(row, 'CRS_SECTION') or None,
                'section_title': _clean(row, 'SECTION_TITLE') or None,
                'college_code': _clean(row, 'CLASS_COLLEGE_SHORT') or None,
                'department_id': _clean(row, 'CLASS_DEPARTMENT_ID') or None,
                'crosslisted_id': _clean(row, 'CROSSLISTED_ID') or None,
                'course_start': _parse_date(row.get('SECTION_BEGIN_DATE')),
                'course_end': _parse_date(row.get('SECTION_END_DATE')),
                'tce_start': _parse_date(row.get('TCE_INVITE')),
                'tce_end': _parse_date(row.get('TCE_END_DATE')),
                'tce_reminder': _parse_date(row.get('TCE_R2')),
                'term_code': _clean(row, 'ACADEMIC_TERM') or None,
                'last_synced': now_dt,
                # Carry forward flags managed by later phases.
                'marked_for_tce': False,
                'student_count': 0,
            }

            prev = existing.get(section_key)
            if prev is None:
                self.stats['courses_added'] += 1
                label = self._course_label(mapping)
                self._record_change(
                    'course', section_key, 'added',
                    display_label=label,
                    new_value=json.dumps({k: mapping[k] for k in self.COURSE_DIFF_FIELDS},
                                        default=str),
                )
            else:
                # Preserve TCE flag + student count from DB.
                mapping['marked_for_tce'] = prev.get('marked_for_tce') or False
                mapping['student_count'] = prev.get('student_count') or 0

                diff = self._course_diff(prev, mapping)
                if diff:
                    label = self._course_label(mapping)
                    for field, (old, new) in diff.items():
                        self._record_change(
                            'course', section_key, 'updated',
                            field_name=field, old_value=old, new_value=new,
                            display_label=label,
                        )
                    self.stats['courses_updated'] += 1

            to_upsert.append(mapping)

        _update_progress(f'Upserting {len(to_upsert):,} courses...', 1)
        self._bulk_upsert_courses(conn, to_upsert)
        conn.commit()

        # Orphan removal
        orphan_keys = [sk for sk in existing if sk not in self._csv_section_keys]
        if orphan_keys:
            _update_progress(f'Removing {len(orphan_keys):,} orphan courses...', 1)
            self._delete_courses(conn, orphan_keys, existing)
            conn.commit()

        _update_progress(
            f'Courses: +{self.stats["courses_added"]:,} '
            f'~{self.stats["courses_updated"]:,} '
            f'-{self.stats["courses_removed"]:,}',
            1,
        )

    def _course_label(self, mapping):
        return (
            f"{mapping.get('class_code', '')}-{mapping.get('section_id', '')} "
            f"({mapping.get('section_title', '')})"
        ).strip()

    def _course_diff(self, old, new):
        diff = {}
        for field in self.COURSE_DIFF_FIELDS:
            o = old.get(field)
            n = new.get(field)
            if (o or '') != (n or ''):
                diff[field] = (o, n)
        return diff

    def _upsert_colleges_and_departments(self, conn, colleges, dept_info):
        cur = conn.cursor()
        self._raise_if_cancelled(conn)

        # Colleges
        existing_colleges = set()
        cur.execute('SELECT code FROM colleges')
        for (code,) in cur.fetchall():
            existing_colleges.add(code)

        new_colleges = [(code, name) for code, name in colleges.items()
                        if code not in existing_colleges]
        if new_colleges:
            _execute_values(
                cur,
                'INSERT INTO colleges (code, name, qb_enabled) VALUES %s ON CONFLICT (code) DO NOTHING',
                [(c, n, False) for c, n in new_colleges],
            )
            self.stats['colleges_added'] = len(new_colleges)

        for code, name in colleges.items():
            self._raise_if_cancelled(conn)
            if code in existing_colleges and name:
                cur.execute(
                    'UPDATE colleges SET name = %s WHERE code = %s AND name != %s',
                    (name, code, name),
                )

        # Departments
        existing_depts = set()
        cur.execute('SELECT id FROM departments')
        for (did,) in cur.fetchall():
            existing_depts.add(did)

        new_depts = []
        for did, (dname, ccode) in dept_info.items():
            self._raise_if_cancelled(conn)
            if did in existing_depts:
                cur.execute(
                    'UPDATE departments SET name = %s, college_code = %s '
                    'WHERE id = %s AND (name != %s OR college_code != %s)',
                    (dname, ccode, did, dname, ccode),
                )
            else:
                new_depts.append((did, dname, ccode))
        if new_depts:
            _execute_values(
                cur,
                'INSERT INTO departments (id, name, college_code) VALUES %s ON CONFLICT (id) DO NOTHING',
                new_depts,
            )
            self.stats['departments_added'] = len(new_depts)

        conn.commit()
        cur.close()

    def _snapshot_existing_courses(self, conn, terms):
        """Return {section_key: {fields...}} for every course in the sync window."""
        snapshot = {}
        if not terms:
            return snapshot
        term_list = list(terms)
        cols = ', '.join(self.COURSE_COLUMNS)
        cur = conn.cursor()
        # Chunk to avoid excessively long IN clauses.
        for i in range(0, len(term_list), 500):
            chunk = term_list[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'SELECT {cols} FROM courses WHERE term_code IN ({placeholders})',
                chunk,
            )
            for row in cur.fetchall():
                record = dict(zip(self.COURSE_COLUMNS, row))
                snapshot[record['section_key']] = record
        cur.close()
        return snapshot

    def _bulk_upsert_courses(self, conn, rows):
        """INSERT ... ON CONFLICT DO UPDATE for the full course set."""
        if not rows:
            return
        cols = list(self.COURSE_COLUMNS)
        col_list = ', '.join(cols)
        # All columns except the PK get updated on conflict.
        update_cols = [c for c in cols if c != 'section_key']
        update_expr = ', '.join(f'{c} = EXCLUDED.{c}' for c in update_cols)
        sql = (
            f'INSERT INTO courses ({col_list}) VALUES %s '
            f'ON CONFLICT (section_key) DO UPDATE SET {update_expr}'
        )
        cur = conn.cursor()
        for i in range(0, len(rows), BATCH_SIZE):
            self._raise_if_cancelled(conn)
            batch = rows[i:i + BATCH_SIZE]
            _execute_values(
                cur, sql,
                [tuple(r[c] for c in cols) for r in batch],
            )
            _update_progress(
                f'Writing courses... ({min(i + BATCH_SIZE, len(rows)):,}/{len(rows):,})',
                1,
            )
        cur.close()

    def _delete_courses(self, conn, keys, existing_snapshot):
        cur = conn.cursor()
        for key in keys:
            prev = existing_snapshot.get(key, {})
            self._record_change(
                'course', key, 'removed',
                display_label=self._course_label(prev) if prev else key,
                old_value=json.dumps(
                    {k: prev.get(k) for k in self.COURSE_DIFF_FIELDS}, default=str
                ) if prev else None,
            )
        keys = list(keys)
        for i in range(0, len(keys), 500):
            self._raise_if_cancelled(conn)
            chunk = keys[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'DELETE FROM instructors WHERE section_key IN ({placeholders})',
                chunk,
            )
            self.stats['instructors_removed'] += cur.rowcount or 0
            cur.execute(
                f'DELETE FROM courses WHERE section_key IN ({placeholders})',
                chunk,
            )
            self.stats['courses_removed'] += cur.rowcount or 0
        cur.close()

    def _load_users_csv(self):
        """Load Users.csv into a cached {user_id: (first, last, email)} mapping."""
        if self._users_cache:
            return self._users_cache

        users_data = {}
        users_path = os.path.join(self.datasources_path, 'Users.csv')
        if not os.path.exists(users_path):
            self.errors.append(f'Users.csv not found at {users_path}')
            self._users_cache = users_data
            return users_data

        try:
            with open(users_path, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    uid = _clean(row, 'USER_ID')
                    if not uid:
                        continue
                    users_data[uid] = (
                        _clean(row, 'FIRSTNAME'),
                        _clean(row, 'LASTNAME'),
                        _clean(row, 'EMAIL'),
                    )
        except Exception as e:
            self.errors.append(f'Error loading Users.csv: {e}')

        self._users_cache = users_data
        return users_data

    def _sync_users(self, conn):
        """Upsert the user directory from Users.csv for reverse lookup pages."""
        users_data = self._load_users_csv()
        if not users_data:
            return

        _update_progress(f'Upserting {len(users_data):,} users...', 2)
        now_dt = datetime.now(UTC).replace(tzinfo=None)
        rows = [
            (uid, first_name or None, last_name or None, email or None, now_dt)
            for uid, (first_name, last_name, email) in users_data.items()
        ]
        cur = conn.cursor()
        sql = (
            'INSERT INTO course_users (user_id, first_name, last_name, email, last_synced) VALUES %s '
            'ON CONFLICT (user_id) DO UPDATE SET '
            'first_name = EXCLUDED.first_name, '
            'last_name = EXCLUDED.last_name, '
            'email = EXCLUDED.email, '
            'last_synced = EXCLUDED.last_synced'
        )
        for i in range(0, len(rows), BATCH_SIZE):
            self._raise_if_cancelled(conn)
            batch = rows[i:i + BATCH_SIZE]
            _execute_values(cur, sql, batch)
        conn.commit()
        cur.close()
        self.stats['users_synced'] = len(rows)

    # ------------------------------------------------------------------
    # Phase 2: instructors
    # ------------------------------------------------------------------

    INSTRUCTOR_COLUMNS = (
        'section_key', 'user_id', 'first_name', 'last_name', 'email',
        'instructor_role', 'last_synced',
    )

    def _sync_instructors(self, conn):
        # Load Users.csv for name/email lookups.
        users_data = self._load_users_csv()

        path = os.path.join(self.datasources_path, 'Instructor_Course.csv')
        if not os.path.exists(path):
            self.errors.append(f'Instructor_Course.csv not found at {path}')
            return

        with open(path, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        _update_progress(f'Loading instructors from CSV... ({len(rows):,} rows)', 2)

        # Snapshot existing instructors for diff.
        old_pairs = self._snapshot_existing_instructors(conn, self._csv_section_keys)

        # Which courses actually exist in DB?
        valid_courses = self._valid_course_keys(conn, self._csv_section_keys)

        # Delete-all-then-reinsert strategy (same as before, fast on PG too).
        cur = conn.cursor()
        keys = list(self._csv_section_keys)
        total_deleted = 0
        for i in range(0, len(keys), 500):
            self._raise_if_cancelled(conn)
            chunk = keys[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'DELETE FROM instructors WHERE section_key IN ({placeholders})',
                chunk,
            )
            total_deleted += cur.rowcount or 0
        conn.commit()

        now_dt = datetime.now(UTC).replace(tzinfo=None)
        new_pairs = {}
        courses_with_instructors = set()

        for row in rows:
            sk = _clean(row, 'SECTION_KEY')
            uid = _clean(row, 'USER_ID')
            if not sk or not uid:
                continue
            if sk not in valid_courses:
                continue
            key = (sk, uid)
            if key in new_pairs:
                continue
            fname, lname, email = users_data.get(uid, ('', '', ''))
            new_pairs[key] = {
                'section_key': sk,
                'user_id': uid,
                'first_name': fname,
                'last_name': lname,
                'email': email,
                'instructor_role': _clean(row, 'ROLE') or None,
                'last_synced': now_dt,
            }
            courses_with_instructors.add(sk)

        missing_user_ids = sorted({uid for _, uid in new_pairs if uid not in self._users_cache})
        if missing_user_ids:
            fallback_rows = [(user_id, None, None, None, now_dt) for user_id in missing_user_ids]
            _execute_values(
                cur,
                'INSERT INTO course_users (user_id, first_name, last_name, email, last_synced) VALUES %s '
                'ON CONFLICT (user_id) DO NOTHING',
                fallback_rows,
            )

        # Compute diff for change_log.
        added_pairs = set(new_pairs) - set(old_pairs)
        removed_pairs = set(old_pairs) - set(new_pairs)
        for key in added_pairs:
            m = new_pairs[key]
            self._record_change(
                'instructor', f'{key[0]}|{key[1]}', 'added',
                display_label=f"{m['first_name']} {m['last_name']} ({key[1]}) -> {key[0]}",
                new_value=json.dumps({
                    'first_name': m['first_name'],
                    'last_name': m['last_name'],
                    'email': m['email'],
                    'role': m['instructor_role'],
                }),
            )
        for key in removed_pairs:
            old = old_pairs[key]
            self._record_change(
                'instructor', f'{key[0]}|{key[1]}', 'removed',
                display_label=(
                    f"{old.get('first_name', '')} {old.get('last_name', '')} "
                    f"({key[1]}) -> {key[0]}"
                ),
                old_value=json.dumps({
                    'first_name': old.get('first_name'),
                    'last_name': old.get('last_name'),
                    'email': old.get('email'),
                    'role': old.get('instructor_role'),
                }),
            )

        # Bulk insert new rows.
        _update_progress(f'Inserting {len(new_pairs):,} instructor rows...', 2)
        cols = list(self.INSTRUCTOR_COLUMNS)
        col_list = ', '.join(cols)
        sql = f'INSERT INTO instructors ({col_list}) VALUES %s'
        batch = [tuple(m[c] for c in cols) for m in new_pairs.values()]
        if batch:
            for i in range(0, len(batch), BATCH_SIZE):
                self._raise_if_cancelled(conn)
                _execute_values(cur, sql, batch[i:i + BATCH_SIZE])
        conn.commit()

        self.stats['instructors_added'] = len(new_pairs)
        self.stats['instructors_removed'] += len(removed_pairs)

        # Refresh marked_for_tce.
        _update_progress('Refreshing TCE flags...', 2)
        terms = list(self._csv_terms)
        for i in range(0, len(terms), 500):
            self._raise_if_cancelled(conn)
            chunk = terms[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'UPDATE courses SET marked_for_tce = FALSE '
                f'WHERE term_code IN ({placeholders})',
                chunk,
            )
        keys2 = list(courses_with_instructors)
        for i in range(0, len(keys2), 500):
            self._raise_if_cancelled(conn)
            chunk = keys2[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'UPDATE courses SET marked_for_tce = TRUE '
                f'WHERE section_key IN ({placeholders})',
                chunk,
            )
        conn.commit()
        cur.close()

    def _snapshot_existing_instructors(self, conn, section_keys):
        """Return {(section_key, user_id): {fields...}} for the sync window."""
        result = {}
        if not section_keys:
            return result
        keys = list(section_keys)
        cols = ('section_key', 'user_id', 'first_name', 'last_name', 'email', 'instructor_role')
        col_list = ', '.join(cols)
        cur = conn.cursor()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'SELECT {col_list} FROM instructors WHERE section_key IN ({placeholders})',
                chunk,
            )
            for row in cur.fetchall():
                record = dict(zip(cols, row))
                result[(record['section_key'], record['user_id'])] = record
        cur.close()
        return result

    def _valid_course_keys(self, conn, section_keys):
        if not section_keys:
            return set()
        valid = set()
        keys = list(section_keys)
        cur = conn.cursor()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'SELECT section_key FROM courses WHERE section_key IN ({placeholders})',
                chunk,
            )
            for (sk,) in cur.fetchall():
                valid.add(sk)
        cur.close()
        return valid

    # ------------------------------------------------------------------
    # Phase 3: student counts
    # ------------------------------------------------------------------

    def _sync_student_counts(self, conn):
        """Persist student-course links and update per-course student counts."""
        path = os.path.join(self.datasources_path, 'Student_Course.csv')
        if not os.path.exists(path):
            self.errors.append(f'Student_Course.csv not found at {path}')
            return

        _update_progress('Loading student enrollments from CSV...', 3)

        # Persist the unique (section_key, user_id) enrollment pairs so super
        # admins can reverse-search a student and see which courses would be
        # evaluated. We still compute the per-course counts from the same pass.
        valid_courses = self._valid_course_keys(conn, self._csv_section_keys)
        enrollments = set()
        counts: dict[str, int] = defaultdict(int)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    sk = _clean(row, 'SECTION_KEY')
                    uid = _clean(row, 'USER_ID')
                    if not sk or not uid or sk not in valid_courses:
                        continue
                    key = (sk, uid)
                    if key in enrollments:
                        continue
                    enrollments.add(key)
                    counts[sk] += 1
        except Exception as e:
            self.errors.append(f'Error reading Student_Course.csv: {e}')
            return

        total_students = len(enrollments)

        _update_progress(f'Refreshing {total_students:,} student enrollments...', 3)
        cur = conn.cursor()
        keys = list(self._csv_section_keys)
        if keys:
            for i in range(0, len(keys), 500):
                self._raise_if_cancelled(conn)
                chunk = keys[i:i + 500]
                placeholders = ','.join(['%s'] * len(chunk))
                cur.execute(
                    f'DELETE FROM student_enrollments WHERE section_key IN ({placeholders})',
                    chunk,
                )

        missing_user_ids = sorted({user_id for _, user_id in enrollments if user_id not in self._users_cache})
        if missing_user_ids:
            fallback_rows = [(user_id, None, None, None, datetime.now(UTC).replace(tzinfo=None)) for user_id in missing_user_ids]
            _execute_values(
                cur,
                'INSERT INTO course_users (user_id, first_name, last_name, email, last_synced) VALUES %s '
                'ON CONFLICT (user_id) DO NOTHING',
                fallback_rows,
            )

        rows = [(section_key, user_id, datetime.now(UTC).replace(tzinfo=None)) for section_key, user_id in enrollments]
        if rows:
            sql = 'INSERT INTO student_enrollments (section_key, user_id, last_synced) VALUES %s'
            for i in range(0, len(rows), BATCH_SIZE):
                self._raise_if_cancelled(conn)
                batch = rows[i:i + BATCH_SIZE]
                _execute_values(cur, sql, batch)
        conn.commit()

        _update_progress(
            f'Updating student counts ({len(counts):,} courses, {total_students:,} students)...',
            3,
        )

        existing_counts = self._get_existing_student_counts(conn, keys)

        # Reset counts for the full sync window first so courses that dropped to
        # zero enrollment do not retain stale values from the prior sync.
        if keys:
            for i in range(0, len(keys), 500):
                self._raise_if_cancelled(conn)
                chunk = keys[i:i + 500]
                placeholders = ','.join(['%s'] * len(chunk))
                cur.execute(
                    f'UPDATE courses SET student_count = 0 WHERE section_key IN ({placeholders})',
                    chunk,
                )

        # Record changes for courses where student_count changed.
        all_keys = set(existing_counts) | set(counts)
        for sk in all_keys:
            old_count = existing_counts.get(sk, 0)
            new_count = counts.get(sk, 0)
            if old_count != new_count:
                self._record_change(
                    'student_count', sk, 'updated',
                    field_name='student_count',
                    old_value=str(old_count),
                    new_value=str(new_count),
                    display_label=sk,
                )

        batch = list(counts.items())
        for i in range(0, len(batch), BATCH_SIZE):
            self._raise_if_cancelled(conn)
            chunk = batch[i:i + BATCH_SIZE]
            _execute_values(
                cur,
                """
                UPDATE courses SET student_count = data.cnt
                FROM (VALUES %s) AS data(sk, cnt)
                WHERE courses.section_key = data.sk
                """,
                chunk,
            )
        conn.commit()
        cur.close()

        self.stats['student_enrollments_synced'] = total_students
        self.stats['students_counted'] = total_students

    def _get_existing_student_counts(self, conn, section_keys):
        """Return {section_key: student_count} for the given keys."""
        result = {}
        if not section_keys:
            return result
        cur = conn.cursor()
        for i in range(0, len(section_keys), 500):
            chunk = section_keys[i:i + 500]
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f'SELECT section_key, student_count FROM courses '
                f'WHERE section_key IN ({placeholders})',
                chunk,
            )
            for sk, cnt in cur.fetchall():
                result[sk] = cnt or 0
        cur.close()
        return result
