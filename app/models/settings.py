"""
System Settings and Data Sync Logging Models
For managing Explorance Blue API integration and unified sync logging
"""
from datetime import datetime
from app.models import db
import json


class SystemSetting(db.Model):
    """
    Key-value store for system-wide settings.
    Used for storing sensitive configuration like API keys.
    """
    __tablename__ = 'system_settings'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.String(500))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)

    # Relationship
    updated_by = db.relationship('Admin', foreign_keys=[updated_by_id])

    # Known setting keys
    BLUE_API_KEY = 'blue_api_key'
    BLUE_WS_URL = 'blue_ws_url'

    def __repr__(self):
        return f'<SystemSetting {self.key}>'

    @classmethod
    def get(cls, key, default=None):
        """Get a setting value by key."""
        setting = cls.query.get(key)
        return setting.value if setting else default

    @classmethod
    def set(cls, key, value, description=None, admin=None):
        """Set a setting value."""
        setting = cls.query.get(key)
        if setting:
            setting.value = value
            if description:
                setting.description = description
            if admin:
                setting.updated_by_id = admin.id
            setting.updated_at = datetime.utcnow()
        else:
            setting = cls(
                key=key,
                value=value,
                description=description,
                updated_by_id=admin.id if admin else None
            )
            db.session.add(setting)
        db.session.commit()
        return setting

    @property
    def masked_value(self):
        """Return masked version of value for display (useful for API keys)."""
        if not self.value:
            return None
        if len(self.value) <= 8:
            return '****'
        return f"{self.value[:4]}...{self.value[-4:]}"

    def to_dict(self):
        return {
            'key': self.key,
            'value_masked': self.masked_value,
            'description': self.description,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'updated_by': self.updated_by.linkblue if self.updated_by else None
        }


class DataSyncLog(db.Model):
    """
    Unified log for all data sync operations.
    Tracks both HANA-to-datasource syncs and datasource-to-Blue pushes.
    """
    __tablename__ = 'data_sync_logs'

    id = db.Column(db.Integer, primary_key=True)

    # Sync type: 'hana_to_datasource', 'datasource_to_blue', 'full_sync'
    sync_type = db.Column(db.String(50), nullable=False, index=True)

    # Operation status: 'running', 'completed', 'failed', 'cancelled'
    status = db.Column(db.String(20), nullable=False, default='running')

    # Timestamps
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    # Who triggered the sync (null for automated)
    triggered_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    trigger_type = db.Column(db.String(20))  # 'manual', 'scheduled', 'api'

    # Summary statistics (JSON)
    # For HANA sync: records per file
    # For Blue push: records per datasource
    summary_json = db.Column(db.Text)

    # Field-level change tracking (JSON)
    # Example: {"ACADEMIC_TERM_ID": 27272, "TCE_END_DATE": 25, ...}
    field_changes_json = db.Column(db.Text)

    # File-level statistics (JSON)
    # Example: {"Courses.csv": {"added": 10, "updated": 50, "unchanged": 100}, ...}
    file_stats_json = db.Column(db.Text)

    # Blue-specific: datasource import results
    # Example: {"users": "SUCCESS", "courses": "SUCCESS", "instructors": "FAILED", ...}
    blue_results_json = db.Column(db.Text)

    # Error messages (JSON array)
    errors_json = db.Column(db.Text)

    # Total records processed
    records_processed = db.Column(db.Integer, default=0)
    records_added = db.Column(db.Integer, default=0)
    records_updated = db.Column(db.Integer, default=0)
    records_failed = db.Column(db.Integer, default=0)

    # Relationship
    triggered_by = db.relationship('Admin', foreign_keys=[triggered_by_id])

    # Sync type constants
    TYPE_HANA_TO_DATASOURCE = 'hana_to_datasource'
    TYPE_DATASOURCE_TO_BLUE = 'datasource_to_blue'
    TYPE_FULL_SYNC = 'full_sync'

    # Status constants
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'

    def __repr__(self):
        return f'<DataSyncLog {self.id}: {self.sync_type} at {self.started_at}>'

    @property
    def summary(self):
        """Get summary as dict."""
        if not self.summary_json:
            return {}
        try:
            return json.loads(self.summary_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @summary.setter
    def summary(self, value):
        """Set summary from dict."""
        self.summary_json = json.dumps(value) if value else None

    @property
    def field_changes(self):
        """Get field changes as dict."""
        if not self.field_changes_json:
            return {}
        try:
            return json.loads(self.field_changes_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @field_changes.setter
    def field_changes(self, value):
        """Set field changes from dict."""
        self.field_changes_json = json.dumps(value) if value else None

    @property
    def file_stats(self):
        """Get file stats as dict."""
        if not self.file_stats_json:
            return {}
        try:
            return json.loads(self.file_stats_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @file_stats.setter
    def file_stats(self, value):
        """Set file stats from dict."""
        self.file_stats_json = json.dumps(value) if value else None

    @property
    def blue_results(self):
        """Get Blue import results as dict."""
        if not self.blue_results_json:
            return {}
        try:
            return json.loads(self.blue_results_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @blue_results.setter
    def blue_results(self, value):
        """Set Blue results from dict."""
        self.blue_results_json = json.dumps(value) if value else None

    @property
    def errors(self):
        """Get errors as list."""
        if not self.errors_json:
            return []
        try:
            return json.loads(self.errors_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @errors.setter
    def errors(self, value):
        """Set errors from list."""
        self.errors_json = json.dumps(value) if value else None

    def add_error(self, error_message):
        """Add an error to the list."""
        errors = self.errors
        errors.append(error_message)
        self.errors = errors

    @property
    def duration(self):
        """Calculate sync duration."""
        if not self.completed_at or not self.started_at:
            return None
        return self.completed_at - self.started_at

    @property
    def duration_display(self):
        """Human-readable duration."""
        dur = self.duration
        if not dur:
            return 'In progress' if self.status == 'running' else 'Unknown'
        total_seconds = int(dur.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}m {seconds}s"

    @property
    def sync_type_display(self):
        """Human-readable sync type."""
        type_names = {
            self.TYPE_HANA_TO_DATASOURCE: 'HANA to Datasource',
            self.TYPE_DATASOURCE_TO_BLUE: 'Datasource to Blue',
            self.TYPE_FULL_SYNC: 'Full Sync (HANA to Blue)'
        }
        return type_names.get(self.sync_type, self.sync_type)

    @property
    def status_class(self):
        """CSS class for status badge."""
        status_classes = {
            'running': 'badge-uk-primary',
            'completed': 'badge-uk-success',
            'failed': 'badge-uk-danger',
            'cancelled': 'badge-uk-secondary'
        }
        return status_classes.get(self.status, 'badge-secondary')

    def complete(self, success=True):
        """Mark sync as completed."""
        self.status = self.STATUS_COMPLETED if success else self.STATUS_FAILED
        self.completed_at = datetime.utcnow()

    def fail(self, error_message=None):
        """Mark sync as failed."""
        self.status = self.STATUS_FAILED
        self.completed_at = datetime.utcnow()
        if error_message:
            self.add_error(error_message)

    def to_dict(self):
        return {
            'id': self.id,
            'sync_type': self.sync_type,
            'sync_type_display': self.sync_type_display,
            'status': self.status,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'duration': self.duration_display,
            'triggered_by': self.triggered_by.linkblue if self.triggered_by else None,
            'trigger_type': self.trigger_type,
            'summary': self.summary,
            'field_changes': self.field_changes,
            'file_stats': self.file_stats,
            'blue_results': self.blue_results,
            'errors': self.errors[:10] if self.errors else [],  # First 10 errors
            'records_processed': self.records_processed,
            'records_added': self.records_added,
            'records_updated': self.records_updated,
            'records_failed': self.records_failed
        }
