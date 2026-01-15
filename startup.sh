#!/bin/bash
# Azure App Service Startup Script

# Create instance directory for SQLite database
mkdir -p instance

# Start gunicorn (database tables created automatically by Flask app)
gunicorn --bind=0.0.0.0:8000 --timeout 600 --workers 2 wsgi:app
