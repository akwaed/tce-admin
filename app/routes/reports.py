"""
Custom CSV report builder for synced HANA course data.
"""
import csv
import io
from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import asc, desc

from app.models import db
from app.models.course import College, Course, CourseUser, Department, Instructor, StudentEnrollment


reports_bp = Blueprint('reports', __name__)


def _field(key, label, group, getter):
    return {
        'key': key,
        'label': label,
        'group': group,
        'getter': getter,
    }


def _date_value(value):
    return value.isoformat() if value else ''


def _yes_no(value):
    return 'Yes' if value else 'No'


def _full_name(first_name, last_name, fallback=''):
    name = f'{first_name or ""} {last_name or ""}'.strip()
    return name or fallback or ''


def _course(row):
    return row['course']


def _instructor(row):
    return row.get('instructor')


def _student(row):
    return row.get('student')


def _enrollment(row):
    return row.get('enrollment')


def _course_instructors(row):
    return row.get('course_instructors') or []


def _instructor_names(row):
    return '; '.join(_full_name(i.first_name, i.last_name, i.user_id) for i in _course_instructors(row))


def _instructor_user_ids(row):
    return '; '.join(i.user_id or '' for i in _course_instructors(row))


def _instructor_emails(row):
    return '; '.join(i.email or '' for i in _course_instructors(row))


COURSE_FIELDS = [
    _field('course_name', 'Course Name', 'Course', lambda r: _course(r).class_code or ''),
    _field('course_section', 'Section', 'Course', lambda r: _course(r).section_number or ''),
    _field('crs_section', 'CRS Section', 'Course', lambda r: _course(r).crs_section or ''),
    _field('section_title', 'Course Title', 'Course', lambda r: _course(r).section_title or ''),
    _field('section_key', 'Section Key', 'Course', lambda r: _course(r).section_key or ''),
    _field('class_id', 'Class ID', 'Course', lambda r: _course(r).class_id or ''),
    _field('term', 'Term', 'Course', lambda r: _course(r).term_code or ''),
    _field('college_code', 'College Code', 'Course', lambda r: _course(r).college_code or ''),
    _field('college_name', 'College Name', 'Course', lambda r: _course(r).college.name if _course(r).college else ''),
    _field('department_id', 'Department ID', 'Course', lambda r: _course(r).department_id or ''),
    _field('department_name', 'Department Name', 'Course', lambda r: _course(r).department.name if _course(r).department else ''),
    _field('course_start', 'Course Start', 'Dates', lambda r: _date_value(_course(r).course_start)),
    _field('course_end', 'Course End', 'Dates', lambda r: _date_value(_course(r).course_end)),
    _field('tce_start', 'TCE Start', 'Dates', lambda r: _date_value(_course(r).tce_start)),
    _field('tce_end', 'TCE End', 'Dates', lambda r: _date_value(_course(r).tce_end)),
    _field('tce_reminder', 'TCE Reminder', 'Dates', lambda r: _date_value(_course(r).tce_reminder)),
    _field('marked_for_tce', 'Marked for TCE', 'TCE', lambda r: _yes_no(_course(r).marked_for_tce)),
    _field('tce_status', 'TCE Status', 'TCE', lambda r: _course(r).status_display),
    _field('student_count', 'Number of Students', 'TCE', lambda r: _course(r).student_count or 0),
    _field('crosslisted_id', 'Crosslisted ID', 'Course', lambda r: _course(r).crosslisted_id or ''),
]

AGGREGATE_INSTRUCTOR_FIELDS = [
    _field('instructor_names', 'Teacher Names', 'Teachers', _instructor_names),
    _field('instructor_user_ids', 'Teacher User IDs', 'Teachers', _instructor_user_ids),
    _field('instructor_emails', 'Teacher Emails', 'Teachers', _instructor_emails),
    _field('instructor_count', 'Teacher Count', 'Teachers', lambda r: len(_course_instructors(r))),
]

INSTRUCTOR_FIELDS = [
    _field(
        'teacher_name',
        'Teacher Name',
        'Teacher',
        lambda r: _full_name(_instructor(r).first_name, _instructor(r).last_name, _instructor(r).user_id)
        if _instructor(r) else '',
    ),
    _field('teacher_first_name', 'Teacher First Name', 'Teacher', lambda r: _instructor(r).first_name if _instructor(r) else ''),
    _field('teacher_last_name', 'Teacher Last Name', 'Teacher', lambda r: _instructor(r).last_name if _instructor(r) else ''),
    _field('teacher_user_id', 'Teacher User ID', 'Teacher', lambda r: _instructor(r).user_id if _instructor(r) else ''),
    _field('teacher_email', 'Teacher Email', 'Teacher', lambda r: _instructor(r).email if _instructor(r) else ''),
    _field('teacher_role', 'Teacher Role', 'Teacher', lambda r: _instructor(r).instructor_role if _instructor(r) else ''),
]

STUDENT_FIELDS = [
    _field(
        'student_name',
        'Student Name',
        'Student',
        lambda r: _full_name(_student(r).first_name, _student(r).last_name, _student(r).user_id)
        if _student(r) else '',
    ),
    _field('student_first_name', 'Student First Name', 'Student', lambda r: _student(r).first_name if _student(r) else ''),
    _field('student_last_name', 'Student Last Name', 'Student', lambda r: _student(r).last_name if _student(r) else ''),
    _field(
        'student_user_id',
        'Student User ID',
        'Student',
        lambda r: _student(r).user_id if _student(r) else (_enrollment(r).user_id if _enrollment(r) else ''),
    ),
    _field('student_email', 'Student Email', 'Student', lambda r: _student(r).email if _student(r) else ''),
]

REPORT_TYPES = {
    'course_sections': {
        'label': 'Course Sections',
        'description': 'One row per course section with optional aggregated teacher columns.',
        'fields': COURSE_FIELDS + AGGREGATE_INSTRUCTOR_FIELDS,
        'default_fields': [
            'course_name', 'course_section', 'section_title', 'term',
            'instructor_names', 'instructor_user_ids', 'student_count',
        ],
        'super_admin_only': False,
    },
    'instructor_assignments': {
        'label': 'Teacher Course Assignments',
        'description': 'One row per teacher assigned to a course section.',
        'fields': COURSE_FIELDS + INSTRUCTOR_FIELDS,
        'default_fields': [
            'term', 'course_name', 'course_section', 'section_title',
            'teacher_name', 'teacher_user_id', 'student_count',
        ],
        'super_admin_only': False,
    },
    'student_enrollments': {
        'label': 'Student Enrollments',
        'description': 'One row per student enrollment. Available to super admins only.',
        'fields': COURSE_FIELDS + STUDENT_FIELDS + AGGREGATE_INSTRUCTOR_FIELDS,
        'default_fields': [
            'term', 'course_name', 'course_section', 'student_name',
            'student_user_id', 'instructor_names',
        ],
        'super_admin_only': True,
    },
}


def _can_use_reports():
    return current_user.is_super_admin() or bool(getattr(current_user, 'has_static_report_access', False))


def _require_report_access():
    if not _can_use_reports():
        flash('You do not have access to custom reports.', 'danger')
        return False
    return True


def _available_report_types():
    if current_user.is_super_admin():
        return REPORT_TYPES
    return {
        key: config for key, config in REPORT_TYPES.items()
        if not config.get('super_admin_only')
    }


def _selected_report_type():
    available = _available_report_types()
    requested = request.args.get('report_type', 'instructor_assignments')
    if requested in available:
        return requested
    return next(iter(available))


def _field_map(report_type):
    return {field['key']: field for field in REPORT_TYPES[report_type]['fields']}


def _selected_fields(report_type):
    valid_keys = set(_field_map(report_type).keys())
    if request.args.get('configured') != '1':
        return list(REPORT_TYPES[report_type]['default_fields'])
    return [key for key in request.args.getlist('fields') if key in valid_keys]


def _group_fields(fields):
    groups = []
    by_group = {}
    for field in fields:
        group = field['group']
        if group not in by_group:
            by_group[group] = {'name': group, 'fields': []}
            groups.append(by_group[group])
        by_group[group]['fields'].append(field)
    return groups


def _parse_date_arg(name):
    value = request.args.get(name, '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _course_scope_filter(query):
    if current_user.is_super_admin():
        return query

    query = query.filter(Course.college_code == current_user.college_code)
    if current_user.is_college_admin():
        return query

    department_ids = current_user.department_ids
    if department_ids:
        return query.filter(Course.department_id.in_(department_ids))
    return query.filter(Course.department_id == current_user.department_id)


def _instructor_search_condition(search):
    pattern = f'%{search}%'
    return db.or_(
        Instructor.user_id.ilike(pattern),
        Instructor.first_name.ilike(pattern),
        Instructor.last_name.ilike(pattern),
        Instructor.email.ilike(pattern),
    )


def _student_search_condition(search):
    pattern = f'%{search}%'
    return db.or_(
        CourseUser.user_id.ilike(pattern),
        CourseUser.first_name.ilike(pattern),
        CourseUser.last_name.ilike(pattern),
        CourseUser.email.ilike(pattern),
    )


def _apply_course_filters(query):
    college = request.args.get('college', '').strip()
    department = request.args.get('department', '').strip()
    term = request.args.get('term', '').strip()
    tce_status = request.args.get('tce_status', '').strip()
    enrollment = request.args.get('enrollment', '').strip()
    course_search = request.args.get('course_search', '').strip()
    course_start_from = _parse_date_arg('course_start_from')
    course_start_to = _parse_date_arg('course_start_to')
    tce_start_from = _parse_date_arg('tce_start_from')
    tce_start_to = _parse_date_arg('tce_start_to')

    query = _course_scope_filter(query)

    if college and current_user.is_super_admin():
        query = query.filter(Course.college_code == college)
    if department:
        query = query.filter(Course.department_id == department)
    if term:
        query = query.filter(Course.term_code == term)
    if tce_status == 'marked':
        query = query.filter(Course.marked_for_tce.is_(True))
    elif tce_status == 'not_marked':
        query = query.filter(Course.marked_for_tce.is_(False))
    elif tce_status == 'marked_zero':
        query = query.filter(Course.marked_for_tce.is_(True), Course.student_count == 0)
    elif tce_status == 'marked_active':
        query = query.filter(Course.marked_for_tce.is_(True), Course.student_count > 0)
    elif tce_status == 'not_marked_active':
        query = query.filter(Course.marked_for_tce.is_(False), Course.student_count > 0)

    if enrollment == 'zero':
        query = query.filter(Course.student_count == 0)
    elif enrollment == 'active':
        query = query.filter(Course.student_count > 0)

    if course_search:
        pattern = f'%{course_search}%'
        query = query.filter(db.or_(
            Course.class_code.ilike(pattern),
            Course.section_title.ilike(pattern),
            Course.section_id.ilike(pattern),
            Course.crs_section.ilike(pattern),
            Course.section_key.ilike(pattern),
        ))

    if course_start_from:
        query = query.filter(Course.course_start >= course_start_from)
    if course_start_to:
        query = query.filter(Course.course_start <= course_start_to)
    if tce_start_from:
        query = query.filter(Course.tce_start >= tce_start_from)
    if tce_start_to:
        query = query.filter(Course.tce_start <= tce_start_to)

    return query


def _build_course_instructor_map(section_keys):
    if not section_keys:
        return {}
    instructors = Instructor.query.filter(Instructor.section_key.in_(section_keys)).order_by(
        Instructor.last_name.asc(),
        Instructor.first_name.asc(),
        Instructor.user_id.asc(),
    ).all()
    by_course = {}
    for instructor in instructors:
        by_course.setdefault(instructor.section_key, []).append(instructor)
    return by_course


def _filtered_report_query(report_type):
    person_search = request.args.get('person_search', '').strip()

    if report_type == 'course_sections':
        query = _apply_course_filters(Course.query)
        if person_search:
            query = query.filter(Course.instructors.any(_instructor_search_condition(person_search)))
        return query

    if report_type == 'instructor_assignments':
        query = db.session.query(Course, Instructor).join(
            Instructor, Course.section_key == Instructor.section_key
        )
        query = _apply_course_filters(query)
        if person_search:
            query = query.filter(_instructor_search_condition(person_search))
        return query

    query = db.session.query(Course, StudentEnrollment, CourseUser).join(
        StudentEnrollment, Course.section_key == StudentEnrollment.section_key
    ).outerjoin(
        CourseUser, CourseUser.user_id == StudentEnrollment.user_id
    )
    query = _apply_course_filters(query)
    if person_search:
        query = query.filter(_student_search_condition(person_search))
    return query


def _row_count_for_report(report_type):
    return _filtered_report_query(report_type).count()


def _rows_for_report(report_type):
    query = _filtered_report_query(report_type)

    if report_type == 'course_sections':
        courses = query.order_by(
            desc(Course.term_code),
            asc(Course.college_code),
            asc(Course.department_id),
            asc(Course.class_code),
            asc(Course.crs_section),
        ).all()
        instructor_map = _build_course_instructor_map([course.section_key for course in courses])
        return [
            {'course': course, 'course_instructors': instructor_map.get(course.section_key, [])}
            for course in courses
        ]

    if report_type == 'instructor_assignments':
        assignments = query.order_by(
            desc(Course.term_code),
            asc(Course.class_code),
            asc(Course.crs_section),
            asc(Instructor.last_name),
            asc(Instructor.first_name),
        ).all()
        return [
            {'course': course, 'instructor': instructor, 'course_instructors': [instructor]}
            for course, instructor in assignments
        ]

    enrollments = query.order_by(
        desc(Course.term_code),
        asc(Course.class_code),
        asc(Course.crs_section),
        asc(CourseUser.last_name),
        asc(CourseUser.first_name),
    ).all()
    instructor_map = _build_course_instructor_map([course.section_key for course, _, _ in enrollments])
    return [
        {
            'course': course,
            'enrollment': enrollment,
            'student': student,
            'course_instructors': instructor_map.get(course.section_key, []),
        }
        for course, enrollment, student in enrollments
    ]


def _filter_options():
    scoped_terms = _course_scope_filter(db.session.query(Course.term_code).filter(Course.term_code.isnot(None)))
    terms = [
        term for (term,) in scoped_terms.distinct().order_by(Course.term_code.desc()).all()
        if term
    ]

    if current_user.is_super_admin():
        colleges = College.query.order_by(College.name).all()
        selected_college = request.args.get('college', '').strip()
        if selected_college:
            departments = Department.query.filter_by(college_code=selected_college).order_by(Department.name).all()
        else:
            departments = Department.query.order_by(Department.name).all()
    elif current_user.is_college_admin():
        colleges = College.query.filter_by(code=current_user.college_code).all()
        departments = Department.query.filter_by(college_code=current_user.college_code).order_by(Department.name).all()
    else:
        colleges = []
        department_ids = current_user.department_ids
        if department_ids:
            departments = Department.query.filter(Department.id.in_(department_ids)).order_by(Department.name).all()
        elif current_user.department_id:
            departments = Department.query.filter_by(id=current_user.department_id).all()
        else:
            departments = []

    return colleges, departments, terms


@reports_bp.route('/')
@login_required
def index():
    """Render the custom report builder."""
    if not _require_report_access():
        return redirect(url_for('main.dashboard'))

    report_type = _selected_report_type()
    selected_fields = _selected_fields(report_type)
    colleges, departments, terms = _filter_options()

    current_filters = {
        'report_type': report_type,
        'college': request.args.get('college', '').strip(),
        'department': request.args.get('department', '').strip(),
        'term': request.args.get('term', '').strip(),
        'tce_status': request.args.get('tce_status', '').strip(),
        'enrollment': request.args.get('enrollment', '').strip(),
        'course_search': request.args.get('course_search', '').strip(),
        'person_search': request.args.get('person_search', '').strip(),
        'course_start_from': request.args.get('course_start_from', '').strip(),
        'course_start_to': request.args.get('course_start_to', '').strip(),
        'tce_start_from': request.args.get('tce_start_from', '').strip(),
        'tce_start_to': request.args.get('tce_start_to', '').strip(),
    }

    return render_template(
        'reports/index.html',
        report_types=_available_report_types(),
        report_type=report_type,
        report_config=REPORT_TYPES[report_type],
        grouped_fields=_group_fields(REPORT_TYPES[report_type]['fields']),
        selected_fields=selected_fields,
        colleges=colleges,
        departments=departments,
        terms=terms,
        row_count=_row_count_for_report(report_type),
        current_filters=current_filters,
    )


@reports_bp.route('/export')
@login_required
def export():
    """Generate the selected custom report as CSV."""
    if not _require_report_access():
        return redirect(url_for('main.dashboard'))

    report_type = _selected_report_type()
    selected_fields = _selected_fields(report_type)
    if not selected_fields:
        flash('Select at least one column before exporting.', 'warning')
        return redirect(url_for('reports.index', **request.args))

    fields = _field_map(report_type)
    export_fields = [fields[key] for key in selected_fields]
    rows = _rows_for_report(report_type)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[field['label'] for field in export_fields])
    writer.writeheader()
    for row in rows:
        writer.writerow({
            field['label']: field['getter'](row)
            for field in export_fields
        })

    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{report_type}_{timestamp}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )
