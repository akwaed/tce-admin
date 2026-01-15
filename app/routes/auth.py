"""
Authentication Routes
Supports Azure AD Easy Auth (production) and fallback login (development)
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
from app.models import db
from app.models.admin import Admin
import base64
import json

auth_bp = Blueprint('auth', __name__)


def get_azure_user_from_headers():
    """
    Extract user info from Azure Easy Auth headers.

    Azure App Service passes these headers when Easy Auth is enabled:
    - X-MS-CLIENT-PRINCIPAL-NAME: User's email/UPN (e.g., linkblue@uky.edu)
    - X-MS-CLIENT-PRINCIPAL-ID: User's Azure AD Object ID
    - X-MS-CLIENT-PRINCIPAL: Base64-encoded JSON with full claims
    """
    principal_name = request.headers.get('X-MS-CLIENT-PRINCIPAL-NAME')
    principal_id = request.headers.get('X-MS-CLIENT-PRINCIPAL-ID')

    if not principal_name:
        return None

    # Extract linkblue from email (e.g., "jsmith@uky.edu" -> "jsmith")
    linkblue = principal_name.split('@')[0].lower()

    # Try to get additional claims from the full principal
    claims = {}
    principal_data = request.headers.get('X-MS-CLIENT-PRINCIPAL')
    if principal_data:
        try:
            decoded = base64.b64decode(principal_data)
            claims_data = json.loads(decoded)
            for claim in claims_data.get('claims', []):
                claims[claim.get('typ', '')] = claim.get('val', '')
        except Exception:
            pass

    return {
        'linkblue': linkblue,
        'email': principal_name,
        'azure_id': principal_id,
        'name': claims.get('name', ''),
        'given_name': claims.get('http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname', ''),
        'family_name': claims.get('http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname', ''),
    }


@auth_bp.before_app_request
def auto_login_azure_user():
    """
    Automatically log in users authenticated via Azure Easy Auth.
    This runs before every request to check for Azure auth headers.
    """
    # Skip if already authenticated or on auth routes
    if current_user.is_authenticated:
        return

    if request.endpoint and request.endpoint.startswith('auth.'):
        return

    # Check for Azure Easy Auth headers
    azure_user = get_azure_user_from_headers()
    if not azure_user:
        return

    # Find admin by linkblue
    admin = Admin.query.filter_by(linkblue=azure_user['linkblue'], is_active=True).first()

    if admin:
        login_user(admin, remember=True)
        current_app.logger.info(f"Azure SSO login: {admin.linkblue}")
    else:
        # User authenticated with Azure but not in admin list
        # Store info in session for the "not authorized" page
        current_app.logger.warning(f"Azure user not authorized: {azure_user['linkblue']}")


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page - Super Admin fallback login"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    # Check if user came through Azure but isn't authorized
    azure_user = get_azure_user_from_headers()
    if azure_user:
        # User authenticated with Azure AD but not in our admin list
        return render_template('auth/not_authorized.html',
                               azure_user=azure_user)
    
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
    """Logout user - handles both local and Azure sessions"""
    logout_user()

    # Check if running under Azure Easy Auth
    if request.headers.get('X-MS-CLIENT-PRINCIPAL-NAME'):
        # Redirect to Azure logout endpoint to clear Azure session too
        # This will sign out of Azure AD and redirect back to the app
        return redirect('/.auth/logout')

    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/me')
@login_required
def me():
    """Debug endpoint to show current user info and Azure headers"""
    azure_user = get_azure_user_from_headers()
    return {
        'authenticated': True,
        'user': {
            'id': current_user.id,
            'linkblue': current_user.linkblue,
            'name': current_user.full_name,
            'role': current_user.role,
            'college': current_user.college_code,
            'department': current_user.department_id,
        },
        'azure_headers': {
            'X-MS-CLIENT-PRINCIPAL-NAME': request.headers.get('X-MS-CLIENT-PRINCIPAL-NAME'),
            'X-MS-CLIENT-PRINCIPAL-ID': request.headers.get('X-MS-CLIENT-PRINCIPAL-ID'),
        },
        'azure_user': azure_user
    }
