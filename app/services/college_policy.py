"""Shared policy evaluation for dashboards and outgoing SAP-sourced data."""
import csv
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import g, has_request_context
from sqlalchemy import func

from app.models import db
from app.models.college_policy import CollegePolicy, CollegeDateOverride


def default_threshold(college_name):
    return 500 if 'pharmacy' in (college_name or '').casefold() else None


def thresholds():
    from app.models.course import College
    result = {c.code: default_threshold(c.name) for c in College.query.all()}
    result.update({p.college_code: p.exclude_sections_above for p in CollegePolicy.query.all()})
    return result


def section_number(crs_section, section_key, section_label=''):
    """Use the displayed section, never SAP's unrelated SECTION_ID object ID."""
    value = (crs_section or '').strip()
    if '-' in value:
        value = value.rsplit('-', 1)[1].strip()
    elif section_key and len(section_key.split('-')) >= 3:
        value = section_key.split('-')[-2].strip()
    else:
        value = re.sub(r'^Section\s+', '', section_label or '', flags=re.I).strip()
    return int(value) if re.fullmatch(r'[0-9]+', value or '') else None


def excluded(crs_section, section_key, threshold, section_label=''):
    number = section_number(crs_section, section_key, section_label)
    return threshold is not None and number is not None and number > threshold


def visible_courses(query, user):
    """Apply college exclusions to contact views; super admins retain oversight."""
    if user.is_super_admin():
        return query
    from app.models.course import Course
    rules = {code: limit for code, limit in thresholds().items()
             if limit is not None and code == user.college_code}
    if not rules:
        return query
    # Parse in Python for consistent behavior on PostgreSQL and SQLite, including
    # alphanumeric sections and leading zeros. Only inspect configured colleges.
    hidden = [key for key, code, crs in db.session.query(
        Course.section_key, Course.college_code, Course.crs_section
    ).filter(Course.college_code.in_(rules)).all()
        if excluded(crs, key, rules[code])]
    return query.filter(~Course.section_key.in_(hidden)) if hidden else query


def date_overrides():
    if has_request_context() and hasattr(g, 'college_date_overrides'):
        return g.college_date_overrides
    result = {(o.college_code, o.term_code): o for o in CollegeDateOverride.query.all()}
    if has_request_context():
        g.college_date_overrides = result
    return result


def current_term(query, today=None):
    """Most ongoing courses wins; ties favor later starts, then term label.

    Between terms use the nearest upcoming course, otherwise the most recently
    ended course. With no usable dates fall back to the most populated term.
    """
    from app.models.course import Course
    today = today or datetime.now(ZoneInfo('America/New_York')).date()
    base = query.order_by(None).filter(Course.term_code.isnot(None), Course.term_code != '')
    row = (base.filter(Course.course_start <= today, Course.course_end >= today)
           .with_entities(Course.term_code)
           .group_by(Course.term_code)
           .order_by(func.count(Course.section_key).desc(),
                     func.max(Course.course_start).desc(), Course.term_code.desc()).first())
    if not row:
        row = (base.filter(Course.course_start > today).with_entities(Course.term_code)
               .order_by(Course.course_start.asc(), Course.term_code.desc()).first())
    if not row:
        row = (base.filter(Course.course_end < today).with_entities(Course.term_code)
               .order_by(Course.course_end.desc(), Course.term_code.desc()).first())
    if not row:
        row = (base.with_entities(Course.term_code).group_by(Course.term_code)
               .order_by(func.count(Course.section_key).desc(), Course.term_code.desc()).first())
    return row[0] if row else ''


class BlueCollegePolicy:
    """One consistent snapshot for all files in a push, before column mapping.

    The fresh course file determines exclusions, even if DB sync is behind.
    Original SAP files and dates stay intact. Related student/instructor rows
    use exactly the same excluded keys as the course payload.
    """
    def __init__(self, courses_path):
        self.limits = thresholds()
        self.dates = {(o.college_code, o.term_code): (o.tce_start, o.tce_end)
                      for o in CollegeDateOverride.query.all()}
        self.hidden = set()
        with Path(courses_path).open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source)
            required = {'SECTION_KEY', 'CLASS_COLLEGE_SHORT', 'CLASS_COLLEGE', 'ACADEMIC_TERM'}
            if self.dates:
                required.update({'TCE_INVITE', 'TCE_END_DATE'})
            if not required.issubset(reader.fieldnames or []):
                raise ValueError('Courses.csv is missing fields required for college policies.')
            for row in reader:
                code = (row.get('CLASS_COLLEGE_SHORT') or '').strip()
                limit = self.limits.get(code, default_threshold(row.get('CLASS_COLLEGE')))
                key = (row.get('SECTION_KEY') or '').strip()
                if excluded(row.get('CRS_SECTION'), key, limit, row.get('SECTION')):
                    self.hidden.add(key)

    def transform(self, row):
        key = (row.get('SECTION_KEY') or '').strip()
        if key in self.hidden:
            return None
        dates = self.dates.get(((row.get('CLASS_COLLEGE_SHORT') or '').strip(),
                                (row.get('ACADEMIC_TERM') or '').strip()))
        if dates:
            row = dict(row)
            row['TCE_INVITE'] = f'{dates[0].isoformat()} 00:00:00'
            row['TCE_END_DATE'] = f'{dates[1].isoformat()} 00:00:00'
        return row
