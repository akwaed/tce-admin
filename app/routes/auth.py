"""
Authentication Routes
Currently supports super admin login only
Azure AD integration to be added later
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
from app.models import db
from app.models.admin import Admin

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page - Super Admin fallback login"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        
        # Check for super admin credentials from config
        config_username = current_app.config['SUPER_ADMIN_USERNAME']
        config_password = current_app.config['SUPER_ADMIN_PASSWORD']

        if username == config_username and password == config_password:
            # Find or create the super admin user
            admin = Admin.query.filter_by(linkblue=username).first()
            if admin and admin.role == 'super_admin':
                login_user(admin, remember=True)
                flash('Welcome, Super Administrator!', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('main.dashboard'))

        # Check for test account credentials from config
        test_username = current_app.config['TEST_ACCOUNT_USERNAME']
        test_password = current_app.config['TEST_ACCOUNT_PASSWORD']

        if username == test_username and password == test_password:
            admin = Admin.query.filter_by(linkblue=username, is_active=True).first()
            if admin:
                login_user(admin, remember=True)
                flash(f'Welcome, {admin.first_name}! (Test Account)', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('main.dashboard'))

        test2_username = current_app.config['TEST_ACCOUNT2_USERNAME']
        test2_password = current_app.config['TEST_ACCOUNT2_PASSWORD']

        if username == test2_username and password == test2_password:
            admin = Admin.query.filter_by(linkblue=username, is_active=True).first()
            if admin:
                login_user(admin, remember=True)
                flash(f'Welcome, {admin.first_name}! (Test Account)', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('main.dashboard'))
        
        # Also check if it's a regular admin with password (for future use)
        admin = Admin.query.filter_by(linkblue=username, is_active=True).first()
        if admin and admin.check_password(password):
            login_user(admin, remember=True)
            flash(f'Welcome, {admin.first_name}!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('main.dashboard'))
        
        flash('Invalid username or password.', 'danger')
    
    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    """Logout user"""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


# Placeholder for future Azure AD integration
@auth_bp.route('/azure-login')
def azure_login():
    """
    Azure AD Login (To be implemented when cloud team is ready)
    Will use MSAL library for OAuth flow
    """
    flash('Azure AD login is not yet available. Please use the super admin login.', 'warning')
    return redirect(url_for('auth.login'))


@auth_bp.route('/azure-callback')
def azure_callback():
    """
    Azure AD Callback (To be implemented)
    """
    flash('Azure AD login is not yet available.', 'warning')
    return redirect(url_for('auth.login'))
