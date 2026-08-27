"""Tests for the super-admin TCE report checklist workflow."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from flask import g
from werkzeug.datastructures import MultiDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault('FLASK_ENV', 'testing')
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import create_app
from app.models import db
from app.models.admin import Admin
from app.models.checklist import (
    ChecklistAuditLog,
    ReportChecklistTemplate,
    ReportChecklistTemplateItem,
    SemesterChecklist,
    SemesterChecklistItem,
    SemesterReportChecklist,
)
from app.services.checklist_service import (
    ChecklistConflictError,
    ChecklistValidationError,
    archive_checklist,
    create_semester_checklist,
    reopen_checklist,
    seed_checklist_templates,
    update_checklist_item,
    update_template,
)


class ChecklistFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.context = cls.app.app_context()
        cls.context.push()
        db.drop_all()
        db.create_all()

        cls.super_one = Admin(
            linkblue='super-one', first_name='Super', last_name='One',
            email='super-one@example.edu', role='super_admin', is_active=True,
        )
        cls.super_two = Admin(
            linkblue='super-two', first_name='Super', last_name='Two',
            email='super-two@example.edu', role='super_admin', is_active=True,
        )
        cls.college_admin = Admin(
            linkblue='college-one', first_name='College', last_name='One',
            email='college-one@example.edu', role='college_admin', is_active=True,
        )
        db.session.add_all([cls.super_one, cls.super_two, cls.college_admin])
        db.session.commit()
        cls.super_one_id = cls.super_one.id
        cls.super_two_id = cls.super_two.id
        cls.college_admin_id = cls.college_admin.id

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        self.client = self.app.test_client()
        ChecklistAuditLog.query.delete()
        SemesterChecklistItem.query.delete()
        SemesterReportChecklist.query.delete()
        SemesterChecklist.query.delete()
        ReportChecklistTemplateItem.query.delete()
        ReportChecklistTemplate.query.delete()
        db.session.commit()
        db.session.remove()

        self.super_one = db.session.get(Admin, self.super_one_id)
        self.super_two = db.session.get(Admin, self.super_two_id)
        self.college_admin = db.session.get(Admin, self.college_admin_id)
        self.template = ReportChecklistTemplate(
            slug='verification', title='Verification', version=1,
            is_active=True, reference_only=False, sort_order=1,
        )
        self.template.items = [
            ReportChecklistTemplateItem(
                section_name='Setup', instruction='Create the report.',
                sort_order=1, is_required=True,
            ),
            ReportChecklistTemplateItem(
                section_name='Publish', instruction='Publish the report.',
                sort_order=2, is_required=True,
            ),
        ]
        db.session.add(self.template)
        db.session.commit()

    def login(self, admin):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(admin.id)
            session['_fresh'] = True

    def create_semester(self, term='2027010', name='Spring 2027'):
        return create_semester_checklist(
            term_code=term,
            display_name=name,
            project_name='Spring project',
            actor=self.super_one,
            selected_template_ids=[self.template.id],
        )

    def test_z_seed_imports_all_workbook_templates_once(self):
        ReportChecklistTemplateItem.query.delete()
        ReportChecklistTemplate.query.delete()
        db.session.commit()
        db.session.remove()

        first = seed_checklist_templates()
        second = seed_checklist_templates()

        self.assertEqual(first['created_templates'], 23)
        self.assertEqual(first['created_items'], 1053)
        self.assertEqual(second['created_templates'], 0)
        self.assertEqual(ReportChecklistTemplate.query.filter_by(is_active=True).count(), 21)
        self.assertEqual(ReportChecklistTemplate.query.filter_by(reference_only=True).count(), 2)

    def test_create_semester_uses_frozen_template_snapshots(self):
        semester = self.create_semester()

        self.assertEqual(len(semester.reports), 1)
        self.assertEqual(len(semester.reports[0].items), 2)
        self.assertEqual(semester.reports[0].title_snapshot, 'Verification')
        self.assertEqual(semester.reports[0].items[0].instruction_snapshot, 'Create the report.')
        self.assertEqual(semester.item_counts['total'], 2)
        self.assertEqual(ChecklistAuditLog.query.filter_by(action='created').count(), 1)

    def test_copy_preserves_instructions_and_resets_work_state(self):
        source = self.create_semester()
        source_item = source.reports[0].items[0]
        update_checklist_item(
            item=source_item,
            payload={
                'version': source_item.version,
                'status': 'complete',
                'notes': 'Finished for Spring.',
                'evidence_url': 'https://example.edu/report',
            },
            actor=self.super_one,
        )
        source.reports[0].items[0].instruction_snapshot = 'Special historical wording.'
        db.session.commit()

        copied = create_semester_checklist(
            term_code='2027080', display_name='Fall 2027', project_name='Fall project',
            actor=self.super_two, source_checklist=source,
        )
        copied_item = copied.reports[0].items[0]

        self.assertEqual(copied_item.instruction_snapshot, 'Special historical wording.')
        self.assertEqual(copied_item.status, 'not_started')
        self.assertIsNone(copied_item.creator_id)
        self.assertIsNone(copied_item.completed_at)
        self.assertIsNone(copied_item.notes)
        self.assertIsNone(copied_item.evidence_url)
        self.assertEqual(copied.source_checklist_id, source.id)

    def test_template_edit_does_not_change_existing_semester(self):
        semester = self.create_semester()
        item = self.template.items[0]
        form = MultiDict([
            ('title', 'Verification Updated'),
            ('description', 'Updated template'),
            ('is_active', '1'),
            (f'item_{item.id}_section', 'Setup'),
            (f'item_{item.id}_instruction', 'New future wording.'),
            (f'item_{item.id}_required', '1'),
            (f'item_{self.template.items[1].id}_section', 'Publish'),
            (f'item_{self.template.items[1].id}_instruction', 'Publish the report.'),
            (f'item_{self.template.items[1].id}_required', '1'),
        ])

        changed = update_template(template=self.template, form=form, actor=self.super_one)

        self.assertTrue(changed)
        self.assertEqual(self.template.version, 2)
        self.assertEqual(self.template.items[0].instruction, 'New future wording.')
        self.assertEqual(semester.reports[0].items[0].instruction_snapshot, 'Create the report.')

    def test_item_updates_use_concurrency_and_separate_verifier(self):
        semester = self.create_semester()
        item = semester.reports[0].items[0]
        original_version = item.version

        update_checklist_item(
            item=item,
            payload={'version': original_version, 'status': 'complete'},
            actor=self.super_one,
        )
        with self.assertRaises(ChecklistConflictError):
            update_checklist_item(
                item=item,
                payload={'version': original_version, 'status': 'blocked'},
                actor=self.super_two,
            )
        with self.assertRaises(ChecklistValidationError):
            update_checklist_item(
                item=item,
                payload={'version': item.version, 'status': 'complete', 'verified': True},
                actor=self.super_one,
            )

        db.session.rollback()
        item = db.session.get(SemesterChecklistItem, item.id)
        update_checklist_item(
            item=item,
            payload={'version': item.version, 'status': 'complete', 'verified': True},
            actor=self.super_two,
        )
        self.assertEqual(item.verifier_id, self.super_two.id)
        self.assertIsNotNone(item.verified_at)

    def test_archive_is_read_only_and_reopen_requires_reason(self):
        semester = self.create_semester()
        item = semester.reports[0].items[0]
        archive_checklist(semester=semester, actor=self.super_one)

        with self.assertRaises(ChecklistValidationError):
            update_checklist_item(
                item=item,
                payload={'version': item.version, 'status': 'complete'},
                actor=self.super_one,
            )
        with self.assertRaises(ChecklistValidationError):
            reopen_checklist(semester=semester, actor=self.super_one, reason='')

        reopen_checklist(
            semester=semester, actor=self.super_two, reason='Correct a verified audit note.'
        )
        self.assertEqual(semester.status, 'in_progress')
        reopened = ChecklistAuditLog.query.filter_by(action='reopened').one()
        self.assertEqual(reopened.reason, 'Correct a verified audit note.')

    def test_non_super_admin_cannot_access_pages_or_api(self):
        semester = self.create_semester()
        item = semester.reports[0].items[0]
        self.login(self.college_admin)

        page_response = self.client.get('/checklists/')
        api_response = self.client.post(
            f'/checklists/items/{item.id}/update',
            json={'version': item.version, 'status': 'complete'},
        )

        self.assertEqual(page_response.status_code, 302)
        self.assertEqual(api_response.status_code, 403)

    def test_super_admin_routes_render_and_create(self):
        self.login(self.super_one)
        index_response = self.client.get('/checklists/')
        create_response = self.client.post('/checklists/new', data={
            'term_code': '2027010',
            'display_name': 'Spring 2027',
            'project_name': 'Spring project',
            'source_mode': 'templates',
            'template_id': str(self.template.id),
        })

        self.assertEqual(index_response.status_code, 200)
        self.assertIn(b'TCE Report Checklists', index_response.data)
        self.assertEqual(create_response.status_code, 302)
        semester = SemesterChecklist.query.filter_by(term_code='2027010').one()
        detail_response = self.client.get(f'/checklists/{semester.id}')
        audit_response = self.client.get(f'/checklists/{semester.id}/audit')
        template_response = self.client.get(f'/checklists/templates/{self.template.id}/edit')
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(b'Create the report.', detail_response.data)
        self.assertEqual(audit_response.status_code, 200)
        self.assertEqual(template_response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
