"""
Course Data Sync Service
Imports course data from UKDIG-generated CSV files into the database

CSV Files Expected:
- Courses.csv: Course/section information
- Instructor_Course.csv: Instructor assignments (presence = marked for TCE in SAP)
- Student_Course.csv: Student enrollments (for counting)

Performance notes
-----------------
The naive ORM path (one row -> one INSERT/UPDATE via the session) is far too
slow for a ~500K row sync on SQLite: it spends most of its time in Python
object construction, identity-map maintenance, and per-row autoflushes.

This module is tuned for bulk throughput:

- SQLite PRAGMAs are set once at the start of ``sync_all`` to eliminate the
  per-transaction fsync overhead that otherwise dominates runtime.
- Existing rows are fetched in one pass (chunked ``IN``) instead of
  per-row lookups.
- Writes go through ``bulk_insert_mappings`` / ``bulk_update_mappings`` in
  fixed-size batches, with a single commit per logical phase.
- Instructor sync uses a "big-hammer" strategy: delete all instructors for
  the sync window, then bulk-insert the current CSV contents. This is
  dramatically faster than row-by-row diffing and always produces a
  consistent state that matches HANA.
- Student counts are grouped by count value so we run O(distinct counts)
  UPDATEs instead of O(sections) UPDATEs.
"""
import csv
import os
import threading
import time
from datetime import datetime
from collections import defaultdict
from sqlalchemy import event, text
from app.models import db
from app.models.course import Course, Instructor, College, Department, SyncLog

# Set once, on first sync. Installs a pool checkout listener so every SQLite
# connection the app pulls from the pool during a sync has the high-throughput
# PRAGMAs applied. Using only a ``connect`` hook is insufficient because
# already-pooled connections can still be checked out later with default
# SQLite settings.
_PRAGMA_LISTENER_INSTALLED = False

PROGRESS_UPDATE_INTERVAL = 5000
# Chunk size for bulk_*_mappings calls. SQLAlchemy will internally batch
# parameter binding, but keeping the Python-side lists this size limits
# transient memory use.
DB_BATCH_SIZE = 2000
# Chunk size for ``IN (...)`` clauses. SQLite imposes a default limit of
# 999 parameters per statement; we stay well below that.
IN_CHUNK_SIZE = 500


_PRAGMA_STATEMENTS = (
    'PRAGMA synchronous = OFF',
    'PRAGMA journal_mode = MEMORY',
    'PRAGMA temp_store = MEMORY',
    'PRAGMA cache_size = -200000',  # ~200MB page cache
)


def _apply_sqlite_pragmas():
    """Enable high-throughput PRAGMAs for the duration of a sync.

    SQLite PRAGMAs are *per connection*. Previous versions set them on a
    throwaway connection via ``db.engine.begin()``, which had no effect on
    the connection the ORM session actually used - so large bulk UPDATE
    batches still ran with ``synchronous=FULL`` and fsync'd per statement,
    causing the multi-hour stall on the real dataset.

    This version:
      1. Installs a pool-level ``checkout`` event listener so every SQLite
         connection, including ones already sitting in the pool, gets the
         PRAGMAs applied immediately before use. Idempotent across calls -
         we only install the listener once per process.
      2. Also applies the PRAGMAs directly to the current session's
         connection, so the already-checked-out connection is tuned
         immediately instead of waiting for the next checkout.

    These are safe for a batch import: we accept the (tiny) risk of losing
    the last transaction on power loss in exchange for a 10x+ speedup on
    large CSV->DB syncs. Other databases ignore this call.
    """
    global _PRAGMA_LISTENER_INSTALLED
    try:
        engine = db.engine
    except Exception:
        return
    if not engine.url.drivername.startswith('sqlite'):
        return

    if not _PRAGMA_LISTENER_INSTALLED:
        def _set_pragmas_on_checkout(dbapi_conn, connection_record, connection_proxy):
            try:
                cursor = dbapi_conn.cursor()
                for stmt in _PRAGMA_STATEMENTS:
                    cursor.execute(stmt)
                cursor.close()
            except Exception:
                pass
        try:
            event.listen(engine.pool, 'checkout', _set_pragmas_on_checkout)
            _PRAGMA_LISTENER_INSTALLED = True
        except Exception:
            pass

    # Apply to the session's current connection right now.
    try:
        for stmt in _PRAGMA_STATEMENTS:
            db.session.execute(text(stmt))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _chunked_bulk_insert(model, rows, progress_label=None, step=1):
    """bulk_insert_mappings with chunking and per-chunk commits.

    Committing per chunk keeps each SQLite transaction small (so the WAL
    doesn't grow unbounded on multi-hundred-thousand row syncs) and lets
    the progress panel actually move instead of appearing frozen.
    """
    if not rows:
        return
    total = len(rows)
    for i in range(0, total, DB_BATCH_SIZE):
        db.session.bulk_insert_mappings(model, rows[i:i + DB_BATCH_SIZE])
        db.session.commit()
        if progress_label:
            done = min(i + DB_BATCH_SIZE, total)
            _update_progress(f'{progress_label} ({done:,}/{total:,})', step)


def _chunked_bulk_update(model, rows, progress_label=None, step=1):
    """bulk_update_mappings with chunking and per-chunk commits. Each row
    must include the PK."""
    if not rows:
        return
    total = len(rows)
    for i in range(0, total, DB_BATCH_SIZE):
        db.session.bulk_update_mappings(model, rows[i:i + DB_BATCH_SIZE])
        db.session.commit()
        if progress_label:
            done = min(i + DB_BATCH_SIZE, total)
            _update_progress(f'{progress_label} ({done:,}/{total:,})', step)


def _chunked_delete_by_column(model, column, values):
    """Delete rows where ``column`` is in ``values``, chunked for SQLite."""
    if not values:
        return 0
    values = list(values)
    deleted = 0
    for i in range(0, len(values), IN_CHUNK_SIZE):
        stmt = model.__table__.delete().where(
            column.in_(values[i:i + IN_CHUNK_SIZE])
        )
        result = db.session.execute(stmt)
        deleted += result.rowcount or 0
    return deleted


def _chunked_update_column(model, key_column, keys, assignments):
    """UPDATE ``model`` SET ... WHERE key_column IN (keys), chunked."""
    if not keys:
        return 0
    keys = list(keys)
    updated = 0
    for i in range(0, len(keys), IN_CHUNK_SIZE):
        stmt = (
            model.__table__.update()
            .where(key_column.in_(keys[i:i + IN_CHUNK_SIZE]))
            .values(**assignments)
        )
        result = db.session.execute(stmt)
        updated += result.rowcount or 0
    return updated


# Global sync progress tracking
_sync_progress = {
    'running': False,
    'current_step': '',
    'step_number': 0,
    'total_steps': 4,
    'records_processed': 0,
    'started_at': None,
    'error': None
}
_sync_lock = threading.Lock()


def get_sync_progress():
    """Get current sync progress."""
    with _sync_lock:
        return _sync_progress.copy()


def _update_progress(step, step_number, records=0, error=None):
    """Update sync progress."""
    with _sync_lock:
        _sync_progress['current_step'] = step
        _sync_progress['step_number'] = step_number
        _sync_progress['records_processed'] += records
        if error:
            _sync_progress['error'] = error


def resolve_datasources_path(primary_path='./datasources'):
    """Resolve the datasources directory, handling common misspellings."""
    expected_files = {'Courses.csv', 'Instructor_Course.csv', 'Student_Course.csv', 'Users.csv'}
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


class CourseSyncService:
    """Service for syncing course data from CSV files to database."""

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
        }
        # Set of section_keys observed in the current Courses.csv. Used by
        # later sync steps to scope orphan deletion (so we never delete data
        # for terms outside the current sync window).
        self._csv_section_keys = set()
        # Set of term_codes observed in the current Courses.csv. Used to
        # decide which DB courses are eligible for removal.
        self._csv_terms = set()

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    def _parse_date(self, value):
        """Parse a date string from HANA in a few common formats.

        Returns ``None`` on empty/unrecognized input rather than raising.
        """
        if not value:
            return None
        value = str(value).strip()
        if not value:
            return None
        for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y-%m-%d %H:%M:%S', '%m/%d/%Y %H:%M:%S'):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None

    def _flush_progress(self, step, step_number, processed_rows, last_reported):
        """Increment progress counters only for rows since the last report."""
        delta = processed_rows - last_reported
        if delta > 0:
            _update_progress(f'{step} ({processed_rows:,} rows)', step_number, delta)
            return processed_rows
        return last_reported

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def sync_all(self):
        """Run full sync of all data."""
        global _sync_progress

        # Apply SQLite performance PRAGMAs before touching any data.
        # A no-op on MySQL/Postgres.
        _apply_sqlite_pragmas()

        # Initialize progress
        with _sync_lock:
            _sync_progress = {
                'running': True,
                'current_step': 'Initializing...',
                'step_number': 0,
                'total_steps': 4,
                'records_processed': 0,
                'started_at': datetime.utcnow().isoformat(),
                'error': None,
            }

        log = SyncLog(sync_type='full', status='running')
        db.session.add(log)
        db.session.commit()

        started = time.monotonic()
        try:
            _update_progress('Loading courses...', 1)
            self.sync_courses()

            _update_progress('Loading instructors...', 2)
            self.sync_instructors()

            _update_progress('Counting students...', 3)
            self.sync_student_counts()

            _update_progress('Finalizing...', 4)

            log.status = 'completed'
            log.completed_at = datetime.utcnow()
            log.records_processed = (
                self.stats['courses_added']
                + self.stats['courses_updated']
                + self.stats['instructors_added']
            )
            if self.errors:
                import json
                log.errors = json.dumps(self.errors[:50])

            db.session.commit()

            elapsed = time.monotonic() - started
            with _sync_lock:
                _sync_progress['running'] = False
                _sync_progress['current_step'] = f'Complete in {elapsed:.1f}s'
                _sync_progress['step_number'] = 4

            return {
                'success': True,
                'stats': self.stats,
                'errors': self.errors[:10],
                'elapsed_seconds': elapsed,
            }

        except Exception as e:
            log.status = 'failed'
            log.errors = str(e)
            db.session.commit()

            with _sync_lock:
                _sync_progress['running'] = False
                _sync_progress['error'] = str(e)

            raise

    # ------------------------------------------------------------------
    # Phase 1: courses
    # ------------------------------------------------------------------

    def sync_courses(self):
        """Import courses from Courses.csv.

        Strategy
        --------
        1. Stream the CSV once into memory as a list of dicts. At the same
           time collect the set of section_keys and term_codes.
        2. Make ONE pass over the Department table to prefetch departments
           referenced by the CSV; load all Colleges up front (small table).
        3. Load every existing ``Course.section_key`` whose ``term_code`` is
           in the sync window in chunked ``IN`` queries. This replaces the
           previous per-chunk existence checks, which cost ~N/chunk queries.
        4. Partition rows into ``to_insert`` and ``to_update`` lists and
           emit a single bulk insert + bulk update pass.
        5. Delete DB courses whose section_key is not in the CSV but whose
           term_code is in the sync window (orphan reconciliation).
        """
        filepath = os.path.join(self.datasources_path, 'Courses.csv')
        if not os.path.exists(filepath):
            self.errors.append(f"Courses.csv not found at {filepath}")
            return

        with open(filepath, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        # First pass: collect metadata so we can do bulk lookups just once.
        dept_ids = set()
        college_codes = set()
        for row in rows:
            section_key = (row.get('SECTION_KEY') or '').strip()
            if section_key:
                self._csv_section_keys.add(section_key)
            term_code = (row.get('ACADEMIC_TERM') or '').strip()
            if term_code:
                self._csv_terms.add(term_code)
            dept_id = (row.get('CLASS_DEPARTMENT_ID') or '').strip()
            if dept_id:
                dept_ids.add(dept_id)
            college_code = (row.get('CLASS_COLLEGE_SHORT') or '').strip()
            if college_code:
                college_codes.add(college_code)

        _update_progress(
            f'Loading courses from CSV... ({len(rows):,} rows)', 1
        )

        # Prefetch colleges + departments (both small).
        colleges = {c.code: c for c in College.query.all()}
        departments = {}
        for i in range(0, len(dept_ids), IN_CHUNK_SIZE):
            chunk = list(dept_ids)[i:i + IN_CHUNK_SIZE]
            for d in Department.query.filter(Department.id.in_(chunk)).all():
                departments[d.id] = d

        # Prefetch existing course keys for the sync window in one shot.
        # This is the critical speed-up vs the old per-chunk existence
        # queries (which performed O(N/batch) SELECTs across the whole CSV).
        existing_course_keys = set()
        term_list = list(self._csv_terms)
        for i in range(0, len(term_list), IN_CHUNK_SIZE):
            chunk = term_list[i:i + IN_CHUNK_SIZE]
            for (section_key,) in (
                db.session.query(Course.section_key)
                .filter(Course.term_code.in_(chunk))
                .all()
            ):
                existing_course_keys.add(section_key)

        # Also fetch any rows in the CSV whose term somehow didn't make it
        # into _csv_terms (shouldn't happen, but keeps us correct).
        leftover = self._csv_section_keys - existing_course_keys
        if leftover:
            leftover_list = list(leftover)
            for i in range(0, len(leftover_list), IN_CHUNK_SIZE):
                chunk = leftover_list[i:i + IN_CHUNK_SIZE]
                for (section_key,) in (
                    db.session.query(Course.section_key)
                    .filter(Course.section_key.in_(chunk))
                    .all()
                ):
                    existing_course_keys.add(section_key)

        # Create missing colleges / departments in a single session flush
        # before issuing bulk course writes, so FKs resolve correctly.
        new_colleges = []
        for code in college_codes:
            if code and code not in colleges:
                # We'll set the name below when we first see it in a row.
                new_colleges.append(code)

        to_insert = []
        to_update = []

        for row in rows:
            try:
                section_key = (row.get('SECTION_KEY') or '').strip()
                if not section_key:
                    continue

                college_code = (row.get('CLASS_COLLEGE_SHORT') or '').strip()
                college_name = (row.get('CLASS_COLLEGE') or '').strip()
                if college_code and college_code not in colleges:
                    college = College(
                        code=college_code,
                        name=college_name or college_code,
                    )
                    db.session.add(college)
                    colleges[college_code] = college
                    self.stats['colleges_added'] += 1

                dept_id = (row.get('CLASS_DEPARTMENT_ID') or '').strip()
                dept_name = (row.get('CLASS_DEPARTMENT') or '').strip()
                if dept_id:
                    dept = departments.get(dept_id)
                    if not dept:
                        dept = Department(
                            id=dept_id,
                            name=dept_name or dept_id,
                            college_code=college_code,
                        )
                        db.session.add(dept)
                        departments[dept_id] = dept
                        self.stats['departments_added'] += 1
                    else:
                        if dept_name and dept.name != dept_name:
                            dept.name = dept_name
                        if college_code and dept.college_code != college_code:
                            dept.college_code = college_code

                course_mapping = {
                    'section_key': section_key,
                    'class_id': (row.get('CLASS_ID') or '').strip(),
                    'class_code': (row.get('CLASS') or '').strip(),
                    'section_id': (row.get('SECTION_ID') or '').strip(),
                    'crs_section': (row.get('CRS_SECTION') or '').strip(),
                    'section_title': (row.get('SECTION_TITLE') or '').strip(),
                    'college_code': college_code,
                    'department_id': dept_id,
                    'crosslisted_id': (row.get('CROSSLISTED_ID') or '').strip() or None,
                    'course_start': self._parse_date(row.get('SECTION_BEGIN_DATE')),
                    'course_end': self._parse_date(row.get('SECTION_END_DATE')),
                    'tce_start': self._parse_date(row.get('TCE_INVITE')),
                    'tce_end': self._parse_date(row.get('TCE_END_DATE')),
                    'tce_reminder': self._parse_date(row.get('TCE_R2')),
                    'term_code': (row.get('ACADEMIC_TERM') or '').strip(),
                    'last_synced': datetime.utcnow(),
                    'marked_for_tce': False,
                }

                if section_key in existing_course_keys:
                    to_update.append(course_mapping)
                    self.stats['courses_updated'] += 1
                else:
                    to_insert.append(course_mapping)
                    self.stats['courses_added'] += 1
            except Exception as e:
                self.errors.append(
                    f"Course {row.get('SECTION_KEY', 'unknown')}: {e}"
                )

        # Flush newly added Colleges/Departments so their PKs exist before
        # we bulk-write the course rows that reference them.
        db.session.flush()

        _update_progress(
            f'Writing courses... ({len(to_insert):,} insert, {len(to_update):,} update)',
            1,
        )
        _chunked_bulk_insert(
            Course, to_insert,
            progress_label='Inserting courses', step=1,
        )
        _chunked_bulk_update(
            Course, to_update,
            progress_label='Updating courses', step=1,
        )
        db.session.commit()

        # ----- Orphan removal -----
        if self._csv_terms and self._csv_section_keys:
            _update_progress('Reconciling removed courses...', 1)
            # existing_course_keys was computed from the sync window terms,
            # so (window keys) - (csv keys) = orphans safely.
            orphan_keys = [
                key for key in existing_course_keys
                if key not in self._csv_section_keys
            ]
            if orphan_keys:
                # Delete instructor children first (the FK cascade would
                # also handle this but an explicit delete runs faster in
                # bulk mode than walking the ORM relationship).
                removed_instructors = _chunked_delete_by_column(
                    Instructor, Instructor.section_key, orphan_keys
                )
                self.stats['instructors_removed'] += removed_instructors
                removed_courses = _chunked_delete_by_column(
                    Course, Course.section_key, orphan_keys
                )
                self.stats['courses_removed'] += removed_courses
                db.session.commit()
            _update_progress(
                f'Courses reconciled ({self.stats["courses_removed"]:,} removed)',
                1,
            )

    # ------------------------------------------------------------------
    # Phase 2: instructors (big-hammer rewrite)
    # ------------------------------------------------------------------

    def sync_instructors(self):
        """Refresh instructor assignments from Instructor_Course.csv.

        Strategy (big-hammer)
        ---------------------
        Row-by-row diffing of ~7-15K instructor rows caused tens of
        thousands of individual UPDATE statements under SQLAlchemy's
        session and was the single biggest bottleneck of the old sync.

        Instead we:
          1. Chunked-DELETE every Instructor row whose course is in the
             current sync window (either because its section_key was in
             Courses.csv, or - to catch leftovers - its course's term_code
             is in the window).
          2. Bulk-INSERT the exact rows present in Instructor_Course.csv.
             Names come from Users.csv.
          3. Flip ``marked_for_tce`` in two chunked UPDATEs: set to False
             for every course in the window, then True for the courses
             that ended up with at least one instructor row.

        This deliberately trades a tiny amount of churn (delete+insert
        instead of update) for an enormous perf win, and it always leaves
        the database in a state that matches the CSV exactly.
        """
        # ----- Users.csv for name lookup -----
        users_data = {}
        users_filepath = os.path.join(self.datasources_path, 'Users.csv')
        if os.path.exists(users_filepath):
            try:
                with open(users_filepath, 'r', encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        user_id = (row.get('USER_ID') or '').strip()
                        if user_id:
                            users_data[user_id] = {
                                'first_name': (row.get('FIRSTNAME') or '').strip(),
                                'last_name': (row.get('LASTNAME') or '').strip(),
                                'email': (row.get('EMAIL') or '').strip(),
                            }
            except Exception as e:
                self.errors.append(f"Error loading Users.csv: {e}")

        filepath = os.path.join(self.datasources_path, 'Instructor_Course.csv')
        if not os.path.exists(filepath):
            self.errors.append(f"Instructor_Course.csv not found at {filepath}")
            return

        with open(filepath, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        _update_progress(
            f'Loading instructors from CSV... ({len(rows):,} rows)', 2
        )

        # Determine which section_keys actually exist in DB; instructor rows
        # referencing missing courses would violate the FK otherwise.
        valid_course_keys = set()
        csv_section_keys_list = list(self._csv_section_keys)
        for i in range(0, len(csv_section_keys_list), IN_CHUNK_SIZE):
            chunk = csv_section_keys_list[i:i + IN_CHUNK_SIZE]
            for (section_key,) in (
                db.session.query(Course.section_key)
                .filter(Course.section_key.in_(chunk))
                .all()
            ):
                valid_course_keys.add(section_key)

        # ----- 1. Nuke existing instructor rows for the sync window -----
        _update_progress('Clearing old instructor rows...', 2)

        # Primary hammer: delete by section_keys we just loaded from the CSV.
        # This is fast because section_key is indexed on instructors.
        removed = _chunked_delete_by_column(
            Instructor, Instructor.section_key, valid_course_keys
        )

        # Secondary hammer: catch any instructor rows whose course is in
        # the sync window but whose section_key wasn't in Courses.csv
        # (shouldn't happen post-phase-1, but keeps us correct).
        if self._csv_terms:
            term_list = list(self._csv_terms)
            window_courses = set()
            for i in range(0, len(term_list), IN_CHUNK_SIZE):
                chunk = term_list[i:i + IN_CHUNK_SIZE]
                for (section_key,) in (
                    db.session.query(Course.section_key)
                    .filter(Course.term_code.in_(chunk))
                    .all()
                ):
                    window_courses.add(section_key)
            stragglers = window_courses - valid_course_keys
            if stragglers:
                removed += _chunked_delete_by_column(
                    Instructor, Instructor.section_key, stragglers
                )
            # Remember the full window for the TCE-flag refresh below.
            all_window_courses = window_courses | valid_course_keys
        else:
            all_window_courses = valid_course_keys

        self.stats['instructors_removed'] += removed
        db.session.commit()

        # ----- 2. Bulk-insert the current CSV contents -----
        courses_with_instructors = set()
        insert_mappings = []
        seen_pairs = set()
        now = datetime.utcnow()

        for row in rows:
            section_key = (row.get('SECTION_KEY') or '').strip()
            user_id = (row.get('USER_ID') or '').strip()
            if not section_key or not user_id:
                continue
            if section_key not in valid_course_keys:
                # Course doesn't exist in DB - skip rather than FK-fail.
                continue
            pair = (section_key, user_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            courses_with_instructors.add(section_key)

            user_info = users_data.get(user_id, {})
            insert_mappings.append({
                'section_key': section_key,
                'user_id': user_id,
                'first_name': user_info.get('first_name', ''),
                'last_name': user_info.get('last_name', ''),
                'email': user_info.get('email', ''),
                'instructor_role': (row.get('ROLE') or '').strip() or None,
                'last_synced': now,
            })

        _update_progress(
            f'Inserting {len(insert_mappings):,} instructor rows...', 2
        )
        _chunked_bulk_insert(Instructor, insert_mappings)
        self.stats['instructors_added'] += len(insert_mappings)
        db.session.commit()

        # ----- 3. Refresh the marked_for_tce flag -----
        _update_progress('Refreshing TCE flags...', 2)
        if all_window_courses:
            _chunked_update_column(
                Course, Course.section_key,
                all_window_courses,
                {'marked_for_tce': False},
            )
        if courses_with_instructors:
            _chunked_update_column(
                Course, Course.section_key,
                courses_with_instructors,
                {'marked_for_tce': True},
            )
        db.session.commit()

    # ------------------------------------------------------------------
    # Phase 3: student counts
    # ------------------------------------------------------------------

    def sync_student_counts(self):
        """Count students per course from Student_Course.csv.

        Strategy
        --------
        Previous versions called ``bulk_update_mappings`` with one mapping
        per section_key, which SQLAlchemy emits as one UPDATE per row -
        50K+ statements on a real sync. Instead we:

          1. Reset ``student_count`` to 0 for every course in the sync
             window in one chunked UPDATE.
          2. Group section_keys by count value and emit ONE chunked
             UPDATE per distinct count (``UPDATE courses SET
             student_count=N WHERE section_key IN (...)``). Real data
             clusters heavily, so this ends up as maybe a few hundred
             statements instead of tens of thousands.
        """
        filepath = os.path.join(self.datasources_path, 'Student_Course.csv')
        if not os.path.exists(filepath):
            self.errors.append(f"Student_Course.csv not found at {filepath}")
            return

        student_counts = defaultdict(int)
        processed_rows = 0
        last_reported = 0

        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                processed_rows += 1
                if processed_rows % PROGRESS_UPDATE_INTERVAL == 0:
                    last_reported = self._flush_progress(
                        'Counting students from CSV...',
                        3,
                        processed_rows,
                        last_reported,
                    )
                section_key = (row.get('SECTION_KEY') or '').strip()
                if section_key:
                    student_counts[section_key] += 1

        self.stats['students_counted'] = processed_rows
        self._flush_progress(
            'Counting students from CSV...', 3, processed_rows, last_reported
        )

        # 1. Reset counts for the sync window so courses that lost all
        # their enrollments drop back to 0.
        if self._csv_terms:
            _update_progress('Resetting student counts...', 3)
            term_list = list(self._csv_terms)
            window_keys = set()
            for i in range(0, len(term_list), IN_CHUNK_SIZE):
                chunk = term_list[i:i + IN_CHUNK_SIZE]
                for (section_key,) in (
                    db.session.query(Course.section_key)
                    .filter(Course.term_code.in_(chunk))
                    .all()
                ):
                    window_keys.add(section_key)
            _chunked_update_column(
                Course, Course.section_key, window_keys,
                {'student_count': 0},
            )

        # 2. Group by count value -> one UPDATE per distinct count.
        _update_progress(
            f'Applying student counts ({len(student_counts):,} sections)...',
            3,
        )
        grouped = defaultdict(list)
        for section_key, count in student_counts.items():
            if count > 0:
                grouped[count].append(section_key)

        for count, keys in grouped.items():
            _chunked_update_column(
                Course, Course.section_key, keys,
                {'student_count': count},
            )

        db.session.commit()


# ----------------------------------------------------------------------
# Sample data helpers (used by run.py --generate-sample)
# ----------------------------------------------------------------------

def generate_sample_data():
    """Generate sample course data for testing."""

    colleges = [
        ('AS', 'Arts and Sciences'),
        ('EN', 'Engineering'),
        ('BE', 'Business & Economics'),
        ('ED', 'Education'),
        ('AG', 'Ag, Food and Environment'),
        ('MD', 'Medicine'),
        ('NU', 'Nursing'),
        ('PH', 'Public Health'),
    ]

    # NOTE: Using 'SAMPLE_' prefix for IDs to avoid conflicts with real data.
    departments = {
        'AS': [('SAMPLE_BIO', 'Biology'), ('SAMPLE_CHE', 'Chemistry'), ('SAMPLE_ENG', 'English'), ('SAMPLE_HIS', 'History')],
        'EN': [('SAMPLE_CS', 'Computer Science'), ('SAMPLE_EE', 'Electrical Engineering'), ('SAMPLE_ME', 'Mechanical Engineering')],
        'BE': [('SAMPLE_ACC', 'Accountancy'), ('SAMPLE_ECO', 'Economics'), ('SAMPLE_FIN', 'Finance')],
        'ED': [('SAMPLE_CI', 'Curriculum & Instruction'), ('SAMPLE_EL', 'Educational Leadership')],
        'AG': [('SAMPLE_ANI', 'Animal Science'), ('SAMPLE_PLT', 'Plant Science')],
        'MD': [('SAMPLE_IM', 'Internal Medicine'), ('SAMPLE_SUR', 'Surgery')],
        'NU': [('SAMPLE_NUR', 'Nursing')],
        'PH': [('SAMPLE_EPI', 'Epidemiology'), ('SAMPLE_BIO', 'Biostatistics')],
    }

    courses_data = []
    instructors_data = []
    students_data = []

    course_num = 0
    for college_code, college_name in colleges:
        for dept_id, dept_name in departments.get(college_code, []):
            for i in range(1, 5):
                course_num += 1
                prefix = dept_name[:3].upper()
                class_num = 100 + (i * 100)
                section_key = f"{prefix}{class_num}-001-2025010"

                course = {
                    'SECTION_KEY': section_key,
                    'CLASS_ID': f'CLS{course_num:05d}',
                    'CLASS': f'{prefix} {class_num}',
                    'SECTION_ID': '001',
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
                }
                courses_data.append(course)

                # 80% of courses have instructors (-> marked for TCE).
                if course_num % 5 != 0:
                    instructors_data.append({
                        'SECTION_KEY': section_key,
                        'USER_ID': f'inst{course_num:03d}',
                        'FIRST_NAME': f'Professor{course_num}',
                        'LAST_NAME': f'Smith{course_num}',
                        'EMAIL': f'inst{course_num:03d}@uky.edu',
                        'ROLE': 'Primary',
                    })

                    if course_num % 7 != 0:  # ~14% zero enrollment
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
            writer = csv.DictWriter(f, fieldnames=courses[0].keys())
            writer.writeheader()
            writer.writerows(courses)

    if instructors:
        with open(os.path.join(output_path, 'Instructor_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=instructors[0].keys())
            writer.writeheader()
            writer.writerows(instructors)

    if students:
        with open(os.path.join(output_path, 'Student_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=students[0].keys())
            writer.writeheader()
            writer.writerows(students)

    return {
        'courses': len(courses),
        'instructors': len(instructors),
        'students': len(students),
    }
