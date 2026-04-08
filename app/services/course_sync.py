"""
Course Data Sync Service
Imports course data from UKDIG-generated CSV files into the database

CSV Files Expected:
- Courses.csv: Course/section information
- Instructor_Course.csv: Instructor assignments (presence = marked for TCE in SAP)
- Student_Course.csv: Student enrollments (for counting)
"""
import csv
import os
import threading
import time
from datetime import datetime
from collections import defaultdict
from app.models import db
from app.models.course import Course, Instructor, College, Department, SyncLog

PROGRESS_UPDATE_INTERVAL = 5000
DB_BATCH_SIZE = 1000

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
    """Service for syncing course data from CSV files to database"""

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
            'students_counted': 0
        }
        # Set of section_keys observed in the current Courses.csv. Used by
        # later sync steps to scope orphan deletion (so we never delete data
        # for terms outside the current sync window).
        self._csv_section_keys = set()
        # Set of term_codes observed in the current Courses.csv. Used to
        # decide which DB courses are eligible for removal.
        self._csv_terms = set()

    def _iter_chunks(self, items, size=DB_BATCH_SIZE):
        """Yield fixed-size chunks from an iterable."""
        batch = []
        for item in items:
            batch.append(item)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _flush_progress(self, step, step_number, processed_rows, last_reported):
        """Increment progress counters only for the rows since the last report."""
        delta = processed_rows - last_reported
        if delta > 0:
            _update_progress(f'{step} ({processed_rows:,} rows)', step_number, delta)
            return processed_rows
        return last_reported

    def _delete_courses_by_keys(self, section_keys):
        """Delete courses in small batches so large sync windows stay tractable."""
        if not section_keys:
            return

        for course in Course.query.filter(Course.section_key.in_(section_keys)).all():
            db.session.delete(course)
            self.stats['courses_removed'] += 1
        db.session.commit()

    def _delete_instructors_by_ids(self, instructor_ids):
        """Delete instructors in small batches."""
        if not instructor_ids:
            return

        for instructor in Instructor.query.filter(Instructor.id.in_(instructor_ids)).all():
            db.session.delete(instructor)
            self.stats['instructors_removed'] += 1
        db.session.commit()

    def _prefetch_by_primary_key(self, model, key_column, values):
        """Load existing rows keyed by primary/business key in manageable chunks."""
        existing = {}
        if not values:
            return existing

        for chunk in self._iter_chunks(values):
            rows = model.query.filter(key_column.in_(chunk)).all()
            for row in rows:
                existing[getattr(row, key_column.key)] = row
        return existing
    
    def sync_all(self):
        """Run full sync of all data"""
        global _sync_progress

        # Initialize progress
        with _sync_lock:
            _sync_progress = {
                'running': True,
                'current_step': 'Initializing...',
                'step_number': 0,
                'total_steps': 4,
                'records_processed': 0,
                'started_at': datetime.utcnow().isoformat(),
                'error': None
            }

        log = SyncLog(sync_type='full', status='running')
        db.session.add(log)
        db.session.commit()

        try:
            # 1. Load courses first (creates colleges/departments)
            _update_progress('Loading courses...', 1)
            self.sync_courses()

            # 2. Load instructor assignments (determines TCE marking)
            _update_progress('Loading instructors...', 2)
            self.sync_instructors()

            # 3. Count students per course
            _update_progress('Counting students...', 3)
            self.sync_student_counts()

            # 4. Finalize
            _update_progress('Finalizing...', 4)

            # Update sync log
            log.status = 'completed'
            log.completed_at = datetime.utcnow()
            log.records_processed = (
                self.stats['courses_added'] +
                self.stats['courses_updated'] +
                self.stats['instructors_added']
            )
            if self.errors:
                import json
                log.errors = json.dumps(self.errors[:50])  # Keep first 50 errors

            db.session.commit()

            # Mark as complete
            with _sync_lock:
                _sync_progress['running'] = False
                _sync_progress['current_step'] = 'Complete'
                _sync_progress['step_number'] = 4

            return {
                'success': True,
                'stats': self.stats,
                'errors': self.errors[:10]
            }

        except Exception as e:
            log.status = 'failed'
            log.errors = str(e)
            db.session.commit()

            with _sync_lock:
                _sync_progress['running'] = False
                _sync_progress['error'] = str(e)

            raise
    
    def sync_courses(self):
        """Import courses from Courses.csv.

        Also removes courses from the database whose ``term_code`` is in the
        current sync window but whose ``section_key`` no longer appears in
        the CSV. The Course/Instructor relationship cascades on delete, so
        orphaned instructor rows are cleaned up automatically.
        """
        filepath = os.path.join(self.datasources_path, 'Courses.csv')

        if not os.path.exists(filepath):
            self.errors.append(f"Courses.csv not found at {filepath}")
            return

        with open(filepath, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        section_keys = set()
        dept_ids = set()
        for row in rows:
            section_key = row.get('SECTION_KEY', '').strip()
            if section_key:
                section_keys.add(section_key)
                self._csv_section_keys.add(section_key)

            term_code = row.get('ACADEMIC_TERM', '').strip()
            if term_code:
                self._csv_terms.add(term_code)

            dept_id = row.get('CLASS_DEPARTMENT_ID', '').strip()
            if dept_id:
                dept_ids.add(dept_id)

        colleges = {college.code: college for college in College.query.all()}
        departments = self._prefetch_by_primary_key(Department, Department.id, dept_ids)
        courses = self._prefetch_by_primary_key(Course, Course.section_key, section_keys)

        processed_rows = 0
        last_reported = 0
        with db.session.no_autoflush:
            for row in rows:
                processed_rows += 1
                if processed_rows % PROGRESS_UPDATE_INTERVAL == 0:
                    last_reported = self._flush_progress(
                        'Loading courses from CSV...',
                        1,
                        processed_rows,
                        last_reported
                    )
                try:
                    section_key = row.get('SECTION_KEY', '').strip()
                    if not section_key:
                        continue

                    college_code = row.get('CLASS_COLLEGE_SHORT', '').strip()
                    college_name = row.get('CLASS_COLLEGE', '').strip()
                    if college_code and college_code not in colleges:
                        college = College(code=college_code, name=college_name or college_code)
                        db.session.add(college)
                        colleges[college_code] = college
                        self.stats['colleges_added'] += 1

                    dept_id = row.get('CLASS_DEPARTMENT_ID', '').strip()
                    dept_name = row.get('CLASS_DEPARTMENT', '').strip()
                    if dept_id:
                        dept = departments.get(dept_id)
                        if not dept:
                            dept = Department(
                                id=dept_id,
                                name=dept_name or dept_id,
                                college_code=college_code
                            )
                            db.session.add(dept)
                            departments[dept_id] = dept
                            self.stats['departments_added'] += 1
                        else:
                            if dept_name and dept.name != dept_name:
                                dept.name = dept_name
                            if college_code and dept.college_code != college_code:
                                dept.college_code = college_code

                    course = courses.get(section_key)
                    is_new = course is None
                    if is_new:
                        course = Course(section_key=section_key)
                        db.session.add(course)
                        courses[section_key] = course

                    course.class_id = row.get('CLASS_ID', '').strip()
                    course.class_code = row.get('CLASS', '').strip()
                    course.section_id = row.get('SECTION_ID', '').strip()
                    course.crs_section = row.get('CRS_SECTION', '').strip()
                    course.section_title = row.get('SECTION_TITLE', '').strip()
                    course.college_code = college_code
                    course.department_id = dept_id
                    course.crosslisted_id = row.get('CROSSLISTED_ID', '').strip() or None
                    course.course_start = self._parse_date(row.get('SECTION_BEGIN_DATE'))
                    course.course_end = self._parse_date(row.get('SECTION_END_DATE'))
                    course.tce_start = self._parse_date(row.get('TCE_INVITE'))
                    course.tce_end = self._parse_date(row.get('TCE_END_DATE'))
                    course.tce_reminder = self._parse_date(row.get('TCE_R2'))
                    course.term_code = row.get('ACADEMIC_TERM', '').strip()
                    course.last_synced = datetime.utcnow()
                    course.marked_for_tce = False

                    if is_new:
                        self.stats['courses_added'] += 1
                    else:
                        self.stats['courses_updated'] += 1

                    if processed_rows % DB_BATCH_SIZE == 0:
                        db.session.flush()
                except Exception as e:
                    self.errors.append(f"Course {row.get('SECTION_KEY', 'unknown')}: {str(e)}")

        self._flush_progress('Loading courses from CSV...', 1, processed_rows, last_reported)
        _update_progress('Saving course changes...', 1)
        db.session.commit()

        # ----- Orphan removal -----
        # Delete DB courses whose term_code is in the current sync window but
        # whose section_key wasn't in the CSV. We deliberately scope this to
        # the terms present in the CSV so we never touch courses from older
        # archived terms that this sync isn't responsible for.
        if self._csv_terms and self._csv_section_keys:
            _update_progress('Reconciling removed courses...', 1)
            term_list = list(self._csv_terms)
            orphan_keys = []
            checked_rows = 0

            course_key_query = (
                db.session.query(Course.section_key)
                .filter(Course.term_code.in_(term_list))
                .yield_per(DB_BATCH_SIZE)
            )
            for (section_key,) in course_key_query:
                checked_rows += 1
                if checked_rows % PROGRESS_UPDATE_INTERVAL == 0:
                    _update_progress(
                        f'Reconciling removed courses... ({checked_rows:,} scanned)',
                        1
                    )
                if section_key not in self._csv_section_keys:
                    orphan_keys.append(section_key)

            for chunk in self._iter_chunks(orphan_keys):
                self._delete_courses_by_keys(chunk)

            _update_progress(
                f'Reconciling removed courses... ({checked_rows:,} scanned, {self.stats["courses_removed"]:,} removed)',
                1
            )

    def sync_instructors(self):
        """
        Import instructor assignments from Instructor_Course.csv
        Presence in this file = course is marked for TCE in SAP
        Names are looked up from Users.csv
        """
        # First, load user data from Users.csv for name lookup
        users_data = {}
        users_filepath = os.path.join(self.datasources_path, 'Users.csv')
        if os.path.exists(users_filepath):
            try:
                with open(users_filepath, 'r', encoding='utf-8') as f:
                    user_reader = csv.DictReader(f)
                    for row in user_reader:
                        user_id = row.get('USER_ID', '').strip()
                        if user_id:
                            users_data[user_id] = {
                                'first_name': row.get('FIRSTNAME', '').strip(),
                                'last_name': row.get('LASTNAME', '').strip(),
                                'email': row.get('EMAIL', '').strip()
                            }
            except Exception as e:
                self.errors.append(f"Error loading Users.csv: {str(e)}")

        filepath = os.path.join(self.datasources_path, 'Instructor_Course.csv')

        if not os.path.exists(filepath):
            self.errors.append(f"Instructor_Course.csv not found at {filepath}")
            return

        # Track which courses have instructors and the exact (section_key,
        # user_id) pairs present in the CSV. Pairs are used below to remove
        # DB instructor rows that no longer appear in HANA - this is what
        # makes "instructor X removed in HANA" actually propagate to the UI.
        courses_with_instructors = set()
        csv_instructor_pairs = set()

        with open(filepath, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        relevant_section_keys = {
            row.get('SECTION_KEY', '').strip()
            for row in rows
            if row.get('SECTION_KEY', '').strip()
        }
        existing_course_keys = {
            section_key
            for chunk in self._iter_chunks(relevant_section_keys)
            for (section_key,) in db.session.query(Course.section_key).filter(Course.section_key.in_(chunk)).all()
        }
        existing_instructors = {}
        for chunk in self._iter_chunks(relevant_section_keys):
            for inst in Instructor.query.filter(Instructor.section_key.in_(chunk)).all():
                existing_instructors[(inst.section_key, inst.user_id)] = inst

        processed_rows = 0
        last_reported = 0
        with db.session.no_autoflush:
            for row in rows:
                processed_rows += 1
                if processed_rows % PROGRESS_UPDATE_INTERVAL == 0:
                    last_reported = self._flush_progress(
                        'Loading instructors from CSV...',
                        2,
                        processed_rows,
                        last_reported
                    )
                try:
                    section_key = row.get('SECTION_KEY', '').strip()
                    user_id = row.get('USER_ID', '').strip()

                    if not section_key or not user_id:
                        continue

                    csv_instructor_pairs.add((section_key, user_id))

                    if section_key not in existing_course_keys:
                        continue

                    courses_with_instructors.add(section_key)
                    existing = existing_instructors.get((section_key, user_id))

                    if not existing:
                        user_info = users_data.get(user_id, {})

                        instructor = Instructor(
                            section_key=section_key,
                            user_id=user_id,
                            first_name=user_info.get('first_name', ''),
                            last_name=user_info.get('last_name', ''),
                            email=user_info.get('email', ''),
                            instructor_role=row.get('ROLE', '').strip() or None,
                            last_synced=datetime.utcnow()
                        )
                        db.session.add(instructor)
                        existing_instructors[(section_key, user_id)] = instructor
                        self.stats['instructors_added'] += 1
                    else:
                        user_info = users_data.get(user_id, {})
                        if user_info:
                            existing.first_name = user_info.get('first_name', existing.first_name)
                            existing.last_name = user_info.get('last_name', existing.last_name)
                            existing.email = user_info.get('email', existing.email)
                            existing.last_synced = datetime.utcnow()

                    if processed_rows % DB_BATCH_SIZE == 0:
                        db.session.flush()
                except Exception as e:
                    self.errors.append(f"Instructor {row.get('USER_ID', 'unknown')}: {str(e)}")

        self._flush_progress('Loading instructors from CSV...', 2, processed_rows, last_reported)
        _update_progress('Saving instructor changes...', 2)

        # ----- Orphan removal: instructors -----
        # For every course in the current sync window, drop any DB instructor
        # row whose (section_key, user_id) is no longer in Instructor_Course.csv.
        # Scoped to courses we just processed so we don't disturb older terms.
        if self._csv_terms:
            _update_progress('Reconciling removed instructors...', 2)
            instructor_ids_to_delete = []
            checked_rows = 0
            existing_instructors = (
                db.session.query(Instructor.id, Instructor.section_key, Instructor.user_id)
                .join(Course, Instructor.section_key == Course.section_key)
                .filter(Course.term_code.in_(list(self._csv_terms)))
                .yield_per(DB_BATCH_SIZE)
            )
            for instructor_id, section_key, user_id in existing_instructors:
                checked_rows += 1
                if checked_rows % PROGRESS_UPDATE_INTERVAL == 0:
                    _update_progress(
                        f'Reconciling removed instructors... ({checked_rows:,} scanned)',
                        2
                    )
                if (section_key, user_id) not in csv_instructor_pairs:
                    instructor_ids_to_delete.append(instructor_id)

            for chunk in self._iter_chunks(instructor_ids_to_delete):
                self._delete_instructors_by_ids(chunk)

        # Mark courses with instructors as "marked for TCE", and unmark
        # courses in this sync window that ended up with no instructors at
        # all (otherwise stale TCE flags persist after HANA removals).
        if self._csv_terms:
            _update_progress('Refreshing TCE flags...', 2)
            Course.query.filter(
                Course.term_code.in_(list(self._csv_terms))
            ).update(
                {Course.marked_for_tce: False},
                synchronize_session=False
            )
            for chunk in self._iter_chunks(courses_with_instructors):
                Course.query.filter(
                    Course.section_key.in_(chunk)
                ).update(
                    {Course.marked_for_tce: True},
                    synchronize_session=False
                )

        db.session.commit()
    
    def sync_student_counts(self):
        """Count students per course from Student_Course.csv.

        For every course in the current sync window the count is reset to
        the value derived from the CSV (defaulting to 0). Without this reset,
        a course that loses all its enrollments in HANA would forever keep
        its old non-zero count in the DB.
        """
        filepath = os.path.join(self.datasources_path, 'Student_Course.csv')

        if not os.path.exists(filepath):
            self.errors.append(f"Student_Course.csv not found at {filepath}")
            return

        # Count students per section
        student_counts = defaultdict(int)

        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            processed_rows = 0
            last_reported = 0

            for row in reader:
                processed_rows += 1
                if processed_rows % PROGRESS_UPDATE_INTERVAL == 0:
                    last_reported = self._flush_progress(
                        'Counting students from CSV...',
                        3,
                        processed_rows,
                        last_reported
                    )
                section_key = row.get('SECTION_KEY', '').strip()
                if section_key:
                    student_counts[section_key] += 1
                    self.stats['students_counted'] += 1

        self._flush_progress('Counting students from CSV...', 3, processed_rows, last_reported)

        # Authoritative reset for every course in the current sync window.
        if self._csv_terms:
            _update_progress('Resetting enrollment counts...', 3)
            Course.query.filter(
                Course.term_code.in_(list(self._csv_terms))
            ).update(
                {Course.student_count: 0},
                synchronize_session=False
            )
            db.session.commit()

            updated_sections = 0
            for chunk in self._iter_chunks(student_counts.items()):
                db.session.bulk_update_mappings(
                    Course,
                    [
                        {'section_key': section_key, 'student_count': count}
                        for section_key, count in chunk
                    ]
                )
                db.session.commit()
                updated_sections += len(chunk)
                if updated_sections % PROGRESS_UPDATE_INTERVAL == 0:
                    _update_progress(
                        f'Writing enrollment counts... ({updated_sections:,} sections updated)',
                        3
                    )
            _update_progress(
                f'Writing enrollment counts... ({len(student_counts):,} sections updated)',
                3
            )
        else:
            # Fallback: if we somehow have no window info, only update
            # courses that appear in the student CSV (legacy behaviour).
            for section_key, count in student_counts.items():
                course = Course.query.get(section_key)
                if course:
                    course.student_count = count

        db.session.commit()
    
    def _parse_date(self, date_str):
        """Parse date string to date object"""
        if not date_str:
            return None
        
        date_str = date_str.strip()
        
        # Try common date formats
        formats = [
            '%Y-%m-%d',
            '%m/%d/%Y',
            '%Y/%m/%d',
            '%d-%m-%Y',
            '%Y-%m-%d %H:%M:%S',
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        
        return None


def generate_sample_data():
    """Generate sample course data for testing"""
    
    # Sample colleges
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
    
    # Sample departments per college
    # NOTE: Using 'SAMPLE_' prefix for IDs to avoid conflicts with real data from Courses.csv
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
    
    # Sample courses
    courses_data = []
    instructors_data = []
    students_data = []
    
    course_num = 0
    for college_code, college_name in colleges:
        for dept_id, dept_name in departments.get(college_code, []):
            # Generate 3-5 courses per department
            for i in range(1, 5):
                course_num += 1
                prefix = dept_name[:3].upper()
                class_num = 100 + (i * 100)
                section_key = f"{prefix}{class_num}-001-2025010"  # Spring 2025
                
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
                }
                courses_data.append(course)
                
                # 80% of courses are marked for TCE (have instructors)
                if course_num % 5 != 0:
                    instructor = {
                        'SECTION_KEY': section_key,
                        'USER_ID': f'inst{course_num:03d}',
                        'FIRST_NAME': f'Professor{course_num}',
                        'LAST_NAME': f'Smith{course_num}',
                        'EMAIL': f'inst{course_num:03d}@uky.edu',
                        'ROLE': 'Primary'
                    }
                    instructors_data.append(instructor)
                    
                    # Add students (10-50 per course, some with 0)
                    if course_num % 7 != 0:  # ~14% zero enrollment
                        num_students = 15 + (course_num % 35)
                        for s in range(num_students):
                            student = {
                                'SECTION_KEY': section_key,
                                'USER_ID': f'stu{course_num:03d}{s:03d}'
                            }
                            students_data.append(student)
    
    return courses_data, instructors_data, students_data


def write_sample_csvs(output_path='./datasources'):
    """Write sample data to CSV files"""
    import os
    
    os.makedirs(output_path, exist_ok=True)
    
    courses, instructors, students = generate_sample_data()
    
    # Write Courses.csv
    if courses:
        with open(os.path.join(output_path, 'Courses.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=courses[0].keys())
            writer.writeheader()
            writer.writerows(courses)
    
    # Write Instructor_Course.csv
    if instructors:
        with open(os.path.join(output_path, 'Instructor_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=instructors[0].keys())
            writer.writeheader()
            writer.writerows(instructors)
    
    # Write Student_Course.csv
    if students:
        with open(os.path.join(output_path, 'Student_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=students[0].keys())
            writer.writeheader()
            writer.writerows(students)
    
    return {
        'courses': len(courses),
        'instructors': len(instructors),
        'students': len(students)
    }
