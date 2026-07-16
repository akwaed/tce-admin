"""
Tracking Routes for Super Users
View audit logs for admin changes and QB/QM changes
Manage backups of QB and QM files
Export DRA data for Explorance Blue integration
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.admin import Admin, AdminAuditLog
from app.models.question import QBAuditLog, QBBackup
from app.models.course import College, Department, Course, Instructor, CourseUser, StudentEnrollment
from app.models.sync_history import ChangeLog, SyncRun
from app.services.backup_service import get_backup_service
from app.services.course_sync import normalize_diff_value
from functools import wraps
from datetime import datetime, timezone
UTC = timezone.utc
from collections import defaultdict
import csv
import io
import math
import re
from sqlalchemy import or_, and_, func, text

tracking_bp = Blueprint('tracking', __name__)

# ---------------------------------------------------------------------------
# DRA College Code Mapping (temporary — new college codes)
# ---------------------------------------------------------------------------
_dra_mapping = None  # lazy-loaded cache


def _load_dra_mapping():
    """Load new→old college Node Id mapping from dra_mapping_file.csv."""
    global _dra_mapping
    if _dra_mapping is not None:
        return _dra_mapping

    import os
    mapping_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        'datasources', 'dra_mapping_file.csv',
    )
    _dra_mapping = {}
    if os.path.exists(mapping_path):
        with open(mapping_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                new_id = (row.get('New Node Id') or '').strip()
                old_id = (row.get('Node Id') or '').strip()
                if new_id and old_id and new_id != old_id:
                    _dra_mapping[new_id] = old_id
    return _dra_mapping


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


def _clean_dra_source(value):
    return str(value).strip() if value is not None else ''


def _resolve_college_code_from_courses(college_value):
    """Resolve a stored college value to CLASS_COLLEGE_SHORT from synced Courses.csv data."""
    college_value = _clean_dra_source(college_value)
    if not college_value:
        return None

    college = College.query.get(college_value)
    if not college:
        college = College.query.filter(
            db.func.lower(College.name) == college_value.lower()
        ).first()

    return college.code if college else None


def get_college_source_for_dra(admin, use_new_codes=False):
    """Return the C4 source value from CLASS_COLLEGE_SHORT.

    When *use_new_codes* is False (default / "Old Codes" layout), the value
    is translated through dra_mapping_file.csv so the old college Node Id is
    emitted.  When True ("New Codes" layout), the code is used as-is.
    """
    college_code = _resolve_college_code_from_courses(admin.college_code)
    if college_code:
        if not use_new_codes:
            college_code = _load_dra_mapping().get(college_code, college_code)
        return college_code, None

    if admin.college_code:
        return None, f"College not found in synced Courses.csv data: {admin.college_code} for {admin.linkblue}"
    return None, f"No college code for college admin {admin.linkblue}"


def resolve_department_source_for_dra(department_value, college_value=None):
    """Resolve a stored department value to CLASS_DEPARTMENT_ID."""
    department_value = _clean_dra_source(department_value)
    if not department_value or department_value.lower() == 'all':
        return None

    department = Department.query.get(department_value)
    if department:
        return department.id

    query = Department.query.filter(
        db.func.lower(Department.name) == department_value.lower()
    )
    college_code = _resolve_college_code_from_courses(college_value)
    if college_code:
        query = query.filter(Department.college_code == college_code)

    departments = query.all()
    if len(departments) == 1:
        return departments[0].id
    return None


def get_department_sources_for_dra(admin):
    """Return D3 source values from CLASS_DEPARTMENT_ID."""
    departments = admin.departments.all()
    if departments:
        return [_clean_dra_source(dept.id) for dept in departments if dept.id], []

    if admin.department_id:
        department_id = resolve_department_source_for_dra(admin.department_id, admin.college_code)
        if department_id:
            return [department_id], []
        return [], [f"Department not found or ambiguous in synced Courses.csv data: {admin.department_id} for {admin.linkblue}"]

    return [], [f"No department ID for dept admin {admin.linkblue}"]


def get_latest_course_load():
    """Return courses from the most recent Courses.csv database load."""
    latest_synced = db.session.query(db.func.max(Course.last_synced)).scalar()
    if not latest_synced:
        return [], None

    courses = Course.query.filter(
        Course.last_synced == latest_synced
    ).all()
    return courses, latest_synced


def build_hierarchy_rows(courses):
    """
    Build Explorance hierarchy rows from synced Courses.csv fields.

    Levels:
    1. University
    2. CLASS_COLLEGE_SHORT
    3. CLASS_DEPARTMENT_ID
    4. CLASS_ID
    """
    colleges = {}
    departments = {}
    course_candidates = defaultdict(set)
    course_sections = defaultdict(set)
    flattened_departments = set()
    errors = []

    for course in courses:
        section_key = _clean_dra_source(course.section_key)
        college_code = _clean_dra_source(course.college_code)
        department_id = _clean_dra_source(course.department_id)
        class_id = _clean_dra_source(course.class_id)
        class_code = _clean_dra_source(course.class_code)
        section_title = _clean_dra_source(course.section_title)

        college_name = _clean_dra_source(
            course.college.name if course.college else college_code
        ) or college_code
        department_name = _clean_dra_source(
            course.department.name if course.department else department_id
        ) or department_id

        if college_code:
            colleges[college_code] = college_name or college_code
        else:
            errors.append(f"Missing CLASS_COLLEGE_SHORT for {section_key}")

        course_parent_id = None
        course_parent_name = None
        if department_id:
            if department_id == college_code:
                # Explorance treats Node Id as globally unique, so a value
                # cannot be emitted as both a college and department node.
                course_parent_id = college_code
                course_parent_name = college_name or college_code
                if department_id not in flattened_departments:
                    errors.append(
                        "CLASS_DEPARTMENT_ID matches CLASS_COLLEGE_SHORT; "
                        f"using the college node for {department_id}"
                    )
                    flattened_departments.add(department_id)
            else:
                department = {
                    'name': department_name or department_id,
                    'college_code': college_code,
                    'college_name': college_name or college_code,
                }
                existing_department = departments.get(department_id)
                if existing_department and existing_department != department:
                    errors.append(
                        "Conflicting hierarchy details for CLASS_DEPARTMENT_ID "
                        f"{department_id}; using {existing_department['college_code']}"
                    )
                    department = existing_department
                else:
                    departments[department_id] = department

                course_parent_id = department_id
                course_parent_name = department['name']
        else:
            errors.append(f"Missing CLASS_DEPARTMENT_ID for {section_key}")

        if class_id and course_parent_id:
            course_candidates[class_id].add((
                course_parent_id,
                course_parent_name,
                class_code,
                section_title or class_code or class_id,
            ))
            course_sections[class_id].add(section_key)
        elif not class_id:
            errors.append(f"Missing CLASS_ID for {section_key}")
        else:
            errors.append(f"Missing hierarchy parent for CLASS_ID {class_id}")

    rows = [
        ['University', 'University', '', '', 1, '']
    ]

    for college_code, college_name in sorted(
        colleges.items(),
        key=lambda item: (item[1], item[0])
    ):
        rows.append([college_code, college_name, 'University', 'University', 2, ''])

    department_items = sorted(
        departments.items(),
        key=lambda item: (item[1]['college_name'], item[1]['name'], item[0])
    )
    for department_id, department in department_items:
        rows.append([
            department_id,
            department['name'],
            department['college_code'],
            department['college_name'],
            3,
            '',
        ])

    course_rows = []
    for class_id, candidates in course_candidates.items():
        sorted_candidates = sorted(
            candidates,
            key=lambda row: (row[1], row[0], row[2], row[3])
        )
        parent_ids = {candidate[0] for candidate in sorted_candidates}
        if len(parent_ids) > 1:
            errors.append(
                f"CLASS_ID {class_id} appears under multiple parents; "
                f"exporting one hierarchy node from {len(course_sections[class_id])} sections"
            )

        parent_id, parent_name, class_code, caption = sorted_candidates[0]
        captions = {candidate[3] for candidate in sorted_candidates if candidate[3]}
        if len(captions) > 1 and class_code:
            caption = class_code

        course_rows.append((
            class_id,
            caption or class_code or class_id,
            parent_id,
            parent_name,
            4,
            class_code,
        ))

    for course_row in sorted(
        course_rows,
        key=lambda row: (row[3], row[5], row[1], row[0])
    ):
        rows.append(list(course_row))

    node_ids = set()
    duplicate_ids = set()
    for row in rows:
        node_id = row[0]
        if node_id in node_ids:
            duplicate_ids.add(node_id)
        node_ids.add(node_id)

    if duplicate_ids:
        errors.append(
            f"Duplicate hierarchy Node Ids generated: {', '.join(sorted(duplicate_ids)[:10])}"
        )

    for index, row in enumerate(rows, start=1):
        parent_id = row[2]
        if parent_id and parent_id not in node_ids:
            errors.append(
                f"Hierarchy parent missing before export: row {index} "
                f"parent {parent_id} for node {row[0]}"
            )

    return rows, errors


@tracking_bp.route('/')
@super_admin_required
def index():
    """Main tracking dashboard for super users"""
    # Get recent admin audit logs
    admin_logs = AdminAuditLog.query.order_by(
        AdminAuditLog.timestamp.desc()
    ).limit(10).all()

    # Get recent QB audit logs
    qb_logs = QBAuditLog.query.order_by(
        QBAuditLog.timestamp.desc()
    ).limit(10).all()

    # Get backup stats
    backup_service = get_backup_service()
    backup_stats = backup_service.get_backup_stats()

    return render_template('tracking/index.html',
                           admin_logs=admin_logs,
                           qb_logs=qb_logs,
                           backup_stats=backup_stats)


@tracking_bp.route('/admin-changes')
@super_admin_required
def admin_changes():
    """View all admin audit logs with filtering"""
    # Get filter parameters
    action_filter = request.args.get('action', '')
    actor_filter = request.args.get('actor', '')
    target_filter = request.args.get('target', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    page = request.args.get('page', 1, type=int)
    per_page = 25

    # Build query
    query = AdminAuditLog.query

    if action_filter:
        query = query.filter(AdminAuditLog.action == action_filter)

    if actor_filter:
        query = query.filter(AdminAuditLog.actor_linkblue.ilike(f'%{actor_filter}%'))

    if target_filter:
        query = query.filter(AdminAuditLog.target_linkblue.ilike(f'%{target_filter}%'))

    if date_from:
        try:
            from_date = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(AdminAuditLog.timestamp >= from_date)
        except ValueError:
            pass

    if date_to:
        try:
            to_date = datetime.strptime(date_to, '%Y-%m-%d')
            # Include the entire day
            to_date = to_date.replace(hour=23, minute=59, second=59)
            query = query.filter(AdminAuditLog.timestamp <= to_date)
        except ValueError:
            pass

    # Order and paginate
    query = query.order_by(AdminAuditLog.timestamp.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    logs = pagination.items

    # Get unique actions for filter dropdown
    actions = db.session.query(AdminAuditLog.action).distinct().all()
    action_options = [a[0] for a in actions]

    return render_template('tracking/admin_changes.html',
                           logs=logs,
                           pagination=pagination,
                           action_options=action_options,
                           current_filters={
                               'action': action_filter,
                               'actor': actor_filter,
                               'target': target_filter,
                               'date_from': date_from,
                               'date_to': date_to
                           })


@tracking_bp.route('/qb-changes')
@super_admin_required
def qb_changes():
    """View all QB/QM audit logs with filtering"""
    from datetime import timedelta

    # Get filter parameters
    action_filter = request.args.get('action', '')
    admin_filter = request.args.get('admin', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    quick_filter = request.args.get('quick', '')  # 48h, 7d, 30d
    page = request.args.get('page', 1, type=int)
    per_page = 25

    # Build query
    query = QBAuditLog.query

    if action_filter:
        query = query.filter(QBAuditLog.action == action_filter)

    if admin_filter:
        query = query.filter(QBAuditLog.admin_linkblue.ilike(f'%{admin_filter}%'))

    # Quick filter takes precedence over date filters
    if quick_filter:
        now = datetime.now(UTC)
        if quick_filter == '48h':
            from_date = now - timedelta(hours=48)
            query = query.filter(QBAuditLog.timestamp >= from_date)
        elif quick_filter == '7d':
            from_date = now - timedelta(days=7)
            query = query.filter(QBAuditLog.timestamp >= from_date)
        elif quick_filter == '30d':
            from_date = now - timedelta(days=30)
            query = query.filter(QBAuditLog.timestamp >= from_date)
    else:
        if date_from:
            try:
                from_date = datetime.strptime(date_from, '%Y-%m-%d')
                query = query.filter(QBAuditLog.timestamp >= from_date)
            except ValueError:
                pass

        if date_to:
            try:
                to_date = datetime.strptime(date_to, '%Y-%m-%d')
                to_date = to_date.replace(hour=23, minute=59, second=59)
                query = query.filter(QBAuditLog.timestamp <= to_date)
            except ValueError:
                pass

    # Order and paginate
    query = query.order_by(QBAuditLog.timestamp.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    logs = pagination.items

    # Get unique actions for filter dropdown
    actions = db.session.query(QBAuditLog.action).distinct().all()
    action_options = [a[0] for a in actions]

    return render_template('tracking/qb_changes.html',
                           logs=logs,
                           pagination=pagination,
                           action_options=action_options,
                           current_filters={
                               'action': action_filter,
                               'admin': admin_filter,
                               'date_from': date_from,
                               'date_to': date_to,
                               'quick': quick_filter
                           })


@tracking_bp.route('/backups')
@super_admin_required
def backups():
    """View and manage QB/QM backups"""
    # Get filter parameters
    type_filter = request.args.get('type', '')
    reason_filter = request.args.get('reason', '')

    # Get backups
    backup_service = get_backup_service()

    if type_filter:
        qb_backups = backup_service.get_backups(backup_type=type_filter, limit=100)
    else:
        qb_backups = backup_service.get_backups(limit=100)

    # Apply reason filter
    if reason_filter:
        qb_backups = [b for b in qb_backups if b.reason == reason_filter]

    # Get stats
    backup_stats = backup_service.get_backup_stats()

    return render_template('tracking/backups.html',
                           backups=qb_backups,
                           backup_stats=backup_stats,
                           current_filters={
                               'type': type_filter,
                               'reason': reason_filter
                           })


@tracking_bp.route('/backups/create', methods=['POST'])
@super_admin_required
def create_backup():
    """Create a manual backup"""
    backup_type = request.form.get('backup_type', 'both')
    backup_service = get_backup_service()

    # Manual backups always create new ones (force=True)
    if backup_type == 'both':
        qb_backup, qm_backup = backup_service.create_both_backups('manual', current_user, force=True)
        if qb_backup or qm_backup:
            flash('Manual backups created successfully.', 'success')
        else:
            flash('No files to backup.', 'warning')
    elif backup_type in ['qb', 'qm']:
        backup = backup_service.create_backup(backup_type, 'manual', current_user, force=True)
        if backup:
            flash(f'{backup.backup_type_display} backup created successfully.', 'success')
        else:
            flash('File does not exist for backup.', 'warning')
    else:
        flash('Invalid backup type.', 'danger')

    return redirect(url_for('tracking.backups'))


@tracking_bp.route('/backups/<int:backup_id>/delete', methods=['POST'])
@super_admin_required
def delete_backup(backup_id):
    """Delete a backup"""
    backup_service = get_backup_service()

    if backup_service.delete_backup(backup_id, current_user):
        flash('Backup deleted successfully.', 'success')
    else:
        flash('Failed to delete backup.', 'danger')

    return redirect(url_for('tracking.backups'))


@tracking_bp.route('/backups/<int:backup_id>/download')
@super_admin_required
def download_backup(backup_id):
    """Download a backup file"""
    backup_service = get_backup_service()
    backup = QBBackup.query.get_or_404(backup_id)

    if backup.is_deleted:
        flash('Backup has been deleted.', 'danger')
        return redirect(url_for('tracking.backups'))

    file_path = backup_service.get_backup_file_path(backup_id)
    if not file_path:
        flash('Backup file not found.', 'danger')
        return redirect(url_for('tracking.backups'))

    with open(file_path, 'rb') as f:
        data = f.read()

    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={backup.filename}'}
    )


@tracking_bp.route('/backups/<int:backup_id>/restore', methods=['POST'])
@super_admin_required
def restore_backup(backup_id):
    """Restore a backup file"""
    backup_service = get_backup_service()

    if backup_service.restore_backup(backup_id, current_user):
        flash('Backup restored successfully. A new backup of the previous version was created.', 'success')
    else:
        flash('Failed to restore backup.', 'danger')

    return redirect(url_for('tracking.backups'))


@tracking_bp.route('/api/admin-changes')
@super_admin_required
def api_admin_changes():
    """API: Get admin audit logs as JSON"""
    limit = request.args.get('limit', 50, type=int)
    logs = AdminAuditLog.query.order_by(
        AdminAuditLog.timestamp.desc()
    ).limit(limit).all()

    return jsonify([log.to_dict() for log in logs])


@tracking_bp.route('/api/qb-changes')
@super_admin_required
def api_qb_changes():
    """API: Get QB audit logs as JSON"""
    limit = request.args.get('limit', 50, type=int)
    logs = QBAuditLog.query.order_by(
        QBAuditLog.timestamp.desc()
    ).limit(limit).all()

    return jsonify([log.to_dict() for log in logs])


@tracking_bp.route('/api/backups')
@super_admin_required
def api_backups():
    """API: Get backups as JSON"""
    backup_type = request.args.get('type')
    backup_service = get_backup_service()

    if backup_type:
        backups = backup_service.get_backups(backup_type=backup_type)
    else:
        backups = backup_service.get_backups()

    return jsonify([b.to_dict() for b in backups])


def get_course_class_ids_for_coordinator(admin):
    """
    Get all class IDs for a course coordinator.

    Uses the new CourseCoordinatorAssignment table first, then falls back to legacy fields.
    Handles both wildcard (prefix-only) and specific (prefix+number) assignments.

    Note: 'All' prefix is treated as a departmental contact and returns empty set.

    Returns:
        tuple: (set of class_ids, list of error messages)
    """
    class_ids = set()
    errors = []

    # Get assignments from new table
    assignments = admin.course_assignments.all()

    if assignments:
        # Use new assignment table
        for assignment in assignments:
            if assignment.is_wildcard:
                # Wildcard - all courses with this prefix
                courses = Course.query.filter(
                    Course.class_code.like(f"{assignment.course_prefix} %")
                ).all()
            else:
                # Specific course pattern
                class_pattern = f"{assignment.course_prefix} {assignment.course_number}"
                courses = Course.query.filter(
                    Course.class_code.like(f"{class_pattern}%")
                ).all()

            if courses:
                for course in courses:
                    if course.class_id:
                        class_ids.add(course.class_id)
            else:
                pattern = assignment.display_name
                errors.append(f"No courses found for {admin.linkblue} ({pattern})")

    elif admin.course_prefix and admin.course_prefix.upper() not in ('ALL', '*'):
        # Fallback to legacy fields - but skip 'All' prefix (it's departmental, not course coordination)
        if admin.course_number:
            # Specific course pattern
            class_pattern = f"{admin.course_prefix} {admin.course_number}"
            courses = Course.query.filter(
                Course.class_code.like(f"{class_pattern}%")
            ).all()
        else:
            # Wildcard - prefix only
            courses = Course.query.filter(
                Course.class_code.like(f"{admin.course_prefix} %")
            ).all()

        if courses:
            for course in courses:
                if course.class_id:
                    class_ids.add(course.class_id)
        else:
            pattern = f"{admin.course_prefix} {admin.course_number if admin.course_number else '(all)'}"
            errors.append(f"No courses found for {admin.linkblue} ({pattern})")
    # Note: We don't add an error for missing course info here anymore,
    # because 'All' prefix means departmental contact, handled elsewhere

    return class_ids, errors


@tracking_bp.route('/export-dra')
@super_admin_required
def export_dra():
    """
    Export DRA (Data Relationship Assignment) file for Explorance Blue.

    Format: source,target,targetType
    - source: The CLASS_COLLEGE_SHORT, CLASS_DEPARTMENT_ID, or CLASS_ID from synced Courses.csv data
    - target: The admin's linkblue
    - targetType: C4 (college), D3 (department), or CRS1 (course)

    Only exports admins with the S flag (has_static_report_access = True)

    Course Coordinator handling:
    - Uses the CourseCoordinatorAssignment table for multiple assignments
    - Falls back to legacy course_prefix/course_number fields
    - Supports wildcard (prefix-only) assignments that match all courses with that prefix
    """
    # Determine layout: 'old' (default) or 'new' college codes
    use_new_codes = request.args.get('layout') == 'new'

    # Get all active admins with S flag (excluding super admins who don't have DRA assignments)
    admins = Admin.query.filter(
        Admin.is_active == True,
        Admin.role != 'super_admin',
        Admin.has_static_report_access == True  # S flag filter
    ).all()

    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['source', 'target', 'targetType'])

    rows_written = 0
    errors = []

    for admin in admins:
        try:
            # Check if this is a course coordinator using new property
            is_course_coordinator = admin.is_course_coordinator

            if is_course_coordinator:
                # Course coordinator - get all class IDs
                target_type = 'CRS1'

                class_ids, coord_errors = get_course_class_ids_for_coordinator(admin)
                errors.extend(coord_errors)

                for class_id in class_ids:
                    writer.writerow([class_id, admin.linkblue, target_type])
                    rows_written += 1

            # Determine the target type and source based on contact level
            elif admin.contact_type == 'College' or admin.role == 'college_admin':
                # College-level contact
                target_type = 'C4'
                college_source, college_error = get_college_source_for_dra(admin, use_new_codes=use_new_codes)
                if college_error:
                    errors.append(college_error)
                    continue

                writer.writerow([college_source, admin.linkblue, target_type])
                rows_written += 1

            elif admin.contact_type == 'Department' or admin.role == 'dept_admin':
                # Department-level contact
                target_type = 'D3'

                department_sources, department_errors = get_department_sources_for_dra(admin)
                errors.extend(department_errors)

                for department_source in department_sources:
                    writer.writerow([department_source, admin.linkblue, target_type])
                    rows_written += 1

        except Exception as e:
            errors.append(f"Error processing {admin.linkblue}: {str(e)}")

    # Log the export
    QBAuditLog.log_action('dra_export', current_user,
                          details={
                              'rows_exported': rows_written,
                              'errors_count': len(errors),
                              'errors': errors[:10]  # First 10 errors
                          })
    db.session.commit()

    # Create response
    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'DRA_export_{timestamp}.csv'

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@tracking_bp.route('/export-hierarchy')
@super_admin_required
def export_hierarchy():
    """
    Export the Explorance hierarchy file from the latest synced Courses.csv data.

    Format: Node Id, Node Caption, Parent Node Id, Parent Node Caption, Level, CourseNo
    - Level 1: University
    - Level 2: CLASS_COLLEGE_SHORT
    - Level 3: CLASS_DEPARTMENT_ID
    - Level 4: CLASS_ID
    """
    courses, latest_synced = get_latest_course_load()
    if not latest_synced or not courses:
        flash(
            'No synced course data found. Run a HANA/database sync before creating the hierarchy file.',
            'warning'
        )
        return redirect(url_for('tracking.index'))

    hierarchy_rows, errors = build_hierarchy_rows(courses)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Node Id',
        'Node Caption',
        'Parent Node Id',
        'Parent Node Caption',
        'Level',
        'CourseNo',
    ])
    writer.writerows(hierarchy_rows)

    QBAuditLog.log_action('hierarchy_export', current_user,
                          details={
                              'latest_course_load': latest_synced.isoformat(),
                              'courses_loaded': len(courses),
                              'rows_exported': len(hierarchy_rows),
                              'errors_count': len(errors),
                              'errors': errors[:10],
                          })
    db.session.commit()

    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'hierarchy_export_{timestamp}.csv'

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@tracking_bp.route('/dra-preview')
@super_admin_required
def dra_preview():
    """Preview DRA export data before downloading.

    Only includes admins with the S flag (has_static_report_access = True)
    Handles multiple course assignments and wildcard prefixes.
    """
    # Determine layout: 'old' (default) or 'new' college codes
    use_new_codes = request.args.get('layout') == 'new'

    # Get all active admins with S flag (excluding super admins)
    admins = Admin.query.filter(
        Admin.is_active == True,
        Admin.role != 'super_admin',
        Admin.has_static_report_access == True  # S flag filter
    ).all()

    preview_data = []
    errors = []

    for admin in admins:
        try:
            # Check if this is a course coordinator using new property
            is_course_coordinator = admin.is_course_coordinator

            if is_course_coordinator:
                target_type = 'CRS1'

                # Get all course patterns for display
                course_patterns = admin.all_course_patterns

                # Get all class IDs
                class_ids, coord_errors = get_course_class_ids_for_coordinator(admin)
                errors.extend(coord_errors)

                # Build display string for course assignments
                if course_patterns:
                    pattern_strs = [
                        f"{p['prefix']} {p['number'] if p['number'] else '(all)'}"
                        for p in course_patterns
                    ]
                    course_display = ', '.join(pattern_strs)
                else:
                    course_display = 'Unknown'

                for class_id in class_ids:
                    preview_data.append({
                        'source': class_id,
                        'target': admin.linkblue,
                        'targetType': target_type,
                        'admin_name': admin.full_name,
                        'contact_type': f'Course: {course_display}'
                    })

            elif admin.contact_type == 'College' or admin.role == 'college_admin':
                target_type = 'C4'
                college_source, college_error = get_college_source_for_dra(admin, use_new_codes=use_new_codes)
                if college_error:
                    errors.append(college_error)
                    continue

                preview_data.append({
                    'source': college_source,
                    'target': admin.linkblue,
                    'targetType': target_type,
                    'admin_name': admin.full_name,
                    'contact_type': 'College'
                })

            elif admin.contact_type == 'Department' or admin.role == 'dept_admin':
                target_type = 'D3'

                department_sources, department_errors = get_department_sources_for_dra(admin)
                errors.extend(department_errors)

                for department_source in department_sources:
                    dept = Department.query.get(department_source)
                    dept_name = dept.name if dept else department_source
                    preview_data.append({
                        'source': department_source,
                        'target': admin.linkblue,
                        'targetType': target_type,
                        'admin_name': admin.full_name,
                        'contact_type': f'Department: {dept_name}'
                    })

        except Exception as e:
            errors.append(f"Error: {admin.linkblue} - {str(e)}")

    # Stats
    stats = {
        'total_rows': len(preview_data),
        'college_rows': len([r for r in preview_data if r['targetType'] == 'C4']),
        'department_rows': len([r for r in preview_data if r['targetType'] == 'D3']),
        'course_rows': len([r for r in preview_data if r['targetType'] == 'CRS1']),
        'errors': len(errors)
    }

    return render_template('tracking/dra_preview.html',
                           preview_data=preview_data,
                           errors=errors,
                           stats=stats,
                           layout=request.args.get('layout', 'old'))


# ---------------------------------------------------------------------------
# Course history search & detail
# ---------------------------------------------------------------------------

COURSE_HISTORY_PAGE_SIZE = 50


@tracking_bp.route('/course-history')
@login_required
def course_history_search():
    """Unified search: course code/title/section, instructor name, student name."""
    q = request.args.get('q', '').strip()
    results = []
    if q:
        results = _search_courses(q)
    return render_template('tracking/course_history.html', q=q, results=results)


@tracking_bp.route('/course-history/<path:section_key>')
@login_required
def course_history_detail(section_key):
    """View change history for a specific course section."""
    course = Course.query.get_or_404(section_key)

    filters = {
        'entity_type': request.args.get('entity_type', '').strip(),
        'change_type': request.args.get('change_type', '').strip(),
        'sync_run': request.args.get('sync_run', '').strip(),
        'date_from': request.args.get('date_from', '').strip(),
        'date_to': request.args.get('date_to', '').strip(),
        'user': request.args.get('user', '').strip(),  # highlight / filter person
        'show_noise': request.args.get('show_noise', '').strip() in ('1', 'true', 'yes'),
        'page': max(1, request.args.get('page', 1, type=int) or 1),
    }

    change_page = _get_course_changes(section_key, filters)
    grouped = _group_changes_by_sync_run(change_page['items'])

    history_meta = _course_history_meta(course, change_page['total_raw'], change_page['total'])

    instructors = Instructor.query.filter_by(section_key=section_key).all()
    related = Course.query.filter(
        Course.class_id == course.class_id,
        Course.section_key != section_key
    ).order_by(Course.term_code.desc()).limit(20).all()
    return render_template(
        'tracking/course_history_detail.html',
        course=course,
        changes=change_page['items'],
        grouped_changes=grouped,
        pagination=change_page,
        filters=filters,
        history_meta=history_meta,
        instructors=instructors,
        related=related,
        highlight_user=filters['user'],
    )


def _search_courses(q, limit=100):
    """Search courses by code/title/section_key and by instructor/student name.

    Returns a list of dict-like rows with an extra ``match_reason`` explaining
    why the course was included (course code, instructor, student, etc.).
    """
    q = (q or '').strip()
    if not q:
        return []

    norm = re.sub(r'[\s\-]', '', q).lower()
    name_like = f'%{q.lower()}%'
    # Split "last, first" or "first last" for multi-token name matching.
    name_parts = [p for p in re.split(r'[\s,]+', q) if p]

    def _person_name_filter(first_col, last_col, user_id_col, email_col=None):
        """Match full query OR (for multi-token) every token against name fields."""
        clauses = [
            func.lower(func.coalesce(first_col, '')).like(name_like),
            func.lower(func.coalesce(last_col, '')).like(name_like),
            func.lower(user_id_col).like(name_like),
        ]
        if email_col is not None:
            clauses.append(func.lower(func.coalesce(email_col, '')).like(name_like))
        if len(name_parts) >= 2:
            token_ands = []
            for part in name_parts:
                pl = f'%{part.lower()}%'
                token_ands.append(
                    or_(
                        func.lower(func.coalesce(first_col, '')).like(pl),
                        func.lower(func.coalesce(last_col, '')).like(pl),
                        func.lower(user_id_col).like(pl),
                    )
                )
            clauses.append(and_(*token_ands))
        return or_(*clauses)

    # 1) Course code / section_key / title / class_id
    course_rows = db.session.execute(text("""
        SELECT section_key, class_code, section_id, section_title,
               term_code, college_code, class_id,
               'course' AS match_kind,
               COALESCE(class_code, section_key) AS match_detail
        FROM courses
        WHERE LOWER(REPLACE(REPLACE(COALESCE(class_code, ''), ' ', ''), '-', '')) LIKE :prefix
           OR LOWER(REPLACE(REPLACE(section_key, ' ', ''), '-', '')) LIKE :prefix
           OR LOWER(COALESCE(section_title, '')) LIKE :title_like
           OR LOWER(COALESCE(class_id, '')) LIKE :title_like
        ORDER BY term_code DESC, class_code, section_id
        LIMIT :lim
    """), {
        'prefix': f'%{norm}%',
        'title_like': f'%{q.lower()}%',
        'lim': limit,
    }).mappings().all()

    seen = {r['section_key']: dict(r) for r in course_rows}
    for sk, row in seen.items():
        row['match_reason'] = f"Course: {row.get('match_detail') or sk}"

    # 2) Instructor name / linkblue
    instructor_q = (
        db.session.query(
            Course.section_key,
            Course.class_code,
            Course.section_id,
            Course.section_title,
            Course.term_code,
            Course.college_code,
            Course.class_id,
            Instructor.user_id,
            Instructor.first_name,
            Instructor.last_name,
        )
        .join(Instructor, Instructor.section_key == Course.section_key)
        .filter(_person_name_filter(
            Instructor.first_name, Instructor.last_name, Instructor.user_id,
        ))
        .order_by(Course.term_code.desc())
        .limit(limit)
    )

    for row in instructor_q.all():
        sk = row.section_key
        label = f"{(row.first_name or '')} {(row.last_name or '')}".strip() or row.user_id
        reason = f"Instructor: {label} ({row.user_id})"
        if sk not in seen:
            seen[sk] = {
                'section_key': sk,
                'class_code': row.class_code,
                'section_id': row.section_id,
                'section_title': row.section_title,
                'term_code': row.term_code,
                'college_code': row.college_code,
                'class_id': row.class_id,
                'match_kind': 'instructor',
                'match_detail': row.user_id,
                'match_reason': reason,
                'matched_user_id': row.user_id,
            }
        else:
            if seen[sk].get('match_kind') == 'course':
                seen[sk]['match_reason'] = (
                    f"{seen[sk]['match_reason']}; also instructor {label}"
                )
            seen[sk].setdefault('matched_user_id', row.user_id)

    # 3) Student name / linkblue via CourseUser
    student_q = (
        db.session.query(
            Course.section_key,
            Course.class_code,
            Course.section_id,
            Course.section_title,
            Course.term_code,
            Course.college_code,
            Course.class_id,
            CourseUser.user_id,
            CourseUser.first_name,
            CourseUser.last_name,
        )
        .join(StudentEnrollment, StudentEnrollment.section_key == Course.section_key)
        .join(CourseUser, CourseUser.user_id == StudentEnrollment.user_id)
        .filter(_person_name_filter(
            CourseUser.first_name, CourseUser.last_name, CourseUser.user_id,
            email_col=CourseUser.email,
        ))
        .order_by(Course.term_code.desc())
        .limit(limit)
    )

    for row in student_q.all():
        sk = row.section_key
        label = f"{(row.first_name or '')} {(row.last_name or '')}".strip() or row.user_id
        reason = f"Student: {label} ({row.user_id})"
        if sk not in seen:
            seen[sk] = {
                'section_key': sk,
                'class_code': row.class_code,
                'section_id': row.section_id,
                'section_title': row.section_title,
                'term_code': row.term_code,
                'college_code': row.college_code,
                'class_id': row.class_id,
                'match_kind': 'student',
                'match_detail': row.user_id,
                'match_reason': reason,
                'matched_user_id': row.user_id,
            }
        else:
            if seen[sk].get('match_kind') == 'course':
                seen[sk]['match_reason'] = (
                    f"{seen[sk]['match_reason']}; also student {label}"
                )
            seen[sk].setdefault('matched_user_id', row.user_id)

    # Sort: term desc, class code
    results = list(seen.values())
    results.sort(
        key=lambda r: (
            r.get('term_code') or '',
            r.get('class_code') or '',
            r.get('section_id') or '',
        ),
        reverse=True,
    )
    return results[:limit]


def _is_noop_change(ch):
    """True when an 'updated' row has Old == New after type normalization.

    Filters historical false-positive date diffs without deleting them.
    """
    if getattr(ch, 'change_type', None) != 'updated':
        return False
    return normalize_diff_value(ch.old_value) == normalize_diff_value(ch.new_value)


def _course_change_base_query(section_key):
    """All change_log rows for a section (course, instructors, students, counts)."""
    return ChangeLog.query.filter(
        or_(
            ChangeLog.entity_key == section_key,
            ChangeLog.entity_key.like(f'{section_key}|%'),
        )
    )


def _get_course_changes(section_key, filters=None):
    """Paginated, filterable change log for a course section.

    By default, no-op Updated rows (Old == New after normalization) are hidden
    so historical date-type false positives do not clutter the timeline.
    Pass show_noise=True to include them.
    """
    filters = filters or {}
    page = max(1, int(filters.get('page') or 1))
    per_page = COURSE_HISTORY_PAGE_SIZE
    show_noise = bool(filters.get('show_noise'))

    query = _course_change_base_query(section_key)
    total_raw = query.count()

    entity_type = filters.get('entity_type') or ''
    if entity_type:
        # Treat 'student' filter as student + student_count for usability.
        if entity_type == 'student':
            query = query.filter(ChangeLog.entity_type.in_(['student', 'student_count']))
        else:
            query = query.filter(ChangeLog.entity_type == entity_type)

    change_type = filters.get('change_type') or ''
    if change_type:
        query = query.filter(ChangeLog.change_type == change_type)

    sync_run = filters.get('sync_run') or ''
    if sync_run:
        try:
            query = query.filter(ChangeLog.sync_run_id == int(sync_run))
        except (TypeError, ValueError):
            pass

    date_from = filters.get('date_from') or ''
    if date_from:
        try:
            dt = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(ChangeLog.created_at >= dt)
        except ValueError:
            pass

    date_to = filters.get('date_to') or ''
    if date_to:
        try:
            dt = datetime.strptime(date_to, '%Y-%m-%d')
            # Inclusive end-of-day
            dt = dt.replace(hour=23, minute=59, second=59)
            query = query.filter(ChangeLog.created_at <= dt)
        except ValueError:
            pass

    user = filters.get('user') or ''
    if user:
        # entity_key is "{section}|{user_id}" for people; also match display_label.
        query = query.filter(
            or_(
                ChangeLog.entity_key.like(f'%|{user}'),
                ChangeLog.entity_key.like(f'%|{user}|%'),
                ChangeLog.display_label.ilike(f'%{user}%'),
            )
        )

    # Fetch a window large enough to filter no-ops in Python while still paging.
    # No-ops are rare going forward; for noisy historical data we may scan more.
    ordered = query.order_by(ChangeLog.created_at.desc(), ChangeLog.id.desc())

    if show_noise:
        total = ordered.count()
        items = ordered.offset((page - 1) * per_page).limit(per_page).all()
    else:
        # Pull candidates; filter no-ops; page in memory for correctness.
        # Cap scan to avoid unbounded memory on pathological sections.
        scan_cap = 5000
        candidates = ordered.limit(scan_cap).all()
        filtered = [c for c in candidates if not _is_noop_change(c)]
        total = len(filtered)
        if len(candidates) >= scan_cap:
            # Approximate: there may be more; still show what we scanned.
            pass
        start = (page - 1) * per_page
        items = filtered[start:start + per_page]

    pages = max(1, math.ceil(total / per_page)) if total else 1
    return {
        'items': items,
        'page': page,
        'per_page': per_page,
        'total': total,
        'total_raw': total_raw,
        'pages': pages,
        'has_prev': page > 1,
        'has_next': page < pages,
    }


def _group_changes_by_sync_run(changes):
    """Group change rows by sync_run_id for timeline display."""
    groups = []
    by_run = defaultdict(list)
    order = []
    for ch in changes:
        rid = ch.sync_run_id
        if rid not in by_run:
            order.append(rid)
        by_run[rid].append(ch)
    for rid in order:
        rows = by_run[rid]
        first = rows[0]
        run = first.sync_run if first else None
        groups.append({
            'sync_run_id': rid,
            'started_at': run.started_at if run else first.created_at,
            'changes': rows,
            'count': len(rows),
        })
    return groups


def _course_history_meta(course, total_raw, total_visible):
    """Honest empty-state / baseline messaging for a course timeline."""
    first_seen = getattr(course, 'first_seen_in_tracking_at', None)
    has_any = (total_raw or 0) > 0
    has_visible = (total_visible or 0) > 0

    if has_visible:
        status = 'has_history'
        message = None
    elif has_any and not has_visible:
        status = 'only_noise'
        message = (
            'This course only has no-op history rows (same Old and New values, '
            'usually from a past date-type comparison bug). Meaningful changes '
            'will appear here once they occur. Toggle “Show no-op rows” to view '
            'the raw log.'
        )
    elif first_seen:
        status = 'tracking_baseline'
        message = (
            f'Tracking began {first_seen.strftime("%Y-%m-%d %H:%M")} UTC for this '
            f'course. No changes have been recorded since that baseline. '
            f'This is not a proven original creation date — only when change '
            f'tracking first observed the row.'
        )
    else:
        status = 'no_tracking_yet'
        message = (
            'No change history is available for this course. Change tracking may '
            'not have run against it yet, or the course predated tracking and has '
            'not been stamped with a baseline. We do not invent a creation date '
            'when none was recorded. After the next successful sync that includes '
            'this section, a “tracking began” baseline will appear here.'
        )

    return {
        'status': status,
        'message': message,
        'first_seen_in_tracking_at': first_seen,
        'total_raw': total_raw,
        'total_visible': total_visible,
    }
