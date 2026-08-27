"""Super-admin pages for collaborative TCE report checklists."""

from functools import wraps

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import db
from app.models.admin import Admin
from app.models.checklist import (
    ChecklistAuditLog,
    ReportChecklistTemplate,
    SemesterChecklist,
    SemesterChecklistItem,
    SemesterReportChecklist,
)
from app.services.checklist_service import (
    ChecklistConflictError,
    ChecklistValidationError,
    archive_checklist,
    create_semester_checklist,
    group_report_items,
    reopen_checklist,
    update_checklist_item,
    update_report_metadata,
    update_template,
)


checklists_bp = Blueprint('checklists', __name__)


def super_admin_required(view):
    """Protect all checklist pages and APIs with server-side role checks."""
    @wraps(view)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_super_admin():
            if request.is_json:
                return jsonify({'ok': False, 'error': 'Super administrator access is required.'}), 403
            flash('You do not have permission to access TCE checklists.', 'danger')
            return redirect(url_for('main.dashboard'))
        return view(*args, **kwargs)
    return decorated


def _active_super_admins():
    return Admin.query.filter_by(role='super_admin', is_active=True).order_by(
        Admin.last_name, Admin.first_name, Admin.linkblue
    ).all()


@checklists_bp.route('/')
@super_admin_required
def index():
    query = SemesterChecklist.query
    status = request.args.get('status', '').strip()
    search = request.args.get('search', '').strip()
    if status:
        query = query.filter(SemesterChecklist.status == status)
    if search:
        pattern = f'%{search}%'
        query = query.filter(db.or_(
            SemesterChecklist.term_code.ilike(pattern),
            SemesterChecklist.display_name.ilike(pattern),
            SemesterChecklist.project_name.ilike(pattern),
        ))
    semesters = query.order_by(SemesterChecklist.created_at.desc()).all()
    return render_template(
        'checklists/index.html',
        semesters=semesters,
        current_filters={'status': status, 'search': search},
    )


@checklists_bp.route('/new', methods=['GET', 'POST'])
@super_admin_required
def create():
    templates = ReportChecklistTemplate.query.filter_by(
        is_active=True, reference_only=False
    ).order_by(ReportChecklistTemplate.sort_order).all()
    prior_semesters = SemesterChecklist.query.order_by(
        SemesterChecklist.created_at.desc()
    ).all()

    if request.method == 'POST':
        source_mode = request.form.get('source_mode', 'templates')
        source = None
        selected_template_ids = None
        if source_mode == 'copy':
            source_id = request.form.get('source_checklist_id', type=int)
            source = db.session.get(SemesterChecklist, source_id) if source_id else None
            if source is None:
                flash('Select a prior semester to copy.', 'danger')
                return render_template(
                    'checklists/create.html', templates=templates, prior_semesters=prior_semesters
                )
        else:
            selected_template_ids = request.form.getlist('template_id', type=int)

        try:
            semester = create_semester_checklist(
                term_code=request.form.get('term_code'),
                display_name=request.form.get('display_name'),
                project_name=request.form.get('project_name'),
                actor=current_user,
                source_checklist=source,
                selected_template_ids=selected_template_ids,
            )
        except ChecklistValidationError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        else:
            flash(f'Created checklist for {semester.display_name}.', 'success')
            return redirect(url_for('checklists.detail', checklist_id=semester.id))

    preselected_source = request.args.get('source', type=int)
    return render_template(
        'checklists/create.html',
        templates=templates,
        prior_semesters=prior_semesters,
        preselected_source=preselected_source,
    )


@checklists_bp.route('/<int:checklist_id>')
@super_admin_required
def detail(checklist_id):
    semester = SemesterChecklist.query.get_or_404(checklist_id)
    report_groups = {
        report.id: group_report_items(report)
        for report in semester.reports
    }
    return render_template(
        'checklists/detail.html',
        semester=semester,
        report_groups=report_groups,
        super_admins=_active_super_admins(),
    )


@checklists_bp.route('/items/<int:item_id>/update', methods=['POST'])
@super_admin_required
def update_item(item_id):
    item = SemesterChecklistItem.query.get_or_404(item_id)
    payload = request.get_json(silent=True) or {}
    try:
        update_checklist_item(item=item, payload=payload, actor=current_user)
    except ChecklistConflictError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc), 'conflict': True}), 409
    except ChecklistValidationError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400

    report = item.report_checklist
    semester = report.semester_checklist
    return jsonify({
        'ok': True,
        'item': {
            'id': item.id,
            'status': item.status,
            'version': item.version,
            'creator_id': item.creator_id,
            'creator_name': item.creator.full_name if item.creator else None,
            'verifier_id': item.verifier_id,
            'verifier_name': item.verifier.full_name if item.verifier else None,
            'completed_at': item.completed_at.isoformat() if item.completed_at else None,
            'verified_at': item.verified_at.isoformat() if item.verified_at else None,
            'updated_by': item.updated_by.full_name if item.updated_by else None,
            'updated_at': item.updated_at.isoformat() if item.updated_at else None,
        },
        'report': {
            'id': report.id,
            'status': report.status,
            'status_display': report.status_display,
            'progress_percent': report.progress_percent,
            'counts': report.item_counts,
        },
        'semester': {
            'status': semester.status,
            'status_display': semester.status_display,
            'progress_percent': semester.progress_percent,
            'verified_percent': semester.verified_percent,
            'counts': semester.item_counts,
        },
    })


@checklists_bp.route('/reports/<int:report_id>/update', methods=['POST'])
@super_admin_required
def update_report(report_id):
    report = SemesterReportChecklist.query.get_or_404(report_id)
    payload = request.get_json(silent=True) or {}
    try:
        update_report_metadata(report=report, payload=payload, actor=current_user)
    except ChecklistValidationError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify({
        'ok': True,
        'report': {
            'id': report.id,
            'status': report.status,
            'status_display': report.status_display,
            'published_status': report.published_status,
        },
        'semester_status': report.semester_checklist.status,
    })


@checklists_bp.route('/<int:checklist_id>/archive', methods=['POST'])
@super_admin_required
def archive(checklist_id):
    semester = SemesterChecklist.query.get_or_404(checklist_id)
    try:
        archive_checklist(semester=semester, actor=current_user)
    except ChecklistValidationError as exc:
        flash(str(exc), 'warning')
    else:
        flash(f'{semester.display_name} was archived and is now read-only.', 'success')
    return redirect(url_for('checklists.detail', checklist_id=semester.id))


@checklists_bp.route('/<int:checklist_id>/reopen', methods=['POST'])
@super_admin_required
def reopen(checklist_id):
    semester = SemesterChecklist.query.get_or_404(checklist_id)
    try:
        reopen_checklist(
            semester=semester,
            actor=current_user,
            reason=request.form.get('reason'),
        )
    except ChecklistValidationError as exc:
        flash(str(exc), 'danger')
    else:
        flash(f'{semester.display_name} was reopened.', 'success')
    return redirect(url_for('checklists.detail', checklist_id=semester.id))


@checklists_bp.route('/<int:checklist_id>/audit')
@super_admin_required
def audit(checklist_id):
    semester = SemesterChecklist.query.get_or_404(checklist_id)
    query = ChecklistAuditLog.query.filter_by(semester_checklist_id=semester.id)
    action = request.args.get('action', '').strip()
    entity_type = request.args.get('entity_type', '').strip()
    actor = request.args.get('actor', '').strip()
    if action:
        query = query.filter(ChecklistAuditLog.action == action)
    if entity_type:
        query = query.filter(ChecklistAuditLog.entity_type == entity_type)
    if actor:
        query = query.filter(ChecklistAuditLog.actor_linkblue.ilike(f'%{actor}%'))
    page = request.args.get('page', 1, type=int)
    pagination = query.order_by(ChecklistAuditLog.timestamp.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    actions = [row[0] for row in db.session.query(ChecklistAuditLog.action).filter_by(
        semester_checklist_id=semester.id
    ).distinct().order_by(ChecklistAuditLog.action).all()]
    entity_types = [row[0] for row in db.session.query(ChecklistAuditLog.entity_type).filter_by(
        semester_checklist_id=semester.id
    ).distinct().order_by(ChecklistAuditLog.entity_type).all()]
    return render_template(
        'checklists/audit.html',
        semester=semester,
        logs=pagination.items,
        pagination=pagination,
        action_options=actions,
        entity_type_options=entity_types,
        current_filters={'action': action, 'entity_type': entity_type, 'actor': actor},
    )


@checklists_bp.route('/templates')
@super_admin_required
def templates():
    all_templates = ReportChecklistTemplate.query.order_by(
        ReportChecklistTemplate.sort_order
    ).all()
    return render_template('checklists/templates.html', templates=all_templates)


@checklists_bp.route('/templates/<int:template_id>/edit', methods=['GET', 'POST'])
@super_admin_required
def edit_template(template_id):
    template = ReportChecklistTemplate.query.get_or_404(template_id)
    if request.method == 'POST':
        try:
            changed = update_template(template=template, form=request.form, actor=current_user)
        except ChecklistValidationError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        else:
            if changed:
                flash(f'{template.title} was saved as template version {template.version}.', 'success')
            else:
                flash('No template changes were detected.', 'info')
            return redirect(url_for('checklists.edit_template', template_id=template.id))
    template_logs = ChecklistAuditLog.query.filter_by(
        entity_type='checklist_template', entity_id=template.id
    ).order_by(ChecklistAuditLog.timestamp.desc()).limit(25).all()
    return render_template(
        'checklists/edit_template.html', template=template, template_logs=template_logs
    )
