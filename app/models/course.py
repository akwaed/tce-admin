"""
Course, Instructor, College, and Department Models
For verification reports and data from UKDIG sync
"""
from datetime import datetime
from app.models import db


class College(db.Model):
    """University colleges"""
    __tablename__ = 'colleges'
    
    code = db.Column(db.String(20), primary_key=True)  # CLASS_COLLEGE_SHORT
    name = db.Column(db.String(200), nullable=False)   # CLASS_COLLEGE
    qb_enabled = db.Column(db.Boolean, default=False)  # Question Bank enabled for this college
    
    # Relationships
    departments = db.relationship('Department', backref='college', lazy='dynamic')
    courses = db.relationship('Course', backref='college', lazy='dynamic')
    
    def __repr__(self):
        return f'<College {self.code}: {self.name}>'


class Department(db.Model):
    """University departments"""
    __tablename__ = 'departments'
    
    id = db.Column(db.String(20), primary_key=True)    # CLASS_DEPARTMENT_ID
    name = db.Column(db.String(200), nullable=False)   # CLASS_DEPARTMENT
    college_code = db.Column(db.String(20), db.ForeignKey('colleges.code'), nullable=False)
    
    # Relationships
    courses = db.relationship('Course', backref='department', lazy='dynamic')
    
    def __repr__(self):
        return f'<Department {self.id}: {self.name}>'


class Course(db.Model):
    """
    Course/Section information from UKDIG
    Primary data for verification reports
    """
    __tablename__ = 'courses'
    
    # Primary key - unique section identifier
    section_key = db.Column(db.String(100), primary_key=True)  # SECTION_KEY
    
    # Course identifiers
    class_id = db.Column(db.String(50), index=True)    # CLASS_ID
    class_code = db.Column(db.String(20), index=True)  # CLASS (e.g., "ACC 201")
    section_id = db.Column(db.String(20))              # SECTION_ID (numeric)
    crs_section = db.Column(db.String(50))             # CRS_SECTION (e.g., "A&S111-001")
    section_title = db.Column(db.String(300))          # SECTION_TITLE
    
    # Hierarchy
    college_code = db.Column(db.String(20), db.ForeignKey('colleges.code'), index=True)
    department_id = db.Column(db.String(20), db.ForeignKey('departments.id'), index=True)
    
    # Cross-listing
    crosslisted_id = db.Column(db.String(100), index=True)  # CROSSLISTED_ID
    
    # Dates
    course_start = db.Column(db.Date)
    course_end = db.Column(db.Date)
    tce_start = db.Column(db.Date)
    tce_end = db.Column(db.Date)
    tce_reminder = db.Column(db.Date)
    
    # Status
    marked_for_tce = db.Column(db.Boolean, default=False, index=True)
    student_count = db.Column(db.Integer, default=0)
    
    # Term info (extracted from section_key)
    term_code = db.Column(db.String(10), index=True)  # e.g., "2025010" for Spring 2025
    
    # Sync metadata
    last_synced = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    instructors = db.relationship('Instructor', backref='course', lazy='dynamic',
                                  cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Course {self.class_code} - {self.section_id}>'
    
    @property
    def display_name(self):
        """Full course display name"""
        return f"{self.class_code}-{self.section_id} ({self.section_title})"
    
    @property
    def has_zero_enrollment(self):
        """Check if course has no students"""
        return self.student_count == 0
    
    @property
    def status_display(self):
        """Human-readable TCE status"""
        if not self.marked_for_tce:
            return "Not Marked for TCE"
        if self.has_zero_enrollment:
            return "Zero Enrollment"
        return "Ready"
    
    @property
    def status_class(self):
        """CSS class for status badge"""
        if not self.marked_for_tce:
            return "badge-secondary"
        if self.has_zero_enrollment:
            return "badge-warning"
        return "badge-success"
    
    def to_dict(self):
        """Convert to dictionary for JSON"""
        return {
            'section_key': self.section_key,
            'class_id': self.class_id,
            'class_code': self.class_code,
            'section_id': self.section_id,
            'section_title': self.section_title,
            'college_code': self.college_code,
            'department_id': self.department_id,
            'crosslisted_id': self.crosslisted_id,
            'course_start': self.course_start.isoformat() if self.course_start else None,
            'course_end': self.course_end.isoformat() if self.course_end else None,
            'tce_start': self.tce_start.isoformat() if self.tce_start else None,
            'tce_end': self.tce_end.isoformat() if self.tce_end else None,
            'marked_for_tce': self.marked_for_tce,
            'student_count': self.student_count,
            'instructors': [i.to_dict() for i in self.instructors]
        }


class Instructor(db.Model):
    """
    Instructor assignments from Course_instructor.csv
    Presence in this table indicates course is marked for TCE
    """
    __tablename__ = 'instructors'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Link to course
    section_key = db.Column(db.String(100), db.ForeignKey('courses.section_key'), 
                           nullable=False, index=True)
    
    # Instructor info
    user_id = db.Column(db.String(50), nullable=False, index=True)  # linkblue
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    email = db.Column(db.String(200))
    
    # Role in course
    instructor_role = db.Column(db.String(50))  # Primary, Secondary, etc.
    
    # Sync metadata
    last_synced = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Instructor {self.user_id} for {self.section_key}>'
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email,
            'instructor_role': self.instructor_role
        }


class SyncLog(db.Model):
    """Track data sync operations from UKDIG"""
    __tablename__ = 'sync_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(50))  # 'full', 'courses', 'instructors', etc.
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    status = db.Column(db.String(20))  # 'running', 'completed', 'failed'
    records_processed = db.Column(db.Integer, default=0)
    errors = db.Column(db.Text)  # JSON array of error messages
    
    def __repr__(self):
        return f'<SyncLog {self.id}: {self.sync_type} at {self.started_at}>'
