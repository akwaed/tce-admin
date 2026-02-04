"""
Verification Reports Routes
Course listings with TCE status from UKDIG data
"""
from flask import Blueprint, render_template, request, Response, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.course import Course, College, Department, Instructor, SyncLog
from app.models.admin import Admin
from sqlalchemy import func, case, asc, desc
import csv
import io
from datetime import datetime

# Mapping of sort column names to database fields
SORT_COLUMNS = {
    'course': Course.class_code,
    'section': Course.crs_section,
    'instructor': None,  # Special case - handled separately
    'title': Course.section_title,
    'college': Course.college_code,
    'department': Course.department_id,
    'course_start': Course.course_start,
    'course_end': Course.course_end,
    'tce_start': Course.tce_start,
    'status': Course.marked_for_tce,
    'students': Course.student_count,
}


def format_term_code(term_code):
    """Convert term code to readable format.

    Term codes follow the pattern: YYYYSSS where:
    - YYYY = year
    - SSS = semester code (010=Spring, 020=Summer, 030=Fall)

    Examples:
    - 2025010 -> Spring 2025
    - 2025020 -> Summer 2025
    - 2025030 -> Fall 2025
    """
    if not term_code or len(term_code) < 7:
        return term_code or 'Unknown'

    year = term_code[:4]
    semester_code = term_code[4:7]

    semester_map = {
        '010': 'Spring',
        '020': 'Summer',
        '030': 'Fall',
    }

    semester = semester_map.get(semester_code, f'Term {semester_code}')
    return f'{semester} {year}'

verification_bp = Blueprint('verification', __name__)


@verification_bp.route('/')
@login_required
def list_courses():
    """List courses with TCE verification status"""
    # Get filter parameters
    college_filter = request.args.get('college', '')
    dept_filter = request.args.get('department', '')
    term_filter = request.args.get('term', '')
    tce_filters = request.args.getlist('tce_status')  # marked, not_marked, zero_enrollment
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50

    # Get sort parameters
    sort_by = request.args.get('sort', 'course')  # Default sort by course code
    sort_order = request.args.get('order', 'asc')  # Default ascending

    # Base query based on user's access
    query = Course.query

    # Apply access restrictions
    if not current_user.is_super_admin():
        if current_user.is_college_admin():
            query = query.filter(Course.college_code == current_user.college_code)
        else:
            query = query.filter(
                Course.college_code == current_user.college_code,
                Course.department_id == current_user.department_id
            )

    # Apply filters
    if college_filter:
        query = query.filter(Course.college_code == college_filter)
    if dept_filter:
        query = query.filter(Course.department_id == dept_filter)
    if term_filter:
        query = query.filter(Course.term_code == term_filter)
    if tce_filters:
        # Check for compound filter combinations
        has_marked = 'marked' in tce_filters
        has_not_marked = 'not_marked' in tce_filters
        has_zero = 'zero_enrollment' in tce_filters
        has_non_zero = 'non_zero_enrollment' in tce_filters

        # Handle compound filters (both marked/not_marked AND enrollment status)
        if (has_marked or has_not_marked) and (has_zero or has_non_zero):
            # Compound filter mode - use AND logic
            compound_conditions = []
            if has_marked and has_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count == 0))
            if has_marked and has_non_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count > 0))
            if has_not_marked and has_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == False, Course.student_count == 0))
            if has_not_marked and has_non_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == False, Course.student_count > 0))
            if compound_conditions:
                query = query.filter(db.or_(*compound_conditions))
        else:
            # Simple filter mode - use OR logic
            status_conditions = []
            if has_marked:
                status_conditions.append(Course.marked_for_tce == True)
            if has_not_marked:
                status_conditions.append(Course.marked_for_tce == False)
            if has_zero:
                status_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count == 0))
            if has_non_zero:
                status_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count > 0))
            if status_conditions:
                query = query.filter(db.or_(*status_conditions))

    if search:
        query = query.filter(
            db.or_(
                Course.class_code.ilike(f'%{search}%'),
                Course.section_title.ilike(f'%{search}%'),
                Course.section_id.ilike(f'%{search}%')
            )
        )
    
    # Get total count before pagination
    total_count = query.count()

    # Apply sorting
    sort_column = SORT_COLUMNS.get(sort_by)
    if sort_column is not None:
        if sort_order == 'desc':
            query = query.order_by(desc(sort_column))
        else:
            query = query.order_by(asc(sort_column))
    else:
        # Default sort: college, department, course code
        query = query.order_by(
            Course.college_code,
            Course.department_id,
            Course.class_code
        )

    # Paginate results
    courses = query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Get statistics for the filtered data
    stats = get_verification_stats(current_user, college_filter, dept_filter)
    
    # Get colleges/departments for filters
    if current_user.is_super_admin():
        colleges = College.query.order_by(College.name).all()
    elif current_user.is_college_admin():
        colleges = College.query.filter_by(code=current_user.college_code).all()
    else:
        colleges = []

    # Get departments (filtered by college if selected)
    if college_filter:
        departments = Department.query.filter_by(college_code=college_filter).order_by(Department.name).all()
    elif current_user.is_super_admin():
        departments = Department.query.order_by(Department.name).all()
    elif current_user.is_college_admin():
        departments = Department.query.filter_by(college_code=current_user.college_code).order_by(Department.name).all()
    else:
        departments = []

    # Get distinct academic terms for filter dropdown
    term_query = db.session.query(Course.term_code).distinct()
    if not current_user.is_super_admin():
        if current_user.is_college_admin():
            term_query = term_query.filter(Course.college_code == current_user.college_code)
        else:
            term_query = term_query.filter(
                Course.college_code == current_user.college_code,
                Course.department_id == current_user.department_id
            )
    term_codes = [t[0] for t in term_query.order_by(Course.term_code.desc()).all() if t[0]]
    terms = [{'code': tc, 'name': tc} for tc in term_codes]

    # Get last sync time
    last_sync = SyncLog.query.filter_by(status='completed').order_by(SyncLog.completed_at.desc()).first()

    return render_template('verification/list.html',
                         courses=courses,
                         colleges=colleges,
                         departments=departments,
                         terms=terms,
                         stats=stats,
                         last_sync=last_sync,
                         total_count=total_count,
                         current_filters={
                             'college': college_filter,
                             'department': dept_filter,
                             'term': term_filter,
                             'tce_status': tce_filters,
                             'search': search,
                             'sort': sort_by,
                             'order': sort_order
                         })


@verification_bp.route('/course/<path:section_key>')
@login_required
def course_detail(section_key):
    """View course details including instructors"""
    course = Course.query.get_or_404(section_key)
    
    # Check access
    if not current_user.is_super_admin():
        if current_user.is_college_admin():
            if course.college_code != current_user.college_code:
                flash('You do not have access to this course.', 'danger')
                return redirect(url_for('verification.list_courses'))
        else:
            if course.college_code != current_user.college_code or \
               course.department_id != current_user.department_id:
                flash('You do not have access to this course.', 'danger')
                return redirect(url_for('verification.list_courses'))
    
    # Get crosslisted courses
    crosslisted = []
    if course.crosslisted_id:
        crosslisted = Course.query.filter(
            Course.crosslisted_id == course.crosslisted_id,
            Course.section_key != course.section_key
        ).all()
    
    return render_template('verification/detail.html', course=course, crosslisted=crosslisted)


@verification_bp.route('/export')
@login_required
def export_courses():
    """Export filtered courses to CSV"""
    # Get same filters as list view
    college_filter = request.args.get('college', '')
    dept_filter = request.args.get('department', '')
    term_filter = request.args.get('term', '')
    tce_filters = request.args.getlist('tce_status')
    search = request.args.get('search', '').strip()

    # Build query with same logic as list view
    query = Course.query

    if not current_user.is_super_admin():
        if current_user.is_college_admin():
            query = query.filter(Course.college_code == current_user.college_code)
        else:
            query = query.filter(
                Course.college_code == current_user.college_code,
                Course.department_id == current_user.department_id
            )

    if college_filter:
        query = query.filter(Course.college_code == college_filter)
    if dept_filter:
        query = query.filter(Course.department_id == dept_filter)
    if term_filter:
        query = query.filter(Course.term_code == term_filter)
    if tce_filters:
        # Check for compound filter combinations
        has_marked = 'marked' in tce_filters
        has_not_marked = 'not_marked' in tce_filters
        has_zero = 'zero_enrollment' in tce_filters
        has_non_zero = 'non_zero_enrollment' in tce_filters

        # Handle compound filters (both marked/not_marked AND enrollment status)
        if (has_marked or has_not_marked) and (has_zero or has_non_zero):
            compound_conditions = []
            if has_marked and has_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count == 0))
            if has_marked and has_non_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count > 0))
            if has_not_marked and has_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == False, Course.student_count == 0))
            if has_not_marked and has_non_zero:
                compound_conditions.append(db.and_(Course.marked_for_tce == False, Course.student_count > 0))
            if compound_conditions:
                query = query.filter(db.or_(*compound_conditions))
        else:
            status_conditions = []
            if has_marked:
                status_conditions.append(Course.marked_for_tce == True)
            if has_not_marked:
                status_conditions.append(Course.marked_for_tce == False)
            if has_zero:
                status_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count == 0))
            if has_non_zero:
                status_conditions.append(db.and_(Course.marked_for_tce == True, Course.student_count > 0))
            if status_conditions:
                query = query.filter(db.or_(*status_conditions))

    if search:
        query = query.filter(
            db.or_(
                Course.class_code.ilike(f'%{search}%'),
                Course.section_title.ilike(f'%{search}%'),
                Course.section_id.ilike(f'%{search}%')
            )
        )

    courses = query.order_by(Course.college_code, Course.class_code).all()
    
    # Create CSV
    output = io.StringIO()
    fieldnames = ['class_code', 'section', 'section_title', 'college', 'department', 'department_name',
                  'marked_for_tce', 'student_count', 'course_start', 'course_end',
                  'tce_start', 'tce_end', 'instructors', 'crosslisted_id']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    
    for course in courses:
        instructors = ', '.join([f"{i.first_name} {i.last_name}" for i in course.instructors])
        writer.writerow({
            'class_code': course.class_code,
            'section': course.section_number,
            'section_title': course.section_title,
            'college': course.college_code,
            'department': course.department_id,
            'department_name': course.department.name if course.department else '',
            'marked_for_tce': 'Yes' if course.marked_for_tce else 'No',
            'student_count': course.student_count,
            'course_start': course.course_start.isoformat() if course.course_start else '',
            'course_end': course.course_end.isoformat() if course.course_end else '',
            'tce_start': course.tce_start.isoformat() if course.tce_start else '',
            'tce_end': course.tce_end.isoformat() if course.tce_end else '',
            'instructors': instructors,
            'crosslisted_id': course.crosslisted_id or ''
        })
    
    output.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'tce_verification_report_{timestamp}.csv'
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@verification_bp.route('/api/departments/<college_code>')
@login_required
def get_departments_api(college_code):
    """API endpoint to get departments for a college"""
    departments = Department.query.filter_by(college_code=college_code).order_by(Department.name).all()
    return jsonify([{'id': d.id, 'name': d.name} for d in departments])


@verification_bp.route('/api/stats')
@login_required
def get_stats_api():
    """API endpoint to get verification statistics"""
    college_filter = request.args.get('college', '')
    dept_filter = request.args.get('department', '')
    
    stats = get_verification_stats(current_user, college_filter, dept_filter)
    return jsonify(stats)


@verification_bp.route('/sync', methods=['GET', 'POST'])
@login_required
def sync_data():
    """Sync course data from CSV files"""
    if not current_user.is_super_admin():
        flash('Only super administrators can sync data.', 'danger')
        return redirect(url_for('verification.list_courses'))

    if request.method == 'POST':
        from app.services.course_sync import CourseSyncService, resolve_datasources_path, get_sync_progress
        import os

        # Check if sync is already running
        progress = get_sync_progress()
        if progress['running']:
            flash('A sync is already in progress. Please wait.', 'warning')
            return redirect(url_for('verification.sync_data'))

        # Check for uploaded files or use default path
        datasources_path = './datasources'

        # Handle file uploads
        files_uploaded = False
        if 'courses_file' in request.files and request.files['courses_file'].filename:
            os.makedirs(datasources_path, exist_ok=True)
            request.files['courses_file'].save(os.path.join(datasources_path, 'Courses.csv'))
            files_uploaded = True
        if 'instructors_file' in request.files and request.files['instructors_file'].filename:
            os.makedirs(datasources_path, exist_ok=True)
            request.files['instructors_file'].save(os.path.join(datasources_path, 'Instructor_Course.csv'))
            files_uploaded = True
        if 'students_file' in request.files and request.files['students_file'].filename:
            os.makedirs(datasources_path, exist_ok=True)
            request.files['students_file'].save(os.path.join(datasources_path, 'Student_Course.csv'))
            files_uploaded = True
        if 'users_file' in request.files and request.files['users_file'].filename:
            os.makedirs(datasources_path, exist_ok=True)
            request.files['users_file'].save(os.path.join(datasources_path, 'Users.csv'))
            files_uploaded = True

        try:
            resolved_path = resolve_datasources_path(datasources_path)
            if resolved_path != datasources_path:
                flash(f'Using datasources from {resolved_path}.', 'info')
            sync = CourseSyncService(resolved_path)
            result = sync.sync_all()

            if result['success']:
                flash(f"Sync completed! Added {result['stats']['courses_added']} courses, "
                      f"{result['stats']['instructors_added']} instructors. "
                      f"{result['stats']['students_counted']} students counted.", 'success')
            else:
                flash('Sync completed with errors. Check the logs.', 'warning')

        except Exception as e:
            flash(f'Sync failed: {str(e)}', 'danger')

        return redirect(url_for('verification.list_courses'))

    # GET - show sync form
    last_sync = SyncLog.query.filter_by(status='completed').order_by(SyncLog.completed_at.desc()).first()
    sync_logs = SyncLog.query.order_by(SyncLog.started_at.desc()).limit(10).all()

    return render_template('verification/sync.html', last_sync=last_sync, sync_logs=sync_logs)


@verification_bp.route('/api/sync/progress')
@login_required
def get_sync_progress_api():
    """API endpoint to get current sync progress"""
    from app.services.course_sync import get_sync_progress
    return jsonify(get_sync_progress())


@verification_bp.route('/reset-departments', methods=['POST'])
@login_required
def reset_departments():
    """Clear all departments and resync from Courses.csv.

    This fixes issues where department names are wrong (e.g., from sample data
    that was synced before real Courses.csv data).
    """
    if not current_user.is_super_admin():
        flash('Only super administrators can reset departments.', 'danger')
        return redirect(url_for('verification.sync_data'))

    from app.services.course_sync import CourseSyncService, resolve_datasources_path
    from app.models.course import Department

    try:
        # Count before deletion
        dept_count = Department.query.count()

        # Delete all departments (will cascade or be recreated)
        Department.query.delete()
        db.session.commit()

        # Resync to recreate departments from Courses.csv
        datasources_path = './datasources'
        resolved_path = resolve_datasources_path(datasources_path)
        if resolved_path != datasources_path:
            flash(f'Using datasources from {resolved_path}.', 'info')
        sync = CourseSyncService(resolved_path)
        result = sync.sync_all()

        if result['success']:
            new_count = Department.query.count()
            flash(f'Departments reset! Deleted {dept_count} old records, created {new_count} from Courses.csv.', 'success')
        else:
            flash('Reset completed but sync had errors. Check the logs.', 'warning')

    except Exception as e:
        db.session.rollback()
        flash(f'Reset failed: {str(e)}', 'danger')

    return redirect(url_for('verification.sync_data'))


def get_verification_stats(user, college_filter='', dept_filter=''):
    """Calculate verification statistics based on user access and filters"""
    
    # Base query
    query = Course.query
    
    # Apply access restrictions
    if not user.is_super_admin():
        if user.is_college_admin():
            query = query.filter(Course.college_code == user.college_code)
        else:
            query = query.filter(
                Course.college_code == user.college_code,
                Course.department_id == user.department_id
            )
    
    # Apply filters
    if college_filter:
        query = query.filter(Course.college_code == college_filter)
    if dept_filter:
        query = query.filter(Course.department_id == dept_filter)
    
    # Calculate statistics
    total = query.count()
    marked = query.filter(Course.marked_for_tce == True).count()
    not_marked = query.filter(Course.marked_for_tce == False).count()
    zero_enrollment = query.filter(
        Course.marked_for_tce == True,
        Course.student_count == 0
    ).count()
    non_zero_enrollment = query.filter(
        Course.marked_for_tce == True,
        Course.student_count > 0
    ).count()
    
    # Total students in marked courses
    total_students = db.session.query(func.sum(Course.student_count)).filter(
        Course.marked_for_tce == True
    )
    if not user.is_super_admin():
        if user.is_college_admin():
            total_students = total_students.filter(Course.college_code == user.college_code)
        else:
            total_students = total_students.filter(
                Course.college_code == user.college_code,
                Course.department_id == user.department_id
            )
    if college_filter:
        total_students = total_students.filter(Course.college_code == college_filter)
    if dept_filter:
        total_students = total_students.filter(Course.department_id == dept_filter)
    
    total_students = total_students.scalar() or 0
    
    return {
        'total': total,
        'marked': marked,
        'not_marked': not_marked,
        'zero_enrollment': zero_enrollment,
        'non_zero_enrollment': non_zero_enrollment,
        'total_students': total_students,
        'marked_percentage': round((marked / total * 100) if total > 0 else 0, 1)
    }
