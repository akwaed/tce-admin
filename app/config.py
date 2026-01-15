"""
TCE Admin System Configuration
University of Kentucky
"""
import os
from datetime import timedelta

class Config:
    """Base configuration"""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'tce-admin-dev-key-change-in-production'
    
    # Database
    basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'instance', 'tce_admin.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Session
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    
    # Super Admin Credentials (for fallback login when Azure AD unavailable)
    SUPER_ADMIN_USERNAME = os.environ.get('SUPER_ADMIN_USERNAME') or 'tceadmin'
    SUPER_ADMIN_PASSWORD = os.environ.get('SUPER_ADMIN_PASSWORD') or 'UK_TCE_2025!'

    # Test Account Credentials (configurable lower-privilege account for testing)
    TEST_ACCOUNT_USERNAME = os.environ.get('TEST_ACCOUNT_USERNAME') or 'testuser'
    TEST_ACCOUNT_PASSWORD = os.environ.get('TEST_ACCOUNT_PASSWORD') or 'Test_2025!'
    TEST_ACCOUNT2_USERNAME = os.environ.get('TEST_ACCOUNT2_USERNAME') or 'testuser2'
    TEST_ACCOUNT2_PASSWORD = os.environ.get('TEST_ACCOUNT2_PASSWORD') or 'Test2_2025!'
    
    # UK Brand Colors
    UK_BLUE = '#0033A0'
    UK_WHITE = '#FFFFFF'
    UK_BLACK = '#000000'
    
    # Data sync settings
    UKDIG_SYNC_HOUR = 18  # 6 PM
    DATA_REFRESH_NOTE = "Data is refreshed daily at 6:00 PM EST"
    
    # Pagination
    ITEMS_PER_PAGE = 50

    @staticmethod
    def init_app(app):
        """Hook for environment-specific initialization."""
        return None


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    @classmethod
    def init_app(cls, app):
        required = [
            'SECRET_KEY',
            'DATABASE_URL',
            'SUPER_ADMIN_USERNAME',
            'SUPER_ADMIN_PASSWORD'
        ]
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise RuntimeError(f"Missing required production env vars: {', '.join(missing)}")
    

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
