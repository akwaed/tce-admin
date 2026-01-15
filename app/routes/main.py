"""
Main Routes - Dashboard and Home
"""
from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user
from app.models import db
from app.models.admin import Admin
from app.models.course import Course, College, Department, SyncLog
from sqlalchemy import func

main_bp = Blueprint('main', __name__)


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
    
    # Calculate total students
    if current_user.is_super_admin():
        stats['total_students'] = db.session.query(func.sum(Course.student_count)).filter(
            Course.marked_for_tce == True
        ).scalar() or 0
    elif current_user.is_college_admin():
        stats['total_students'] = db.session.query(func.sum(Course.student_count)).filter(
            Course.college_code == current_user.college_code,
            Course.marked_for_tce == True
        ).scalar() or 0
    else:
        stats['total_students'] = db.session.query(func.sum(Course.student_count)).filter(
            Course.college_code == current_user.college_code,
            Course.department_id == current_user.department_id,
            Course.marked_for_tce == True
        ).scalar() or 0
    
    # Last sync time
    last_sync = SyncLog.query.filter_by(status='completed').order_by(SyncLog.completed_at.desc()).first()
    stats['last_sync'] = last_sync.completed_at.strftime('%Y-%m-%d %H:%M') if last_sync else None
    
    return render_template('dashboard.html', stats=stats)
