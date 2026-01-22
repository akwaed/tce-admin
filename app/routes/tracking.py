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
from app.models.course import College, Department, Course
from app.services.backup_service import get_backup_service
from functools import wraps
from datetime import datetime
import csv
import io

tracking_bp = Blueprint('tracking', __name__)

# DRA College Code Mapping - Maps internal college codes to Explorance DRA codes
DRA_COLLEGE_CODES = {
    'AS': '8E000',    # Arts and Sciences
    'FA': '8X000',    # Fine Arts
    'AG': '81010',    # Ag, Food and Environment
    'BE': '8F000',    # Business & Economics
    'EN': '8H000',    # Engineering
    'ME': '7H000',    # Medicine
    'DE': '8N000',    # Design
    'HS': '7N800',    # Health Sciences
    'PH': '7P610',    # Public Health
    'ED': '8G000',    # Education
    'DEN': '7A000',   # Dentistry
    'CI': '8M000',    # Communication and Information
    'SW': '8T110',    # Social Work
    'GS': '8W300',    # Graduate School
    'UE': '8Z110',    # Undergraduate Education
    'LHC': '30000055',  # Lewis Honors College
    'LA': '8K000',    # Law
    'NU': '7E000',    # Nursing
    'PHA': '7K000',   # Pharmacy
}


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
    # Get filter parameters
    action_filter = request.args.get('action', '')
    admin_filter = request.args.get('admin', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    page = request.args.get('page', 1, type=int)
    per_page = 25

    # Build query
    query = QBAuditLog.query

    if action_filter:
        query = query.filter(QBAuditLog.action == action_filter)

    if admin_filter:
        query = query.filter(QBAuditLog.admin_linkblue.ilike(f'%{admin_filter}%'))

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
                               'date_to': date_to
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

    if backup_type == 'both':
        qb_backup, qm_backup = backup_service.create_both_backups('manual', current_user)
        if qb_backup or qm_backup:
            flash('Manual backups created successfully.', 'success')
        else:
            flash('No files to backup.', 'warning')
    elif backup_type in ['qb', 'qm']:
        backup = backup_service.create_backup(backup_type, 'manual', current_user)
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


@tracking_bp.route('/export-dra')
@super_admin_required
def export_dra():
    """
    Export DRA (Data Relationship Assignment) file for Explorance Blue.

    Format: source,target,targetType
    - source: The organizational unit code (college DRA code, department ID, or class ID)
    - target: The admin's linkblue
    - targetType: C4 (college), D3 (department), or CRS1 (course)
    """
    # Get all active admins (excluding super admins who don't have DRA assignments)
    admins = Admin.query.filter(
        Admin.is_active == True,
        Admin.role != 'super_admin'
    ).all()

    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['source', 'target', 'targetType'])

    rows_written = 0
    errors = []

    for admin in admins:
        try:
            # Determine the target type and source based on contact level
            if admin.contact_type == 'College' or admin.role == 'college_admin':
                # College-level contact
                target_type = 'C4'
                # Map internal college code to DRA code
                dra_code = DRA_COLLEGE_CODES.get(admin.college_code)
                if not dra_code:
                    # Try to find from College table
                    college = College.query.get(admin.college_code)
                    if college:
                        # If no mapping, use a default pattern or skip
                        errors.append(f"No DRA code mapping for college {admin.college_code}")
                        continue
                    else:
                        errors.append(f"College not found: {admin.college_code} for {admin.linkblue}")
                        continue

                writer.writerow([dra_code, admin.linkblue, target_type])
                rows_written += 1

            elif admin.contact_type == 'Department' or admin.role == 'dept_admin':
                # Department-level contact
                target_type = 'D3'

                # Check if admin has multiple departments
                if admin.departments.count() > 0:
                    for dept in admin.departments.all():
                        writer.writerow([dept.id, admin.linkblue, target_type])
                        rows_written += 1
                elif admin.department_id:
                    writer.writerow([admin.department_id, admin.linkblue, target_type])
                    rows_written += 1
                else:
                    errors.append(f"No department ID for dept admin {admin.linkblue}")

            elif admin.course_prefix and admin.course_number:
                # Course coordinator
                target_type = 'CRS1'

                # Find all courses matching this prefix and number
                class_pattern = f"{admin.course_prefix} {admin.course_number}"
                courses = Course.query.filter(
                    Course.class_code.like(f"{class_pattern}%")
                ).all()

                if courses:
                    # Get unique class_ids
                    class_ids = set()
                    for course in courses:
                        if course.class_id:
                            class_ids.add(course.class_id)

                    for class_id in class_ids:
                        writer.writerow([class_id, admin.linkblue, target_type])
                        rows_written += 1
                else:
                    errors.append(f"No courses found for coordinator {admin.linkblue} ({class_pattern})")

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


@tracking_bp.route('/dra-preview')
@super_admin_required
def dra_preview():
    """Preview DRA export data before downloading"""
    # Get all active admins (excluding super admins)
    admins = Admin.query.filter(
        Admin.is_active == True,
        Admin.role != 'super_admin'
    ).all()

    preview_data = []
    errors = []

    for admin in admins:
        try:
            if admin.contact_type == 'College' or admin.role == 'college_admin':
                target_type = 'C4'
                dra_code = DRA_COLLEGE_CODES.get(admin.college_code, f'UNMAPPED:{admin.college_code}')
                preview_data.append({
                    'source': dra_code,
                    'target': admin.linkblue,
                    'targetType': target_type,
                    'admin_name': admin.full_name,
                    'contact_type': 'College'
                })

            elif admin.contact_type == 'Department' or admin.role == 'dept_admin':
                target_type = 'D3'
                if admin.departments.count() > 0:
                    for dept in admin.departments.all():
                        preview_data.append({
                            'source': dept.id,
                            'target': admin.linkblue,
                            'targetType': target_type,
                            'admin_name': admin.full_name,
                            'contact_type': f'Department: {dept.name}'
                        })
                elif admin.department_id:
                    dept = Department.query.get(admin.department_id)
                    dept_name = dept.name if dept else admin.department_id
                    preview_data.append({
                        'source': admin.department_id,
                        'target': admin.linkblue,
                        'targetType': target_type,
                        'admin_name': admin.full_name,
                        'contact_type': f'Department: {dept_name}'
                    })
                else:
                    errors.append(f"No department for {admin.linkblue}")

            elif admin.course_prefix and admin.course_number:
                target_type = 'CRS1'
                class_pattern = f"{admin.course_prefix} {admin.course_number}"
                courses = Course.query.filter(
                    Course.class_code.like(f"{class_pattern}%")
                ).all()

                if courses:
                    class_ids = set()
                    for course in courses:
                        if course.class_id:
                            class_ids.add(course.class_id)

                    for class_id in class_ids:
                        preview_data.append({
                            'source': class_id,
                            'target': admin.linkblue,
                            'targetType': target_type,
                            'admin_name': admin.full_name,
                            'contact_type': f'Course: {class_pattern}'
                        })
                else:
                    errors.append(f"No courses for coordinator {admin.linkblue} ({class_pattern})")

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
                           dra_codes=DRA_COLLEGE_CODES)
