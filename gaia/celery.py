# -------------------------------------------------------------------------------
# ----- celery.py ---------------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    purpose:  Celery configuration for the GAIA project. This file initializes
#              the Celery app and integrates it with Django's settings.
#
# -------------------------------------------------------------------------------
import os
import django
from celery import Celery

# Set the default Django settings module for the 'celery' program
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gaia.settings')

# Setup Django first
django.setup()

# Create the Celery app
app = Celery('gaia')

# Use Django settings for Celery configuration
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django app configs
app.autodiscover_tasks()

# Export the app for use in other modules
__all__ = ('app',)