"""Validate and apply atomic, field-specific local TCE edits."""
import json
from datetime import date

from flask import g, has_request_context

from app.models import db
from app.models.course_tce import CourseTCEAudit, CourseTCEOverride


def course_overrides():
    if has_request_context() and hasattr(g, 'course_tce_overrides'):
        return g.course_tce_overrides
    rules = {rule.section_key: rule for rule in CourseTCEOverride.query.all()}
    if has_request_context():
        g.course_tce_overrides = rules
    return rules


def _values(rule):
    return {
        'tce_start': rule.tce_start.isoformat() if rule and rule.tce_start else None,
        'tce_end': rule.tce_end.isoformat() if rule and rule.tce_end else None,
        'blocked': bool(rule and rule.blocked),
    }


def update_course_tce(courses, form, actor):
    """Stage edits and audit rows. Caller commits once for the entire selection."""
    if not actor.is_super_admin():
        raise PermissionError('Only super administrators can change TCE settings.')
    reason = form.get('reason', '').strip()
    if not reason or len(reason) > 2000:
        raise ValueError('Provide a reason of 1–2,000 characters.')

    changes = {}
    for field, label in (('tce_start', 'TCE start'), ('tce_end', 'TCE end')):
        action = form.get(field + '_action', 'keep')
        if action == 'set':
            try:
                raw = form.get(field, '')
                parsed = date.fromisoformat(raw)
                if parsed.isoformat() != raw:
                    raise ValueError
                changes[field] = parsed
            except ValueError:
                raise ValueError(f'Provide a valid {label} date (YYYY-MM-DD).') from None
        elif action == 'restore':
            changes[field] = None
        elif action != 'keep':
            raise ValueError(f'Unknown {label} action.')
    flag_action = form.get('tce_flag_action', 'keep')
    if flag_action in ('off', 'restore'):
        changes['blocked'] = flag_action == 'off'
    elif flag_action != 'keep':
        raise ValueError('Unknown TCE flag action.')
    if not changes:
        raise ValueError('Select at least one TCE field to change.')

    # Validate every selected course before staging any changes. In particular,
    # a start-only edit must respect each course's unchanged effective end date.
    rules = {rule.section_key: rule for rule in CourseTCEOverride.query.filter(
        CourseTCEOverride.section_key.in_([course.section_key for course in courses])
    ).with_for_update().all()}
    pending = []
    for course in courses:
        rule = rules.get(course.section_key)
        values = {field: getattr(rule, field) if rule else None for field in ('tce_start', 'tce_end')}
        values['blocked'] = bool(rule and rule.blocked)
        values.update(changes)
        if 'tce_start' in changes or 'tce_end' in changes:
            college_rule = course.date_override
            start = values['tce_start'] or (college_rule.tce_start if college_rule else course.sap_tce_start)
            end = values['tce_end'] or (college_rule.tce_end if college_rule else course.sap_tce_end)
            if start and end and end < start:
                raise ValueError(f'{course.section_key}: TCE end must be on or after TCE start. No courses were changed.')
        pending.append((course, rule, values))

    changed = 0
    for course, rule, values in pending:
        before = _values(rule)
        after = {field: value.isoformat() if isinstance(value, date) else value
                 for field, value in values.items()}
        if before == after:
            continue
        if not any(values.values()):
            db.session.delete(rule)
        else:
            if rule is None:
                rule = CourseTCEOverride(section_key=course.section_key)
                db.session.add(rule)
            for field, value in values.items():
                setattr(rule, field, value)
            rule.reason, rule.updated_by_id = reason, actor.id
        db.session.add(CourseTCEAudit(
            section_key=course.section_key, before_json=json.dumps(before),
            after_json=json.dumps(after), reason=reason, actor_id=actor.id,
        ))
        changed += 1
    if has_request_context():
        g.pop('course_tce_overrides', None)
    return changed
