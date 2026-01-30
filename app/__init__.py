"""
TCE Admin System - Flask Application Factory
University of Kentucky
"""
from flask import Flask
from flask_login import LoginManager
from app.config import config
from app.models import db
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix

# After db.init_app(app) and login_manager.init_app(app), add:
#Session(app)
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access the TCE Admin System.'
login_manager.login_message_category = 'info'


def create_app(config_name='default'):
    """Application factory"""
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)
    
    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    Session(app)
    
    # User loader for Flask-Login
    @login_manager.user_loader
    def load_user(user_id):
        from app.models.admin import Admin
        return Admin.query.get(int(user_id))
    
    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.admin import admin_bp
    from app.routes.verification import verification_bp
    from app.routes.questions import questions_bp
    from app.routes.main import main_bp
    from app.routes.tracking import tracking_bp
    from app.routes.settings import settings_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(verification_bp, url_prefix='/verification')
    app.register_blueprint(questions_bp, url_prefix='/questions')
    app.register_blueprint(tracking_bp, url_prefix='/tracking')
    app.register_blueprint(settings_bp, url_prefix='/settings')
    
    # Create database tables
    with app.app_context():
        db.create_all()
        
        # Create default super admin if doesn't exist
        from app.models.admin import Admin
        super_admin = Admin.query.filter_by(linkblue='tceadmin').first()
        if not super_admin:
            super_admin = Admin(
                linkblue='tceadmin',
                first_name='TCE',
                last_name='Administrator',
                email='tce-admin@uky.edu',
                role='super_admin',
                is_primary_contact=True,
                has_dashboard_access=True,
                has_static_report_access=True,
                has_qb_access=True
            )
            super_admin.set_password(app.config['SUPER_ADMIN_PASSWORD'])
            db.session.add(super_admin)
            db.session.commit()
            print("✓ Created default super admin account")

        # # Create test account if doesn't exist (lower privilege, configurable via admin UI)
        # test_username = app.config['TEST_ACCOUNT_USERNAME']
        # test_account = Admin.query.filter_by(linkblue=test_username).first()
        # if not test_account:
        #     test_account = Admin(
        #         linkblue=test_username,
        #         first_name='Test',
        #         last_name='User',
        #         email='test-user@uky.edu',
        #         role='college_admin',
        #         college_code='EN',
        #         is_primary_contact=False,
        #         has_dashboard_access=True,
        #         has_static_report_access=False,
        #         has_qb_access=False
        #     )
        #     test_account.set_password(app.config['TEST_ACCOUNT_PASSWORD'])
        #     db.session.add(test_account)
        #     db.session.commit()
        #     print("✓ Created test account (college_admin for EN)")

        # # Create departmental test account if doesn't exist (engineering contact)
        # test2_username = app.config['TEST_ACCOUNT2_USERNAME']
        # test2_account = Admin.query.filter_by(linkblue=test2_username).first()
        # if not test2_account:
        #     from app.models.course import Department
        #     engineering_dept = Department.query.filter_by(college_code='EN').order_by(Department.name).first()
        #     engineering_dept_id = engineering_dept.id if engineering_dept else None

        #     test2_account = Admin(
        #         linkblue=test2_username,
        #         first_name='Test',
        #         last_name='User2',
        #         email='test-user2@uky.edu',
        #         role='dept_admin',
        #         college_code='EN',
        #         department_id=engineering_dept_id,
        #         contact_type='Department',
        #         is_primary_contact=False,
        #         has_dashboard_access=True,
        #         has_static_report_access=False,
        #         has_qb_access=False
        #     )
        #     test2_account.set_password(app.config['TEST_ACCOUNT2_PASSWORD'])
        #     db.session.add(test2_account)
        #     db.session.commit()
        #     print("✓ Created test account (dept_admin for EN)")
    
    # Context processors for templates
    @app.context_processor
    def inject_uk_brand():
        """Make UK brand colors available in all templates"""
        return {
            'UK_BLUE': app.config['UK_BLUE'],
            'UK_WHITE': app.config['UK_WHITE'],
            'UK_BLACK': app.config['UK_BLACK'],
            'DATA_REFRESH_NOTE': app.config['DATA_REFRESH_NOTE']
        }
    
    return app
