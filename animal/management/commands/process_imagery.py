# -------------------------------------------------------------------------------
# ----- process_imagery.py ----------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  John Wall
#    created:  2025-08-21
#
#    purpose:  Management command to launch the GAIA imagery processing pipeline.
#              This is the primary entry point for running the complete workflow
#              from the command line.
#
# -------------------------------------------------------------------------------

import uuid
import logging
from pathlib import Path
from datetime import datetime

from django.core.management.base import BaseCommand
from django.conf import settings

from animal.orchestration.workflow_launcher import launch_pipeline
from animal.models import Project
from animal.utils.azure_utils import get_blob_service_client, download_shapefile_from_blob
import geopandas as gpd

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Launches the full asynchronous imagery processing pipeline.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--project',
            type=int,
            required=False,
            help='Project ID to scope this pipeline run.'
        )

    def handle(self, *args, **options):
        """The main logic of the management command."""
        chain_id = str(uuid.uuid4())
        project_id = options.get('project')
        self.stdout.write("Starting the imagery processing pipeline...")
        logger.info(f"[CHAIN {chain_id}] Starting imagery processing chain from management command")

        try:
            # 1. Perform initial setup (this runs synchronously)
            self.stdout.write("Downloading prerequisite files...")
            blob_service = get_blob_service_client(
                settings.AZURE_STORAGE_ACCOUNT_NAME,
                settings.AZURE_STORAGE_ACCOUNT_KEY
            )
            
            aoi_path = download_shapefile_from_blob(
                settings.AOI_BLOB_URI,
                Path("/app/gis/data/shapefiles"),
                blob_service
            )
            
            if not aoi_path or not aoi_path.exists():
                raise FileNotFoundError("Failed to download or locate AOI shapefile")
                
            self.stdout.write("Prerequisites downloaded successfully.")

            # 2. Prepare the task input
            self.stdout.write("Preparing AOI data...")
            gdf = gpd.read_file(aoi_path)
            gdf_wgs84 = gdf.to_crs("EPSG:4326")
            polygon = gdf_wgs84.geometry.iloc[0]
            aoi_wkt_string = polygon.wkt
            self.stdout.write("AOI data prepared successfully.")

            # 3. Launch the pipeline using the workflow launcher
            self.stdout.write("Launching the asynchronous processing chain...")
            azure_credentials = {
                "account_name": settings.AZURE_STORAGE_ACCOUNT_NAME,
                "account_key": settings.AZURE_STORAGE_ACCOUNT_KEY,
                "container_name": settings.AZURE_CONTAINER_NAME
            }
            if project_id:
                try:
                    project = Project.objects.only('id', 'label').get(id=project_id)
                    project_display = f"{project.label} (#{project.id})"
                except Project.DoesNotExist:
                    project_display = f"Project #{project_id}"
            else:
                project_display = "Unknown Project"
            
            result = launch_pipeline(
                aoi_geojson_str=aoi_wkt_string,
                start_date=settings.START_DATE,
                end_date=settings.END_DATE,
                usgs_username=settings.USGS_USERNAME,
                token=settings.USGS_TOKEN,
                azure_credentials=azure_credentials,
                chain_id=chain_id,
                project_id=project_id,
                requested_by_username="system",
                project_display=project_display,
            )

            self.stdout.write(self.style.SUCCESS(
                f"\nSuccessfully launched the imagery processing pipeline!"
                f"\nChain ID: {chain_id}"
                f"\n\nTo monitor progress:"
                f"\n1. Check the logs: tail -f logs/gaia.log"
                f"\n2. Use the Celery monitoring tools:"
                f"\n   - celery -A gaia events"
                f"\n   - celery -A gaia status"
            ))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"An error occurred while launching the chain: {e}"))
            logger.error(f"[CHAIN {chain_id}] Failed to launch imagery chain", exc_info=True)
            raise
