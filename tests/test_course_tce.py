"""Course-level TCE edits: permissions, atomic updates, SAP persistence and Blue payloads."""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from flask import g
from sqlalchemy import text

from app import create_app
from app.models import db
from app.models.admin import Admin
from app.models.course import College, Course, Department
from app.models.college_policy import CollegeDateOverride
from app.models.course_tce import CourseTCEAudit, CourseTCEOverride
from app.services.college_policy import BlueCollegePolicy
from app.services.blue_push.orchestrator import BluePushOrchestrator
from app.routes.main import _build_tce_timeline_stats


class CourseTCETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        db.session.add(College(code='AS', name='Arts and Sciences'))
        db.session.add(Department(id='math', name='Mathematics', college_code='AS'))
        self.admin = Admin(linkblue='super', first_name='Super', last_name='Admin', role='super_admin')
        self.contact = Admin(linkblue='contact', first_name='College', last_name='Admin',
                             role='college_admin', college_code='AS')
        self.dept = Admin(linkblue='dept', first_name='Department', last_name='Admin',
                          role='dept_admin', college_code='AS', department_id='math')
        db.session.add_all([self.admin, self.contact, self.dept])
        self.courses = [Course(
            section_key=f'MA101-{section:03d}-2026030', class_code='MA 101',
            crs_section=f'MA101-{section:03d}', section_id=str(section), section_title='Calculus',
            college_code='AS', department_id='math', term_code='2026030',
            course_start=date(2026, 8, 20), course_end=date(2026, 12, 20),
            tce_start=date(2026, 12, 1), tce_end=date(2026, 12, 15 + section),
            tce_reminder=date(2026, 12, 10), marked_for_tce=True, student_count=10,
        ) for section in range(1, 4)]
        db.session.add_all(self.courses)
        db.session.commit()
        self.client = self.app.test_client()
        self.login(self.admin)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, user):
        for key in ('_login_user', 'course_tce_overrides', 'college_date_overrides'):
            g.pop(key, None)
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def post(self, courses=None, **fields):
        self.client.get('/verification/?term=')
        with self.client.session_transaction() as session:
            token = session.get('course_tce_csrf', '')
        values = {'section_keys': [course.section_key for course in (courses or self.courses[:2])],
                  'csrf_token': token, 'reason': 'Approved schedule correction', **fields}
        return self.client.post('/verification/tce-overrides?college=AS&term=2026030', data=values)

    def test_bulk_start_only_preserves_end_flag_course_dates_and_other_courses(self):
        response = self.post(tce_start_action='set', tce_start='2026-12-05',
                             tce_end='2099-01-01', course_start='2099-01-01')
        self.assertEqual(response.status_code, 302)
        self.assertIn('term=2026030', response.location)
        for course in self.courses[:2]:
            self.assertEqual(course.tce_start, date(2026, 12, 5))
            self.assertEqual(course.tce_end, course.sap_tce_end)
            self.assertEqual(course.sap_tce_start, date(2026, 12, 1))
            self.assertEqual(course.course_start, date(2026, 8, 20))
            self.assertEqual(course.tce_reminder, date(2026, 12, 10))
            self.assertTrue(course.marked_for_tce)
        self.assertEqual(self.courses[2].tce_start, date(2026, 12, 1))
        self.assertEqual(CourseTCEAudit.query.count(), 2)
        entry = CourseTCEAudit.query.first()
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(json.loads(entry.after_json)['tce_start'], '2026-12-05')
        self.assertEqual(Course.query.filter(Course.tce_start == date(2026, 12, 5)).count(), 2)
        self.assertEqual(_build_tce_timeline_stats(Course.query, date(2026, 12, 3))['active_tce_today'], 1)
        self.assertIn(b'2026-12-05', self.client.get('/verification/export?term=').data)

    def test_detail_end_only_edit_and_history(self):
        course = self.courses[0]
        response = self.post([course], detail_section_key=course.section_key,
                             tce_end_action='set', tce_end='2026-12-20')
        self.assertTrue(response.location.endswith('/course/' + course.section_key))
        self.assertEqual(course.tce_start, course.sap_tce_start)
        self.assertEqual(course.tce_end, date(2026, 12, 20))
        detail = self.client.get(response.location)
        self.assertIn(b'Edit TCE settings for this course', detail.data)
        self.assertIn(b'2026-12-20', detail.data)
        self.assertIn(b'Approved schedule correction', detail.data)

    def test_mixed_selection_invalid_range_rolls_back_every_course(self):
        # First selected course accepts the new start, second does not.
        self.courses[0].sap_tce_end = date(2026, 12, 30)
        db.session.commit()
        response = self.post(tce_start_action='set', tce_start='2026-12-20')
        self.assertIn(b'No courses were changed', self.client.get(response.location).data)
        self.assertEqual(CourseTCEOverride.query.count(), 0)
        self.assertEqual(CourseTCEAudit.query.count(), 0)

    def test_bad_dates_unknown_actions_empty_reason_and_no_changes_are_rejected(self):
        for values in ({'tce_start_action': 'set', 'tce_start': ''},
                       {'tce_end_action': 'set', 'tce_end': '2026-02-30'},
                       {'tce_end_action': 'set', 'tce_end': '20261210'},
                       {'tce_start_action': 'unknown'}, {'tce_flag_action': 'on'},
                       {'tce_flag_action': 'off', 'reason': ''}, {}):
            self.post(**values)
            self.assertEqual(CourseTCEOverride.query.count(), 0)
            self.assertEqual(CourseTCEAudit.query.count(), 0)

    def test_csrf_missing_selection_and_unknown_course_are_rejected(self):
        response = self.post(tce_flag_action='off', csrf_token='bad')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(tce_flag_action='off', section_keys=[]).status_code, 400)
        self.assertEqual(self.post(tce_flag_action='off', section_keys=[
            self.courses[0].section_key, 'missing']).status_code, 404)
        self.assertEqual(CourseTCEOverride.query.count(), 0)

    def test_regular_admins_cannot_see_controls_or_post(self):
        for user in (self.contact, self.dept):
            self.login(user)
            listing = self.client.get('/verification/?term=')
            detail = self.client.get('/verification/course/' + self.courses[0].section_key)
            self.assertNotIn(b'id="tce-edit-form"', listing.data)
            self.assertNotIn(b'id="tce-edit-form"', detail.data)
            self.assertNotIn(b'tce-course-select', listing.data)
            self.assertEqual(self.post(tce_flag_action='off').status_code, 403)
        self.assertEqual(CourseTCEOverride.query.count(), 0)

    def test_flag_off_preserves_sap_and_dates_and_updates_views_and_stats(self):
        self.post(tce_flag_action='off')
        for course in self.courses[:2]:
            self.assertTrue(course.sap_marked_for_tce)
            self.assertTrue(course.tce_blocked)
            self.assertFalse(course.marked_for_tce)
            self.assertEqual(course.tce_start, course.sap_tce_start)
        self.assertEqual(Course.query.filter(Course.marked_for_tce.is_(True)).count(), 1)
        self.assertEqual(Course.query.filter(Course.marked_for_tce.is_(False)).count(), 2)
        self.assertEqual(self.client.get('/verification/api/stats?term=').json['marked'], 1)
        self.assertIn(b'Blocked from Blue', self.client.get('/verification/?term=').data)
        self.assertIn(b'TCE Off', self.client.get('/verification/course/' + self.courses[0].section_key).data)
        self.login(self.contact)
        self.assertIn(b'Blocked from Blue', self.client.get('/verification/?term=').data)

    def test_local_edits_survive_sap_refresh_and_restore_fields_independently(self):
        course = self.courses[0]
        self.post([course], tce_start_action='set', tce_start='2026-12-05',
                  tce_end_action='set', tce_end='2026-12-20', tce_flag_action='off')
        db.session.execute(text('UPDATE courses SET tce_start=:start, tce_end=:end, marked_for_tce=TRUE'),
                           {'start': '2026-12-07', 'end': '2026-12-25'})
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(course.tce_start, date(2026, 12, 5))
        self.assertEqual(course.tce_end, date(2026, 12, 20))
        self.assertFalse(course.marked_for_tce)
        self.assertTrue(course.sap_marked_for_tce)
        self.post([course], tce_start_action='restore', tce_flag_action='restore')
        self.assertEqual(course.tce_start, date(2026, 12, 7))
        self.assertEqual(course.tce_end, date(2026, 12, 20))
        self.assertTrue(course.marked_for_tce)
        self.post([course], tce_end_action='restore')
        self.assertEqual(course.tce_end, date(2026, 12, 25))
        self.assertEqual(CourseTCEOverride.query.count(), 0)
        self.assertEqual(CourseTCEAudit.query.count(), 3)

    def test_restore_block_does_not_force_sap_flag_on_or_replace_dates(self):
        course = self.courses[0]
        course.sap_marked_for_tce = False
        db.session.commit()
        self.post([course], tce_flag_action='off', tce_end_action='set', tce_end='2026-12-20')
        self.post([course], tce_flag_action='restore')
        self.assertFalse(course.marked_for_tce)
        self.assertFalse(course.tce_blocked)
        self.assertEqual(course.tce_end, date(2026, 12, 20))

    def test_course_dates_take_precedence_over_legacy_college_dates(self):
        db.session.add(CollegeDateOverride(college_code='AS', term_code='2026030',
                       tce_start=date(2026, 11, 1), tce_end=date(2026, 11, 30),
                       reason='Legacy', updated_by_id=self.admin.id))
        db.session.commit()
        self.post([self.courses[0]], tce_start_action='set', tce_start='2026-11-05')
        self.assertEqual(self.courses[0].tce_start, date(2026, 11, 5))
        self.assertEqual(self.courses[0].tce_end, date(2026, 11, 30))
        self.assertEqual(self.courses[1].tce_start, date(2026, 11, 1))
        self.assertEqual(Course.query.filter(Course.tce_start == date(2026, 11, 5)).count(), 1)
        self.post([self.courses[0]], tce_start_action='restore')
        self.assertEqual(self.courses[0].tce_start, date(2026, 11, 1))

    def test_blue_payloads_filter_blocked_courses_and_only_override_selected_date(self):
        self.post([self.courses[0]], tce_flag_action='off')
        self.post([self.courses[1]], tce_end_action='set', tce_end='2026-12-20')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Courses.csv'
            fields = ['SECTION_KEY', 'CLASS_COLLEGE_SHORT', 'CLASS_COLLEGE', 'ACADEMIC_TERM',
                      'CLASS', 'TCE_INVITE', 'TCE_END_DATE']
            with path.open('w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                for course in self.courses:
                    writer.writerow(dict(zip(fields, [course.section_key, 'AS', 'Arts and Sciences',
                        '2026030', 'MA 101', '2026-12-08 00:00:00', '2026-12-22 00:00:00'])))
            for name in ('Instructor_Course.csv', 'Student_Course.csv'):
                with (Path(directory) / name).open('w', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow(['SECTION_KEY', 'USER_ID'])
                    writer.writerows((course.section_key, 'synthetic-user') for course in self.courses)
            originals = {file.name: file.read_bytes() for file in Path(directory).glob('*.csv')}
            policy = BlueCollegePolicy(path)
            with path.open(encoding='utf-8') as file:
                rows = list(csv.DictReader(file))
            self.assertIsNone(policy.transform(rows[0]))
            transformed = policy.transform(rows[1])
            self.assertEqual(transformed['TCE_INVITE'], '2026-12-08 00:00:00')
            self.assertEqual(transformed['TCE_END_DATE'], '2026-12-20 00:00:00')
            relation = {'SECTION_KEY': self.courses[1].section_key, 'USER_ID': 'synthetic-user'}
            self.assertEqual(policy.transform(relation), relation)
            with patch('app.services.blue_push.pusher.BlueSoapClient') as soap:
                result = BluePushOrchestrator(directory).push_all(
                    datasources=['courses', 'instructors', 'students'],
                    dry_run=True, trigger_type='scheduled', skip_gaps=True)
                self.assertTrue(result['success'], result.get('errors'))
                self.assertEqual(result['stats']['total_records'], 6)
                for key in ('courses', 'instructors', 'students'):
                    self.assertEqual(result['push_details'][key]['total_rows'], 2)
                soap.return_value.register_import.assert_not_called()
            self.assertEqual(originals, {file.name: file.read_bytes() for file in Path(directory).glob('*.csv')})


if __name__ == '__main__':
    unittest.main()
