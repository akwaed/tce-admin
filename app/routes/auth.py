"""
Authentication Routes
Supports both Azure AD authentication and Super Admin fallback login
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, session
from flask_login import login_user, logout_user, login_required, current_user
from app.models import db
from app.models.admin import Admin
import msal

auth_bp = Blueprint('auth', __name__)


def get_msal_app():
    """Initialize MSAL Confidential Client Application"""
    return msal.ConfidentialClientApplication(
        current_app.config['AZURE_AD_CLIENT_ID'],
        authority=f"https://login.microsoftonline.com/{current_app.config['AZURE_AD_TENANT_ID']}",
        client_credential=current_app.config['AZURE_AD_CLIENT_SECRET']
    )


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page - Azure AD or Super Admin fallback"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    # Handle super admin fallback login
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
        
        # Also check if it's a regular admin with password (for future use)
        admin = Admin.query.filter_by(linkblue=username, is_active=True).first()
        if admin and admin.check_password(password):
            login_user(admin, remember=True)
            flash(f'Welcome, {admin.first_name}!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('main.dashboard'))
        
        flash('Invalid username or password.', 'danger')
    
    return render_template('auth/login.html')


@auth_bp.route('/azure-login')
def azure_login():
    """Redirect to Azure AD for authentication"""
    # Store the next URL in session
    session['next_url'] = request.args.get('next')
    
    msal_app = get_msal_app()
    
    # Generate authorization URL
    auth_url = msal_app.get_authorization_request_url(
        scopes=["User.Read"],
        redirect_uri=current_app.config['AZURE_AD_REDIRECT_URI']
    )
    
    return redirect(auth_url)


@auth_bp.route('/azure-callback')
def azure_callback():
    """Handle Azure AD authentication callback"""
    # Get authorization code from callback
    code = request.args.get('code')
    if not code:
        error = request.args.get('error_description', 'Authentication failed')
        flash(f'Azure AD error: {error}', 'danger')
        return redirect(url_for('auth.login'))
    
    try:
        # Exchange code for token
        msal_app = get_msal_app()
        result = msal_app.acquire_token_by_authorization_code(
            code,
            scopes=["User.Read"],
            redirect_uri=current_app.config['AZURE_AD_REDIRECT_URI']
        )
        
        if "error" in result:
            flash(f'Authentication error: {result.get("error_description")}', 'danger')
            return redirect(url_for('auth.login'))
        
        # Extract user information from token
        id_token_claims = result.get('id_token_claims', {})
        user_email = id_token_claims.get('preferred_username', '').lower()
        user_name = id_token_claims.get('name', '')
        
        # Extract linkblue from email
        if '@' in user_email:
            linkblue = user_email.split('@')[0]
        else:
            linkblue = user_email
        
        # Find admin in database
        admin = Admin.query.filter_by(linkblue=linkblue, is_active=True).first()
        
        if not admin:
            flash('Your account is not authorized to access this system. Please contact your administrator.', 'danger')
            return redirect(url_for('auth.login'))
        
        # Log the user in
        login_user(admin, remember=True)
        flash(f'Welcome, {admin.first_name}!', 'success')
        
        # Redirect to next URL or dashboard
        next_page = session.pop('next_url', None)
        return redirect(next_page or url_for('main.dashboard'))
        
    except Exception as e:
        current_app.logger.error(f'Azure AD authentication error: {str(e)}')
        flash('An error occurred during authentication. Please try again.', 'danger')
        return redirect(url_for('auth.login'))


@auth_bp.route('/logout')
@login_required
def logout():
    """Logout user"""
    logout_user()
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))