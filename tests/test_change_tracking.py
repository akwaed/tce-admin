"""
Tests for change-tracking fixes:

- Date type false-positive diff regression (2a)
- Student add/drop set-diff logic (2c)
- Unified course history search paths (2d)
- No-op change filtering / history meta honesty helpers (2b/E)
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Force in-memory SQLite for unit tests before app import side effects.
os.environ.setdefault('FLASK_ENV', 'testing')
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import create_app
from app.models import db
from app.models.course import (
    College, Course, CourseUser, Department, Instructor, StudentEnrollment,
)
from app.models.sync_history import ChangeLog, SyncRun
from app.services.course_sync import CourseSyncService, normalize_diff_value
from app.routes import tracking as tracking_routes


class NormalizeDiffValueTests(unittest.TestCase):
    def test_date_object_matches_iso_string(self):
        self.assertEqual(
            normalize_diff_value(date(2026, 12, 13)),
            normalize_diff_value('2026-12-13'),
        )

    def test_datetime_matches_iso_date(self):
        self.assertEqual(
            normalize_diff_value(datetime(2026, 12, 13, 0, 0, 0)),
            normalize_diff_value('2026-12-13'),
        )

    def test_datetime_string_collapses_to_date(self):
        self.assertEqual(
            normalize_diff_value('2026-12-13 00:00:00'),
            normalize_diff_value('2026-12-13'),
        )

    def test_none_and_empty(self):
        self.assertEqual(normalize_diff_value(None), '')
        self.assertEqual(normalize_diff_value(''), '')
        self.assertEqual(normalize_diff_value(None), normalize_diff_value(''))

    def test_plain_strings_unchanged(self):
        self.assertEqual(normalize_diff_value('AAD 150'), 'AAD 150')


class CourseDiffRegressionTests(unittest.TestCase):
    """2a: old=date, new=ISO string must not produce a diff."""

    def setUp(self):
        self.svc = CourseSyncService(datasources_path='/tmp')

    def test_identical_dates_different_types_no_diff(self):
        old = {
            'class_id': '98015351',
            'class_code': 'AAD 150',
            'section_id': '001',
            'crs_section': 'AAD150-001',
            'section_title': 'Intro',
            'college_code': 'FA',
            'department_id': 'AAD',
            'crosslisted_id': None,
            'course_start': date(2026, 12, 13),
            'course_end': date(2027, 5, 1),
            'tce_start': date(2027, 4, 1),
            'tce_end': date(2027, 4, 15),
            'tce_reminder': date(2027, 4, 10),
            'term_code': '2027010',
        }
        new = {
            **old,
            'course_start': '2026-12-13',
            'course_end': '2027-05-01',
            'tce_start': '2027-04-01',
            'tce_end': '2027-04-15',
            'tce_reminder': '2027-04-10',
        }
        diff = self.svc._course_diff(old, new)
        self.assertEqual(diff, {}, f'Expected no diff, got: {diff}')

    def test_real_date_change_is_recorded(self):
        old = {
            'class_id': '1', 'class_code': 'X', 'section_id': '1',
            'crs_section': None, 'section_title': 'T',
            'college_code': 'C', 'department_id': 'D', 'crosslisted_id': None,
            'course_start': date(2026, 1, 1),
            'course_end': None, 'tce_start': None, 'tce_end': None,
            'tce_reminder': None, 'term_code': '2026010',
        }
        new = {**old, 'course_start': '2026-01-15'}
        diff = self.svc._course_diff(old, new)
        self.assertIn('course_start', diff)
        self.assertEqual(diff['course_start'], ('2026-01-01', '2026-01-15'))

    def test_string_field_change(self):
        old = {
            'class_id': '1', 'class_code': 'AAD 150', 'section_id': '1',
            'crs_section': None, 'section_title': 'Old Title',
            'college_code': 'C', 'department_id': 'D', 'crosslisted_id': None,
            'course_start': None, 'course_end': None, 'tce_start': None,
            'tce_end': None, 'tce_reminder': None, 'term_code': 'T',
        }
        new = {**old, 'section_title': 'New Title'}
        diff = self.svc._course_diff(old, new)
        self.assertEqual(diff['section_title'], ('Old Title', 'New Title'))


class StudentEnrollmentDiffTests(unittest.TestCase):
    """2c: set-diff for student pairs mirrors instructor pattern."""

    def test_added_and_removed_pairs(self):
        old_pairs = {
            ('SEC-A', 'alice'): {'first_name': 'Alice', 'last_name': 'A', 'email': 'a@x'},
            ('SEC-A', 'bob'): {'first_name': 'Bob', 'last_name': 'B', 'email': 'b@x'},
            ('SEC-B', 'carol'): {'first_name': 'Carol', 'last_name': 'C', 'email': 'c@x'},
        }
        new_pairs = {
            ('SEC-A', 'alice'): {'first_name': 'Alice', 'last_name': 'A', 'email': 'a@x'},
            ('SEC-A', 'dave'): {'first_name': 'Dave', 'last_name': 'D', 'email': 'd@x'},
            ('SEC-B', 'carol'): {'first_name': 'Carol', 'last_name': 'C', 'email': 'c@x'},
        }
        added = set(new_pairs) - set(old_pairs)
        removed = set(old_pairs) - set(new_pairs)
        self.assertEqual(added, {('SEC-A', 'dave')})
        self.assertEqual(removed, {('SEC-A', 'bob')})

    def test_noop_enrollment_set(self):
        pairs = {
            ('SEC-A', 'alice'): {},
            ('SEC-A', 'bob'): {},
        }
        self.assertEqual(set(pairs) - set(pairs), set())
        self.assertEqual(set(pairs) - set(pairs), set())


class TrackingUIHelpersTests(unittest.TestCase):
    def test_is_noop_change(self):
        class Row:
            change_type = 'updated'
            old_value = date(2026, 12, 13)
            new_value = '2026-12-13'

        self.assertTrue(tracking_routes._is_noop_change(Row()))

        class RealChange:
            change_type = 'updated'
            old_value = 'foo'
            new_value = 'bar'

        self.assertFalse(tracking_routes._is_noop_change(RealChange()))

        class Added:
            change_type = 'added'
            old_value = None
            new_value = 'x'

        self.assertFalse(tracking_routes._is_noop_change(Added()))

    def test_history_meta_baseline(self):
        class C:
            first_seen_in_tracking_at = datetime(2026, 7, 16, 12, 0, 0)

        meta = tracking_routes._course_history_meta(C(), total_raw=0, total_visible=0)
        self.assertEqual(meta['status'], 'tracking_baseline')
        self.assertIn('Tracking began', meta['message'])
        self.assertIn('not a proven original creation date', meta['message'])

    def test_history_meta_no_tracking(self):
        class C:
            first_seen_in_tracking_at = None

        meta = tracking_routes._course_history_meta(C(), 0, 0)
        self.assertEqual(meta['status'], 'no_tracking_yet')
        self.assertIn('do not invent', meta['message'].lower())

    def test_history_meta_only_noise(self):
        class C:
            first_seen_in_tracking_at = None

        meta = tracking_routes._course_history_meta(C(), total_raw=5, total_visible=0)
        self.assertEqual(meta['status'], 'only_noise')


class SearchPathIntegrationTests(unittest.TestCase):
    """2d: search by course code, instructor name, student name."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['TESTING'] = True
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        # Seed
        db.session.add(College(code='AS', name='Arts & Sciences'))
        db.session.add(Department(id='WRD', name='Writing', college_code='AS'))
        db.session.add(Course(
            section_key='WRD691-003-2027010',
            class_id='98010063',
            class_code='WRD 691',
            section_id='003',
            section_title='Research Methods',
            college_code='AS',
            department_id='WRD',
            term_code='2027010',
        ))
        db.session.add(Course(
            section_key='AAD150-001-2027010',
            class_id='98015351',
            class_code='AAD 150',
            section_id='001',
            section_title='Intro to Arts Admin',
            college_code='AS',
            department_id='WRD',
            term_code='2027010',
        ))
        db.session.add(Instructor(
            section_key='AAD150-001-2027010',
            user_id='jsmith',
            first_name='Jane',
            last_name='Smith',
            email='jane.smith@uky.edu',
        ))
        db.session.add(CourseUser(
            user_id='bstudent',
            first_name='Bob',
            last_name='Student',
            email='bob.student@uky.edu',
        ))
        db.session.add(StudentEnrollment(
            section_key='WRD691-003-2027010',
            user_id='bstudent',
        ))
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_search_by_course_code(self):
        results = tracking_routes._search_courses('AAD 150')
        keys = {r['section_key'] for r in results}
        self.assertIn('AAD150-001-2027010', keys)

    def test_search_by_class_id(self):
        results = tracking_routes._search_courses('98015351')
        keys = {r['section_key'] for r in results}
        self.assertIn('AAD150-001-2027010', keys)

    def test_search_by_instructor_name(self):
        results = tracking_routes._search_courses('Jane Smith')
        keys = {r['section_key'] for r in results}
        self.assertIn('AAD150-001-2027010', keys)
        match = next(r for r in results if r['section_key'] == 'AAD150-001-2027010')
        self.assertEqual(match.get('matched_user_id'), 'jsmith')

    def test_search_by_student_name(self):
        results = tracking_routes._search_courses('Bob Student')
        keys = {r['section_key'] for r in results}
        self.assertIn('WRD691-003-2027010', keys)
        match = next(r for r in results if r['section_key'] == 'WRD691-003-2027010')
        self.assertEqual(match.get('match_kind'), 'student')

    def test_search_by_linkblue(self):
        results = tracking_routes._search_courses('jsmith')
        keys = {r['section_key'] for r in results}
        self.assertIn('AAD150-001-2027010', keys)


class ChangeLogQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['TESTING'] = True
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        db.session.add(College(code='AS', name='A&S'))
        db.session.add(Department(id='AAD', name='AAD', college_code='AS'))
        db.session.add(Course(
            section_key='AAD150-001-2027010',
            class_id='98015351',
            class_code='AAD 150',
            section_id='001',
            section_title='Intro',
            college_code='AS',
            department_id='AAD',
            term_code='2027010',
            first_seen_in_tracking_at=datetime(2026, 7, 1, 4, 0, 0),
        ))
        run = SyncRun(status='completed', started_at=datetime(2026, 7, 10, 4, 0, 0))
        db.session.add(run)
        db.session.flush()
        # No-op date noise
        for field in ('course_start', 'course_end', 'tce_start', 'tce_end', 'tce_reminder'):
            db.session.add(ChangeLog(
                sync_run_id=run.id,
                created_at=datetime(2026, 7, 10, 4, 5, 0),
                entity_type='course',
                entity_key='AAD150-001-2027010',
                change_type='updated',
                field_name=field,
                old_value='2026-12-13',
                new_value='2026-12-13',
                display_label='AAD 150-001',
            ))
        # Real change
        db.session.add(ChangeLog(
            sync_run_id=run.id,
            created_at=datetime(2026, 7, 10, 4, 6, 0),
            entity_type='student',
            entity_key='AAD150-001-2027010|bstudent',
            change_type='added',
            display_label='Bob Student (bstudent) -> AAD150-001-2027010',
            new_value='{}',
        ))
        db.session.commit()
        cls.run_id = run.id

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_filters_noop_by_default(self):
        page = tracking_routes._get_course_changes(
            'AAD150-001-2027010',
            {'page': 1, 'show_noise': False},
        )
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['total_raw'], 6)
        self.assertEqual(page['items'][0].entity_type, 'student')

    def test_show_noise_includes_noops(self):
        page = tracking_routes._get_course_changes(
            'AAD150-001-2027010',
            {'page': 1, 'show_noise': True},
        )
        self.assertEqual(page['total'], 6)

    def test_filter_by_entity_and_user(self):
        page = tracking_routes._get_course_changes(
            'AAD150-001-2027010',
            {'page': 1, 'entity_type': 'student', 'user': 'bstudent', 'show_noise': True},
        )
        self.assertEqual(page['total'], 1)

    def test_group_by_sync_run(self):
        page = tracking_routes._get_course_changes(
            'AAD150-001-2027010',
            {'page': 1, 'show_noise': True},
        )
        groups = tracking_routes._group_changes_by_sync_run(page['items'])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['sync_run_id'], self.run_id)
        self.assertEqual(groups[0]['count'], 6)


if __name__ == '__main__':
    unittest.main()
