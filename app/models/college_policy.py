"""Local college rules, separate from the authoritative SAP source values."""
from datetime import datetime, timezone

from app.models import db


class CollegePolicy(db.Model):
    __tablename__ = 'college_policies'

    college_code = db.Column(db.String(20), db.ForeignKey('colleges.code'), primary_key=True)
    # NULL explicitly disables exclusion, including the Pharmacy default.
    # Keep the original database column name so this correction is migration-free.
    exclude_course_numbers_above = db.Column('exclude_sections_above', db.Integer, nullable=True)


class CollegeDateOverride(db.Model):
    __tablename__ = 'college_date_overrides'

    college_code = db.Column(db.String(20), db.ForeignKey('colleges.code'), primary_key=True)
    term_code = db.Column(db.String(20), primary_key=True)
    tce_start = db.Column(db.Date, nullable=False)
    tce_end = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
                           onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class CollegePolicyAudit(db.Model):
    __tablename__ = 'college_policy_audit'

    id = db.Column(db.Integer, primary_key=True)
    college_code = db.Column(db.String(20), nullable=False, index=True)
    term_code = db.Column(db.String(20))
    action = db.Column(db.String(40), nullable=False)
    before_json = db.Column(db.Text, nullable=False)
    after_json = db.Column(db.Text, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    actor = db.relationship('Admin')
    created_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
