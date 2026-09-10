"""College exception integration tests. Synthetic data; no SAP or Blue writes."""
import csv
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from flask import g, template_rendered
from sqlalchemy import text
from app import create_app
from app.models import db
from app.models.admin import Admin
from app.models.course import College, Course, Department
from app.models.college_policy import CollegePolicy, CollegeDateOverride, CollegePolicyAudit
from app.services.college_policy import BlueCollegePolicy, current_term, excluded, visible_courses
from app.services.blue_push.config import DEFAULT_DATASOURCES
from app.services.blue_push.csv_loader import load_datasource_csv, sample_rows
from app.services.blue_push.pusher import push_datasource
from app.services.blue_push.orchestrator import BluePushOrchestrator
from app.routes.main import _build_tce_timeline_stats


class CollegePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        db.session.add_all([College(code='PH', name='College of Pharmacy'),
                            College(code='LA', name='College of Law')])
        db.session.add_all([Department(id='pharm', name='Pharmacy', college_code='PH'),
                            Department(id='law', name='Law', college_code='LA')])
        self.superadmin = Admin(linkblue='super', first_name='Super', last_name='Admin', role='super_admin')
        self.contact = Admin(linkblue='contact', first_name='College', last_name='Contact',
                             role='college_admin', college_code='PH')
        self.dept = Admin(linkblue='dept', first_name='Dept', last_name='Contact',
                          role='dept_admin', college_code='PH', department_id='pharm')
        db.session.add_all([self.superadmin, self.contact, self.dept])
        db.session.commit()
        self.today = date.today()
        self.courses = []
        for code, section in [('PH', '001'), ('PH', '500'), ('PH', '0501'), ('PH', 'A01'), ('LA', '601')]:
            self.add_course(code, section)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_course(self, code='PH', section='002', term='Current', start=None, end=None):
        course = Course(section_key=f'{code}101-{section}-{term}', crs_section=f'{code}101-{section}',
                        class_code=f'{code} 101', section_id='97465276', section_title=f'Title {code} {section}',
                        college_code=code, department_id='pharm' if code == 'PH' else 'law',
                        term_code=term, course_start=start or self.today-timedelta(days=20),
                        course_end=end or self.today+timedelta(days=20),
                        tce_start=self.today-timedelta(days=2), tce_end=self.today+timedelta(days=2),
                        marked_for_tce=True, student_count=12)
        db.session.add(course)
        db.session.commit()
        self.courses.append(course)
        return course

    def login(self, user):
        g.pop('_login_user', None)
        g.pop('college_date_overrides', None)
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def render(self, url):
        captured = []
        def receiver(sender, template, context, **extra):
            captured.append(context)
        template_rendered.connect(receiver, self.app)
        try:
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)
            return result, captured[-1]
        finally:
            template_rendered.disconnect(receiver, self.app)

    def post(self, **values):
        self.client.get('/settings/college-policies?college=PH')
        with self.client.session_transaction() as session:
            token = session['college_policy_csrf']
        values.setdefault('college', 'PH')
        values.setdefault('reason', 'Approved professional-school evaluation schedule')
        return self.client.post('/settings/college-policies', data={'csrf_token': token, **values})

    def save_dates(self):
        self.login(self.superadmin)
        self.post(action='dates', college='LA', term='Current', acknowledge='yes',
                  tce_start='2026-11-01', tce_end='2026-11-15')

    def test_section_boundaries_and_object_id_not_used(self):
        for section, expected in [('001', False), ('500', False), ('0501', True), ('600', True),
                                  ('A501', False), ('501A', False), ('', False)]:
            with self.subTest(section=section):
                self.assertEqual(excluded(f'PH101-{section}', '', 500), expected)
        visible = visible_courses(Course.query.filter_by(college_code='PH'), self.contact).all()
        self.assertEqual({c.section_number for c in visible}, {'001', '500', 'A01'})
        self.assertEqual(visible_courses(Course.query, self.superadmin).count(), 5)

    def test_contact_dashboard_list_export_detail_and_stats_agree(self):
        self.login(self.contact)
        _, dashboard = self.render('/dashboard')
        self.assertEqual(dashboard['stats']['total_courses'], 3)
        self.assertEqual(dashboard['stats']['total_students'], 36)
        _, listing = self.render('/verification/')
        self.assertEqual(listing['total_count'], 3)
        self.assertEqual(listing['stats']['total'], 3)
        exported = self.client.get('/verification/export?term=')
        self.assertEqual(exported.status_code, 200)
        self.assertNotIn(b'0501', exported.data)
        self.assertIn(b'500', exported.data)
        hidden = self.courses[2].section_key
        self.assertEqual(self.client.get(f'/verification/course/{hidden}').status_code, 404)
        self.assertEqual(self.client.get('/verification/api/stats?term=').json['total'], 3)

    def test_default_term_majority_ongoing_explicit_all_and_old_term(self):
        self.add_course(section='003', term='Future', start=self.today+timedelta(days=90), end=self.today+timedelta(days=120))
        self.add_course(section='004', term='Old', start=self.today-timedelta(days=90), end=self.today-timedelta(days=40))
        self.assertEqual(current_term(Course.query, self.today), 'Current')
        self.login(self.contact)
        _, listing = self.render('/verification/')
        self.assertEqual(listing['current_filters']['term'], 'Current')
        self.assertEqual(listing['stats']['total'], 3)
        _, listing = self.render('/verification/?term=')
        self.assertEqual(listing['current_filters']['term'], '')
        self.assertEqual(listing['total_count'], 5)
        _, listing = self.render('/verification/?term=Old')
        self.assertEqual(listing['total_count'], 1)
        self.assertEqual(listing['stats']['total'], 1)

    def test_term_fallbacks_empty_dates_and_ties(self):
        query = Course.query.filter_by(college_code='PH')
        self.assertEqual(current_term(query, self.today-timedelta(days=30)), 'Current')
        self.assertEqual(current_term(query, self.today+timedelta(days=30)), 'Current')
        for course in self.courses:
            course.course_start = course.course_end = None
        db.session.commit()
        self.assertEqual(current_term(query, self.today), 'Current')
        self.assertEqual(current_term(Course.query.filter_by(college_code='NONE')), '')
        one = self.add_course('LA', '002', 'A term')
        two = self.add_course('LA', '003', 'B term')
        self.assertEqual(current_term(Course.query.filter(Course.section_key.in_([one.section_key, two.section_key]))), 'B term')

    def test_only_superadmin_can_read_or_write_settings(self):
        for user in (self.contact, self.dept):
            self.login(user)
            self.assertEqual(self.client.get('/settings/college-policies').status_code, 302)
            response = self.client.post('/settings/college-policies', data={
                'action': 'dates', 'college': 'LA', 'term': 'Current', 'acknowledge': 'yes',
                'tce_start': '2026-11-01', 'tce_end': '2026-11-15', 'reason': 'attempt'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(CollegeDateOverride.query.count(), 0)
        self.assertEqual(CollegePolicyAudit.query.count(), 0)

    def test_date_validation_acknowledgment_reason_and_csrf(self):
        self.login(self.superadmin)
        valid = dict(action='dates', college='LA', term='Current', acknowledge='yes',
                     tce_start='2026-11-01', tce_end='2026-11-15')
        for bad in ({'acknowledge': ''}, {'reason': ''}, {'tce_end': '2026-10-01'},
                    {'tce_start': 'invalid'}, {'term': 'Unknown'}, {'tce_end': ''}):
            self.post(**{**valid, **bad})
            self.assertEqual(CollegeDateOverride.query.count(), 0)
        self.assertEqual(self.client.post('/settings/college-policies', data=valid).status_code, 400)
        self.assertEqual(CollegePolicyAudit.query.count(), 0)

    def test_dates_effective_in_objects_queries_exports_and_restore_latest_sap(self):
        self.save_dates()
        law = self.courses[-1]
        original_sap = law.sap_tce_start
        self.assertEqual(law.tce_start, date(2026, 11, 1))
        self.assertEqual(law.sap_tce_start, original_sap)
        self.assertEqual(Course.query.filter(Course.tce_start == date(2026, 11, 1)).count(), 1)
        self.assertEqual(_build_tce_timeline_stats(Course.query, date(2026, 11, 2))['active_tce_today'], 1)
        # Same raw DB write performed by SAP's bulk upsert, while the override is active.
        db.session.execute(text('UPDATE courses SET tce_start=:start, tce_end=:end WHERE college_code=:college'),
                           {'start': '2026-12-01', 'end': '2026-12-15', 'college': 'LA'})
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(law.tce_start, date(2026, 11, 1))
        self.assertEqual(law.sap_tce_start, date(2026, 12, 1))
        self.assertNotEqual(self.courses[0].tce_start, date(2026, 11, 1))
        other_term = self.add_course('LA', '002', 'Other term')
        self.assertNotEqual(other_term.tce_start, date(2026, 11, 1))
        response = self.client.get('/verification/export?college=LA&term=Current')
        self.assertIn(b'2026-11-01', response.data)
        response = self.client.get(f'/verification/course/{law.section_key}')
        self.assertIn(b'Local TCE date override active', response.data)
        self.post(action='restore', college='LA', term='Current', acknowledge='yes')
        self.assertEqual(law.tce_start, date(2026, 12, 1))
        self.assertEqual(CollegeDateOverride.query.count(), 0)
        self.assertEqual(CollegePolicyAudit.query.count(), 2)

    def test_exclusion_can_be_disabled_and_audited(self):
        self.login(self.superadmin)
        self.post(action='exclusion', threshold='')
        self.assertIsNone(db.session.get(CollegePolicy, 'PH').exclude_sections_above)
        self.assertEqual(visible_courses(Course.query.filter_by(college_code='PH'), self.contact).count(), 4)
        self.assertEqual(CollegePolicyAudit.query.first().actor_id, self.superadmin.id)
        self.post(action='exclusion', threshold='-1')
        self.assertEqual(CollegePolicyAudit.query.count(), 1)
        self.post(action='exclusion', threshold='500')
        self.assertEqual(visible_courses(Course.query.filter_by(college_code='PH'), self.contact).count(), 3)

    def write_sources(self, directory):
        columns = ['SECTION_KEY', 'CRS_SECTION', 'CLASS_COLLEGE_SHORT', 'CLASS_COLLEGE',
                   'ACADEMIC_TERM', 'TCE_INVITE', 'TCE_END_DATE', 'TITLE']
        path = Path(directory) / 'Courses.csv'
        with path.open('w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for course in self.courses:
                writer.writerow(dict(zip(columns, [course.section_key, course.crs_section, course.college_code,
                    'College of Pharmacy' if course.college_code == 'PH' else 'College of Law',
                    course.term_code, '2026-12-01 00:00:00', '2026-12-15 00:00:00', course.section_title])))
        for name in ['Instructor_Course.csv', 'Student_Course.csv']:
            with (Path(directory)/name).open('w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(['SECTION_KEY', 'USER_ID'])
                writer.writerows((c.section_key, 'user1') for c in self.courses)
        return path

    def test_blue_payload_exclusions_relations_dates_and_raw_files_unchanged(self):
        self.save_dates()
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_sources(directory)
            original = path.read_bytes()
            policy = BlueCollegePolicy(path)
            for key in ['courses', 'instructors', 'students']:
                config = DEFAULT_DATASOURCES[key]
                loaded = load_datasource_csv(str(Path(directory)/config.csv_file), config,
                                             row_transform=policy.transform)
                records = sample_rows(loaded.columns, loaded.rows, 20)
                self.assertEqual(len(records), 4)
                self.assertFalse(any('0501' in row['SECTION_KEY'] for row in records))
                if key == 'courses':
                    law = next(r for r in records if r['CLASS_COLLEGE_SHORT'] == 'LA')
                    self.assertEqual(law['TCE_INVITE'], '2026-11-01 00:00:00')
                    self.assertEqual(law['TCE_END_DATE'], '2026-11-15 00:00:00')
                with patch('app.services.blue_push.pusher.BlueSoapClient') as client:
                    result = push_datasource(config, '', '', directory, dry_run=True, college_policy=policy)
                    self.assertTrue(result.success, result.error)
                    self.assertEqual(result.rows_pushed, 4)
                    client.return_value.register_import.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_blue_uses_fresh_csv_and_filters_before_test_row_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.courses.insert(0, self.courses.pop(2))  # Excluded course first in CSV.
            path = self.write_sources(directory)
            Course.query.filter_by(section_key=self.courses[0].section_key).delete()
            db.session.commit()
            policy = BlueCollegePolicy(path)
            loaded = load_datasource_csv(str(path), DEFAULT_DATASOURCES['courses'], test_rows=1,
                                         row_transform=policy.transform)
            self.assertEqual(len(loaded.rows), 1)
            self.assertNotIn('0501', loaded.rows[0][0])
            self.assertEqual(loaded.total_rows, 4)

    def test_settings_template_renders_saved_values_and_history(self):
        self.save_dates()
        response, _ = self.render('/settings/college-policies?college=LA')
        self.assertIn(b'Data integrity warning', response.data)
        self.assertIn(b'2026-11-01', response.data)
        self.assertIn(b'Restore SAP dates', response.data)
        self.assertIn(b'super', response.data)

    def test_scheduled_pipeline_uses_same_rules_for_all_three_payloads(self):
        self.save_dates()
        with tempfile.TemporaryDirectory() as directory:
            self.write_sources(directory)
            with patch('app.services.blue_push.pusher.BlueSoapClient') as soap:
                result = BluePushOrchestrator(directory).push_all(
                    datasources=['courses', 'instructors', 'students'],
                    dry_run=True, trigger_type='scheduled', skip_gaps=True)
                self.assertTrue(result['success'], result.get('errors'))
                self.assertEqual(result['stats']['total_records'], 12)
                self.assertEqual(result['stats']['datasources_success'], 3)
                soap.return_value.register_import.assert_not_called()
                for key in ['courses', 'instructors', 'students']:
                    self.assertEqual(result['push_details'][key]['total_rows'], 4)

    def test_relation_only_push_applies_rules_without_prebuilt_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_sources(directory)
            result = push_datasource(DEFAULT_DATASOURCES['students'], '', '', directory, dry_run=True)
            self.assertTrue(result.success, result.error)
            self.assertEqual(result.total_rows, 4)

    def test_missing_course_source_fails_before_blue_write(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('app.services.blue_push.pusher.BlueSoapClient') as soap:
                with self.assertRaises(FileNotFoundError):
                    BluePushOrchestrator(directory).push_all(
                        datasources=['students'], dry_run=True, skip_gaps=True)
                soap.assert_not_called()

    def test_all_excluded_payload_retains_abort_on_empty(self):
        self.courses = [self.courses[2]]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_sources(directory)
            policy = BlueCollegePolicy(path)
            with patch('app.services.blue_push.pusher.BlueSoapClient') as soap:
                result = push_datasource(DEFAULT_DATASOURCES['courses'], '', '', directory,
                                         dry_run=False, college_policy=policy)
                self.assertFalse(result.success)
                self.assertEqual(result.error, 'No data to import')
                soap.return_value.register_import.assert_not_called()


if __name__ == '__main__':
    unittest.main()
