"""Local course settings and audit history, independent of SAP imports."""
from datetime import datetime, timezone

from app.models import db


class CourseTCEOverride(db.Model):
    __tablename__ = 'course_tce_overrides'

    # Intentionally independent of courses: a removed/reimported SAP row must
    # not silently discard a local setting for the same section key.
    section_key = db.Column(db.String(100), primary_key=True)
    tce_start = db.Column(db.Date)
    tce_end = db.Column(db.Date)
    blocked = db.Column(db.Boolean, nullable=False, default=False)
    reason = db.Column(db.Text, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
                           onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class CourseTCEAudit(db.Model):
    __tablename__ = 'course_tce_audit'

    id = db.Column(db.Integer, primary_key=True)
    section_key = db.Column(db.String(100), nullable=False, index=True)
    before_json = db.Column(db.Text, nullable=False)
    after_json = db.Column(db.Text, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    actor = db.relationship('Admin')
    created_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
