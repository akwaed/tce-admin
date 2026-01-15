"""
WSGI Entry Point for Azure App Service
"""
import os
from app import create_app

# Use production config when FLASK_ENV is set, otherwise default to production for Azure
config_name = os.environ.get('FLASK_ENV', 'production')
app = create_app(config_name)

if __name__ == '__main__':
    app.run()
