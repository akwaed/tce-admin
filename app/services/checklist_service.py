"""Business rules for TCE report checklist templates and semester runs."""

from collections import defaultdict
from datetime import date
import json
from pathlib import Path
from urllib.parse import urlparse

from app.models import db
from app.models.admin import Admin
from app.models.checklist import (
    ChecklistAuditLog,
    ReportChecklistTemplate,
    ReportChecklistTemplateItem,
    SemesterChecklist,
    SemesterChecklistItem,
    SemesterReportChecklist,
    utcnow,
)


class ChecklistValidationError(ValueError):
    """Raised when a checklist request violates a user-facing rule."""


class ChecklistConflictError(ChecklistValidationError):
    """Raised when an item was updated by another user first."""


def _seed_path():
    return Path(__file__).resolve().parents[2] / 'data' / 'tce_report_checklist_templates.json'


def seed_checklist_templates(path=None, *, commit=True):
    """Create missing workbook-derived templates without overwriting UI edits."""
    source_path = Path(path) if path else _seed_path()
    if not source_path.exists():
        return {'created_templates': 0, 'created_items': 0, 'missing_seed': True}

    payload = json.loads(source_path.read_text(encoding='utf-8'))
    created_templates = 0
    created_items = 0

    for template_data in payload.get('templates', []):
        template = ReportChecklistTemplate.query.filter_by(
            slug=template_data['slug']
        ).first()
        if template is not None:
            continue

        template = ReportChecklistTemplate(
            slug=template_data['slug'],
            title=template_data['title'],
            description=template_data.get('description'),
            version=template_data.get('version', 1),
            is_active=template_data.get('active', True),
            reference_only=template_data.get('reference_only', False),
            sort_order=template_data.get('sort_order', 0),
            source_note=template_data.get('source_note'),
        )
        db.session.add(template)
        db.session.flush()
        created_templates += 1

        for item_data in template_data.get('items', []):
            db.session.add(ReportChecklistTemplateItem(
                template_id=template.id,
                section_name=item_data.get('section') or 'Setup',
                instruction=item_data['instruction'],
                sort_order=item_data.get('sort_order', 0),
                is_required=item_data.get('required', True),
            ))
            created_items += 1

    if commit:
        db.session.commit()
    return {
        'created_templates': created_templates,
        'created_items': created_items,
        'missing_seed': False,
    }


def _validate_term(term_code, display_name):
    term_code = (term_code or '').strip()
    display_name = (display_name or '').strip()
    if not term_code:
        raise ChecklistValidationError('A semester or term code is required.')
    if not display_name:
        raise ChecklistValidationError('A semester display name is required.')
    if len(term_code) > 50 or len(display_name) > 150:
        raise ChecklistValidationError('The semester code or display name is too long.')
    if SemesterChecklist.query.filter_by(term_code=term_code).first():
        raise ChecklistValidationError(f'A checklist already exists for term {term_code}.')
    return term_code, display_name


def _add_report_snapshot(semester, *, template=None, source_report=None):
    if source_report is not None:
        report = SemesterReportChecklist(
            semester_checklist=semester,
            template_id=source_report.template_id,
            template_version=source_report.template_version,
            title_snapshot=source_report.title_snapshot,
            description_snapshot=source_report.description_snapshot,
            sort_order=source_report.sort_order,
        )
        db.session.add(report)
        source_items = source_report.items
        item_factory = lambda item: SemesterChecklistItem(
            report_checklist=report,
            template_item_id=item.template_item_id,
            section_snapshot=item.section_snapshot,
            instruction_snapshot=item.instruction_snapshot,
            sort_order=item.sort_order,
            is_required=item.is_required,
        )
    else:
        report = SemesterReportChecklist(
            semester_checklist=semester,
            template_id=template.id,
            template_version=template.version,
            title_snapshot=template.title,
            description_snapshot=template.description,
            sort_order=template.sort_order,
        )
        db.session.add(report)
        source_items = template.items
        item_factory = lambda item: SemesterChecklistItem(
            report_checklist=report,
            template_item_id=item.id,
            section_snapshot=item.section_name,
            instruction_snapshot=item.instruction,
            sort_order=item.sort_order,
            is_required=item.is_required,
        )

    for item in source_items:
        db.session.add(item_factory(item))
    return report


def create_semester_checklist(
    *,
    term_code,
    display_name,
    project_name,
    actor,
    source_checklist=None,
    selected_template_ids=None,
):
    """Create a semester from current templates or an exact historical snapshot."""
    term_code, display_name = _validate_term(term_code, display_name)
    semester = SemesterChecklist(
        term_code=term_code,
        display_name=display_name,
        project_name=(project_name or '').strip() or None,
        source_checklist_id=source_checklist.id if source_checklist else None,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    db.session.add(semester)

    if source_checklist:
        for source_report in source_checklist.reports:
            _add_report_snapshot(semester, source_report=source_report)
        action = 'copied'
    else:
        query = ReportChecklistTemplate.query.filter_by(
            is_active=True,
            reference_only=False,
        )
        if selected_template_ids is not None:
            query = query.filter(ReportChecklistTemplate.id.in_(selected_template_ids))
        templates = query.order_by(ReportChecklistTemplate.sort_order).all()
        if not templates:
            raise ChecklistValidationError('No active report checklist templates were selected.')
        for template in templates:
            _add_report_snapshot(semester, template=template)
        action = 'created'

    db.session.flush()
    ChecklistAuditLog.log(
        actor=actor,
        entity_type='semester_checklist',
        entity_id=semester.id,
        action=action,
        semester_checklist_id=semester.id,
        new_values={
            'term_code': semester.term_code,
            'display_name': semester.display_name,
            'project_name': semester.project_name,
            'source_checklist_id': semester.source_checklist_id,
            'report_count': len(semester.reports),
        },
    )
    db.session.commit()
    return semester


def group_report_items(report):
    """Return ordered report items grouped by their frozen section name."""
    groups = defaultdict(list)
    for item in report.items:
        groups[item.section_snapshot or 'Setup'].append(item)
    return list(groups.items())


def _parse_admin_id(value, field_name):
    if value in (None, '', 0, '0'):
        return None
    try:
        admin_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ChecklistValidationError(f'Invalid {field_name}.') from exc
    admin = db.session.get(Admin, admin_id)
    if not admin or not admin.is_active or not admin.is_super_admin():
        raise ChecklistValidationError(f'{field_name.title()} must be an active super administrator.')
    return admin_id


def _validate_evidence_url(value):
    value = (value or '').strip()
    if not value:
        return None
    if len(value) > 1000:
        raise ChecklistValidationError('The evidence link is too long.')
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ChecklistValidationError('Evidence links must start with http:// or https://.')
    return value


def _item_snapshot(item):
    return {
        'status': item.status,
        'creator_id': item.creator_id,
        'verifier_id': item.verifier_id,
        'notes': item.notes,
        'evidence_url': item.evidence_url,
        'completed_at': item.completed_at,
        'verified_at': item.verified_at,
        'version': item.version,
    }


def recalculate_statuses(report):
    """Update report and semester status from required task state."""
    semester = report.semester_checklist
    required = [item for item in report.items if item.is_required]
    any_started = any(item.status != SemesterChecklistItem.STATUS_NOT_STARTED for item in required)
    all_complete = bool(required) and all(item.is_complete for item in required)
    all_verified = bool(required) and all(item.is_verified for item in required)

    if report.published_status and all_verified:
        report.status = SemesterChecklist.STATUS_PUBLISHED
    elif all_verified:
        report.status = SemesterChecklist.STATUS_VERIFIED
    elif all_complete:
        report.status = SemesterChecklist.STATUS_READY
    elif any_started:
        report.status = SemesterChecklist.STATUS_IN_PROGRESS
    else:
        report.status = SemesterChecklist.STATUS_DRAFT

    if semester.is_archived:
        return

    report_statuses = {entry.status for entry in semester.reports}
    if report_statuses and report_statuses == {SemesterChecklist.STATUS_PUBLISHED}:
        semester.status = SemesterChecklist.STATUS_PUBLISHED
    elif report_statuses and report_statuses.issubset({
        SemesterChecklist.STATUS_VERIFIED,
        SemesterChecklist.STATUS_PUBLISHED,
    }):
        semester.status = SemesterChecklist.STATUS_VERIFIED
    elif report_statuses and report_statuses.issubset({
        SemesterChecklist.STATUS_READY,
        SemesterChecklist.STATUS_VERIFIED,
        SemesterChecklist.STATUS_PUBLISHED,
    }):
        semester.status = SemesterChecklist.STATUS_READY
    elif any(status != SemesterChecklist.STATUS_DRAFT for status in report_statuses):
        semester.status = SemesterChecklist.STATUS_IN_PROGRESS
    else:
        semester.status = SemesterChecklist.STATUS_DRAFT


def update_checklist_item(*, item, payload, actor):
    """Update one item with optimistic concurrency and audit logging."""
    semester = item.report_checklist.semester_checklist
    if semester.is_archived:
        raise ChecklistValidationError('Archived checklists are read-only.')

    try:
        expected_version = int(payload.get('version'))
    except (TypeError, ValueError) as exc:
        raise ChecklistValidationError('The item version is required.') from exc
    if expected_version != item.version:
        raise ChecklistConflictError(
            'This task was changed by another administrator. Reload the page and review their update.'
        )

    before = _item_snapshot(item)
    status = payload.get('status', item.status)
    if status not in SemesterChecklistItem.VALID_STATUSES:
        raise ChecklistValidationError('Invalid checklist status.')

    item.status = status
    if 'creator_id' in payload:
        item.creator_id = _parse_admin_id(payload.get('creator_id'), 'creator')
    if 'notes' in payload:
        notes = (payload.get('notes') or '').strip()
        if len(notes) > 10000:
            raise ChecklistValidationError('Notes cannot exceed 10,000 characters.')
        item.notes = notes or None
    if 'evidence_url' in payload:
        item.evidence_url = _validate_evidence_url(payload.get('evidence_url'))

    if item.is_complete:
        if not item.creator_id:
            item.creator_id = actor.id
        if item.completed_at is None:
            item.completed_at = utcnow()
    else:
        item.completed_at = None
        item.verifier_id = None
        item.verified_at = None

    verify_requested = payload.get('verified')
    if verify_requested is True:
        if not item.is_complete:
            raise ChecklistValidationError('Complete the task before verifying it.')
        if item.creator_id == actor.id:
            raise ChecklistValidationError('The creator and verifier must be different people.')
        item.verifier_id = actor.id
        item.verified_at = utcnow()
    elif verify_requested is False:
        item.verifier_id = None
        item.verified_at = None

    if item.verified_at is not None and item.creator_id == item.verifier_id:
        raise ChecklistValidationError('The creator and verifier must be different people.')

    item.updated_by_id = actor.id
    item.version += 1
    item.updated_at = utcnow()
    semester.updated_by_id = actor.id
    semester.updated_at = utcnow()
    recalculate_statuses(item.report_checklist)

    after = _item_snapshot(item)
    changes = {
        key: {'old': before[key], 'new': after[key]}
        for key in before
        if before[key] != after[key]
    }
    if changes:
        ChecklistAuditLog.log(
            actor=actor,
            entity_type='checklist_item',
            entity_id=item.id,
            action='updated',
            semester_checklist_id=semester.id,
            old_values={key: value['old'] for key, value in changes.items()},
            new_values={key: value['new'] for key, value in changes.items()},
        )
    db.session.commit()
    return item


def _parse_date(value, field_name):
    if value in (None, ''):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ChecklistValidationError(f'Invalid {field_name}.') from exc


def update_report_metadata(*, report, payload, actor):
    semester = report.semester_checklist
    if semester.is_archived:
        raise ChecklistValidationError('Archived checklists are read-only.')

    before = {
        'published_status': report.published_status,
        'creation_date': report.creation_date,
        'verification_date': report.verification_date,
    }
    if 'published_status' in payload:
        report.published_status = bool(payload.get('published_status'))
    if 'creation_date' in payload:
        report.creation_date = _parse_date(payload.get('creation_date'), 'creation date')
    if 'verification_date' in payload:
        report.verification_date = _parse_date(payload.get('verification_date'), 'verification date')
    report.updated_at = utcnow()
    semester.updated_by_id = actor.id
    semester.updated_at = utcnow()
    recalculate_statuses(report)

    after = {
        'published_status': report.published_status,
        'creation_date': report.creation_date,
        'verification_date': report.verification_date,
    }
    if before != after:
        ChecklistAuditLog.log(
            actor=actor,
            entity_type='report_checklist',
            entity_id=report.id,
            action='updated',
            semester_checklist_id=semester.id,
            old_values=before,
            new_values=after,
        )
    db.session.commit()
    return report


def archive_checklist(*, semester, actor):
    if semester.is_archived:
        raise ChecklistValidationError('This checklist is already archived.')
    old_status = semester.status
    semester.status = SemesterChecklist.STATUS_ARCHIVED
    semester.archived_at = utcnow()
    semester.archived_by_id = actor.id
    semester.updated_by_id = actor.id
    ChecklistAuditLog.log(
        actor=actor,
        entity_type='semester_checklist',
        entity_id=semester.id,
        action='archived',
        semester_checklist_id=semester.id,
        old_values={'status': old_status},
        new_values={'status': semester.status},
    )
    db.session.commit()


def reopen_checklist(*, semester, actor, reason):
    reason = (reason or '').strip()
    if not semester.is_archived:
        raise ChecklistValidationError('Only archived checklists can be reopened.')
    if not reason:
        raise ChecklistValidationError('A reason is required to reopen an archived checklist.')
    semester.status = SemesterChecklist.STATUS_IN_PROGRESS
    semester.archived_at = None
    semester.archived_by_id = None
    semester.updated_by_id = actor.id
    ChecklistAuditLog.log(
        actor=actor,
        entity_type='semester_checklist',
        entity_id=semester.id,
        action='reopened',
        semester_checklist_id=semester.id,
        old_values={'status': SemesterChecklist.STATUS_ARCHIVED},
        new_values={'status': semester.status},
        reason=reason,
    )
    db.session.commit()


def update_template(*, template, form, actor):
    """Apply a versioned template edit while preserving existing snapshots."""
    before = {
        'title': template.title,
        'description': template.description,
        'is_active': template.is_active,
        'reference_only': template.reference_only,
        'version': template.version,
        'item_count': len(template.items),
    }

    title = (form.get('title') or '').strip()
    if not title:
        raise ChecklistValidationError('Template title is required.')
    template.title = title[:200]
    template.description = (form.get('description') or '').strip() or None
    template.is_active = form.get('is_active') == '1'
    template.reference_only = form.get('reference_only') == '1'

    delete_ids = {
        int(value) for value in form.getlist('delete_item_id') if str(value).isdigit()
    }
    changed = before['title'] != template.title or before['description'] != template.description
    changed = changed or before['is_active'] != template.is_active
    changed = changed or before['reference_only'] != template.reference_only
    item_changes = []

    for item in list(template.items):
        if item.id in delete_ids:
            item_changes.append({
                'action': 'deleted',
                'item_id': item.id,
                'section': item.section_name,
                'instruction': item.instruction,
            })
            db.session.delete(item)
            changed = True
            continue
        instruction = (form.get(f'item_{item.id}_instruction') or '').strip()
        section_name = (form.get(f'item_{item.id}_section') or 'Setup').strip()[:150]
        required = form.get(f'item_{item.id}_required') == '1'
        if not instruction:
            raise ChecklistValidationError('Existing instructions cannot be blank; mark them for deletion instead.')
        if (
            instruction != item.instruction
            or section_name != item.section_name
            or required != item.is_required
        ):
            item_changes.append({
                'action': 'updated',
                'item_id': item.id,
                'old': {
                    'section': item.section_name,
                    'instruction': item.instruction,
                    'required': item.is_required,
                },
                'new': {
                    'section': section_name or 'Setup',
                    'instruction': instruction,
                    'required': required,
                },
            })
            item.instruction = instruction
            item.section_name = section_name or 'Setup'
            item.is_required = required
            changed = True

    new_instruction = (form.get('new_instruction') or '').strip()
    if new_instruction:
        max_order = max((item.sort_order for item in template.items), default=0)
        new_section = (form.get('new_section') or 'Setup').strip()[:150] or 'Setup'
        new_required = form.get('new_required') == '1'
        db.session.add(ReportChecklistTemplateItem(
            template=template,
            section_name=new_section,
            instruction=new_instruction,
            sort_order=max_order + 1,
            is_required=new_required,
        ))
        item_changes.append({
            'action': 'added',
            'section': new_section,
            'instruction': new_instruction,
            'required': new_required,
        })
        changed = True

    if not changed:
        return False

    template.version += 1
    template.updated_at = utcnow()
    db.session.flush()
    item_count = ReportChecklistTemplateItem.query.filter_by(template_id=template.id).count()
    if item_count == 0:
        raise ChecklistValidationError('A checklist template must contain at least one instruction.')
    after = {
        'title': template.title,
        'description': template.description,
        'is_active': template.is_active,
        'reference_only': template.reference_only,
        'version': template.version,
        'item_count': item_count,
        'item_changes': item_changes,
    }
    ChecklistAuditLog.log(
        actor=actor,
        entity_type='checklist_template',
        entity_id=template.id,
        action='template_updated',
        old_values=before,
        new_values=after,
    )
    db.session.commit()
    return True
