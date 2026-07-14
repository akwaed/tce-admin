"""
Admin Management Routes
CRUD operations for TCE administrators with role-based access control
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.admin import Admin, AdminAuditLog, CourseCoordinatorAssignment
from app.models.course import College, Department, Course
from functools import wraps
import csv
import io
from datetime import datetime

admin_bp = Blueprint('admin', __name__)


def _sync_super_admin_blue_role(linkblue, *, promote=True):
    """Best-effort Users.csv BLUE_ROLE update when super-admin role changes.

    Promotes to 528 or demotes to 23. Failures are logged via flash warning
    but never block the admin CRUD flow — nightly hana_sync re-applies from DB.
    """
    try:
        from app.services.blue_user_roles import (
            SUPER_ADMIN_BLUE_ROLE,
            DEFAULT_STAFF_BLUE_ROLE,
            set_user_blue_role,
        )
        role = SUPER_ADMIN_BLUE_ROLE if promote else DEFAULT_STAFF_BLUE_ROLE
        updated = set_user_blue_role(linkblue, role)
        if updated:
            action = 'promoted to' if promote else 'set to'
            flash(
                f'Users.csv BLUE_ROLE for {linkblue} {action} {role} '
                f'(takes effect in Blue on next Users push).',
                'info',
            )
        elif promote:
            # LinkBlue may not exist in HANA Users yet (e.g. tceadmin fallback).
            flash(
                f'Note: {linkblue} was not found in Users.csv — BLUE_ROLE was not updated. '
                f'If this LinkBlue appears after the next HANA sync, role 528 will be applied then.',
                'warning',
            )
    except Exception as exc:
        flash(
            f'Could not update Users.csv BLUE_ROLE for {linkblue}: {exc}. '
            f'Nightly HANA sync will still apply super-admin roles from the Admin table.',
            'warning',
        )


def validate_course_assignment(course_prefix, course_number, department_ids, require_course=False, validate_department=True):
    """
    Validate a course coordinator assignment.

    Args:
        course_prefix: Course prefix (e.g., "UK", "BAE") - required for course coordinators
        course_number: Course number (e.g., "101") - optional, if omitted acts as wildcard for all courses with prefix
        department_ids: List of department IDs for fail-safe validation
        require_course: If True, at least a prefix is required
        validate_department: If True, verify course exists in selected department(s)

    Returns:
        Error message string if validation fails, None if valid
    """
    # If no prefix provided
    if not course_prefix:
        if require_course:
            return 'Course prefix is required for course coordinators.'
        return None

    # Normalize prefix
    course_prefix = course_prefix.strip().upper()

    # Build search pattern - if number is provided, use it; otherwise search by prefix only
    if course_number and course_number.strip():
        # Specific course pattern (e.g., "UK 101")
        class_pattern = f"{course_prefix} {course_number.strip()}"
        course_query = Course.query.filter(
            Course.class_code.like(f"{class_pattern}%")
        )
    else:
        # Wildcard - all courses with this prefix (e.g., "UK %")
        course_query = Course.query.filter(
            Course.class_code.like(f"{course_prefix} %")
        )
        class_pattern = f"{course_prefix} (all)"

    # Fail-safe: If department_ids provided and validate_department is True,
    # verify the courses are within the selected departments
    if validate_department and department_ids:
        course_query = course_query.filter(Course.department_id.in_(department_ids))

    courses = course_query.all()

    if not courses:
        if validate_department and department_ids:
            return f'No courses found for {class_pattern} in the selected department(s). Ensure the course belongs to your department.'
        return f'No courses found matching {class_pattern}.'

    return None


def get_course_class_ids(course_prefix, course_number=None, department_ids=None):
    """
    Get all class IDs for a course pattern.

    Args:
        course_prefix: Course prefix (e.g., "UK")
        course_number: Course number (optional - if omitted, returns all courses with prefix)
        department_ids: Optional department filter

    Returns:
        Set of class_id values
    """
    if course_number and course_number.strip():
        # Specific course pattern
        class_pattern = f"{course_prefix} {course_number.strip()}"
        course_query = Course.query.filter(
            Course.class_code.like(f"{class_pattern}%")
        )
    else:
        # Wildcard - all courses with this prefix
        course_query = Course.query.filter(
            Course.class_code.like(f"{course_prefix} %")
        )

    if department_ids:
        course_query = course_query.filter(Course.department_id.in_(department_ids))

    class_ids = set()
    for course in course_query.all():
        if course.class_id:
            class_ids.add(course.class_id)

    return class_ids


def admin_required(f):
    """Decorator to ensure user has admin access"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def super_admin_required(f):
    """Decorator for super admin only routes"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_super_admin():
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated_function


@admin_bp.route('/')
@admin_required
def list_admins():
    """List all admins based on user's access level"""
    # Get filter parameters
    college_filter = request.args.get('college', '')
    dept_filter = request.args.get('department', '')
    type_filter = request.args.get('type', '')
    search = request.args.get('search', '').strip().lower()
    primary_filter = request.args.get('primary', '')
    access_filter = request.args.get('access', '')

    # Base query based on user's access
    query = current_user.get_visible_admins_query()

    # Apply filters
    if college_filter:
        query = query.filter(Admin.college_code == college_filter)
    if dept_filter:
        query = query.filter(Admin.department_id == dept_filter)
    if type_filter:
        query = query.filter(Admin.contact_type == type_filter)
    if primary_filter == 'yes':
        query = query.filter(Admin.is_primary_contact == True)
    elif primary_filter == 'no':
        query = query.filter(Admin.is_primary_contact == False)
    if access_filter == 'D':
        query = query.filter(Admin.has_dashboard_access == True)
    elif access_filter == 'S':
        query = query.filter(Admin.has_static_report_access == True)
    elif access_filter == 'QB':
        query = query.filter(Admin.has_qb_access == True)
    if search:
        query = query.filter(
            db.or_(
                Admin.linkblue.ilike(f'%{search}%'),
                Admin.first_name.ilike(f'%{search}%'),
                Admin.last_name.ilike(f'%{search}%')
            )
        )

    # Only show active admins by default
    query = query.filter(Admin.is_active == True)

    admins = query.order_by(Admin.college_code, Admin.department_id, Admin.last_name).all()

    # Get colleges and departments for filter dropdowns
    if current_user.is_super_admin():
        colleges = College.query.order_by(College.name).all()
        departments = Department.query.order_by(Department.name).all()
    elif current_user.is_college_admin():
        colleges = [College.query.get(current_user.college_code)]
        departments = Department.query.filter_by(college_code=current_user.college_code).order_by(Department.name).all()
    else:
        colleges = []
        departments = [Department.query.get(current_user.department_id)] if current_user.department_id else []

    # Get unique colleges from admins for backwards compatibility when no College model data
    unique_colleges = sorted(set(a.college_code for a in Admin.query.distinct(Admin.college_code).all() if a.college_code))

    return render_template('admin/list.html',
                         admins=admins,
                         colleges=colleges,
                         departments=departments,
                         unique_colleges=unique_colleges,
                         current_filters={
                             'college': college_filter,
                             'department': dept_filter,
                             'type': type_filter,
                             'search': search,
                             'primary': primary_filter,
                             'access': access_filter
                         })


@admin_bp.route('/add', methods=['GET', 'POST'])
@admin_required
def add_admin():
    """Add a new admin or add a course assignment to existing admin"""
    # Super admins can always add admins
    # College admins can only add if they are primary contacts
    if current_user.role == 'college_admin' and not current_user.is_primary_contact:
        flash('Only primary college contacts can add new administrators.', 'danger')
        return redirect(url_for('admin.list_admins'))

    # Helper to cache form data for re-rendering on error
    def get_form_data():
        return {
            'linkblue': request.form.get('linkblue', '').strip().lower(),
            'first_name': request.form.get('first_name', '').strip(),
            'last_name': request.form.get('last_name', '').strip(),
            'email': request.form.get('email', '').strip(),
            'admin_role': request.form.get('admin_role', 'regular'),
            'contact_type': request.form.get('contact_type', 'Department'),
            'college': request.form.get('college', '').strip(),
            'departments': request.form.getlist('departments'),
            'primary_contact': request.form.get('primary_contact', 'no'),
            'level_type': request.form.get('level_type', 'Subject Viewer'),
            'prefix': request.form.get('prefix', '').strip(),
            'course': request.form.get('course', '').strip(),
            'is_course_coordinator': request.form.get('is_course_coordinator') == 'on',
            'has_dashboard_access': request.form.get('has_dashboard_access') == 'yes',
            'has_static_report_access': request.form.get('has_static_report_access') == 'yes',
            'has_qb_access': request.form.get('has_qb_access') == 'yes'
        }

    def render_form_with_error(error_msg, form_data=None):
        """Render the form with an error message and cached form data"""
        flash(error_msg, 'danger')
        colleges = get_colleges_for_user()
        departments = get_departments_for_user()
        college_depts = get_college_departments_map()
        return render_template('admin/add.html',
                             colleges=colleges,
                             departments=departments,
                             college_depts=college_depts,
                             form_data=form_data or {})

    if request.method == 'POST':
        # Get and cache form data
        form_data = get_form_data()

        linkblue = form_data['linkblue']
        first_name = form_data['first_name']
        last_name = form_data['last_name']
        email = form_data['email']

        # Check if creating a super admin (only super admins can do this)
        admin_role = form_data['admin_role']
        is_creating_super_admin = admin_role == 'super_admin' and current_user.is_super_admin()

        contact_type = form_data['contact_type']
        college_code = form_data['college']
        # Support multiple department selections
        department_ids = [d.strip() for d in form_data['departments'] if d.strip()]
        is_primary = form_data['primary_contact'] == 'yes'
        level_type = form_data['level_type']
        course_prefix = form_data['prefix'].upper() if form_data['prefix'] else ''
        course_number = form_data['course']
        # 'All' prefix is a legacy value meaning departmental access, not course coordination
        is_all_prefix = course_prefix.upper() in ('ALL', '*', '')
        is_course_coordinator = form_data['is_course_coordinator'] and not is_all_prefix
        # Clear the prefix if it's the special 'All' value
        if course_prefix.upper() in ('ALL', '*'):
            course_prefix = ''

        # Access flags
        has_dashboard = form_data['has_dashboard_access']
        has_static_report = form_data['has_static_report_access']
        has_qb = form_data['has_qb_access'] if current_user.can_grant_qb_access() else False

        # Validation
        if not linkblue or not first_name or not last_name:
            return render_form_with_error('LinkBlue, First Name, and Last Name are required.', form_data)

        # Handle super admin creation
        # No password required - super admins authenticate via Azure AD
        if is_creating_super_admin:
            role = 'super_admin'
            college_code = None
            department_id = None
            contact_type = None
            is_primary = False
            has_qb = True
            is_course_coordinator = False
        else:
            # Permission check - can only add admins within your scope
            if not current_user.is_super_admin():
                if college_code != current_user.college_code:
                    return render_form_with_error('You can only add admins within your college.', form_data)

            # Determine if this is a course coordinator
            if contact_type != 'College' and is_course_coordinator:
                contact_type = 'Course Coordinator'

            # Validate course assignment if this is a course coordinator
            if contact_type == 'Course Coordinator':
                course_error = validate_course_assignment(
                    course_prefix,
                    course_number,  # Can be empty for wildcard
                    department_ids,
                    require_course=True,
                    validate_department=True  # Fail-safe: ensure course is in selected department
                )
                if course_error:
                    return render_form_with_error(course_error, form_data)

            # Determine role based on contact type and departments
            if contact_type == 'College':
                role = 'college_admin'
                department_ids = []
            elif contact_type == 'Course Coordinator':
                role = 'dept_admin'
            else:
                role = 'dept_admin' if department_ids else 'college_admin'

            # Primary contact validation
            if is_primary and contact_type == 'College':
                existing_primary = Admin.query.filter_by(
                    college_code=college_code,
                    contact_type='College',
                    is_primary_contact=True,
                    is_active=True
                ).first()
                if existing_primary:
                    flash(f'College {college_code} already has a primary contact: {existing_primary.full_name}', 'warning')

        # Check for existing admin with same linkblue
        existing = Admin.query.filter_by(linkblue=linkblue).first()

        if existing:
            if existing.is_active:
                # Admin already exists and is active
                # For course coordinators, allow adding another course assignment
                if contact_type == 'Course Coordinator' and course_prefix:
                    # Check if this exact assignment already exists
                    existing_assignment = CourseCoordinatorAssignment.query.filter_by(
                        admin_id=existing.id,
                        course_prefix=course_prefix,
                        course_number=course_number or None
                    ).first()

                    if existing_assignment:
                        return render_form_with_error(
                            f'Admin "{existing.full_name}" already has a course assignment for '
                            f'{course_prefix} {course_number if course_number else "(all)"}.',
                            form_data
                        )

                    # Add new course assignment to existing admin
                    assignment = CourseCoordinatorAssignment(
                        admin_id=existing.id,
                        course_prefix=course_prefix,
                        course_number=course_number or None,
                        department_id=department_ids[0] if department_ids else None,
                        created_by_id=current_user.id
                    )
                    db.session.add(assignment)

                    # Update admin to be a course coordinator if not already
                    if existing.contact_type != 'Course Coordinator':
                        existing.contact_type = 'Course Coordinator'
                        existing.role = 'dept_admin'

                    db.session.commit()

                    # Log the assignment addition
                    AdminAuditLog.log_change(
                        existing, current_user, 'updated',
                        changes={
                            'action': 'course_assignment_added',
                            'course_prefix': course_prefix,
                            'course_number': course_number,
                            'is_wildcard': not course_number
                        }
                    )
                    db.session.commit()

                    flash(f'Course assignment "{course_prefix} {course_number if course_number else "(all)"}" '
                          f'added to existing admin "{existing.full_name}".', 'success')
                    return redirect(url_for('admin.list_admins'))
                else:
                    return render_form_with_error(
                        f'An admin with LinkBlue "{linkblue}" already exists. '
                        f'To add another course coordinator assignment, check the "Course Coordinator" box '
                        f'and provide a course prefix.',
                        form_data
                    )

            # Reactivate and update details for inactive admin
            existing.first_name = first_name
            existing.last_name = last_name
            existing.email = email or f'{linkblue}@uky.edu'
            existing.role = role
            existing.college_code = college_code
            existing.department_id = department_ids[0] if len(department_ids) == 1 else None  # Backwards compat
            existing.contact_type = contact_type
            existing.course_prefix = course_prefix or None
            existing.course_number = course_number or None
            existing.level_type = level_type
            existing.is_primary_contact = is_primary
            existing.has_dashboard_access = has_dashboard
            existing.has_static_report_access = has_static_report
            if current_user.can_grant_qb_access():
                existing.has_qb_access = has_qb
            existing.is_active = True
            if not existing.created_by_id:
                existing.created_by_id = current_user.id

            # Clear and set multiple departments
            existing.departments = []
            if department_ids:
                for dept_id in department_ids:
                    dept = Department.query.get(dept_id)
                    if dept:
                        existing.departments.append(dept)

            # Add course assignment to the new table if course coordinator
            if contact_type == 'Course Coordinator' and course_prefix:
                assignment = CourseCoordinatorAssignment(
                    admin_id=existing.id,
                    course_prefix=course_prefix,
                    course_number=course_number or None,
                    department_id=department_ids[0] if department_ids else None,
                    created_by_id=current_user.id
                )
                db.session.add(assignment)

            db.session.add(existing)
            db.session.commit()

            # Log the reactivation
            AdminAuditLog.log_change(
                existing, current_user, 'activated',
                changes={
                    'first_name': first_name,
                    'last_name': last_name,
                    'role': role,
                    'college_code': college_code,
                    'department_ids': department_ids
                }
            )
            db.session.commit()

            if existing.role == 'super_admin':
                _sync_super_admin_blue_role(existing.linkblue, promote=True)

            flash(f'Admin "{existing.full_name}" reactivated successfully.', 'success')
            return redirect(url_for('admin.list_admins'))

        # Create new admin
        admin = Admin(
            linkblue=linkblue,
            first_name=first_name,
            last_name=last_name,
            email=email or f'{linkblue}@uky.edu',
            role=role,
            college_code=college_code,
            department_id=department_ids[0] if len(department_ids) == 1 else None,  # Backwards compat
            contact_type=contact_type,
            course_prefix=course_prefix or None,
            course_number=course_number or None,
            level_type=level_type,
            is_primary_contact=is_primary,
            has_dashboard_access=has_dashboard,
            has_static_report_access=has_static_report,
            has_qb_access=has_qb,
            created_by_id=current_user.id
        )

        db.session.add(admin)
        db.session.flush()  # Get the admin ID

        # Add multiple departments
        if department_ids:
            for dept_id in department_ids:
                dept = Department.query.get(dept_id)
                if dept:
                    admin.departments.append(dept)

        # Add course assignment to the new table if course coordinator
        if contact_type == 'Course Coordinator' and course_prefix:
            assignment = CourseCoordinatorAssignment(
                admin_id=admin.id,
                course_prefix=course_prefix,
                course_number=course_number or None,
                department_id=department_ids[0] if department_ids else None,
                created_by_id=current_user.id
            )
            db.session.add(assignment)

        db.session.commit()

        # Log the creation
        AdminAuditLog.log_change(
            admin, current_user, 'created',
            changes={
                'linkblue': admin.linkblue,
                'first_name': admin.first_name,
                'last_name': admin.last_name,
                'role': admin.role,
                'college_code': admin.college_code,
                'department_ids': department_ids,
                'contact_type': admin.contact_type,
                'is_primary_contact': admin.is_primary_contact,
                'has_dashboard_access': admin.has_dashboard_access,
                'has_static_report_access': admin.has_static_report_access,
                'has_qb_access': admin.has_qb_access,
                'course_prefix': course_prefix if contact_type == 'Course Coordinator' else None,
                'course_number': course_number if contact_type == 'Course Coordinator' else None
            }
        )
        db.session.commit()

        # Super admins need BLUE_ROLE=528 in Users.csv for Explorance Blue.
        if admin.role == 'super_admin':
            _sync_super_admin_blue_role(admin.linkblue, promote=True)

        flash(f'Admin "{admin.full_name}" created successfully.', 'success')
        return redirect(url_for('admin.list_admins'))

    # GET - show form
    colleges = get_colleges_for_user()
    departments = get_departments_for_user()
    college_depts = get_college_departments_map()

    return render_template('admin/add.html',
                         colleges=colleges,
                         departments=departments,
                         college_depts=college_depts,
                         form_data={})


@admin_bp.route('/copy/<int:admin_id>', methods=['GET', 'POST'])
@admin_required
def copy_admin(admin_id):
    """Copy an existing admin's settings to create a new admin"""
    source_admin = Admin.query.get_or_404(admin_id)

    # Permission check - must be able to edit the source admin to copy them
    if not current_user.can_edit_admin(source_admin):
        flash('You do not have permission to copy this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))

    if request.method == 'POST':
        # Get the new admin's basic info
        linkblue = request.form.get('linkblue', '').strip().lower()
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip()

        # Validation
        if not linkblue or not first_name or not last_name:
            flash('LinkBlue, First Name, and Last Name are required.', 'danger')
            return redirect(url_for('admin.copy_admin', admin_id=admin_id))

        # Check for duplicate
        existing = Admin.query.filter_by(linkblue=linkblue).first()
        if existing:
            if existing.is_active:
                flash(f'An admin with LinkBlue "{linkblue}" already exists.', 'danger')
                return redirect(url_for('admin.copy_admin', admin_id=admin_id))
            else:
                # Reactivate with copied settings
                existing.first_name = first_name
                existing.last_name = last_name
                existing.email = email or f'{linkblue}@uky.edu'
                existing.role = source_admin.role
                existing.college_code = source_admin.college_code
                existing.department_id = source_admin.department_id
                existing.contact_type = source_admin.contact_type
                existing.course_prefix = source_admin.course_prefix
                existing.course_number = source_admin.course_number
                existing.level_type = source_admin.level_type
                existing.is_primary_contact = False  # Never copy primary contact status
                existing.has_dashboard_access = source_admin.has_dashboard_access
                existing.has_static_report_access = source_admin.has_static_report_access
                if current_user.can_grant_qb_access():
                    existing.has_qb_access = source_admin.has_qb_access
                existing.is_active = True
                if not existing.created_by_id:
                    existing.created_by_id = current_user.id

                # Copy multiple departments
                existing.departments = []
                for dept in source_admin.departments.all():
                    existing.departments.append(dept)

                db.session.commit()

                # Log the copy/reactivation
                AdminAuditLog.log_change(
                    existing, current_user, 'copied',
                    changes={
                        'copied_from': source_admin.linkblue,
                        'role': existing.role,
                        'college_code': existing.college_code,
                        'reactivated': True
                    }
                )
                db.session.commit()

                flash(f'Admin "{existing.full_name}" reactivated with copied settings from {source_admin.full_name}.', 'success')
                return redirect(url_for('admin.list_admins'))

        # Create new admin with copied settings
        new_admin = Admin(
            linkblue=linkblue,
            first_name=first_name,
            last_name=last_name,
            email=email or f'{linkblue}@uky.edu',
            role=source_admin.role,
            college_code=source_admin.college_code,
            department_id=source_admin.department_id,
            contact_type=source_admin.contact_type,
            course_prefix=source_admin.course_prefix,
            course_number=source_admin.course_number,
            level_type=source_admin.level_type,
            is_primary_contact=False,  # Never copy primary contact status
            has_dashboard_access=source_admin.has_dashboard_access,
            has_static_report_access=source_admin.has_static_report_access,
            has_qb_access=source_admin.has_qb_access if current_user.can_grant_qb_access() else False,
            created_by_id=current_user.id
        )

        db.session.add(new_admin)
        db.session.flush()

        # Copy multiple departments
        for dept in source_admin.departments.all():
            new_admin.departments.append(dept)

        db.session.commit()

        # Log the copy
        AdminAuditLog.log_change(
            new_admin, current_user, 'copied',
            changes={
                'copied_from': source_admin.linkblue,
                'linkblue': new_admin.linkblue,
                'first_name': new_admin.first_name,
                'last_name': new_admin.last_name,
                'role': new_admin.role,
                'college_code': new_admin.college_code
            }
        )
        db.session.commit()

        flash(f'Admin "{new_admin.full_name}" created with copied settings from {source_admin.full_name}.', 'success')
        return redirect(url_for('admin.list_admins'))

    # GET - show copy form
    return render_template('admin/copy.html', source_admin=source_admin)


@admin_bp.route('/edit/<int:admin_id>', methods=['GET', 'POST'])
@admin_required
def edit_admin(admin_id):
    """Edit an existing admin"""
    admin = Admin.query.get_or_404(admin_id)

    # Permission check
    if not current_user.can_edit_admin(admin):
        flash('You do not have permission to edit this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))

    # Helper to cache form data for re-rendering on error
    def get_form_data():
        return {
            'linkblue': request.form.get('linkblue', admin.linkblue).strip().lower(),
            'first_name': request.form.get('first_name', admin.first_name).strip(),
            'last_name': request.form.get('last_name', admin.last_name).strip(),
            'email': request.form.get('email', admin.email or '').strip(),
            'admin_role': request.form.get('admin_role', 'super_admin' if admin.role == 'super_admin' else 'regular'),
            'contact_type': request.form.get('contact_type', admin.contact_type),
            'college': request.form.get('college', admin.college_code or '').strip(),
            'departments': request.form.getlist('departments') or admin.department_ids,
            'primary_contact': request.form.get('primary_contact', 'yes' if admin.is_primary_contact else 'no'),
            'level_type': request.form.get('level_type', admin.level_type),
            'prefix': request.form.get('prefix', admin.course_prefix or '').strip(),
            'course': request.form.get('course', admin.course_number or '').strip(),
            'has_dashboard_access': request.form.get('has_dashboard_access') == 'yes' if request.form else admin.has_dashboard_access,
            'has_static_report_access': request.form.get('has_static_report_access') == 'yes' if request.form else admin.has_static_report_access,
            'has_qb_access': request.form.get('has_qb_access') == 'yes' if request.form else admin.has_qb_access
        }

    def render_form_with_error(error_msg, form_data=None):
        """Render the form with an error message and cached form data"""
        flash(error_msg, 'danger')
        colleges = get_colleges_for_user()
        departments = get_departments_for_user()
        college_depts = get_college_departments_map()
        return render_template('admin/edit.html',
                             admin=admin,
                             colleges=colleges,
                             departments=departments,
                             college_depts=college_depts,
                             form_data=form_data or {})

    if request.method == 'POST':
        form_data = get_form_data()

        # Store original values for audit logging
        original_values = {
            'linkblue': admin.linkblue,
            'first_name': admin.first_name,
            'last_name': admin.last_name,
            'email': admin.email,
            'role': admin.role,
            'college_code': admin.college_code,
            'department_id': admin.department_id,
            'contact_type': admin.contact_type,
            'is_primary_contact': admin.is_primary_contact,
            'has_dashboard_access': admin.has_dashboard_access,
            'has_static_report_access': admin.has_static_report_access,
            'has_qb_access': admin.has_qb_access
        }

        # Get form data
        admin.linkblue = form_data['linkblue']
        admin.first_name = form_data['first_name']
        admin.last_name = form_data['last_name']
        admin.email = form_data['email']

        # Handle role change (only super admins can change roles, and not their own)
        if current_user.is_super_admin() and admin.id != current_user.id:
            new_admin_role = form_data['admin_role']
            was_super_admin = admin.role == 'super_admin'
            is_becoming_super_admin = new_admin_role == 'super_admin'

            if is_becoming_super_admin and not was_super_admin:
                # Elevating to super admin
                password = request.form.get('password', '')
                password_confirm = request.form.get('password_confirm', '')

                # Password required when elevating (unless they already have one)
                if password:
                    if len(password) < 8:
                        return render_form_with_error('Password must be at least 8 characters.', form_data)
                    if password != password_confirm:
                        return render_form_with_error('Passwords do not match.', form_data)
                    admin.set_password(password)

                admin.role = 'super_admin'
                admin.college_code = None
                admin.department_id = None
                admin.contact_type = None
                admin.is_primary_contact = False
                admin.has_qb_access = True

                db.session.commit()

                # Log elevation to super admin
                AdminAuditLog.log_change(
                    admin, current_user, 'elevated',
                    changes={
                        'old_role': original_values['role'],
                        'new_role': 'super_admin',
                        'old_college': original_values['college_code']
                    }
                )
                db.session.commit()

                _sync_super_admin_blue_role(admin.linkblue, promote=True)

                flash(f'Admin "{admin.full_name}" elevated to Super Administrator.', 'success')
                return redirect(url_for('admin.list_admins'))

            elif not is_becoming_super_admin and was_super_admin:
                # Demoting from super admin - need college assignment
                new_college = form_data['college']
                if not new_college:
                    return render_form_with_error('Must assign a college when demoting from super admin.', form_data)

                admin.role = 'college_admin'
                admin.college_code = new_college
                admin.contact_type = 'College'

                db.session.commit()

                # Log demotion from super admin
                AdminAuditLog.log_change(
                    admin, current_user, 'demoted',
                    changes={
                        'old_role': 'super_admin',
                        'new_role': 'college_admin',
                        'new_college': new_college
                    }
                )
                db.session.commit()

                _sync_super_admin_blue_role(admin.linkblue, promote=False)

                flash(f'Admin "{admin.full_name}" demoted to College Administrator.', 'success')
                return redirect(url_for('admin.list_admins'))

            elif is_becoming_super_admin and was_super_admin:
                # Already super admin - just update password if provided
                password = request.form.get('password', '')
                if password:
                    password_confirm = request.form.get('password_confirm', '')
                    if len(password) < 8:
                        return render_form_with_error('Password must be at least 8 characters.', form_data)
                    if password != password_confirm:
                        return render_form_with_error('Passwords do not match.', form_data)
                    admin.set_password(password)

                db.session.commit()

                # Log password update
                AdminAuditLog.log_change(
                    admin, current_user, 'updated',
                    changes={'password_changed': True}
                )
                db.session.commit()

                flash(f'Admin "{admin.full_name}" updated successfully.', 'success')
                return redirect(url_for('admin.list_admins'))

        # Regular admin fields (skip if super admin)
        if admin.role != 'super_admin':
            admin.contact_type = form_data['contact_type']
            admin.level_type = form_data['level_type']
            course_prefix = form_data['prefix'].upper() if form_data['prefix'] else None
            course_number = form_data['course'] or None

            # 'All' prefix is a legacy value meaning departmental access, not course coordination
            if course_prefix and course_prefix.upper() in ('ALL', '*'):
                course_prefix = None
                course_number = None

            # College and departments - only super admin can change these freely
            new_college = form_data['college']
            new_department_ids = [d.strip() for d in form_data['departments'] if d and d.strip()]

            # Only treat as course coordinator if there's a valid (non-All) prefix
            if admin.contact_type != 'College' and course_prefix:
                admin.contact_type = 'Course Coordinator'

            # Validate course assignment - course number is now optional (wildcard if empty)
            if admin.contact_type == 'Course Coordinator':
                course_error = validate_course_assignment(
                    course_prefix,
                    course_number,  # Can be empty for wildcard
                    new_department_ids,
                    require_course=True,
                    validate_department=True  # Fail-safe
                )
                if course_error:
                    return render_form_with_error(course_error, form_data)

            # Update legacy course fields
            admin.course_prefix = course_prefix
            admin.course_number = course_number

            if current_user.is_super_admin():
                admin.college_code = new_college
                # Set single department_id for backwards compatibility
                admin.department_id = new_department_ids[0] if len(new_department_ids) == 1 else None
                # Update multiple departments
                admin.departments = []
                for dept_id in new_department_ids:
                    dept = Department.query.get(dept_id)
                    if dept:
                        admin.departments.append(dept)
            elif current_user.is_college_admin() and current_user.is_primary_contact:
                # Primary college admin can reassign within their college
                if new_college == current_user.college_code:
                    admin.department_id = new_department_ids[0] if len(new_department_ids) == 1 else None
                    # Update multiple departments
                    admin.departments = []
                    for dept_id in new_department_ids:
                        dept = Department.query.get(dept_id)
                        if dept:
                            admin.departments.append(dept)

            # Determine role based on contact type and departments
            has_departments = bool(admin.departments.count()) or bool(admin.department_id)
            if admin.contact_type == 'College':
                admin.role = 'college_admin'
                admin.department_id = None
                admin.departments = []
                admin.course_prefix = None
                admin.course_number = None
            elif admin.contact_type == 'Course Coordinator':
                admin.role = 'dept_admin'
            else:
                admin.role = 'dept_admin' if has_departments else 'college_admin'

            # Primary contact - with validation
            is_primary = form_data['primary_contact'] == 'yes'
            if is_primary and admin.contact_type == 'College' and not admin.is_primary_contact:
                existing_primary = Admin.query.filter(
                    Admin.id != admin.id,
                    Admin.college_code == admin.college_code,
                    Admin.contact_type == 'College',
                    Admin.is_primary_contact == True,
                    Admin.is_active == True
                ).first()
                if existing_primary:
                    flash(f'Note: {existing_primary.full_name} is already the primary contact for this college.', 'warning')
            admin.is_primary_contact = is_primary

            # Update course assignments in the new table if course coordinator
            if admin.contact_type == 'Course Coordinator' and course_prefix:
                # Check if assignment already exists
                existing_assignment = CourseCoordinatorAssignment.query.filter_by(
                    admin_id=admin.id,
                    course_prefix=course_prefix,
                    course_number=course_number
                ).first()

                if not existing_assignment:
                    # Add new assignment
                    assignment = CourseCoordinatorAssignment(
                        admin_id=admin.id,
                        course_prefix=course_prefix,
                        course_number=course_number,
                        department_id=new_department_ids[0] if new_department_ids else None,
                        created_by_id=current_user.id
                    )
                    db.session.add(assignment)

        # Access flags
        admin.has_dashboard_access = request.form.get('has_dashboard_access') == 'yes'
        admin.has_static_report_access = request.form.get('has_static_report_access') == 'yes'

        # QB access can only be granted by super admins.
        if current_user.can_grant_qb_access():
            admin.has_qb_access = request.form.get('has_qb_access') == 'yes'

        db.session.commit()

        # Build changes dict for audit log
        changes = {}
        new_values = {
            'linkblue': admin.linkblue,
            'first_name': admin.first_name,
            'last_name': admin.last_name,
            'email': admin.email,
            'role': admin.role,
            'college_code': admin.college_code,
            'department_id': admin.department_id,
            'contact_type': admin.contact_type,
            'is_primary_contact': admin.is_primary_contact,
            'has_dashboard_access': admin.has_dashboard_access,
            'has_static_report_access': admin.has_static_report_access,
            'has_qb_access': admin.has_qb_access
        }
        for key, old_val in original_values.items():
            new_val = new_values.get(key)
            if old_val != new_val:
                changes[key] = {'old': old_val, 'new': new_val}

        if changes:
            AdminAuditLog.log_change(admin, current_user, 'updated', changes=changes)
            db.session.commit()

        flash(f'Admin "{admin.full_name}" updated successfully.', 'success')
        return redirect(url_for('admin.list_admins'))

    # GET - show form
    colleges = get_colleges_for_user()
    departments = get_departments_for_user()
    college_depts = get_college_departments_map()

    return render_template('admin/edit.html',
                         admin=admin,
                         colleges=colleges,
                         departments=departments,
                         college_depts=college_depts,
                         form_data={})


@admin_bp.route('/delete/<int:admin_id>', methods=['POST'])
@admin_required
def delete_admin(admin_id):
    """Soft delete an admin (sets is_active=False)"""
    admin = Admin.query.get_or_404(admin_id)
    
    # Permission check
    if not current_user.can_edit_admin(admin):
        flash('You do not have permission to delete this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))
    
    # Cannot delete yourself
    if admin.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin.list_admins'))
    
    # Soft delete
    admin.is_active = False
    db.session.commit()

    # Log the deactivation
    AdminAuditLog.log_change(
        admin, current_user, 'deactivated',
        changes={
            'linkblue': admin.linkblue,
            'full_name': admin.full_name,
            'role': admin.role,
            'college_code': admin.college_code
        }
    )
    db.session.commit()

    flash(f'Admin "{admin.full_name}" has been deactivated.', 'success')
    return redirect(url_for('admin.list_admins'))


@admin_bp.route('/<int:admin_id>/assignments')
@admin_required
def view_course_assignments(admin_id):
    """View all course assignments for an admin"""
    admin = Admin.query.get_or_404(admin_id)

    # Permission check
    if not current_user.can_edit_admin(admin):
        flash('You do not have permission to view this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))

    assignments = admin.course_assignments.all()

    return render_template('admin/course_assignments.html',
                         admin=admin,
                         assignments=assignments)


@admin_bp.route('/<int:admin_id>/assignments/add', methods=['POST'])
@admin_required
def add_course_assignment(admin_id):
    """Add a new course assignment to an existing admin"""
    admin = Admin.query.get_or_404(admin_id)

    # Permission check
    if not current_user.can_edit_admin(admin):
        flash('You do not have permission to edit this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))

    course_prefix = request.form.get('prefix', '').strip().upper()
    course_number = request.form.get('course', '').strip() or None
    department_id = request.form.get('department_id', '').strip() or None

    if not course_prefix:
        flash('Course prefix is required.', 'danger')
        return redirect(url_for('admin.view_course_assignments', admin_id=admin_id))

    # Validate the course assignment
    department_ids = [department_id] if department_id else admin.department_ids
    course_error = validate_course_assignment(
        course_prefix,
        course_number,
        department_ids,
        require_course=True,
        validate_department=bool(department_ids)  # Fail-safe validation
    )
    if course_error:
        flash(course_error, 'danger')
        return redirect(url_for('admin.view_course_assignments', admin_id=admin_id))

    # Check if assignment already exists
    existing = CourseCoordinatorAssignment.query.filter_by(
        admin_id=admin.id,
        course_prefix=course_prefix,
        course_number=course_number
    ).first()

    if existing:
        flash(f'Assignment for {course_prefix} {course_number if course_number else "(all)"} already exists.', 'warning')
        return redirect(url_for('admin.view_course_assignments', admin_id=admin_id))

    # Create new assignment
    assignment = CourseCoordinatorAssignment(
        admin_id=admin.id,
        course_prefix=course_prefix,
        course_number=course_number,
        department_id=department_id,
        created_by_id=current_user.id
    )
    db.session.add(assignment)

    # Update admin to be course coordinator if not already
    if admin.contact_type != 'Course Coordinator':
        admin.contact_type = 'Course Coordinator'
        admin.role = 'dept_admin'

    db.session.commit()

    # Log the change
    AdminAuditLog.log_change(
        admin, current_user, 'updated',
        changes={
            'action': 'course_assignment_added',
            'course_prefix': course_prefix,
            'course_number': course_number,
            'is_wildcard': not course_number
        }
    )
    db.session.commit()

    flash(f'Course assignment "{course_prefix} {course_number if course_number else "(all)"}" added.', 'success')
    return redirect(url_for('admin.view_course_assignments', admin_id=admin_id))


@admin_bp.route('/<int:admin_id>/assignments/<int:assignment_id>/delete', methods=['POST'])
@admin_required
def delete_course_assignment(admin_id, assignment_id):
    """Delete a course assignment from an admin"""
    admin = Admin.query.get_or_404(admin_id)
    assignment = CourseCoordinatorAssignment.query.get_or_404(assignment_id)

    # Permission check
    if not current_user.can_edit_admin(admin):
        flash('You do not have permission to edit this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))

    # Verify assignment belongs to this admin
    if assignment.admin_id != admin.id:
        flash('Invalid assignment.', 'danger')
        return redirect(url_for('admin.view_course_assignments', admin_id=admin_id))

    course_display = assignment.display_name
    db.session.delete(assignment)

    # Log the change
    AdminAuditLog.log_change(
        admin, current_user, 'updated',
        changes={
            'action': 'course_assignment_removed',
            'course_prefix': assignment.course_prefix,
            'course_number': assignment.course_number
        }
    )

    db.session.commit()

    flash(f'Course assignment "{course_display}" removed.', 'success')
    return redirect(url_for('admin.view_course_assignments', admin_id=admin_id))


@admin_bp.route('/export')
@super_admin_required
def export_admins():
    """Export all admins to CSV (super admin only)"""
    admins = Admin.query.filter_by(is_active=True).order_by(
        Admin.college_code, Admin.department_id, Admin.last_name
    ).all()
    
    # Create CSV in memory
    output = io.StringIO()
    fieldnames = ['id', 'linkblue', 'first_name', 'last_name', 'primary_contact',
                  'contact_type', 'college', 'department', 'course', 'prefix', 'level_type',
                  'has_static_report_access']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    
    for admin in admins:
        writer.writerow(admin.to_csv_row())
    
    # Create response
    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'tce_admins_export_{timestamp}.csv'
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@admin_bp.route('/cleanup', methods=['GET', 'POST'])
@super_admin_required
def cleanup_admins():
    """Remove all admins except super admin and test user (for fresh re-import)"""
    from flask import current_app

    if request.method == 'POST':
        # Get the protected usernames
        super_admin_username = current_app.config.get('SUPER_ADMIN_USERNAME', 'ukytce_admin')
        test_account_username = current_app.config.get('TEST_ACCOUNT_USERNAME', 'testuser')

        # Get count before deletion
        total_before = Admin.query.count()

        # Delete all admins except protected ones
        deleted = Admin.query.filter(
            ~Admin.linkblue.in_([super_admin_username, test_account_username])
        ).delete(synchronize_session=False)

        db.session.commit()

        flash(f'Cleanup complete: {deleted} admins removed. Kept: {super_admin_username}, {test_account_username}', 'success')
        return redirect(url_for('admin.list_admins'))

    # GET - show confirmation page
    super_admin_username = current_app.config.get('SUPER_ADMIN_USERNAME', 'ukytce_admin')
    test_account_username = current_app.config.get('TEST_ACCOUNT_USERNAME', 'testuser')

    total_admins = Admin.query.count()
    protected_admins = Admin.query.filter(
        Admin.linkblue.in_([super_admin_username, test_account_username])
    ).all()
    to_delete = total_admins - len(protected_admins)

    return render_template('admin/cleanup.html',
                           total_admins=total_admins,
                           protected_admins=protected_admins,
                           to_delete=to_delete)


@admin_bp.route('/fix-departments', methods=['GET', 'POST'])
@super_admin_required
def fix_department_ids():
    """Fix admin records that have department names instead of IDs.

    This repairs admins where department_id contains a name like 'Biology'
    instead of the proper CLASS_DEPARTMENT_ID like '30000485'.
    """
    # Build mapping of department name -> department id
    departments = Department.query.all()
    name_to_id = {dept.name.lower(): dept.id for dept in departments}

    # Find admins with department_id that looks like a name (not numeric)
    admins_to_fix = []
    for admin in Admin.query.filter(Admin.department_id != None).all():
        dept_id = admin.department_id
        # If it's already a valid department ID, skip
        if Department.query.get(dept_id):
            continue
        # Check if it matches a department name
        if dept_id.lower() in name_to_id:
            admins_to_fix.append({
                'admin': admin,
                'old_value': dept_id,
                'new_value': name_to_id[dept_id.lower()]
            })

    if request.method == 'POST':
        fixed_count = 0
        for item in admins_to_fix:
            item['admin'].department_id = item['new_value']
            fixed_count += 1

        db.session.commit()
        flash(f'Fixed {fixed_count} admin records with correct department IDs.', 'success')
        return redirect(url_for('admin.list_admins'))

    # GET - show what will be fixed
    return render_template('admin/fix_departments.html',
                           admins_to_fix=admins_to_fix,
                           total_departments=len(departments))


@admin_bp.route('/import', methods=['GET', 'POST'])
@super_admin_required
def import_admins():
    """Import admins from CSV (super admin only)"""
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file uploaded.', 'danger')
            return redirect(url_for('admin.import_admins'))

        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'danger')
            return redirect(url_for('admin.import_admins'))

        if not file.filename.endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(url_for('admin.import_admins'))

        # Parse CSV
        try:
            content = file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(content))

            # Build college name -> code mapping from College table
            college_name_to_code = {}
            colleges = College.query.all()
            for college in colleges:
                college_name_to_code[college.name] = college.code
                # Also add lowercase version for case-insensitive matching
                college_name_to_code[college.name.lower()] = college.code
                # Also map code to code (in case CSV already has codes)
                college_name_to_code[college.code] = college.code
                college_name_to_code[college.code.lower()] = college.code

            # Build department name -> ID mapping from Department table
            dept_name_to_id = {}
            departments = Department.query.all()
            for dept in departments:
                dept_name_to_id[dept.name] = dept.id
                # Also add lowercase version for case-insensitive matching
                dept_name_to_id[dept.name.lower()] = dept.id

            imported = 0
            skipped = 0
            errors = []
            unmapped_depts = set()
            unmapped_colleges = set()

            for row in reader:
                linkblue = row.get('linkblue', '').strip().lower()
                if not linkblue:
                    continue

                # Check if already exists
                existing = Admin.query.filter_by(linkblue=linkblue).first()
                if existing:
                    skipped += 1
                    continue

                try:
                    admin = Admin.from_csv_row(row, created_by_id=current_user.id,
                                               dept_name_to_id_map=dept_name_to_id,
                                               college_name_to_code_map=college_name_to_code)

                    # Track unmapped departments (where dept contact was converted to college-level)
                    dept_name = row.get('department', '').strip()
                    contact_type = row.get('contact_type', 'Department')
                    if dept_name and dept_name.lower() not in ['all', ''] and contact_type == 'Department' and admin.department_id is None:
                        unmapped_depts.add(dept_name)

                    # Track unmapped colleges
                    college_name = row.get('college', '').strip()
                    if college_name and college_name not in college_name_to_code and college_name.lower() not in college_name_to_code:
                        unmapped_colleges.add(college_name)

                    db.session.add(admin)
                    imported += 1
                except Exception as e:
                    errors.append(f"Row {linkblue}: {str(e)}")

            db.session.commit()

            # Log the import operation
            if imported > 0:
                AdminAuditLog.log_change(
                    None, current_user, 'imported',
                    changes={
                        'filename': file.filename,
                        'imported_count': imported,
                        'skipped_count': skipped,
                        'error_count': len(errors)
                    }
                )
                # Set target_linkblue to indicate bulk import
                log = AdminAuditLog.query.order_by(AdminAuditLog.id.desc()).first()
                if log:
                    log.target_linkblue = f'bulk_import_{imported}_admins'
                db.session.commit()

            flash(f'Import complete: {imported} added, {skipped} skipped (already exist).', 'success')
            if unmapped_colleges:
                flash(f'Warning: {len(unmapped_colleges)} colleges not found: {", ".join(list(unmapped_colleges)[:5])}. Run course sync first.', 'warning')
            if unmapped_depts:
                flash(f'Warning: {len(unmapped_depts)} departments not found: {", ".join(list(unmapped_depts)[:5])}. Run course sync first.', 'warning')
            if errors:
                flash(f'Errors: {"; ".join(errors[:5])}', 'warning')

        except Exception as e:
            flash(f'Error processing file: {str(e)}', 'danger')

        return redirect(url_for('admin.list_admins'))

    return render_template('admin/import.html')


@admin_bp.route('/api/departments/<college_code>')
@admin_required
def get_departments_api(college_code):
    """API endpoint to get departments for a college

    Only uses Department table to ensure consistent department IDs.
    Run course sync first to populate departments.
    """
    departments = Department.query.filter_by(college_code=college_code).order_by(Department.name).all()
    return jsonify([{'id': d.id, 'name': d.name} for d in departments])


# Helper functions
def get_colleges_for_user():
    """Get list of colleges the current user can see"""
    if current_user.is_super_admin():
        colleges = College.query.order_by(College.name).all()
        if not colleges:
            # Fallback: get unique colleges from Admin table
            college_names = db.session.query(Admin.college_code).distinct().all()
            return [{'code': c[0], 'name': c[0]} for c in college_names if c[0]]
        return [{'code': c.code, 'name': c.name} for c in colleges]
    else:
        return [{'code': current_user.college_code, 'name': current_user.college_code}]


def get_departments_for_user():
    """Get list of departments the current user can see

    Only uses Department table to ensure consistent department IDs.
    For dept_admin users, looks up their department from the Department table.
    """
    if current_user.is_super_admin():
        departments = Department.query.order_by(Department.name).all()
        return [{'id': d.id, 'name': d.name, 'college_code': d.college_code} for d in departments]
    elif current_user.is_college_admin():
        departments = Department.query.filter_by(college_code=current_user.college_code).order_by(Department.name).all()
        return [{'id': d.id, 'name': d.name, 'college_code': d.college_code} for d in departments]
    else:
        # For dept_admin, look up their department from the Department table
        if current_user.department_id:
            dept = Department.query.get(current_user.department_id)
            if dept:
                return [{'id': dept.id, 'name': dept.name, 'college_code': dept.college_code}]
        return []


def get_college_departments_map():
    """Get mapping of college -> departments for JavaScript
    Returns dict with college_code -> list of {id: dept_id, name: dept_name}

    IMPORTANT: Only uses the Department table (populated by course sync).
    This ensures department IDs are the proper CLASS_DEPARTMENT_ID values
    and prevents duplicates from inconsistent Admin table data.
    """
    result = {}

    # Only use Department table - this has the canonical department data
    departments = Department.query.all()
    for dept in departments:
        if dept.college_code not in result:
            result[dept.college_code] = []
        result[dept.college_code].append({'id': dept.id, 'name': dept.name})

    # Sort departments within each college by name
    for college in result:
        result[college] = sorted(result[college], key=lambda x: x['name'])

    return result
