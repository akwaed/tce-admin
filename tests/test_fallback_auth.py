"""Tests for the environment-configured super-admin fallback login."""

import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("FLASK_ENV", "testing")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.models import db
from app.models.admin import Admin


class FallbackAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            SECRET_KEY="test-secret",
            SUPER_ADMIN_USERNAME="  Custom.Admin  ",
            SUPER_ADMIN_PASSWORD="test-only-password",
        )
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        admin = Admin(
            linkblue="custom.admin",
            first_name="Custom",
            last_name="Admin",
            role="super_admin",
            is_active=True,
        )
        db.session.add(admin)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_custom_username_is_normalized_for_fallback_login(self):
        response = self.client.post(
            "/auth/admin-login",
            data={"username": "CUSTOM.ADMIN", "password": "test-only-password"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/dashboard"))

    def test_inactive_fallback_account_cannot_log_in(self):
        admin = Admin.query.filter_by(linkblue="custom.admin").one()
        admin.is_active = False
        db.session.commit()

        response = self.client.post(
            "/auth/admin-login",
            data={"username": "custom.admin", "password": "test-only-password"},
            follow_redirects=True,
        )

        self.assertIn(b"Invalid username or password", response.data)


if __name__ == "__main__":
    unittest.main()
