"""
System Settings and Data Sync Logging Models
For managing Explorance Blue API integration and unified sync logging
"""
from datetime import datetime, timezone
UTC = timezone.utc
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
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        onupdate=lambda: datetime.now(UTC).replace(tzinfo=None),
    )
    updated_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)

    # Relationship
    updated_by = db.relationship('Admin', foreign_keys=[updated_by_id])

    # Known setting keys
    BLUE_API_KEY = 'blue_api_key'
    BLUE_WS_URL = 'blue_ws_url'

    # DRA (Data151) push controls
    # When true, daily_sync always runs DRA after HANA/DB/Blue steps.
    DRA_INCLUDE_IN_DAILY_SYNC = 'dra_include_in_daily_sync'
    # When true, college/super admin list changes mark DRA pending for daily sync.
    DRA_QUEUE_ON_ADMIN_CHANGE = 'dra_queue_on_admin_change'
    # Dirty flag set by admin-list changes; cleared after a successful DRA push.
    DRA_SYNC_PENDING = 'dra_sync_pending'

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
            setting.updated_at = datetime.now(UTC).replace(tzinfo=None)
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
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
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
    TYPE_DRA_TO_BLUE = 'dra_to_blue'

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
        ca = self.completed_at
        sa = self.started_at
        if getattr(sa, 'tzinfo', None) is None:
            sa = sa.replace(tzinfo=UTC)
        if getattr(ca, 'tzinfo', None) is None:
            ca = ca.replace(tzinfo=UTC)
        return ca - sa

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
            self.TYPE_FULL_SYNC: 'Full Sync (HANA to Blue)',
            self.TYPE_DRA_TO_BLUE: 'DRA to Blue (Data151)',
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
        self.completed_at = datetime.now(UTC).replace(tzinfo=None)

    def fail(self, error_message=None):
        """Mark sync as failed."""
        self.status = self.STATUS_FAILED
        self.completed_at = datetime.now(UTC).replace(tzinfo=None)
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


class DataFileSyncEvent(db.Model):
    """
    Per-file granular sync event for timeline display.
    Tracks each individual file pulled from HANA or pushed to Blue.
    """
    __tablename__ = 'data_file_sync_events'

    id = db.Column(db.Integer, primary_key=True)
    sync_log_id = db.Column(
        db.Integer, db.ForeignKey('data_sync_logs.id', ondelete='CASCADE'),
        nullable=True, index=True
    )

    # 'hana_pull' | 'blue_push'
    direction = db.Column(db.String(20), nullable=False, index=True)

    # e.g. 'Courses.csv', 'Users.csv'
    file_name = db.Column(db.String(100), nullable=False)

    # Blue datasource ID e.g. 'Data161' (null for HANA pulls)
    datasource_id = db.Column(db.String(50), nullable=True)

    started_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    completed_at = db.Column(db.DateTime, nullable=True)

    # 'running' | 'success' | 'failed' | 'skipped'
    status = db.Column(db.String(20), nullable=False, default='running')

    row_count = db.Column(db.Integer, nullable=True, default=0)
    rows_added = db.Column(db.Integer, nullable=True, default=0)
    rows_updated = db.Column(db.Integer, nullable=True, default=0)
    rows_removed = db.Column(db.Integer, nullable=True, default=0)
    error_message = db.Column(db.Text, nullable=True)
    elapsed_seconds = db.Column(db.Float, nullable=True)

    # Relationship back to the parent sync log
    sync_log = db.relationship('DataSyncLog', backref=db.backref(
        'file_events', lazy='dynamic', cascade='all, delete-orphan'
    ))

    __table_args__ = (
        db.Index('ix_file_events_direction_file', 'direction', 'file_name'),
    )

    def __repr__(self):
        return f'<DataFileSyncEvent {self.direction} {self.file_name} {self.status}>'

    @property
    def direction_display(self):
        return {
            'hana_pull': 'HANA PULL',
            'blue_push': 'BLUE PUSH',
        }.get(self.direction, self.direction)

    @property
    def status_icon(self):
        return {
            'running': '⏳',
            'success': '✅',
            'failed': '❌',
            'skipped': '⏩',
        }.get(self.status, '❓')

    @property
    def elapsed_display(self):
        if self.elapsed_seconds is None:
            return ''
        s = int(self.elapsed_seconds)
        if s < 60:
            return f'{s}s'
        return f'{s // 60}m {s % 60}s'

    def to_dict(self):
        return {
            'id': self.id,
            'sync_log_id': self.sync_log_id,
            'direction': self.direction,
            'direction_display': self.direction_display,
            'file_name': self.file_name,
            'datasource_id': self.datasource_id,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'status': self.status,
            'status_icon': self.status_icon,
            'row_count': self.row_count,
            'rows_added': self.rows_added,
            'rows_updated': self.rows_updated,
            'rows_removed': self.rows_removed,
            'error_message': self.error_message,
            'elapsed_seconds': self.elapsed_seconds,
            'elapsed_display': self.elapsed_display,
        }


class BlueSyncDatasource(db.Model):
    """
    DB-driven registry of datasources pushed to Explorance Blue.
    Replaces the hardcoded DATASOURCES dict in blue_sync.py.
    """
    __tablename__ = 'blue_sync_datasources'

    id = db.Column(db.Integer, primary_key=True)
    datasource_id = db.Column(db.String(50), nullable=False, unique=True)
    # e.g. 'Data144', 'Data161', 'Data999'
    display_name = db.Column(db.String(100), nullable=False)
    # e.g. 'Users', 'Courses', 'Question Bank'

    # Legacy key for backward-compatible lookups (e.g. 'users', 'courses')
    legacy_key = db.Column(db.String(50), nullable=True, index=True)

    block_name = db.Column(db.String(100), nullable=True)
    # Blue DataBlockName; null = auto-discover via GetDataBlockInformation()

    csv_file = db.Column(db.String(255), nullable=False)
    # Relative to datasources/ dir, e.g. 'Courses.csv' or 'QB.xlsx'

    source_type = db.Column(db.String(30), nullable=False, default='hana_csv')
    # 'hana_csv' | 'generated_csv' | 'custom'

    columns = db.Column(db.JSON, nullable=True)
    # List of column name strings; null = auto-discover from Blue

    required_columns = db.Column(db.JSON, nullable=True)
    # Subset of columns that must be non-empty

    column_renames = db.Column(db.JSON, nullable=True)
    # Optional map e.g. {"FIRSTNAME": "FIRSTNAME_1", "LASTNAME": "LASTNAME_1"}
    # Applied during CSV load before sending to Blue (Bug fix port from push_users_to_blue.py)

    import_order = db.Column(db.Integer, nullable=False, default=99)
    # Lower = imported first; enforces dependency order

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # If False, skipped in the daily sync

    is_system = db.Column(db.Boolean, nullable=False, default=False)
    # Core HANA datasources — can be disabled but not deleted

    wait_after_seconds = db.Column(db.Integer, nullable=False, default=300)
    # Delay after this datasource push before the next one

    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at = db.Column(
        db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None),
        onupdate=lambda: datetime.now(UTC).replace(tzinfo=None)
    )
    created_by_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)

    created_by = db.relationship('Admin', foreign_keys=[created_by_id])

    __table_args__ = (
        db.Index('ix_blue_ds_import_order', 'import_order'),
        db.Index('ix_blue_ds_active', 'is_active'),
    )

    def __repr__(self):
        return f'<BlueSyncDatasource {self.datasource_id}: {self.display_name}>'

    def to_dict(self):
        return {
            'id': self.id,
            'datasource_id': self.datasource_id,
            'display_name': self.display_name,
            'legacy_key': self.legacy_key,
            'block_name': self.block_name,
            'csv_file': self.csv_file,
            'source_type': self.source_type,
            'columns': self.columns,
            'required_columns': self.required_columns,
            'column_renames': self.column_renames,
            'import_order': self.import_order,
            'is_active': self.is_active,
            'is_system': self.is_system,
            'wait_after_seconds': self.wait_after_seconds,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
