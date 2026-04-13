# -------------------------------------------------------------------------------
# ----- run_pipeline_async.py ---------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  John Wall
#
#    purpose:  Django management command that launches the GAIA Celery pipeline
#              for processing imagery asynchronously.
#
# -------------------------------------------------------------------------------

from django.core.management.base import BaseCommand
from pathlib import Path
from animal.orchestration.workflow_launcher import launch_pipeline

class Command(BaseCommand):
    help = "Run GAIA processing pipeline using Celery"

    def handle(self, *args, **options):
        calibrated_dir = Path("/path/to/calibrated")
        cog_dir = Path("/path/to/cogs")

        azure_credentials = {
            'account_name': 'your_account_name',
            'account_key': 'your_account_key',
            'container_name': 'your_container_name',
        }

        launch_pipeline(calibrated_dir, cog_dir, azure_credentials)
        self.stdout.write(self.style.SUCCESS("Pipeline launched via Celery."))
