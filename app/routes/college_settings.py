"""Super-admin college policy forms and append-only audit history."""
import json
import secrets
from datetime import date

from flask import abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user

from app.models import db
from app.models.course import College, Course
from app.models.college_policy import CollegePolicy, CollegeDateOverride, CollegePolicyAudit
from app.routes.settings import settings_bp, super_admin_required
from app.services.college_policy import thresholds


def _dates(rule):
    return {'start': rule.tce_start.isoformat(), 'end': rule.tce_end.isoformat()} if rule else {}


@settings_bp.route('/college-policies', methods=['GET', 'POST'])
@super_admin_required
def college_policies():
    session.setdefault('college_policy_csrf', secrets.token_urlsafe(32))
    colleges = College.query.order_by(College.name).all()
    code = (request.values.get('college') or '').strip()
    if not code and colleges:
        code = next((c.code for c in colleges if 'pharmacy' in c.name.casefold()), colleges[0].code)
    college = db.session.get(College, code) if code else None
    if code and not college:
        abort(404)
    terms = [t for (t,) in db.session.query(Course.term_code).filter(
        Course.college_code == code, Course.term_code.isnot(None), Course.term_code != ''
    ).distinct().order_by(Course.term_code.desc()).all()]
    limits = thresholds()
    if request.method == 'POST':
        token = request.form.get('csrf_token', '')
        if not secrets.compare_digest(token, session['college_policy_csrf']):
            abort(400, 'Invalid form token. Reload the settings page.')
        if not college:
            abort(400, 'Select a college.')
        action = request.form.get('action')
        reason = request.form.get('reason', '').strip()
        term = request.form.get('term', '').strip()
        try:
            if not reason or len(reason) > 2000:
                raise ValueError('Provide a reason of 1–2,000 characters.')
            if action == 'exclusion':
                raw = request.form.get('threshold', '').strip()
                limit = None
                if raw:
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
                        raise ValueError('Section threshold must be a whole number from 0 to 999999, or blank.')
                    limit = int(raw)
                before = {'exclude_sections_above': limits.get(code)}
                rule = db.session.get(CollegePolicy, code)
                if rule is None:
                    rule = CollegePolicy(college_code=code)
                    db.session.add(rule)
                rule.exclude_sections_above = limit
                after = {'exclude_sections_above': limit}
                term = None
            elif action in {'dates', 'restore'}:
                if request.form.get('acknowledge') != 'yes':
                    raise ValueError('Acknowledge the data integrity warning before changing dates.')
                rule = db.session.get(CollegeDateOverride, (code, term))
                if not term or (term not in terms and rule is None):
                    raise ValueError('Select an academic term belonging to this college.')
                before = _dates(rule)
                if action == 'restore':
                    if rule is None:
                        raise ValueError('There is no override for that college and term.')
                    db.session.delete(rule)
                    after = {}
                else:
                    try:
                        start = date.fromisoformat(request.form.get('tce_start', ''))
                        end = date.fromisoformat(request.form.get('tce_end', ''))
                    except ValueError:
                        raise ValueError('Provide valid TCE start and end dates.') from None
                    if end < start:
                        raise ValueError('TCE end date must be on or after the start date.')
                    if rule is None:
                        rule = CollegeDateOverride(college_code=code, term_code=term)
                        db.session.add(rule)
                    rule.tce_start, rule.tce_end = start, end
                    rule.reason, rule.updated_by_id = reason, current_user.id
                    after = _dates(rule)
            else:
                raise ValueError('Unknown settings action.')
            db.session.add(CollegePolicyAudit(
                college_code=code, term_code=term, action=action,
                before_json=json.dumps(before), after_json=json.dumps(after),
                reason=reason, actor_id=current_user.id,
            ))
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return redirect(url_for('settings.college_policies', college=code))
        flash('College settings saved. Changes apply immediately here and to the next Blue sync.', 'success')
        return redirect(url_for('settings.college_policies', college=code))
    overrides = CollegeDateOverride.query.filter_by(college_code=code).order_by(
        CollegeDateOverride.term_code.desc()).all()
    return render_template(
        'settings/college_policies.html', colleges=colleges, college=college,
        threshold=limits.get(code), terms=terms, overrides=overrides,
        override_map={o.term_code: o for o in overrides},
        csrf_token=session['college_policy_csrf'],
        audit=CollegePolicyAudit.query.filter_by(college_code=code).order_by(
            CollegePolicyAudit.created_at.desc(), CollegePolicyAudit.id.desc()).limit(50).all(),
    )
