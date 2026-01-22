"""
Database models for TCE Admin System
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

from app.models.admin import Admin, AdminAuditLog
from app.models.course import Course, Instructor, College, Department
from app.models.question import QuestionBank, Question, QuestionTypeDefinition, QuestionAuditLog, QBAuditLog, QBBackup
