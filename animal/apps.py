
"""
Django Application Configuration for Animal app.

This class extends Django's AppConfig to provide custom configuration for the Animal application.
It includes functionality to configure Git settings and synchronize repositories.

Methods:
    ready(): Called when Django starts. Configures Git SSL settings and imports signal handlers
        - Disables SSL certificate revocation checking
        - Sets SSL backend to OpenSSL
        - Imports Celery signal handlers
        - Calls sync_repo management command
        
Raises:
    subprocess.CalledProcessError: If Git configuration commands fail
"""

from django.apps import AppConfig
from django.core.management import call_command
import subprocess

class AnimalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'animal'

    def ready(self):
        """
        Configure Git settings and import signal handlers when Django starts.
        """
        # Import signal handlers
        import animal.tasks.signals  # noqa

        # Configure Git SSL settings
        try:
            # Disable SSL certificate revocation checking
            subprocess.run(['git', 'config', '--global', 'http.schannelCheckRevoke', 'false'],
                         check=True, capture_output=True)
            # Set SSL backend to OpenSSL
            subprocess.run(['git', 'config', '--global', 'http.sslBackend', 'openssl'],
                         check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Failed to set Git configuration: {e}")