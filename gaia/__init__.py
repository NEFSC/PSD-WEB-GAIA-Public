# -------------------------------------------------------------------------------
# ----- __init__.py (gaia) ------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  Marcus England & John Wall
#
#    purpose:  Bootstraps Celery integration so `shared_task` decorators use the
#              correct Celery app defined in gaia/celery.py.
#
# -------------------------------------------------------------------------------

from __future__ import absolute_import, unicode_literals
from .celery import app as celery_app

__all__ = ['celery_app']