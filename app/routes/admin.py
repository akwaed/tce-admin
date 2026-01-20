"""
Admin Management Routes
CRUD operations for TCE administrators with role-based access control
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.admin import Admin
from app.models.course import College, Department
from functools import wraps
import csv
import io
from datetime import datetime

admin_bp = Blueprint('admin', __name__)


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
                             'primary': primary_filter
                         })


@admin_bp.route('/add', methods=['GET', 'POST'])
@admin_required
def add_admin():
    """Add a new admin"""
    # Super admins can always add admins
    # College admins can only add if they are primary contacts
    if current_user.role == 'college_admin' and not current_user.is_primary_contact:
        flash('Only primary college contacts can add new administrators.', 'danger')
        return redirect(url_for('admin.list_admins'))

    if request.method == 'POST':
        # Get form data
        linkblue = request.form.get('linkblue', '').strip().lower()
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip()

        # Check if creating a super admin (only super admins can do this)
        admin_role = request.form.get('admin_role', 'regular')
        is_creating_super_admin = admin_role == 'super_admin' and current_user.is_super_admin()

        contact_type = request.form.get('contact_type', 'Department')
        college_code = request.form.get('college', '').strip()
        department_id = request.form.get('department', '').strip()
        is_primary = request.form.get('primary_contact') == 'yes'
        level_type = request.form.get('level_type', 'Subject Viewer')
        course_prefix = request.form.get('prefix', '').strip()
        course_number = request.form.get('course', '').strip()

        # Access flags
        has_dashboard = request.form.get('has_dashboard_access') == 'yes'
        has_static_report = request.form.get('has_static_report_access') == 'yes'
        has_qb = request.form.get('has_qb_access') == 'yes' if (
            current_user.is_super_admin() or
            (current_user.is_college_admin() and current_user.is_primary_contact)
        ) else False

        # Validation
        if not linkblue or not first_name or not last_name:
            flash('LinkBlue, First Name, and Last Name are required.', 'danger')
            return redirect(url_for('admin.add_admin'))

        # Check for duplicate
        existing = Admin.query.filter_by(linkblue=linkblue).first()
        if existing:
            flash(f'An admin with LinkBlue "{linkblue}" already exists.', 'danger')
            return redirect(url_for('admin.add_admin'))

        # Handle super admin creation
        # No password required - super admins authenticate via Azure AD
        if is_creating_super_admin:
            role = 'super_admin'
            college_code = None
            department_id = None
            contact_type = None
            is_primary = False
            has_qb = True
        else:
            # Permission check - can only add admins within your scope
            if not current_user.is_super_admin():
                if college_code != current_user.college_code:
                    flash('You can only add admins within your college.', 'danger')
                    return redirect(url_for('admin.add_admin'))

            # Determine role
            if contact_type == 'College' or department_id == 'All' or not department_id:
                role = 'college_admin'
                department_id = None
            else:
                role = 'dept_admin'

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

        # Create admin
        admin = Admin(
            linkblue=linkblue,
            first_name=first_name,
            last_name=last_name,
            email=email or f'{linkblue}@uky.edu',
            role=role,
            college_code=college_code,
            department_id=department_id,
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
        db.session.commit()

        flash(f'Admin "{admin.full_name}" created successfully.', 'success')
        return redirect(url_for('admin.list_admins'))
    
    # GET - show form
    colleges = get_colleges_for_user()
    departments = get_departments_for_user()
    college_depts = get_college_departments_map()
    
    return render_template('admin/add.html',
                         colleges=colleges,
                         departments=departments,
                         college_depts=college_depts)


@admin_bp.route('/edit/<int:admin_id>', methods=['GET', 'POST'])
@admin_required
def edit_admin(admin_id):
    """Edit an existing admin"""
    admin = Admin.query.get_or_404(admin_id)
    
    # Permission check
    if not current_user.can_edit_admin(admin):
        flash('You do not have permission to edit this admin.', 'danger')
        return redirect(url_for('admin.list_admins'))
    
    if request.method == 'POST':
        # Get form data
        admin.linkblue = request.form.get('linkblue', admin.linkblue).strip().lower()
        admin.first_name = request.form.get('first_name', admin.first_name).strip()
        admin.last_name = request.form.get('last_name', admin.last_name).strip()
        admin.email = request.form.get('email', admin.email).strip()

        # Handle role change (only super admins can change roles, and not their own)
        if current_user.is_super_admin() and admin.id != current_user.id:
            new_admin_role = request.form.get('admin_role', 'regular')
            was_super_admin = admin.role == 'super_admin'
            is_becoming_super_admin = new_admin_role == 'super_admin'

            if is_becoming_super_admin and not was_super_admin:
                # Elevating to super admin
                password = request.form.get('password', '')
                password_confirm = request.form.get('password_confirm', '')

                # Password required when elevating (unless they already have one)
                if password:
                    if len(password) < 8:
                        flash('Password must be at least 8 characters.', 'danger')
                        return redirect(url_for('admin.edit_admin', admin_id=admin_id))
                    if password != password_confirm:
                        flash('Passwords do not match.', 'danger')
                        return redirect(url_for('admin.edit_admin', admin_id=admin_id))
                    admin.set_password(password)

                admin.role = 'super_admin'
                admin.college_code = None
                admin.department_id = None
                admin.contact_type = None
                admin.is_primary_contact = False
                admin.has_qb_access = True

                db.session.commit()
                flash(f'Admin "{admin.full_name}" elevated to Super Administrator.', 'success')
                return redirect(url_for('admin.list_admins'))

            elif not is_becoming_super_admin and was_super_admin:
                # Demoting from super admin - need college assignment
                new_college = request.form.get('college', '').strip()
                if not new_college:
                    flash('Must assign a college when demoting from super admin.', 'danger')
                    return redirect(url_for('admin.edit_admin', admin_id=admin_id))

                admin.role = 'college_admin'
                admin.college_code = new_college
                admin.contact_type = 'College'

            elif is_becoming_super_admin and was_super_admin:
                # Already super admin - just update password if provided
                password = request.form.get('password', '')
                if password:
                    password_confirm = request.form.get('password_confirm', '')
                    if len(password) < 8:
                        flash('Password must be at least 8 characters.', 'danger')
                        return redirect(url_for('admin.edit_admin', admin_id=admin_id))
                    if password != password_confirm:
                        flash('Passwords do not match.', 'danger')
                        return redirect(url_for('admin.edit_admin', admin_id=admin_id))
                    admin.set_password(password)

                db.session.commit()
                flash(f'Admin "{admin.full_name}" updated successfully.', 'success')
                return redirect(url_for('admin.list_admins'))

        # Regular admin fields (skip if super admin)
        if admin.role != 'super_admin':
            admin.contact_type = request.form.get('contact_type', admin.contact_type)
            admin.level_type = request.form.get('level_type', admin.level_type)
            admin.course_prefix = request.form.get('prefix', '').strip() or None
            admin.course_number = request.form.get('course', '').strip() or None

            # College and department - only super admin can change these freely
            new_college = request.form.get('college', '').strip()
            new_department = request.form.get('department', '').strip()

            if current_user.is_super_admin():
                admin.college_code = new_college
                admin.department_id = new_department if new_department and new_department != 'All' else None
            elif current_user.is_college_admin() and current_user.is_primary_contact:
                # Primary college admin can reassign within their college
                if new_college == current_user.college_code:
                    admin.department_id = new_department if new_department and new_department != 'All' else None

            # Determine role based on contact type and department
            if admin.contact_type == 'College' or not admin.department_id:
                admin.role = 'college_admin'
                admin.department_id = None
            else:
                admin.role = 'dept_admin'

            # Primary contact - with validation
            is_primary = request.form.get('primary_contact') == 'yes'
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

        # Access flags
        admin.has_dashboard_access = request.form.get('has_dashboard_access') == 'yes'
        admin.has_static_report_access = request.form.get('has_static_report_access') == 'yes'

        # QB access - super admin or primary college admin only
        if current_user.is_super_admin() or (current_user.is_college_admin() and current_user.is_primary_contact):
            admin.has_qb_access = request.form.get('has_qb_access') == 'yes'

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
                         college_depts=college_depts)


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
    
    flash(f'Admin "{admin.full_name}" has been deactivated.', 'success')
    return redirect(url_for('admin.list_admins'))


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
                  'contact_type', 'college', 'department', 'course', 'prefix', 'level_type']
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
