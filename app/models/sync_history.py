"""
Sync history models: SyncRun + ChangeLog

These were introduced as part of the fast-sync rewrite. The sync service
itself writes to these tables via raw sqlite3 for speed; these SQLAlchemy
classes exist so the Flask UI can read from them like any other table.

Schema notes
------------
- sync_run: one row per call to the sync service. Holds the wall-clock,
  row-count summary, and any error text. The UI shows these alongside
  the older data_sync_logs entries.
- change_log: one row per added/updated/removed field. Written in bulk
  by the sync service during diff-commit. Indexed on (entity_type,
  entity_key) for "what changed for this course?" lookups and on
  (sync_run_id, change_type) for "what happened in this sync?" views.
"""
from datetime import datetime, timezone
UTC = timezone.utc
from app.models import db


class SyncRun(db.Model):
    """One row per CSV->DB sync execution."""
    __tablename__ = 'sync_runs'

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), index=True)
    completed_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='running', index=True)

    # Row count roll-ups
    courses_added = db.Column(db.Integer, default=0)
    courses_updated = db.Column(db.Integer, default=0)
    courses_removed = db.Column(db.Integer, default=0)
    instructors_added = db.Column(db.Integer, default=0)
    instructors_removed = db.Column(db.Integer, default=0)
    students_counted = db.Column(db.Integer, default=0)
    change_log_rows = db.Column(db.Integer, default=0)

    elapsed_seconds = db.Column(db.Float, default=0.0)
    error_text = db.Column(db.Text)

    # What term range / datasource directory did this sync cover?
    datasources_path = db.Column(db.String(500))
    term_codes = db.Column(db.String(500))  # comma-separated

    changes = db.relationship(
        'ChangeLog', backref='sync_run', lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<SyncRun {self.id} {self.status} {self.started_at}>'

    @property
    def duration_display(self):
        if self.elapsed_seconds is None:
            return 'Unknown'
        s = int(self.elapsed_seconds)
        if s < 60:
            return f'{s}s'
        return f'{s // 60}m {s % 60}s'


class ChangeLog(db.Model):
    """Per-field change record, one row per added/updated/removed value."""
    __tablename__ = 'change_log'

    id = db.Column(db.Integer, primary_key=True)
    sync_run_id = db.Column(
        db.Integer, db.ForeignKey('sync_runs.id'), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), index=True)

    # 'course', 'instructor', 'student', 'student_count'
    entity_type = db.Column(db.String(30), nullable=False, index=True)

    # For courses / student_count: section_key
    # For instructors / students: "{section_key}|{user_id}"
    entity_key = db.Column(db.String(200), nullable=False, index=True)

    # 'added', 'updated', 'removed'
    change_type = db.Column(db.String(20), nullable=False, index=True)

    # For 'updated' rows, which field changed.
    # Null for 'added'/'removed' (whole row).
    field_name = db.Column(db.String(100))

    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)

    # Cached human-readable label for the UI (e.g. "CS101-001 (Intro to CS)")
    display_label = db.Column(db.String(300))

    __table_args__ = (
        db.Index('ix_change_entity', 'entity_type', 'entity_key'),
        db.Index('ix_change_run_type', 'sync_run_id', 'change_type'),
    )

    def __repr__(self):
        return (
            f'<ChangeLog {self.change_type} {self.entity_type} '
            f'{self.entity_key} {self.field_name}>'
        )
