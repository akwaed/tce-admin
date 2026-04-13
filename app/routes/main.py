"""
Main Routes - Dashboard and Home
"""
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user
from app.models import db
from app.models.admin import Admin
from app.models.course import Course, College, Department, SyncLog
from app.models.sync_history import SyncRun
from sqlalchemy import func

main_bp = Blueprint('main', __name__)
APP_TIMEZONE = ZoneInfo('America/New_York')


def _local_today():
    """Dashboard date in UK's local timezone."""
    return datetime.now(APP_TIMEZONE).date()


def _format_date_label(value):
    if not value:
        return 'Not scheduled'
    return value.strftime('%b %d, %Y').replace(' 0', ' ')


def _build_tce_timeline_stats(course_query, today):
    """Return active/upcoming TCE metrics for the dashboard."""
    marked_query = course_query.filter(Course.marked_for_tce.is_(True))
    active_today_query = marked_query.filter(
        Course.tce_start.isnot(None),
        Course.tce_end.isnot(None),
        Course.tce_start <= today,
        Course.tce_end >= today,
    )

    active_today_count = active_today_query.count()
    active_today_students = (
        active_today_query.with_entities(func.coalesce(func.sum(Course.student_count), 0))
        .scalar()
        or 0
    )

    peak_release = (
        marked_query.filter(
            Course.tce_start.isnot(None),
            Course.tce_start >= today,
        )
        .with_entities(
            Course.tce_start.label('release_date'),
            func.count(Course.section_key).label('course_count'),
        )
        .group_by(Course.tce_start)
        .order_by(func.count(Course.section_key).desc(), Course.tce_start.asc())
        .first()
    )

    upcoming_windows = (
        marked_query.filter(
            Course.tce_start.isnot(None),
            Course.tce_end.isnot(None),
            Course.tce_end >= today,
        )
        .with_entities(Course.tce_start, Course.tce_end)
        .all()
    )

    next_zero_day = today
    if upcoming_windows:
        deltas = defaultdict(int)
        current_active = 0
        max_end = today

        for window in upcoming_windows:
            start = window.tce_start
            end = window.tce_end
            if start is None or end is None:
                continue
            if start <= today <= end:
                current_active += 1
            elif start > today:
                deltas[start] += 1

            deltas[end + timedelta(days=1)] -= 1
            if end > max_end:
                max_end = end

        if current_active > 0:
            running = current_active
            probe_day = today + timedelta(days=1)
            zero_day = None

            while probe_day <= max_end + timedelta(days=1):
                running += deltas.get(probe_day, 0)
                if running == 0:
                    zero_day = probe_day
                    break
                probe_day += timedelta(days=1)

            next_zero_day = zero_day or (max_end + timedelta(days=1))

    return {
        'dashboard_date': today,
        'dashboard_date_label': _format_date_label(today),
        'active_tce_today': active_today_count,
        'active_tce_today_students': active_today_students,
        'peak_release_date': peak_release.release_date if peak_release else None,
        'peak_release_date_label': _format_date_label(peak_release.release_date) if peak_release else 'None scheduled',
        'peak_release_count': peak_release.course_count if peak_release else 0,
        'peak_release_days_away': (peak_release.release_date - today).days if peak_release else None,
        'next_zero_day': next_zero_day,
        'next_zero_day_label': _format_date_label(next_zero_day),
        'next_zero_day_countdown': (next_zero_day - today).days,
    }


@main_bp.route('/')
def index():
    """Home page - redirect to dashboard if logged in"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('auth.login'))


@main_bp.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard with quick stats"""
    stats = {}
    today = _local_today()
    
    # Base queries based on user's access level
    if current_user.is_super_admin():
        # Full access
        stats['total_admins'] = Admin.query.filter_by(is_active=True).count()
        stats['total_colleges'] = College.query.count()
        stats['total_departments'] = Department.query.count()
        
        course_query = Course.query
    elif current_user.is_college_admin():
        stats['total_admins'] = Admin.query.filter_by(
            college_code=current_user.college_code, 
            is_active=True
        ).count()
        stats['total_departments'] = Department.query.filter_by(
            college_code=current_user.college_code
        ).count()
        
        course_query = Course.query.filter_by(college_code=current_user.college_code)
    else:
        # Department admin
        stats['total_admins'] = Admin.query.filter_by(
            college_code=current_user.college_code,
            department_id=current_user.department_id,
            is_active=True
        ).count()
        
        course_query = Course.query.filter_by(
            college_code=current_user.college_code,
            department_id=current_user.department_id
        )
    
    # Course statistics
    stats['total_courses'] = course_query.count()
    stats['courses_marked_tce'] = course_query.filter(Course.marked_for_tce == True).count()
    stats['courses_zero_enrollment'] = course_query.filter(
        Course.marked_for_tce == True,
        Course.student_count == 0
    ).count()
    stats['courses_not_marked'] = course_query.filter(Course.marked_for_tce == False).count()
    
    stats['total_students'] = (
        course_query
        .filter(Course.marked_for_tce == True)
        .with_entities(func.coalesce(func.sum(Course.student_count), 0))
        .scalar()
        or 0
    )
    stats.update(_build_tce_timeline_stats(course_query, today))
    
    # Last sync time - prefer the newer SyncRun table, fall back to legacy SyncLog.
    last_sync = (
        SyncRun.query.filter_by(status='completed')
        .order_by(SyncRun.completed_at.desc())
        .first()
        or SyncLog.query.filter_by(status='completed').order_by(SyncLog.completed_at.desc()).first()
    )
    stats['last_sync'] = last_sync.completed_at.strftime('%Y-%m-%d %H:%M') if last_sync else None
    
    return render_template('dashboard.html', stats=stats)
