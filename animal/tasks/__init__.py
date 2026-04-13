# -------------------------------------------------------------------------------
# ----- __init__.py -------------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  John Wall
#
#    purpose:  Imports all Celery task modules in the tasks/ directory
#              so Celery autodiscovery works correctly.
#
# -------------------------------------------------------------------------------

from animal.tasks.imagery_tasks import *
from animal.tasks.tasks import *