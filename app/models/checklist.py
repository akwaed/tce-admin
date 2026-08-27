"""Models for reusable TCE report checklist templates and semester runs."""

from datetime import datetime, timezone
import json

from app.models import db


UTC = timezone.utc


def utcnow():
    """Return a naive UTC datetime, matching the application's DB convention."""
    return datetime.now(UTC).replace(tzinfo=None)


class ReportChecklistTemplate(db.Model):
    """Reusable definition for one TCE report checklist."""

    __tablename__ = 'report_checklist_templates'

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    version = db.Column(db.Integer, nullable=False, default=1)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    reference_only = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    source_note = db.Column(db.String(500))
    created_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    created_by = db.relationship('Admin', foreign_keys=[created_by_id])
    items = db.relationship(
        'ReportChecklistTemplateItem',
        back_populates='template',
        cascade='all, delete-orphan',
        order_by='ReportChecklistTemplateItem.sort_order',
    )

    def __repr__(self):
        return f'<ReportChecklistTemplate {self.slug} v{self.version}>'


class ReportChecklistTemplateItem(db.Model):
    """One ordered instruction in a report checklist template."""

    __tablename__ = 'report_checklist_template_items'

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(
        db.Integer,
        db.ForeignKey('report_checklist_templates.id'),
        nullable=False,
        index=True,
    )
    section_name = db.Column(db.String(150), nullable=False, default='Setup')
    instruction = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_required = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    template = db.relationship('ReportChecklistTemplate', back_populates='items')


class SemesterChecklist(db.Model):
    """A frozen, collaborative checklist for one academic semester."""

    __tablename__ = 'semester_checklists'

    STATUS_DRAFT = 'draft'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_READY = 'ready_for_verification'
    STATUS_VERIFIED = 'verified'
    STATUS_PUBLISHED = 'published'
    STATUS_ARCHIVED = 'archived'

    id = db.Column(db.Integer, primary_key=True)
    term_code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(150), nullable=False)
    project_name = db.Column(db.String(200))
    status = db.Column(db.String(30), nullable=False, default=STATUS_DRAFT, index=True)
    source_checklist_id = db.Column(db.Integer, db.ForeignKey('semester_checklists.id'))
    created_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    archived_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    archived_at = db.Column(db.DateTime)

    source_checklist = db.relationship(
        'SemesterChecklist', remote_side=[id], foreign_keys=[source_checklist_id]
    )
    created_by = db.relationship('Admin', foreign_keys=[created_by_id])
    updated_by = db.relationship('Admin', foreign_keys=[updated_by_id])
    archived_by = db.relationship('Admin', foreign_keys=[archived_by_id])
    reports = db.relationship(
        'SemesterReportChecklist',
        back_populates='semester_checklist',
        cascade='all, delete-orphan',
        order_by='SemesterReportChecklist.sort_order',
    )

    @property
    def is_archived(self):
        return self.status == self.STATUS_ARCHIVED

    @property
    def item_counts(self):
        total = complete = verified = blocked = 0
        for report in self.reports:
            counts = report.item_counts
            total += counts['total']
            complete += counts['complete']
            verified += counts['verified']
            blocked += counts['blocked']
        return {
            'total': total,
            'complete': complete,
            'verified': verified,
            'blocked': blocked,
        }

    @property
    def progress_percent(self):
        counts = self.item_counts
        return round((counts['complete'] / counts['total']) * 100) if counts['total'] else 0

    @property
    def verified_percent(self):
        counts = self.item_counts
        return round((counts['verified'] / counts['total']) * 100) if counts['total'] else 0

    @property
    def status_display(self):
        return self.status.replace('_', ' ').title()


class SemesterReportChecklist(db.Model):
    """One report checklist snapshot within a semester run."""

    __tablename__ = 'semester_report_checklists'

    id = db.Column(db.Integer, primary_key=True)
    semester_checklist_id = db.Column(
        db.Integer, db.ForeignKey('semester_checklists.id'), nullable=False, index=True
    )
    template_id = db.Column(db.Integer, db.ForeignKey('report_checklist_templates.id'))
    template_version = db.Column(db.Integer, nullable=False, default=1)
    title_snapshot = db.Column(db.String(200), nullable=False)
    description_snapshot = db.Column(db.Text)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(30), nullable=False, default=SemesterChecklist.STATUS_DRAFT)
    published_status = db.Column(db.Boolean, nullable=False, default=False)
    creation_date = db.Column(db.Date)
    verification_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    semester_checklist = db.relationship('SemesterChecklist', back_populates='reports')
    template = db.relationship('ReportChecklistTemplate')
    items = db.relationship(
        'SemesterChecklistItem',
        back_populates='report_checklist',
        cascade='all, delete-orphan',
        order_by='SemesterChecklistItem.sort_order',
    )

    @property
    def item_counts(self):
        required_items = [item for item in self.items if item.is_required]
        return {
            'total': len(required_items),
            'complete': sum(item.is_complete for item in required_items),
            'verified': sum(item.is_verified for item in required_items),
            'blocked': sum(item.status == SemesterChecklistItem.STATUS_BLOCKED for item in required_items),
        }

    @property
    def progress_percent(self):
        counts = self.item_counts
        return round((counts['complete'] / counts['total']) * 100) if counts['total'] else 0

    @property
    def status_display(self):
        return self.status.replace('_', ' ').title()


class SemesterChecklistItem(db.Model):
    """A frozen instruction plus its semester-specific work state."""

    __tablename__ = 'semester_checklist_items'

    STATUS_NOT_STARTED = 'not_started'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETE = 'complete'
    STATUS_BLOCKED = 'blocked'
    STATUS_NOT_APPLICABLE = 'not_applicable'
    VALID_STATUSES = frozenset({
        STATUS_NOT_STARTED,
        STATUS_IN_PROGRESS,
        STATUS_COMPLETE,
        STATUS_BLOCKED,
        STATUS_NOT_APPLICABLE,
    })

    id = db.Column(db.Integer, primary_key=True)
    report_checklist_id = db.Column(
        db.Integer, db.ForeignKey('semester_report_checklists.id'), nullable=False, index=True
    )
    template_item_id = db.Column(db.Integer, db.ForeignKey('report_checklist_template_items.id'))
    section_snapshot = db.Column(db.String(150), nullable=False, default='Setup')
    instruction_snapshot = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_required = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(30), nullable=False, default=STATUS_NOT_STARTED, index=True)
    creator_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    verifier_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    notes = db.Column(db.Text)
    evidence_url = db.Column(db.String(1000))
    completed_at = db.Column(db.DateTime)
    verified_at = db.Column(db.DateTime)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    report_checklist = db.relationship('SemesterReportChecklist', back_populates='items')
    template_item = db.relationship('ReportChecklistTemplateItem')
    creator = db.relationship('Admin', foreign_keys=[creator_id])
    verifier = db.relationship('Admin', foreign_keys=[verifier_id])
    updated_by = db.relationship('Admin', foreign_keys=[updated_by_id])

    @property
    def is_complete(self):
        return self.status in {self.STATUS_COMPLETE, self.STATUS_NOT_APPLICABLE}

    @property
    def is_verified(self):
        return self.is_complete and self.verified_at is not None

    @property
    def status_display(self):
        return self.status.replace('_', ' ').title()


class ChecklistAuditLog(db.Model):
    """Append-only audit record for checklist and template changes."""

    __tablename__ = 'checklist_audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    semester_checklist_id = db.Column(
        db.Integer, db.ForeignKey('semester_checklists.id'), index=True
    )
    actor_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    actor_linkblue = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=False, index=True)
    action = db.Column(db.String(50), nullable=False, index=True)
    old_values_json = db.Column(db.Text)
    new_values_json = db.Column(db.Text)
    reason = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    semester_checklist = db.relationship('SemesterChecklist')
    actor = db.relationship('Admin', foreign_keys=[actor_id])

    @staticmethod
    def _serialize(value):
        return json.dumps(value, default=str, sort_keys=True) if value else None

    @staticmethod
    def _deserialize(value):
        if not value:
            return {}
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}

    @property
    def old_values(self):
        return self._deserialize(self.old_values_json)

    @property
    def new_values(self):
        return self._deserialize(self.new_values_json)

    @property
    def action_display(self):
        return self.action.replace('_', ' ').title()

    @classmethod
    def log(
        cls,
        *,
        actor,
        entity_type,
        entity_id,
        action,
        semester_checklist_id=None,
        old_values=None,
        new_values=None,
        reason=None,
    ):
        log = cls(
            semester_checklist_id=semester_checklist_id,
            actor_id=actor.id if actor else None,
            actor_linkblue=actor.linkblue if actor else 'system',
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            old_values_json=cls._serialize(old_values),
            new_values_json=cls._serialize(new_values),
            reason=reason,
        )
        db.session.add(log)
        return log
