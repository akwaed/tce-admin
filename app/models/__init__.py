"""
Database models for TCE Admin System
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

from app.models.admin import Admin, AdminAuditLog, CourseCoordinatorAssignment
from app.models.course import Course, Instructor, College, Department, SyncLog, CourseUser, StudentEnrollment
from app.models.question import QuestionBank, Question, QuestionTypeDefinition, QuestionAuditLog, QBAuditLog, QBBackup
from app.models.settings import SystemSetting, DataSyncLog
from app.models.sync_history import SyncRun, ChangeLog
