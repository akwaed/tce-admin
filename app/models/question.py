"""
Question Bank Models
For managing additional questions with approval workflow
"""
from datetime import datetime, timezone
UTC = timezone.utc
from app.models import db
import json


class QuestionBank(db.Model):
    """
    Question bank container - one per college or university-wide
    """
    __tablename__ = 'question_banks'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    college_code = db.Column(db.String(20), db.ForeignKey('colleges.code'))  # NULL = university-wide
    is_enabled = db.Column(db.Boolean, default=True)
    
    # Import tracking
    last_import_at = db.Column(db.DateTime)
    last_import_filename = db.Column(db.String(255))
    last_import_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), onupdate=lambda: datetime.now(UTC).replace(tzinfo=None))
    
    # Relationships
    questions = db.relationship('Question', backref='question_bank', lazy='dynamic',
                               cascade='all, delete-orphan')
    type_definitions = db.relationship('QuestionTypeDefinition', backref='question_bank', 
                                       lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<QuestionBank {self.id}: {self.name}>'


class QuestionTypeDefinition(db.Model):
    """
    Question type definitions (Selection scales, Comment, SectionTitle, PageBreak)
    From the Question Type Definitions sheet in import file
    """
    __tablename__ = 'question_type_definitions'
    
    id = db.Column(db.Integer, primary_key=True)
    question_bank_id = db.Column(db.Integer, db.ForeignKey('question_banks.id'), nullable=False)
    
    # From import file
    type_definition_id = db.Column(db.String(100), nullable=False)  # User-defined ID like "SS_Strongly"
    type_definition_name = db.Column(db.String(200))  # Notes/description
    question_type = db.Column(db.String(50), nullable=False)  # SectionTitle, Comment, PageBreak, Selection
    
    # For Selection type - stored as JSON
    options_json = db.Column(db.Text)  # [{label: "Strongly", score: 5}, ...]
    na_label = db.Column(db.String(100))  # "Not Applicable" label if used
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    
    # Unique constraint
    __table_args__ = (
        db.UniqueConstraint('question_bank_id', 'type_definition_id', name='uq_qb_type_def'),
    )
    
    def __repr__(self):
        return f'<QuestionTypeDefinition {self.type_definition_id}>'
    
    @property
    def options(self):
        """Parse options JSON"""
        if self.options_json:
            return json.loads(self.options_json)
        return []
    
    @options.setter
    def options(self, value):
        """Set options from list"""
        self.options_json = json.dumps(value) if value else None
    
    def to_dict(self):
        return {
            'id': self.id,
            'type_definition_id': self.type_definition_id,
            'type_definition_name': self.type_definition_name,
            'question_type': self.question_type,
            'options': self.options,
            'na_label': self.na_label
        }


class Question(db.Model):
    """
    Individual questions in the question bank
    From the Question Bank Questions sheet in import file
    """
    __tablename__ = 'questions'
    
    id = db.Column(db.Integer, primary_key=True)
    question_bank_id = db.Column(db.Integer, db.ForeignKey('question_banks.id'), nullable=False)
    
    # From import file
    question_id = db.Column(db.String(100), nullable=False, index=True)  # User-defined ID like "ARTSC-001"
    type_definition_id = db.Column(db.String(100), nullable=False)  # References QuestionTypeDefinition
    type_definition_name = db.Column(db.String(200))  # Notes field
    
    question_title = db.Column(db.Text, nullable=False)  # The actual question text
    question_detail = db.Column(db.Text)  # Additional details
    block_title = db.Column(db.String(300))  # Section block title for reports
    
    # Scope - what level this question applies to
    level = db.Column(db.String(20))  # 'college', 'department', 'course', 'section'
    college_code = db.Column(db.String(20))  # If department/course level
    department_id = db.Column(db.String(20))  # If department level
    
    # Approval workflow
    status = db.Column(db.String(20), default='approved', index=True)  # approved, pending, rejected
    submitted_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    reviewed_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.Text)
    
    # Ordering
    sort_order = db.Column(db.Integer, default=0)
    
    # Audit
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), onupdate=lambda: datetime.now(UTC).replace(tzinfo=None))
    
    # Unique constraint
    __table_args__ = (
        db.UniqueConstraint('question_bank_id', 'question_id', name='uq_qb_question'),
    )
    
    # Relationships
    submitted_by = db.relationship('Admin', foreign_keys=[submitted_by_id])
    reviewed_by = db.relationship('Admin', foreign_keys=[reviewed_by_id])
    audit_logs = db.relationship('QuestionAuditLog', backref='question', lazy='dynamic',
                                cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Question {self.question_id}: {self.question_title[:50]}>'
    
    @property
    def is_pending(self):
        return self.status == 'pending'
    
    @property
    def is_approved(self):
        return self.status == 'approved'
    
    def to_dict(self):
        return {
            'id': self.id,
            'question_id': self.question_id,
            'type_definition_id': self.type_definition_id,
            'question_title': self.question_title,
            'question_detail': self.question_detail,
            'block_title': self.block_title,
            'level': self.level,
            'college_code': self.college_code,
            'department_id': self.department_id,
            'status': self.status,
            'sort_order': self.sort_order
        }


class QuestionAuditLog(db.Model):
    """
    Audit trail for question changes
    """
    __tablename__ = 'question_audit_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('questions.id'), nullable=False)
    
    # Action: created, updated, approved, rejected, deleted
    action = db.Column(db.String(30), nullable=False)
    
    # Who performed the action
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    
    # When
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    
    # Details of the change (JSON)
    details_json = db.Column(db.Text)
    
    # Relationships
    admin = db.relationship('Admin')
    
    def __repr__(self):
        return f'<QuestionAuditLog {self.id}: {self.action} at {self.timestamp}>'
    
    @property
    def details(self):
        if self.details_json:
            return json.loads(self.details_json)
        return {}
    
    @details.setter
    def details(self, value):
        self.details_json = json.dumps(value) if value else None


class QuestionMapping(db.Model):
    """
    Maps questions to organizational units (for export to Blue)
    """
    __tablename__ = 'question_mappings'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # What unit this mapping is for
    mapping_type = db.Column(db.String(20), nullable=False)  # DEPARTMENT, COURSE, SECTION
    unit_id = db.Column(db.String(50), nullable=False)  # The ID matching Type
    
    # The placeholder column (e.g., "Dept_Crs_Sel_001")
    placeholder = db.Column(db.String(100), nullable=False)
    
    # The question ID mapped to this placeholder
    question_id = db.Column(db.String(100))  # From Question.question_id
    
    # Audit
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), onupdate=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    
    __table_args__ = (
        db.UniqueConstraint('mapping_type', 'unit_id', 'placeholder', name='uq_mapping'),
    )
    
    def __repr__(self):
        return f'<QuestionMapping {self.mapping_type}:{self.unit_id} - {self.placeholder}>'


class QBAuditLog(db.Model):
    """
    Audit trail for Question Bank and Question Mapping changes
    Tracks all modifications including imports, exports, and edits
    """
    __tablename__ = 'qb_audit_logs_db'

    id = db.Column(db.Integer, primary_key=True)

    # What type of change (qb_import, qb_export, qm_import, qm_export, question_add, question_remove, etc.)
    action = db.Column(db.String(50), nullable=False, index=True)

    # Who made the change
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    admin_linkblue = db.Column(db.String(50), nullable=False)

    # When
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), index=True)

    # Optional: reference to backup created
    backup_id = db.Column(db.Integer, db.ForeignKey('qb_backups.id'))

    # Details of the change (JSON)
    details_json = db.Column(db.Text)

    # Relationships
    admin = db.relationship('Admin')
    backup = db.relationship('QBBackup', backref='audit_log')

    def __repr__(self):
        return f'<QBAuditLog {self.id}: {self.action} by {self.admin_linkblue}>'

    @property
    def details(self):
        """Parse details JSON"""
        if self.details_json:
            return json.loads(self.details_json)
        return {}

    @details.setter
    def details(self, value):
        """Set details from dict"""
        self.details_json = json.dumps(value) if value else None

    @property
    def action_display(self):
        """Human-readable action name"""
        action_names = {
            'qb_import': 'Question Bank Import',
            'qb_export': 'Question Bank Export',
            'qm_import': 'Question Mapping Import',
            'qm_export': 'Question Mapping Export',
            'question_add': 'Question Added',
            'question_remove': 'Question Removed',
            'question_edit': 'Question Edited',
            'question_create': 'New Question Created',
            'change_approved': 'Change Approved',
            'change_rejected': 'Change Rejected',
            'backup_created': 'Backup Created',
            'backup_deleted': 'Backup Deleted'
        }
        return action_names.get(self.action, self.action)

    @staticmethod
    def log_action(action, admin, details=None, backup_id=None):
        """Helper method to create an audit log entry"""
        log = QBAuditLog(
            action=action,
            admin_id=admin.id if admin else None,
            admin_linkblue=admin.linkblue if admin else 'system',
            backup_id=backup_id
        )
        if details:
            log.details = details
        db.session.add(log)
        return log

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'action': self.action,
            'action_display': self.action_display,
            'admin_linkblue': self.admin_linkblue,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'backup_id': self.backup_id,
            'details': self.details
        }


class QBBackup(db.Model):
    """
    Backup records for Question Bank and Question Mapping files
    Stores metadata about backups with files stored on filesystem
    """
    __tablename__ = 'qb_backups'

    id = db.Column(db.Integer, primary_key=True)

    # Type of backup: 'qb' for Question Bank, 'qm' for Question Mapping
    backup_type = db.Column(db.String(10), nullable=False, index=True)

    # Timestamp for the backup (used in filename)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), nullable=False, index=True)

    # Filename of the backup (stored in backups directory)
    filename = db.Column(db.String(255), nullable=False)

    # File size in bytes
    file_size = db.Column(db.Integer)

    # Reason for backup (import, export, change, manual)
    reason = db.Column(db.String(50), nullable=False)

    # Who created the backup
    created_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    created_by_linkblue = db.Column(db.String(50))

    # Additional details (JSON)
    details_json = db.Column(db.Text)

    # Track deletion
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))

    # Relationships
    created_by = db.relationship('Admin', foreign_keys=[created_by_id])
    deleted_by = db.relationship('Admin', foreign_keys=[deleted_by_id])

    def __repr__(self):
        return f'<QBBackup {self.id}: {self.backup_type} - {self.filename}>'

    @property
    def details(self):
        """Parse details JSON"""
        if self.details_json:
            return json.loads(self.details_json)
        return {}

    @details.setter
    def details(self, value):
        """Set details from dict"""
        self.details_json = json.dumps(value) if value else None

    @property
    def backup_type_display(self):
        """Human-readable backup type"""
        return 'Question Bank' if self.backup_type == 'qb' else 'Question Mapping'

    @property
    def reason_display(self):
        """Human-readable reason"""
        reasons = {
            'import': 'Before Import',
            'export': 'On Export',
            'change': 'Before Change',
            'manual': 'Manual Backup'
        }
        return reasons.get(self.reason, self.reason)

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'backup_type': self.backup_type,
            'backup_type_display': self.backup_type_display,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'filename': self.filename,
            'file_size': self.file_size,
            'reason': self.reason,
            'reason_display': self.reason_display,
            'created_by_linkblue': self.created_by_linkblue,
            'is_deleted': self.is_deleted
        }
