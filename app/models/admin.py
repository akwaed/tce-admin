"""
Admin User Model
Handles TCE administrators at various levels (Super Admin, College Admin, Department Admin)
Also includes AdminAuditLog for tracking admin changes
"""
from datetime import datetime
from app.models import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
import json


# Association table for Admin <-> Department many-to-many relationship
admin_departments = db.Table('admin_departments',
    db.Column('admin_id', db.Integer, db.ForeignKey('admins.id'), primary_key=True),
    db.Column('department_id', db.String(50), db.ForeignKey('departments.id'), primary_key=True)
)


class Admin(UserMixin, db.Model):
    """
    Admin users for TCE system
    
    Roles:
    - super_admin: Full system access, can manage all colleges/departments
    - college_admin: Can manage their college and its departments
    - dept_admin: Can only manage their specific department
    """
    __tablename__ = 'admins'
    
    id = db.Column(db.Integer, primary_key=True)
    linkblue = db.Column(db.String(50), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200))
    
    # Password hash for superadmin fallback (only used for super_admin role)
    password_hash = db.Column(db.String(256))
    
    # Role: super_admin, college_admin, dept_admin
    role = db.Column(db.String(20), nullable=False, default='dept_admin')
    
    # Scope - determines what data this admin can access
    college_code = db.Column(db.String(50), db.ForeignKey('colleges.code'))  # NULL for super_admin (sees all)
    department_id = db.Column(db.String(50), db.ForeignKey('departments.id'))  # NULL for college_admin+ (sees all depts in college)

    # Relationships
    college = db.relationship('College', foreign_keys=[college_code], backref='admins')
    department = db.relationship('Department', foreign_keys=[department_id], backref='admins')

    # Many-to-many relationship for admins managing multiple departments
    departments = db.relationship('Department', secondary=admin_departments,
                                  backref=db.backref('department_admins', lazy='dynamic'),
                                  lazy='dynamic')
    
    # Contact type from legacy system: College, Department, Course Coordinator
    contact_type = db.Column(db.String(30), default='Department')
    
    # Course coordinator specific fields
    course_prefix = db.Column(db.String(10))  # e.g., "UK", "BAE"
    course_number = db.Column(db.String(10))  # e.g., "101", "201"
    
    # Level type from legacy system
    level_type = db.Column(db.String(30), default='Subject Viewer')
    
    # Flags
    is_primary_contact = db.Column(db.Boolean, default=False, index=True)
    has_dashboard_access = db.Column(db.Boolean, default=True)
    has_static_report_access = db.Column(db.Boolean, default=True)
    has_qb_access = db.Column(db.Boolean, default=False)  # Question Bank access
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    
    # Audit fields
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    
    # Relationships
    created_by = db.relationship('Admin', remote_side=[id], backref='created_admins')
    
    def __repr__(self):
        return f'<Admin {self.linkblue}: {self.first_name} {self.last_name}>'
    
    def set_password(self, password):
        """Set password hash (only for super_admin)"""
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')
    
    def check_password(self, password):
        """Check password (only for super_admin)"""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
    @property
    def display_role(self):
        """Human-readable role name"""
        role_names = {
            'super_admin': 'Super Administrator',
            'college_admin': 'College Administrator',
            'dept_admin': 'Department Administrator'
        }
        return role_names.get(self.role, self.role)
    
    @property
    def scope_display(self):
        """Display the scope this admin can access"""
        if self.role == 'super_admin':
            return 'All Colleges'
        elif self.role == 'college_admin':
            return self.college_code or 'Unknown College'
        else:
            # Check for multiple departments first
            dept_list = self.departments.all()
            if dept_list:
                dept_names = [d.name for d in dept_list]
                return f"{self.college_code} - {', '.join(dept_names)}"
            elif self.department_id:
                return f"{self.college_code} - {self.department_id}"
            return self.college_code

    @property
    def department_ids(self):
        """Get list of all department IDs this admin can access"""
        dept_list = self.departments.all()
        if dept_list:
            return [d.id for d in dept_list]
        elif self.department_id:
            return [self.department_id]
        return []

    @property
    def department_names(self):
        """Get list of all department names this admin can access"""
        dept_list = self.departments.all()
        if dept_list:
            return [d.name for d in dept_list]
        elif self.department:
            return [self.department.name]
        elif self.department_id:
            return [self.department_id]
        return []
    
    # Permission check methods
    def is_super_admin(self):
        return self.role == 'super_admin'
    
    def is_college_admin(self):
        return self.role in ['super_admin', 'college_admin']
    
    def can_view_college(self, college_code):
        """Check if admin can view data for a specific college"""
        if self.role == 'super_admin':
            return True
        return self.college_code == college_code
    
    def can_view_department(self, college_code, department_id):
        """Check if admin can view data for a specific department"""
        if self.role == 'super_admin':
            return True
        if self.role == 'college_admin' and self.college_code == college_code:
            return True
        if self.role == 'dept_admin':
            if self.college_code != college_code:
                return False
            # Check against multiple departments
            admin_dept_ids = self.department_ids
            if admin_dept_ids:
                return department_id in admin_dept_ids
            return self.department_id == department_id
        return False
    
    def can_edit_admin(self, other_admin):
        """Check if this admin can edit another admin"""
        if self.role == 'super_admin':
            return True
        
        if self.role == 'college_admin':
            # Only primary college admins can edit within their college
            if self.is_primary_contact:
                return other_admin.college_code == self.college_code
            return False
        
        # Dept admins cannot edit anyone
        return False
    
    def can_export_admins(self):
        """Only super admins can export the full admin list"""
        return self.role == 'super_admin'
    
    def can_manage_qb(self):
        """Check if admin can access Question Bank"""
        if self.role == 'super_admin':
            return True
        return self.has_qb_access
    
    def can_approve_questions(self):
        """Check if admin can approve pending questions"""
        return self.role in ['super_admin', 'college_admin'] and self.is_primary_contact
    
    def get_visible_admins_query(self):
        """Get query for admins this user can see"""
        if self.role == 'super_admin':
            return Admin.query
        elif self.role == 'college_admin':
            return Admin.query.filter_by(college_code=self.college_code)
        else:
            # Dept admin can see admins in any of their departments
            admin_dept_ids = self.department_ids
            if admin_dept_ids:
                from app.models.course import Department
                # Include admins whose department_id matches OR who have overlapping departments
                return Admin.query.filter(
                    Admin.college_code == self.college_code,
                    db.or_(
                        Admin.department_id.in_(admin_dept_ids),
                        Admin.departments.any(Department.id.in_(admin_dept_ids))
                    )
                )
            return Admin.query.filter_by(
                college_code=self.college_code,
                department_id=self.department_id
            )
    
    @property
    def is_course_coordinator(self):
        """Check if this admin is a course coordinator (has course assignments or legacy fields)

        Note: 'All' prefix is treated as a departmental contact, not a course coordinator.
        This handles legacy data where contacts had 'All' set as their prefix.
        """
        # Check new assignment table first
        if self.course_assignments.count() > 0:
            return True
        # Fallback to legacy fields - but 'All' prefix means departmental contact, not course coordinator
        if self.course_prefix and self.course_prefix.upper() not in ('ALL', '*'):
            return True
        return self.contact_type == 'Course Coordinator' and self.course_prefix and self.course_prefix.upper() not in ('ALL', '*')

    @property
    def all_course_patterns(self):
        """Get all course patterns this admin coordinates (from both new table and legacy fields)

        Note: 'All' prefix is excluded as it represents departmental access, not course coordination.
        """
        patterns = []

        # From new assignment table
        for assignment in self.course_assignments.all():
            patterns.append({
                'prefix': assignment.course_prefix,
                'number': assignment.course_number,
                'is_wildcard': assignment.is_wildcard,
                'department_id': assignment.department_id
            })

        # From legacy fields (if not already covered) - exclude 'All' prefix
        if self.course_prefix and not patterns and self.course_prefix.upper() not in ('ALL', '*'):
            patterns.append({
                'prefix': self.course_prefix,
                'number': self.course_number,
                'is_wildcard': not self.course_number,
                'department_id': self.department_id
            })

        return patterns

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'linkblue': self.linkblue,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email,
            'role': self.role,
            'college_code': self.college_code,
            'department_id': self.department_id,
            'department_ids': self.department_ids,
            'department_names': self.department_names,
            'contact_type': self.contact_type,
            'is_primary_contact': self.is_primary_contact,
            'has_dashboard_access': self.has_dashboard_access,
            'has_static_report_access': self.has_static_report_access,
            'has_qb_access': self.has_qb_access,
            'level_type': self.level_type,
            'is_active': self.is_active,
            'is_course_coordinator': self.is_course_coordinator,
            'course_assignments': [a.to_dict() for a in self.course_assignments.all()]
        }
    
    def to_csv_row(self):
        """Convert to CSV row format (for export)"""
        # For multiple departments, join with semicolon
        dept_ids = self.department_ids
        if dept_ids:
            dept_value = ';'.join(dept_ids)
        elif self.department_id:
            dept_value = self.department_id
        else:
            dept_value = 'All'

        return {
            'id': self.id,
            'linkblue': self.linkblue,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'primary_contact': 'true' if self.is_primary_contact else 'false',
            'contact_type': self.contact_type,
            'college': self.college_code,
            'department': dept_value,
            'course': self.course_number or '',
            'prefix': self.course_prefix or '',
            'level_type': self.level_type,
            'has_static_report_access': 'true' if self.has_static_report_access else 'false'
        }
    
    @staticmethod
    def from_csv_row(row, created_by_id=None, dept_name_to_id_map=None, college_name_to_code_map=None):
        """Create Admin from CSV row (for import)

        Args:
            row: CSV row dict
            created_by_id: ID of admin performing import
            dept_name_to_id_map: Dict mapping department names to CLASS_DEPARTMENT_ID
            college_name_to_code_map: Dict mapping college names to college codes
        """
        from app.models.course import Department, College  # Import here to avoid circular import

        # Determine role based on contact_type and department
        contact_type = row.get('contact_type', 'Department')
        department = row.get('department', '').strip()
        college_value = row.get('college', '').strip()

        # Map college name to college code
        college_code = None
        if college_value:
            # First try the provided mapping
            if college_name_to_code_map and college_value in college_name_to_code_map:
                college_code = college_name_to_code_map[college_value]
            elif college_name_to_code_map and college_value.lower() in college_name_to_code_map:
                college_code = college_name_to_code_map[college_value.lower()]
            else:
                # Try to look up by name in College table
                college = College.query.filter_by(name=college_value).first()
                if college:
                    college_code = college.code
                else:
                    # Try case-insensitive match
                    college = College.query.filter(
                        db.func.lower(College.name) == college_value.lower()
                    ).first()
                    if college:
                        college_code = college.code
                    else:
                        # Check if it's already a valid code
                        college = College.query.filter_by(code=college_value).first()
                        if college:
                            college_code = college.code
                        else:
                            # Last resort: use as-is (will likely fail foreign key)
                            college_code = college_value

        def map_department_id(department_value):
            if not department_value or department_value.lower() == 'all':
                return None

            # First try the provided mapping (exact match)
            if dept_name_to_id_map and department_value in dept_name_to_id_map:
                return dept_name_to_id_map[department_value]
            # Try lowercase version of mapping
            if dept_name_to_id_map and department_value.lower() in dept_name_to_id_map:
                return dept_name_to_id_map[department_value.lower()]

            # Try to look up by name in Department table
            dept = Department.query.filter_by(name=department_value).first()
            if dept:
                return dept.id

            # Also try case-insensitive match
            dept = Department.query.filter(
                db.func.lower(Department.name) == department_value.lower()
            ).first()
            if dept:
                return dept.id

            return None

        if contact_type == 'College':
            role = 'college_admin'
            dept_id = None
        elif contact_type == 'Course Coordinator':
            role = 'dept_admin'
            dept_id = map_department_id(department)
        else:
            dept_id = map_department_id(department)
            if department.lower() == 'all' or not department:
                role = 'college_admin'
                dept_id = None
            elif dept_id:
                role = 'dept_admin'
            else:
                # If still not found, leave as None and set role to college_admin
                # This prevents foreign key violations - department contacts without
                # a valid department become college-level contacts
                dept_id = None
                role = 'college_admin'

        admin = Admin(
            linkblue=row['linkblue'].lower().strip(),
            first_name=row['first_name'].strip(),
            last_name=row['last_name'].strip(),
            role=role,
            college_code=college_code,
            department_id=dept_id,
            contact_type=contact_type,
            course_prefix=row.get('prefix', '').strip() or None,
            course_number=row.get('course', '').strip() or None,
            level_type=row.get('level_type', 'Subject Viewer').strip(),
            is_primary_contact=str(row.get('primary_contact', 'false')).lower() == 'true',
            has_dashboard_access=True,
            has_static_report_access=True,
            has_qb_access=False,
            created_by_id=created_by_id
        )
        return admin


class CourseCoordinatorAssignment(db.Model):
    """
    Course Coordinator Assignments - allows a single admin (linkblue) to have multiple
    course coordinator assignments without violating the unique linkblue constraint.

    Each assignment links an admin to a course prefix and optional course number.
    - If course_number is NULL, it's a wildcard for all courses with that prefix
    - If course_number is provided, it's specific to that course pattern
    """
    __tablename__ = 'course_coordinator_assignments'

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False, index=True)

    # Course assignment - prefix is required, number is optional (wildcard if NULL)
    course_prefix = db.Column(db.String(10), nullable=False)  # e.g., "UK", "BAE"
    course_number = db.Column(db.String(10))  # e.g., "101", "201" - NULL means all courses with prefix

    # Optional department scope for fail-safe validation
    department_id = db.Column(db.String(50), db.ForeignKey('departments.id'))

    # Audit fields
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'))

    # Relationships
    admin = db.relationship('Admin', foreign_keys=[admin_id], backref=db.backref(
        'course_assignments', lazy='dynamic', cascade='all, delete-orphan'
    ))
    department = db.relationship('Department', foreign_keys=[department_id])
    created_by = db.relationship('Admin', foreign_keys=[created_by_id])

    def __repr__(self):
        course = f"{self.course_prefix} {self.course_number}" if self.course_number else f"{self.course_prefix} (all)"
        return f'<CourseCoordinatorAssignment {self.admin_id}: {course}>'

    @property
    def course_pattern(self):
        """Get the course pattern for matching"""
        if self.course_number:
            return f"{self.course_prefix} {self.course_number}"
        return self.course_prefix

    @property
    def is_wildcard(self):
        """Check if this is a wildcard assignment (prefix only)"""
        return not self.course_number

    @property
    def display_name(self):
        """Human-readable display name"""
        if self.course_number:
            return f"{self.course_prefix} {self.course_number}"
        return f"{self.course_prefix} (all courses)"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'admin_id': self.admin_id,
            'course_prefix': self.course_prefix,
            'course_number': self.course_number,
            'department_id': self.department_id,
            'is_wildcard': self.is_wildcard,
            'display_name': self.display_name,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AdminAuditLog(db.Model):
    """
    Audit trail for admin changes
    Tracks all modifications to the admin list including:
    - Admin created/updated/deleted
    - Role changes
    - Permission changes
    """
    __tablename__ = 'admin_audit_logs'

    id = db.Column(db.Integer, primary_key=True)

    # Who was affected
    target_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    target_linkblue = db.Column(db.String(50), nullable=False)  # Store linkblue in case admin is deleted

    # Who made the change
    actor_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    actor_linkblue = db.Column(db.String(50), nullable=False)

    # What happened
    action = db.Column(db.String(50), nullable=False)  # created, updated, deleted, role_changed, activated, deactivated, imported

    # When
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Details of the change (JSON)
    changes_json = db.Column(db.Text)

    # Relationships
    target_admin = db.relationship('Admin', foreign_keys=[target_admin_id], backref='audit_logs')
    actor_admin = db.relationship('Admin', foreign_keys=[actor_admin_id])

    def __repr__(self):
        return f'<AdminAuditLog {self.id}: {self.action} on {self.target_linkblue} by {self.actor_linkblue}>'

    @property
    def changes(self):
        """Parse changes JSON"""
        if self.changes_json:
            return json.loads(self.changes_json)
        return {}

    @changes.setter
    def changes(self, value):
        """Set changes from dict"""
        self.changes_json = json.dumps(value) if value else None

    @property
    def action_display(self):
        """Human-readable action name"""
        action_names = {
            'created': 'Created',
            'updated': 'Updated',
            'deleted': 'Deleted',
            'role_changed': 'Role Changed',
            'activated': 'Activated',
            'deactivated': 'Deactivated',
            'imported': 'Imported via CSV',
            'copied': 'Copied from existing admin',
            'elevated': 'Elevated to Super Admin',
            'demoted': 'Demoted from Super Admin'
        }
        return action_names.get(self.action, self.action)

    @staticmethod
    def log_change(target_admin, actor_admin, action, changes=None):
        """Helper method to create an audit log entry"""
        log = AdminAuditLog(
            target_admin_id=target_admin.id if target_admin else None,
            target_linkblue=target_admin.linkblue if target_admin else 'unknown',
            actor_admin_id=actor_admin.id if actor_admin else None,
            actor_linkblue=actor_admin.linkblue if actor_admin else 'system',
            action=action
        )
        if changes:
            log.changes = changes
        db.session.add(log)
        return log

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'target_linkblue': self.target_linkblue,
            'actor_linkblue': self.actor_linkblue,
            'action': self.action,
            'action_display': self.action_display,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'changes': self.changes
        }
