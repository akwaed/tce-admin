"""
Question Bank Models
For managing additional questions with approval workflow
"""
from datetime import datetime
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
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
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
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    
    __table_args__ = (
        db.UniqueConstraint('mapping_type', 'unit_id', 'placeholder', name='uq_mapping'),
    )
    
    def __repr__(self):
        return f'<QuestionMapping {self.mapping_type}:{self.unit_id} - {self.placeholder}>'
