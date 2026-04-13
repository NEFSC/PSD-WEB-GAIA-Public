# -------------------------------------------------------------------------------
# ----- run_pipeline_manual.py --------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  John Wall
#
#    purpose:  Manually executes the GAIA processing pipeline: searches, downloads,
#              organizes, calibrates, pansharpens, and generates Cloud Optimized GeoTIFFs.
#              Uses retry decorators for resiliency during network and file operations.
#
# -------------------------------------------------------------------------------

import os
import sys
import json
import requests
import argparse
import importlib
from glob import glob
import geopandas as gpd
from pathlib import Path
from functools import partial
from multiprocessing import Pool
from django.core.management.base import BaseCommand

# Ensure project root is in sys.path
project_root = Path("C:/gis/PSD-WEB-GAIA").resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import modules (only the first time)
import animal.utils.config
import animal.utils.utils
import animal.utils.imagery_ops
import animal.utils.api_utils
import animal.utils.pgc_wrapper

# Reload to pick up code changes
importlib.reload(animal.utils.config)
importlib.reload(animal.utils.utils)
importlib.reload(animal.utils.imagery_ops)
importlib.reload(animal.utils.api_utils)
importlib.reload(animal.utils.pgc_wrapper)

# Re-import updated names after reload
from animal.utils.config import settings
from animal.utils.logging import get_animal_logger
from animal.utils.utils import logger, retry, collect_geotiffs, move_zip_to_catalog, unzip_to_loading_events, match_pan_ms_pairs, copy_subdir_tiffs_to_flat_dir, upload_to_azure
from animal.utils.imagery_ops import pansharpen_imagery, run_cog_creation
from animal.utils.api_utils import ee_login, search_imagery, build_ee_query_payload, robust_download
from animal.utils.pgc_wrapper import calibrate_pair

logger = get_animal_logger(__name__)

# Define the sequence of pipeline steps
PIPELINE_STEPS = [
    'search',
    'download',
    'organize',
    'calibrate',
    'pansharpen',
    'create_cogs',
    'upload'
]

class Command(BaseCommand):
    help = 'Runs the end-to-end GAIA imagery processing pipeline manually.'

    def add_arguments(self, parser):
        # Arguments for file paths and dates, with defaults from settings
        parser.add_argument("--aoi-shapefile", default=settings.aoi_shp, help="Path to the AOI ESRI Shapefile.")
        parser.add_argument("--catalog", default=settings.imagery_dataset, help="Imagery catalog to be pulled from.")
        parser.add_argument("--dem", default=settings.dem_file, help="Path to the DEM file for calibration.")
        parser.add_argument("--start-date", default=settings.start_date, help="Start date in YYYY-MM-DD format.")
        parser.add_argument("--end-date", default=settings.end_date, help="End date in YYYY-MM-DD format.")
        parser.add_argument("--data-dir", default=settings.data_dir, help="Primary data directory.")
        parser.add_argument("--output-dir", default=settings.img_dir, help="Directory to save output imagery.")
        parser.add_argument("--geojson-dir", default=settings.geojson_dir, help="Directory to save output GeoJSON.")
        parser.add_argument("--usgs-username", default=settings.usgs_username, help="USGS EarthExplorer username.")
        parser.add_argument("--token", default=settings.token, help="USGS token string.")
        parser.add_argument("--processes", type=int, default=4, help="Number of parallel processes to use for heavy tasks.")
        parser.add_argument("--azure-account-name", default=settings.azure_account_name, help="Azure account name.")
        parser.add_argument("--azure-account-key", default=settings.azure_account_key, help="Azure account key.")
        parser.add_argument("--azure-container-name", default=settings.azure_container_name, help="Azure container name.")

        # Arguments to control pipeline execution
        parser.add_argument("--start-at", choices=PIPELINE_STEPS, default=PIPELINE_STEPS[0], help="The step to start the pipeline from.")
        parser.add_argument("--stop-after", choices=PIPELINE_STEPS, default=PIPELINE_STEPS[-1], help="The step to stop the pipeline after.")

    def handle(self, *args, **options):
        # --- SETUP ---
        aoi_shp = Path(options['aoi_shapefile'])
        catalog = options['catalog']
        dem_file = Path(options['dem'])
        start_date = options['start_date']
        end_date = options['end_date']
        data_dir = Path(options['data_dir'])
        img_dir = Path(options['output_dir'])
        geojson_dir = Path(options['geojson_dir'])
        usgs_user = options['usgs_username']
        usgs_token = options['token']
        azure_account_name = options['azure_account_name']
        azure_account_key = options['azure_account_key']
        azure_container_name = options['azure_container_name']

        # Determine which steps to run
        start_index = PIPELINE_STEPS.index(options['start_at'])
        stop_index = PIPELINE_STEPS.index(options['stop_after'])
        steps_to_run = PIPELINE_STEPS[start_index : stop_index + 1]

        self.stdout.write(f"Steps to run: {steps_to_run}")

        # Temp checkpoint files
        search_checkpoint = geojson_dir / "search_results.geojson"
        download_checkpoint = data_dir / "download.SUCCESS"
        organize_checkpoint = data_dir / "organize.SUCCESS"
        calibrate_checkpoint = data_dir / "calibrate.SUCCESS"
        pansharpen_checkpoint = data_dir / "pansharpen.SUCCESS"
        cogs_checkpoint = data_dir / "create_cogs.SUCCESS"
        upload_checkpoint = data_dir / "upload.SUCCESS"

        self.stdout.write(self.style.SUCCESS(f"--- Starting GAIA Pipeline ---"))

        # --- PIPELINE ORCHESTRATION ---

        # Steps 1, 2, and 3: Search, Download, and Organize
        if "search" in steps_to_run or "download" in steps_to_run:
            if not search_checkpoint.exists():
                self.stdout.write(self.style.HTTP_INFO("Step 1: Searching for imagery..."))
                session = ee_login(requests.Session(), settings.usgs_username, settings.token)
                gdf = gpd.read_file(aoi_shp).to_crs("EPSG:4326")
                aoi = gdf.geometry.iloc[0]
                results_gdf = search_imagery(aoi, catalog, start_date, end_date, session)
                results_gdf.to_file(search_checkpoint, driver='GeoJSON')
                search_checkpoint.touch()
            else:
                self.stdout.write("Checkpoint found: Skipping search.")
                results_gdf = gpd.read_file(search_checkpoint)

        if "download" in steps_to_run:
            if not download_checkpoint.exists():
                self.stdout.write(self.style.HTTP_INFO("Step 2: Downloading imagery..."))
                session = ee_login(requests.Session(), settings.usgs_username, settings.token)
                process_partial = partial(robust_download, session=session, datasetName=catalog, out_dir=img_dir)
                with Pool(processes=3) as pool:
                    pool.map(process_partial, results_gdf['Entity ID'].to_list())
                download_checkpoint.touch()
            else:
                self.stdout.write("Checkpoint found: Skipping download.")

        if "organize" in steps_to_run:
            # Check for the prerequisite checkpoint from the 'download' step.
            if not download_checkpoint.exists():
                self.stderr.write(self.style.ERROR(
                    "Cannot run 'organize' without running the 'download' step.\n\n Exiting..."
                ))
                return

            if not organize_checkpoint.exists():
                self.stdout.write("Running organization step...")
                organize_and_unzip_from_manifest(img_dir, search_checkpoint)
                organize_checkpoint.touch()
            else:
                self.stdout.write("Skipping organization, checkpoint found.")

        # Step 4: Calibrate Pairs
        if "calibrate" in steps_to_run:
            if not calibrate_checkpoint.exists():
                self.stdout.write("Running calibration step...")
                tifs = collect_geotiffs(img_dir)
                pairs, no_ms, no_pan = match_pan_ms_pairs([str(t) for t in tifs])
                self.stdout.write(f"Found {len(pairs)} pairs, {no_ms} without MS, {no_pan} without PAN.")
                process_partial = partial(calibrate_pair, dem=dem_file)
                with Pool(processes=8) as pool:
                    calibrated = pool.map(process_partial, list(pairs.items()))
                calibrated_pairs = [(str(pan_path), str(msi_path)) for pan_path, msi_path in calibrated]
                with open(calibrate_checkpoint, 'w') as f:
                    json.dump(calibrated_pairs, f)
            else:
                self.stdout.write("Skipping calibration, checkpoint found.")
                with open(calibrate_checkpoint, 'r') as f:
                    calibrated_pairs = json.load(f)

        # Step 5: Pansharpen Imagery
        if "pansharpen" in steps_to_run:
            if not pansharpen_checkpoint.exists():
                self.stdout.write("Running pansharpening step...")
                with Pool(processes=4) as pool:
                    pansharpened_files = pool.map(pansharpen_imagery, calibrated_pairs)
                with open(pansharpen_checkpoint, 'w') as f:
                    json.dump(pansharpened, f)
                # copy_subdir_tiffs_to_flat_dir(img_dir, 'pansharpened', 'pan')
            else:
                self.stdout.write("Skipping pansharpening, checkpoint found.")
                with open(pansharpen_checkpoint, 'r') as f:
                    pansharpened_files = json.load(f)
                self.stdout.write(f"Pansharpening images: {pansharpened_files}")

        # Step 6: Create Cloud Optimized GeoTIFFs
        if "create_cogs" in steps_to_run:
            if not cogs_checkpoint.exists():
                self.stdout.write("Running COG creation step...")
                cogs = glob(os.path.join(img_dir, 'cogs', '*.tif'), recursive=True)
                cog_filenames = {os.path.basename(cog) for cog in cogs}
                remaining = [img for img in pansharpened_files if os.path.basename(img) not in cog_filenames]
                run_cog_creation(primary_list=remaining, fallback_list=pansharpened_files, processes=2)
                cogs = glob(os.path.join(img_dir, 'cogs', '*.tif'), recursive=True)
                with open(cogs_checkpoint, 'w') as f:
                    json.dump(cogs, f)
            else:
                self.stdout.write("Skipping COG creation, checkpoint found.")
                with open(cogs_checkpoint, 'r') as f:
                    cogs = json.load(f)

        # Step 7: Upload COGs to Azure
        if "upload" in steps_to_run:
            if not upload_checkpoint.exists():
                self.stdout.write("Running upload step...")
                upload_to_azure(azure_account_name,
                                azure_account_key,
                                azure_container_name,
                                local_dir=os.path.join(settings.img_dir, 'cogs'))
                upload_checkpoint.touch()
            else:
                self.stdout.write("Skipping upload, checkpoint found.")

        logger.info("[SUCCESS] GAIA manual pipeline run completed!!!")