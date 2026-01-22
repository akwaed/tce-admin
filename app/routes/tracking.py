"""
Tracking Routes for Super Users
View audit logs for admin changes and QB/QM changes
Manage backups of QB and QM files
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.admin import Admin, AdminAuditLog
from app.models.question import QBAuditLog, QBBackup
from app.services.backup_service import get_backup_service
from functools import wraps
from datetime import datetime

tracking_bp = Blueprint('tracking', __name__)


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
