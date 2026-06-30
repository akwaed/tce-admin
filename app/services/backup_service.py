"""
Backup Service for Question Bank and Question Mapping files
Handles automatic and manual backups with timestamp tracking
"""
import os
import shutil
from datetime import datetime, timezone
UTC = timezone.utc
from app.models import db
from app.models.question import QBBackup, QBAuditLog


# Backup storage paths
DATASOURCES_PATH = './datasources'
BACKUPS_PATH = './datasources/backups'
QB_FILENAME = 'QB.xlsx'
QM_FILENAME = 'QM.xlsx'


class BackupService:
    """Service class for managing QB and QM backups"""

    def __init__(self, datasources_path=DATASOURCES_PATH, backups_path=BACKUPS_PATH):
        self.datasources_path = datasources_path
        self.backups_path = backups_path
        self._ensure_backup_dir()

    def _ensure_backup_dir(self):
        """Ensure the backups directory exists"""
        os.makedirs(self.backups_path, exist_ok=True)

    def _get_source_path(self, backup_type):
        """Get the source file path for a backup type"""
        filename = QB_FILENAME if backup_type == 'qb' else QM_FILENAME
        return os.path.join(self.datasources_path, filename)

    def _get_backup_filename(self, backup_type, timestamp):
        """Generate backup filename with timestamp"""
        prefix = 'QB' if backup_type == 'qb' else 'QM'
        ts_str = timestamp.strftime('%Y%m%d_%H%M%S')
        return f'{prefix}_backup_{ts_str}.xlsx'

    def _has_backup_today(self, backup_type):
        """
        Check if a backup of this type already exists for today.

        Args:
            backup_type: 'qb' for Question Bank, 'qm' for Question Mapping

        Returns:
            QBBackup record if exists, None otherwise
        """
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)

        existing = QBBackup.query.filter(
            QBBackup.backup_type == backup_type,
            QBBackup.is_deleted == False,
            QBBackup.timestamp >= today_start,
            QBBackup.timestamp <= today_end
        ).first()

        return existing

    def create_backup(self, backup_type, reason, admin, details=None, force=False):
        """
        Create a backup of the specified file type

        Args:
            backup_type: 'qb' for Question Bank, 'qm' for Question Mapping
            reason: 'import', 'export', 'change', 'manual'
            admin: The admin user creating the backup
            details: Optional dict with additional details
            force: If True, create backup even if one exists today (for manual backups)

        Returns:
            QBBackup record or None if source file doesn't exist or backup already exists today
        """
        source_path = self._get_source_path(backup_type)

        # Check if source file exists
        if not os.path.exists(source_path):
            return None

        # EOD Logic: Only create one backup per day per type (unless manual/forced)
        # Manual backups always create a new backup
        if reason != 'manual' and not force:
            existing_backup = self._has_backup_today(backup_type)
            if existing_backup:
                # Return existing backup instead of creating a new one
                return existing_backup

        timestamp = datetime.now(UTC)
        backup_filename = self._get_backup_filename(backup_type, timestamp)
        backup_path = os.path.join(self.backups_path, backup_filename)

        # Copy the file
        shutil.copy2(source_path, backup_path)

        # Get file size
        file_size = os.path.getsize(backup_path)

        # Create database record
        backup = QBBackup(
            backup_type=backup_type,
            timestamp=timestamp,
            filename=backup_filename,
            file_size=file_size,
            reason=reason,
            created_by_id=admin.id if admin else None,
            created_by_linkblue=admin.linkblue if admin else 'system'
        )
        if details:
            backup.details = details

        db.session.add(backup)
        db.session.commit()

        # Log the backup creation
        QBAuditLog.log_action(
            'backup_created',
            admin,
            details={
                'backup_type': backup_type,
                'filename': backup_filename,
                'reason': reason
            },
            backup_id=backup.id
        )
        db.session.commit()

        return backup

    def create_both_backups(self, reason, admin, details=None, force=False):
        """
        Create backups of both QB and QM files

        Args:
            reason: 'import', 'export', 'change', 'manual'
            admin: The admin user creating the backup
            details: Optional dict with additional details
            force: If True, create backup even if one exists today

        Returns:
            Tuple of (qb_backup, qm_backup) - either can be None if file doesn't exist
        """
        qb_backup = self.create_backup('qb', reason, admin, details, force)
        qm_backup = self.create_backup('qm', reason, admin, details, force)
        return qb_backup, qm_backup

    def delete_backup(self, backup_id, admin):
        """
        Delete a backup (soft delete in database, removes file)

        Args:
            backup_id: ID of the backup to delete
            admin: The admin user deleting the backup

        Returns:
            True if successful, False otherwise
        """
        backup = QBBackup.query.get(backup_id)
        if not backup or backup.is_deleted:
            return False

        # Delete the file if it exists
        backup_path = os.path.join(self.backups_path, backup.filename)
        if os.path.exists(backup_path):
            os.remove(backup_path)

        # Soft delete in database
        backup.is_deleted = True
        backup.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        backup.deleted_by_id = admin.id if admin else None

        # Log the deletion
        QBAuditLog.log_action(
            'backup_deleted',
            admin,
            details={
                'backup_id': backup_id,
                'backup_type': backup.backup_type,
                'filename': backup.filename
            }
        )

        db.session.commit()
        return True

    def restore_backup(self, backup_id, admin):
        """
        Restore a backup file (copy back to active location)

        Args:
            backup_id: ID of the backup to restore
            admin: The admin user performing the restore

        Returns:
            True if successful, False otherwise
        """
        backup = QBBackup.query.get(backup_id)
        if not backup or backup.is_deleted:
            return False

        backup_path = os.path.join(self.backups_path, backup.filename)
        if not os.path.exists(backup_path):
            return False

        # First, create a backup of the current file before restoring
        self.create_backup(
            backup.backup_type,
            'change',
            admin,
            details={'reason': f'Before restore from backup {backup_id}'}
        )

        # Copy the backup to the active location
        dest_path = self._get_source_path(backup.backup_type)
        shutil.copy2(backup_path, dest_path)

        # Log the restore
        QBAuditLog.log_action(
            'backup_restored',
            admin,
            details={
                'backup_id': backup_id,
                'backup_type': backup.backup_type,
                'filename': backup.filename
            }
        )

        db.session.commit()
        return True

    def get_backups(self, backup_type=None, include_deleted=False, limit=50):
        """
        Get list of backups with optional filtering

        Args:
            backup_type: Optional - filter by 'qb' or 'qm'
            include_deleted: Include soft-deleted backups
            limit: Maximum number of results

        Returns:
            List of QBBackup records
        """
        query = QBBackup.query

        if backup_type:
            query = query.filter_by(backup_type=backup_type)

        if not include_deleted:
            query = query.filter_by(is_deleted=False)

        return query.order_by(QBBackup.timestamp.desc()).limit(limit).all()

    def get_backup_file_path(self, backup_id):
        """
        Get the full file path for a backup

        Args:
            backup_id: ID of the backup

        Returns:
            Full file path or None if not found
        """
        backup = QBBackup.query.get(backup_id)
        if not backup or backup.is_deleted:
            return None

        backup_path = os.path.join(self.backups_path, backup.filename)
        if os.path.exists(backup_path):
            return backup_path
        return None

    def get_backup_stats(self):
        """
        Get statistics about backups

        Returns:
            Dict with backup statistics
        """
        qb_count = QBBackup.query.filter_by(backup_type='qb', is_deleted=False).count()
        qm_count = QBBackup.query.filter_by(backup_type='qm', is_deleted=False).count()

        # Calculate total size
        total_size = 0
        for backup in QBBackup.query.filter_by(is_deleted=False).all():
            if backup.file_size:
                total_size += backup.file_size

        return {
            'qb_count': qb_count,
            'qm_count': qm_count,
            'total_count': qb_count + qm_count,
            'total_size': total_size,
            'total_size_mb': round(total_size / (1024 * 1024), 2) if total_size > 0 else 0
        }


# Global instance
_backup_service = None


def get_backup_service():
    """Get or create the global backup service instance"""
    global _backup_service
    if _backup_service is None:
        _backup_service = BackupService()
    return _backup_service
