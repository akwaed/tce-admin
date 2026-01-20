"""
Admin User Model
Handles TCE administrators at various levels (Super Admin, College Admin, Department Admin)
"""
from datetime import datetime
from app.models import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin


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
            return f"{self.college_code} - {self.department_id}" if self.department_id else self.college_code
    
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
            return self.college_code == college_code and self.department_id == department_id
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
            return Admin.query.filter_by(
                college_code=self.college_code,
                department_id=self.department_id
            )
    
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
            'contact_type': self.contact_type,
            'is_primary_contact': self.is_primary_contact,
            'has_dashboard_access': self.has_dashboard_access,
            'has_static_report_access': self.has_static_report_access,
            'has_qb_access': self.has_qb_access,
            'level_type': self.level_type,
            'is_active': self.is_active
        }
    
    def to_csv_row(self):
        """Convert to CSV row format (for export)"""
        return {
            'id': self.id,
            'linkblue': self.linkblue,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'primary_contact': 'true' if self.is_primary_contact else 'false',
            'contact_type': self.contact_type,
            'college': self.college_code,
            'department': self.department_id or 'All',
            'course': self.course_number or '',
            'prefix': self.course_prefix or '',
            'level_type': self.level_type
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

        if contact_type == 'College' or department.lower() == 'all' or not department:
            role = 'college_admin'
            dept_id = None
        else:
            role = 'dept_admin'
            # Map department NAME to department ID
            dept_id = None

            # First try the provided mapping (exact match)
            if dept_name_to_id_map and department in dept_name_to_id_map:
                dept_id = dept_name_to_id_map[department]
            # Try lowercase version of mapping
            elif dept_name_to_id_map and department.lower() in dept_name_to_id_map:
                dept_id = dept_name_to_id_map[department.lower()]
            else:
                # Try to look up by name in Department table
                dept = Department.query.filter_by(name=department).first()
                if dept:
                    dept_id = dept.id
                else:
                    # Also try case-insensitive match
                    dept = Department.query.filter(
                        db.func.lower(Department.name) == department.lower()
                    ).first()
                    if dept:
                        dept_id = dept.id
                    # If still not found, leave as None and set role to college_admin
                    # This prevents foreign key violations - department contacts without
                    # a valid department become college-level contacts
                    else:
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
