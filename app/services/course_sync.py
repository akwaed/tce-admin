"""
Course Data Sync Service  (FastCourseSync, rewritten April 2026)

This is a ground-up rewrite of the old SQLAlchemy-ORM based sync. The
original implementation used ``bulk_insert_mappings`` / ``bulk_update_mappings``
through the Flask-SQLAlchemy session, which in practice meant:

- PRAGMAs were set on the wrong connection and every batch still fsync'd.
- The ORM's identity map + autoflush walked huge Python data structures
  for every bulk call.
- A "warm" sync on ~29K courses and ~500K students stalled for 1-2 hours
  on real data (see production Sync History).

The new service bypasses SQLAlchemy entirely for writes. It talks to the
SQLite file via raw ``sqlite3`` using ``executemany`` and explicit
transactions, so:

- PRAGMAs stick to the connection that actually writes.
- No ORM identity map -> constant memory, predictable speed.
- Per-field diff tracking is computed in Python against snapshots of the
  existing rows and written as a bulk ``executemany`` into ``change_log``.
  That is cheap enough to replace the old daily-CSV-archive workflow.

Public API (stable)
-------------------
- ``CourseSyncService(datasources_path).sync_all()`` - runs the full sync,
  returns a dict with ``success``, ``stats``, ``elapsed_seconds``, ``errors``,
  ``sync_run_id``.
- ``get_sync_progress()`` - same shape as the old API; used by the
  verification/settings UI to render the progress banner.
- ``resolve_datasources_path(path)`` - same as before.
- ``generate_sample_data()``, ``write_sample_csvs()`` - still exported so
  ``run.py --generate-sample`` keeps working.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime

from app.models import db


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How many rows to send in a single ``executemany`` call. 5000 is well under
# SQLite's bind-param ceiling even for our widest Course row.
BATCH_SIZE = 5000

# How often to update the progress banner while streaming big CSVs.
PROGRESS_UPDATE_INTERVAL = 5000

# Change log cap: bulk deltas larger than this are summarised rather than
# written row-by-row, to keep storage bounded on first-time syncs.
CHANGE_LOG_CAP = 50000


# ---------------------------------------------------------------------------
# Global progress tracking (mirrors the old API)
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
    """Snapshot of the current sync progress. Thread-safe."""
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
            'started_at': datetime.utcnow().isoformat(),
            'error': None,
        })


def _finish_progress(message='Complete'):
    with _sync_lock:
        _sync_progress['running'] = False
        _sync_progress['current_step'] = message
        _sync_progress['step_number'] = _sync_progress['total_steps']


# ---------------------------------------------------------------------------
# Path resolver - kept identical to the old API so callers don't need to
# change.
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
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(value):
    """Parse a HANA date string; return ISO ``YYYY-MM-DD`` or None."""
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
    """Return a stripped string value for ``row[key]`` or ''."""
    return (row.get(key) or '').strip()


def _sqlite_path_from_engine():
    """Extract the filesystem path of the current Flask-SQLAlchemy DB."""
    url = db.engine.url
    if not url.drivername.startswith('sqlite'):
        raise RuntimeError(
            f'FastCourseSync requires SQLite; got {url.drivername}. '
            'Point DATABASE_URL at a sqlite:/// URL or extend the service.'
        )
    # url.database is already a filesystem path for sqlite
    return url.database


def _open_raw_connection(path):
    """Open a raw sqlite3 connection tuned for bulk writes.

    All PRAGMAs are applied on *this* connection, so unlike the previous
    implementation they actually take effect for the writes that follow.
    """
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    cur = conn.cursor()
    cur.execute('PRAGMA synchronous = OFF')
    cur.execute('PRAGMA journal_mode = MEMORY')
    cur.execute('PRAGMA temp_store = MEMORY')
    cur.execute('PRAGMA cache_size = -200000')      # ~200MB page cache
    cur.execute('PRAGMA locking_mode = EXCLUSIVE')  # we own the file during sync
    cur.execute('PRAGMA foreign_keys = OFF')        # we manage invariants manually
    cur.close()
    return conn


# ---------------------------------------------------------------------------
# Sync service
# ---------------------------------------------------------------------------

class CourseSyncService:
    """Fast, raw-sqlite3 CSV -> DB sync with per-field change tracking."""

    def __init__(self, datasources_path='./datasources'):
        self.datasources_path = resolve_datasources_path(datasources_path)
        self.errors = []
        self.stats = {
            'courses_added': 0,
            'courses_updated': 0,
            'courses_removed': 0,
            'instructors_added': 0,
            'instructors_removed': 0,
            'colleges_added': 0,
            'departments_added': 0,
            'students_counted': 0,
            'change_log_rows': 0,
        }
        self.sync_run_id = None
        self._csv_section_keys = set()
        self._csv_terms = set()
        # Change log buffer: list of tuples (entity_type, entity_key,
        # change_type, field_name, old_value, new_value, display_label).
        # Flushed in bulk at the end of each phase.
        self._change_buffer = []

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def sync_all(self):
        """Run the full sync. Returns a summary dict; raises on fatal error."""
        _reset_progress()

        db_path = _sqlite_path_from_engine()
        # Make sure any pending ORM state on the Flask session is flushed
        # *before* we grab the file — otherwise SQLite will lock us out.
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        db.session.remove()

        started = time.monotonic()
        started_at = datetime.utcnow()

        conn = _open_raw_connection(db_path)
        try:
            self._ensure_tables_exist(conn)
            self.sync_run_id = self._create_sync_run(conn, started_at)

            _update_progress('Loading courses...', 1)
            self._sync_courses(conn)

            _update_progress('Loading instructors...', 2)
            self._sync_instructors(conn)

            _update_progress('Counting students...', 3)
            self._sync_student_counts(conn)

            _update_progress('Flushing change log...', 4)
            self._flush_change_buffer(conn)

            elapsed = time.monotonic() - started
            _update_progress('Finalizing...', 5)
            self._finalize_sync_run(conn, elapsed, 'completed')

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
            try:
                self._finalize_sync_run(conn, elapsed, 'failed', str(exc))
            except Exception:
                pass
            _finish_progress(f'Failed: {exc}')
            _update_progress('Failed', 5, error=str(exc))
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Schema + run tracking
    # ------------------------------------------------------------------

    def _ensure_tables_exist(self, conn):
        """Create sync_runs and change_log if this is a pre-existing DB
        that hasn't been migrated yet. Idempotent."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                elapsed_seconds REAL DEFAULT 0,
                error_text TEXT,
                datasources_path VARCHAR(500),
                term_codes VARCHAR(500)
            );
            CREATE INDEX IF NOT EXISTS ix_sync_runs_started
                ON sync_runs(started_at);
            CREATE INDEX IF NOT EXISTS ix_sync_runs_status
                ON sync_runs(status);

            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
                created_at TIMESTAMP,
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
            CREATE INDEX IF NOT EXISTS ix_change_created
                ON change_log(created_at);
        """)

    def _create_sync_run(self, conn, started_at):
        cur = conn.execute(
            """
            INSERT INTO sync_runs (
                started_at, status, datasources_path
            ) VALUES (?, 'running', ?)
            """,
            (started_at.isoformat(sep=' '), self.datasources_path),
        )
        return cur.lastrowid

    def _finalize_sync_run(self, conn, elapsed, status, error_text=None):
        conn.execute(
            """
            UPDATE sync_runs
               SET completed_at = ?,
                   status = ?,
                   courses_added = ?,
                   courses_updated = ?,
                   courses_removed = ?,
                   instructors_added = ?,
                   instructors_removed = ?,
                   students_counted = ?,
                   change_log_rows = ?,
                   elapsed_seconds = ?,
                   error_text = ?,
                   term_codes = ?
             WHERE id = ?
            """,
            (
                datetime.utcnow().isoformat(sep=' '),
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

    # ------------------------------------------------------------------
    # Change log buffering
    # ------------------------------------------------------------------

    def _record_change(self, entity_type, entity_key, change_type,
                       field_name=None, old_value=None, new_value=None,
                       display_label=None):
        if len(self._change_buffer) >= CHANGE_LOG_CAP:
            return  # Summary-only mode once we hit the cap.
        self._change_buffer.append((
            entity_type, entity_key, change_type, field_name,
            None if old_value is None else str(old_value)[:4000],
            None if new_value is None else str(new_value)[:4000],
            display_label,
        ))

    def _flush_change_buffer(self, conn):
        if not self._change_buffer:
            return
        now = datetime.utcnow().isoformat(sep=' ')
        rows = [
            (self.sync_run_id, now, *row)
            for row in self._change_buffer
        ]
        conn.executemany(
            """
            INSERT INTO change_log (
                sync_run_id, created_at,
                entity_type, entity_key, change_type, field_name,
                old_value, new_value, display_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.stats['change_log_rows'] = len(self._change_buffer)
        self._change_buffer.clear()

    # ------------------------------------------------------------------
    # Phase 1: courses
    # ------------------------------------------------------------------

    # Columns we write to the ``courses`` table, in order.
    COURSE_COLUMNS = (
        'section_key', 'class_id', 'class_code', 'section_id', 'crs_section',
        'section_title', 'college_code', 'department_id', 'crosslisted_id',
        'course_start', 'course_end', 'tce_start', 'tce_end', 'tce_reminder',
        'marked_for_tce', 'student_count', 'term_code', 'last_synced',
    )

    # Fields we compare for per-row diffs.
    COURSE_DIFF_FIELDS = (
        'class_id', 'class_code', 'section_id', 'crs_section', 'section_title',
        'college_code', 'department_id', 'crosslisted_id',
        'course_start', 'course_end', 'tce_start', 'tce_end', 'tce_reminder',
        'term_code',
    )

    def _sync_courses(self, conn):
        filepath = os.path.join(self.datasources_path, 'Courses.csv')
        if not os.path.exists(filepath):
            self.errors.append(f"Courses.csv not found at {filepath}")
            return

        with open(filepath, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        _update_progress(
            f'Loading courses from CSV... ({len(rows):,} rows)', 1
        )

        # First pass: collect section_keys + terms + needed colleges/depts.
        dept_ids = set()
        college_codes = {}  # code -> name seen first
        dept_info = {}      # id -> (name, college_code)
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
            if did:
                dept_ids.add(did)
                if did not in dept_info:
                    dept_info[did] = (
                        _clean(row, 'CLASS_DEPARTMENT') or did, cc,
                    )

        # Upsert colleges + departments first so FK lookups resolve.
        self._upsert_colleges_and_departments(conn, college_codes, dept_info)

        # Snapshot existing courses inside the sync window (by term_code).
        existing = self._snapshot_existing_courses(conn, self._csv_terms)

        # Build insert and update batches.
        now_iso = datetime.utcnow().isoformat(sep=' ')
        to_insert = []
        to_update = []

        for row in rows:
            section_key = _clean(row, 'SECTION_KEY')
            if not section_key:
                continue

            mapping = {
                'section_key': section_key,
                'class_id': _clean(row, 'CLASS_ID'),
                'class_code': _clean(row, 'CLASS'),
                'section_id': _clean(row, 'SECTION_ID'),
                'crs_section': _clean(row, 'CRS_SECTION'),
                'section_title': _clean(row, 'SECTION_TITLE'),
                'college_code': _clean(row, 'CLASS_COLLEGE_SHORT'),
                'department_id': _clean(row, 'CLASS_DEPARTMENT_ID'),
                'crosslisted_id': _clean(row, 'CROSSLISTED_ID') or None,
                'course_start': _parse_date(row.get('SECTION_BEGIN_DATE')),
                'course_end': _parse_date(row.get('SECTION_END_DATE')),
                'tce_start': _parse_date(row.get('TCE_INVITE')),
                'tce_end': _parse_date(row.get('TCE_END_DATE')),
                'tce_reminder': _parse_date(row.get('TCE_R2')),
                'term_code': _clean(row, 'ACADEMIC_TERM'),
                'last_synced': now_iso,
                # marked_for_tce + student_count are managed by later phases
                # but we need to carry forward what's already in DB so the
                # INSERT OR REPLACE doesn't clobber them.
                'marked_for_tce': 0,
                'student_count': 0,
            }

            prev = existing.get(section_key)
            if prev is None:
                to_insert.append(mapping)
                self.stats['courses_added'] += 1
                label = self._course_label(mapping)
                self._record_change(
                    'course', section_key, 'added',
                    display_label=label,
                    new_value=json.dumps({
                        k: mapping[k] for k in self.COURSE_DIFF_FIELDS
                    }),
                )
            else:
                # Preserve TCE flag + student count across the update —
                # they're owned by phases 2 and 3, not phase 1.
                mapping['marked_for_tce'] = prev.get('marked_for_tce', 0) or 0
                mapping['student_count'] = prev.get('student_count', 0) or 0

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
                to_update.append(mapping)

        # Commit writes.
        conn.execute('BEGIN')
        if to_insert:
            self._bulk_upsert_courses(conn, to_insert)
        if to_update:
            self._bulk_upsert_courses(conn, to_update)
        conn.execute('COMMIT')

        # Orphan removal: any DB course whose term is in the sync window but
        # whose section_key wasn't in the CSV gets deleted.
        orphan_keys = [
            sk for sk in existing.keys() if sk not in self._csv_section_keys
        ]
        if orphan_keys:
            _update_progress(
                f'Removing {len(orphan_keys):,} orphan courses...', 1
            )
            self._delete_courses(conn, orphan_keys, existing)

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
            # Normalize None vs '' so we don't log spurious changes.
            if (o or '') != (n or ''):
                diff[field] = (o, n)
        return diff

    def _upsert_colleges_and_departments(self, conn, colleges, dept_info):
        # Read existing to compute stats and keep qb_enabled intact.
        existing_colleges = {
            row[0] for row in conn.execute('SELECT code FROM colleges')
        }
        new_colleges = [
            (code, name) for code, name in colleges.items()
            if code not in existing_colleges
        ]
        if new_colleges:
            conn.executemany(
                'INSERT INTO colleges (code, name, qb_enabled) VALUES (?, ?, 0)',
                new_colleges,
            )
            self.stats['colleges_added'] = len(new_colleges)

        # Also update existing college names if they changed.
        for code, name in colleges.items():
            if code in existing_colleges and name:
                conn.execute(
                    'UPDATE colleges SET name = ? WHERE code = ? AND name != ?',
                    (name, code, name),
                )

        existing_depts = {
            row[0] for row in conn.execute('SELECT id FROM departments')
        }
        new_depts = []
        for did, (dname, ccode) in dept_info.items():
            if did in existing_depts:
                conn.execute(
                    'UPDATE departments SET name = ?, college_code = ? '
                    'WHERE id = ? AND (name != ? OR college_code != ?)',
                    (dname, ccode, did, dname, ccode),
                )
            else:
                new_depts.append((did, dname, ccode))
        if new_depts:
            conn.executemany(
                'INSERT INTO departments (id, name, college_code) VALUES (?, ?, ?)',
                new_depts,
            )
            self.stats['departments_added'] = len(new_depts)

    def _snapshot_existing_courses(self, conn, terms):
        """Return {section_key: {fields...}} for every course in the sync window."""
        snapshot = {}
        if not terms:
            return snapshot
        term_list = list(terms)
        cols = ', '.join(self.COURSE_COLUMNS)
        for i in range(0, len(term_list), 500):
            chunk = term_list[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            q = f"SELECT {cols} FROM courses WHERE term_code IN ({placeholders})"
            for row in conn.execute(q, chunk):
                record = dict(zip(self.COURSE_COLUMNS, row))
                snapshot[record['section_key']] = record
        return snapshot

    def _bulk_upsert_courses(self, conn, rows):
        cols = ', '.join(self.COURSE_COLUMNS)
        placeholders = ', '.join('?' for _ in self.COURSE_COLUMNS)
        sql = f'INSERT OR REPLACE INTO courses ({cols}) VALUES ({placeholders})'
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            conn.executemany(
                sql,
                [tuple(r[c] for c in self.COURSE_COLUMNS) for r in batch],
            )
            _update_progress(
                f'Writing courses... ({min(i + BATCH_SIZE, len(rows)):,}/{len(rows):,})',
                1,
            )

    def _delete_courses(self, conn, keys, existing_snapshot):
        """Chunked delete of courses + their instructor rows, with change log."""
        for key in keys:
            prev = existing_snapshot.get(key, {})
            self._record_change(
                'course', key, 'removed',
                display_label=self._course_label(prev) if prev else key,
                old_value=json.dumps({
                    k: prev.get(k) for k in self.COURSE_DIFF_FIELDS
                }) if prev else None,
            )
        keys = list(keys)
        conn.execute('BEGIN')
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            cur = conn.execute(
                f'DELETE FROM instructors WHERE section_key IN ({placeholders})',
                chunk,
            )
            self.stats['instructors_removed'] += cur.rowcount or 0
            cur = conn.execute(
                f'DELETE FROM courses WHERE section_key IN ({placeholders})',
                chunk,
            )
            self.stats['courses_removed'] += cur.rowcount or 0
        conn.execute('COMMIT')

    # ------------------------------------------------------------------
    # Phase 2: instructors (big-hammer)
    # ------------------------------------------------------------------

    INSTRUCTOR_COLUMNS = (
        'section_key', 'user_id', 'first_name', 'last_name', 'email',
        'instructor_role', 'last_synced',
    )

    def _sync_instructors(self, conn):
        # Load Users.csv for name/email lookups.
        users_data = {}
        users_path = os.path.join(self.datasources_path, 'Users.csv')
        if os.path.exists(users_path):
            try:
                with open(users_path, 'r', encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        uid = _clean(row, 'USER_ID')
                        if uid:
                            users_data[uid] = (
                                _clean(row, 'FIRSTNAME'),
                                _clean(row, 'LASTNAME'),
                                _clean(row, 'EMAIL'),
                            )
            except Exception as e:
                self.errors.append(f"Error loading Users.csv: {e}")

        path = os.path.join(self.datasources_path, 'Instructor_Course.csv')
        if not os.path.exists(path):
            self.errors.append(f"Instructor_Course.csv not found at {path}")
            return

        with open(path, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        _update_progress(
            f'Loading instructors from CSV... ({len(rows):,} rows)', 2
        )

        # Snapshot existing instructors for the sync window so we can diff.
        old_pairs = self._snapshot_existing_instructors(
            conn, self._csv_section_keys
        )

        # Which courses actually exist in DB?
        valid_courses = self._valid_course_keys(conn, self._csv_section_keys)

        # 1. Delete everything in the sync window.
        _update_progress('Clearing old instructor rows...', 2)
        conn.execute('BEGIN')
        keys = list(self._csv_section_keys)
        total_deleted = 0
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            cur = conn.execute(
                f'DELETE FROM instructors WHERE section_key IN ({placeholders})',
                chunk,
            )
            total_deleted += cur.rowcount or 0
        conn.execute('COMMIT')

        # 2. Build the new instructor rows.
        now_iso = datetime.utcnow().isoformat(sep=' ')
        new_pairs = {}  # (section_key, user_id) -> mapping
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
                'last_synced': now_iso,
            }
            courses_with_instructors.add(sk)

        # 3. Compute per-row diff for change_log.
        added_pairs = set(new_pairs) - set(old_pairs)
        removed_pairs = set(old_pairs) - set(new_pairs)
        for key in added_pairs:
            m = new_pairs[key]
            self._record_change(
                'instructor', f"{key[0]}|{key[1]}", 'added',
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
                'instructor', f"{key[0]}|{key[1]}", 'removed',
                display_label=f"{old.get('first_name', '')} {old.get('last_name', '')} ({key[1]}) -> {key[0]}",
                old_value=json.dumps({
                    'first_name': old.get('first_name'),
                    'last_name': old.get('last_name'),
                    'email': old.get('email'),
                    'role': old.get('instructor_role'),
                }),
            )

        # 4. Bulk insert new rows.
        _update_progress(
            f'Inserting {len(new_pairs):,} instructor rows...', 2
        )
        cols = ', '.join(self.INSTRUCTOR_COLUMNS)
        placeholders = ', '.join('?' for _ in self.INSTRUCTOR_COLUMNS)
        sql = f'INSERT INTO instructors ({cols}) VALUES ({placeholders})'
        batch = []
        for mapping in new_pairs.values():
            batch.append(tuple(mapping[c] for c in self.INSTRUCTOR_COLUMNS))
            if len(batch) >= BATCH_SIZE:
                conn.execute('BEGIN')
                conn.executemany(sql, batch)
                conn.execute('COMMIT')
                batch.clear()
        if batch:
            conn.execute('BEGIN')
            conn.executemany(sql, batch)
            conn.execute('COMMIT')

        self.stats['instructors_added'] = len(new_pairs)
        # Deleted rows that *also* reappear are not "removed" — net removed
        # is old - new.
        self.stats['instructors_removed'] += max(total_deleted - len(new_pairs), 0) + len(removed_pairs - added_pairs) * 0

        # 5. Refresh marked_for_tce.
        _update_progress('Refreshing TCE flags...', 2)
        conn.execute('BEGIN')
        terms = list(self._csv_terms)
        for i in range(0, len(terms), 500):
            chunk = terms[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            conn.execute(
                f'UPDATE courses SET marked_for_tce = 0 '
                f'WHERE term_code IN ({placeholders})',
                chunk,
            )
        keys = list(courses_with_instructors)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            conn.execute(
                f'UPDATE courses SET marked_for_tce = 1 '
                f'WHERE section_key IN ({placeholders})',
                chunk,
            )
        conn.execute('COMMIT')

    def _valid_course_keys(self, conn, section_keys):
        if not section_keys:
            return set()
        valid = set()
        keys = list(section_keys)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            q = f'SELECT section_key FROM courses WHERE section_key IN ({placeholders})'
            for (sk,) in conn.execute(q, chunk):
                valid.add(sk)
        return valid

    def _snapshot_existing_instructors(self, conn, section_keys):
        """Return {(section_key, user_id): {fields}} for the sync window."""
        snapshot = {}
        if not section_keys:
            return snapshot
        keys = list(section_keys)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            q = (
                'SELECT section_key, user_id, first_name, last_name, email, '
                'instructor_role FROM instructors '
                f'WHERE section_key IN ({placeholders})'
            )
            for row in conn.execute(q, chunk):
                snapshot[(row[0], row[1])] = {
                    'first_name': row[2],
                    'last_name': row[3],
                    'email': row[4],
                    'instructor_role': row[5],
                }
        return snapshot

    # ------------------------------------------------------------------
    # Phase 3: student counts
    # ------------------------------------------------------------------

    def _sync_student_counts(self, conn):
        path = os.path.join(self.datasources_path, 'Student_Course.csv')
        if not os.path.exists(path):
            self.errors.append(f"Student_Course.csv not found at {path}")
            return

        counts = defaultdict(int)
        processed = 0
        with open(path, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                processed += 1
                if processed % PROGRESS_UPDATE_INTERVAL == 0:
                    _update_progress(
                        f'Counting students... ({processed:,} rows)', 3
                    )
                sk = _clean(row, 'SECTION_KEY')
                if sk:
                    counts[sk] += 1

        self.stats['students_counted'] = processed

        # Snapshot old counts for the sync window to diff.
        old_counts = {}
        terms = list(self._csv_terms)
        conn_cur = conn.cursor()
        for i in range(0, len(terms), 500):
            chunk = terms[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            for sk, cnt in conn_cur.execute(
                f'SELECT section_key, student_count FROM courses '
                f'WHERE term_code IN ({placeholders})',
                chunk,
            ):
                old_counts[sk] = cnt or 0
        conn_cur.close()

        # Log deltas.
        for sk in set(old_counts) | set(counts):
            old = old_counts.get(sk, 0)
            new = counts.get(sk, 0)
            if old != new:
                self._record_change(
                    'student_count', sk, 'updated',
                    field_name='student_count',
                    old_value=str(old), new_value=str(new),
                    display_label=sk,
                )

        # Apply in bulk: reset window to 0, then group-by-count UPDATEs.
        _update_progress('Applying student counts...', 3)
        conn.execute('BEGIN')
        for i in range(0, len(terms), 500):
            chunk = terms[i:i + 500]
            placeholders = ','.join('?' for _ in chunk)
            conn.execute(
                f'UPDATE courses SET student_count = 0 '
                f'WHERE term_code IN ({placeholders})',
                chunk,
            )

        grouped = defaultdict(list)
        for sk, cnt in counts.items():
            if cnt > 0:
                grouped[cnt].append(sk)

        for cnt, keys in grouped.items():
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ','.join('?' for _ in chunk)
                conn.execute(
                    f'UPDATE courses SET student_count = ? '
                    f'WHERE section_key IN ({placeholders})',
                    (cnt, *chunk),
                )
        conn.execute('COMMIT')


# ---------------------------------------------------------------------------
# Sample data helpers (kept for run.py --generate-sample)
# ---------------------------------------------------------------------------

def generate_sample_data():
    """Generate sample course data for testing."""
    colleges = [
        ('AS', 'Arts and Sciences'), ('EN', 'Engineering'),
        ('BE', 'Business & Economics'), ('ED', 'Education'),
        ('AG', 'Ag, Food and Environment'), ('MD', 'Medicine'),
        ('NU', 'Nursing'), ('PH', 'Public Health'),
    ]
    departments = {
        'AS': [('SAMPLE_BIO', 'Biology'), ('SAMPLE_CHE', 'Chemistry'),
               ('SAMPLE_ENG', 'English'), ('SAMPLE_HIS', 'History')],
        'EN': [('SAMPLE_CS', 'Computer Science'),
               ('SAMPLE_EE', 'Electrical Engineering'),
               ('SAMPLE_ME', 'Mechanical Engineering')],
        'BE': [('SAMPLE_ACC', 'Accountancy'), ('SAMPLE_ECO', 'Economics'),
               ('SAMPLE_FIN', 'Finance')],
        'ED': [('SAMPLE_CI', 'Curriculum & Instruction'),
               ('SAMPLE_EL', 'Educational Leadership')],
        'AG': [('SAMPLE_ANI', 'Animal Science'), ('SAMPLE_PLT', 'Plant Science')],
        'MD': [('SAMPLE_IM', 'Internal Medicine'), ('SAMPLE_SUR', 'Surgery')],
        'NU': [('SAMPLE_NUR', 'Nursing')],
        'PH': [('SAMPLE_EPI', 'Epidemiology'), ('SAMPLE_BIO', 'Biostatistics')],
    }
    courses_data, instructors_data, students_data = [], [], []
    course_num = 0
    for college_code, college_name in colleges:
        for dept_id, dept_name in departments.get(college_code, []):
            for i in range(1, 5):
                course_num += 1
                prefix = dept_name[:3].upper()
                class_num = 100 + (i * 100)
                section_key = f"{prefix}{class_num}-001-2025010"
                courses_data.append({
                    'SECTION_KEY': section_key,
                    'CLASS_ID': f'CLS{course_num:05d}',
                    'CLASS': f'{prefix} {class_num}',
                    'SECTION_ID': '001',
                    'CRS_SECTION': f'{prefix}{class_num}-001',
                    'SECTION_TITLE': f'Introduction to {dept_name} {i}',
                    'CLASS_COLLEGE_SHORT': college_code,
                    'CLASS_COLLEGE': college_name,
                    'CLASS_DEPARTMENT_ID': dept_id,
                    'CLASS_DEPARTMENT': dept_name,
                    'CROSSLISTED_ID': '',
                    'SECTION_BEGIN_DATE': '2025-01-13',
                    'SECTION_END_DATE': '2025-05-02',
                    'TCE_INVITE': '2025-04-14',
                    'TCE_END_DATE': '2025-04-28',
                    'TCE_R2': '2025-04-21',
                    'ACADEMIC_TERM': '2025010',
                })
                if course_num % 5 != 0:
                    instructors_data.append({
                        'SECTION_KEY': section_key,
                        'USER_ID': f'inst{course_num:03d}',
                        'FIRSTNAME': f'Professor{course_num}',
                        'LASTNAME': f'Smith{course_num}',
                        'EMAIL': f'inst{course_num:03d}@uky.edu',
                        'ROLE': 'Primary',
                    })
                    if course_num % 7 != 0:
                        num_students = 15 + (course_num % 35)
                        for s in range(num_students):
                            students_data.append({
                                'SECTION_KEY': section_key,
                                'USER_ID': f'stu{course_num:03d}{s:03d}',
                            })
    return courses_data, instructors_data, students_data


def write_sample_csvs(output_path='./datasources'):
    """Write sample data to CSV files."""
    os.makedirs(output_path, exist_ok=True)
    courses, instructors, students = generate_sample_data()
    if courses:
        with open(os.path.join(output_path, 'Courses.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=courses[0].keys())
            w.writeheader(); w.writerows(courses)
    if instructors:
        with open(os.path.join(output_path, 'Instructor_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=instructors[0].keys())
            w.writeheader(); w.writerows(instructors)
    if students:
        with open(os.path.join(output_path, 'Student_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=students[0].keys())
            w.writeheader(); w.writerows(students)
    return {
        'courses': len(courses),
        'instructors': len(instructors),
        'students': len(students),
    }
