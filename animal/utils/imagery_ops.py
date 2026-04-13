# ------------------------------------------------------------------------------
# ----- imagery_ops.py ---------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#
#    purpose:  Contains image processing routines for the GAIA pipeline.
#              Includes imagery search, pan/MSI pairing, calibration, pansharpening,
#              and COG creation routines.
#
#    usage:
#        These functions can be called in manual pipelines or integrated
#        with Celery workers and async processing logic.
#
# ------------------------------------------------------------------------------

import os
import sys
import subprocess
import multiprocessing
from time import time
from typing import List, Tuple
import importlib
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from multiprocessing import Pool
from osgeo_utils.gdal_pansharpen import gdal_pansharpen

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from django.conf import settings

from animal.utils.api_utils import build_ee_query_payload
from animal.utils.logging import get_animal_logger
from animal.utils.utils import retry

logger = get_animal_logger(__name__)


def pansharpen_imagery(imagery_tuple: Tuple[str, str], bands: List[int] = [5, 3, 2]) -> str | None:
    """
    Runs GDAL pansharpening on a PAN/MSI pair. Includes fallback logic for block size.
    """
    logger = get_animal_logger(__name__)
    start = time()

    pan_image_path = Path(imagery_tuple[0])
    msi_image_path = Path(imagery_tuple[1])
    
    # Define the block sizes to attempt, from most performant to most stable.
    blocksizes_to_try = [512, 128]
    last_exception = None

    for blocksize in blocksizes_to_try:
        try:
            logger.info(f"[PANSHARPEN] Attempting with blocksize={blocksize} for {pan_image_path.name}...")

            shrp_dir = pan_image_path.parent.parent / "pansharpened"
            shrp_dir.mkdir(parents=True, exist_ok=True)
            shrp_image_path = shrp_dir / pan_image_path.name.replace('P1BS', 'S1BS')

            creation_options = [
                '-co', 'TILED=YES',
                '-co', f'BLOCKXSIZE={blocksize}',
                '-co', f'BLOCKYSIZE={blocksize}',
                '-co', 'COMPRESS=DEFLATE',
                '-co', 'PREDICTOR=2',
                '-co', 'BIGTIFF=YES'
            ]
            
            cmd_args = ['', '-r', 'cubic']
            for band in bands:
                cmd_args.extend(['-b', str(band)])
            cmd_args.extend(creation_options)
            cmd_args.extend([str(pan_image_path), str(msi_image_path), str(shrp_image_path)])

            gdal_pansharpen(cmd_args)

            logger.info(f"[PANSHARPEN] Success with blocksize={blocksize}. Output: {shrp_image_path}")
            return str(shrp_image_path) # Success, so we exit the function.

        except Exception as e:
            logger.warning(f"[PANSHARPEN] Failed with blocksize={blocksize}: {e}")
            last_exception = e
            # The loop will now continue to the next, smaller blocksize.

    # If the loop finishes, it means all attempts have failed.
    logger.error(f"All pansharpening attempts failed for {pan_image_path.name}.")
    if last_exception:
        # Re-raise the last known error to signal a hard failure to the pipeline.
        raise last_exception
    
    return None

@retry(max_retries=3, wait_seconds=10)
def create_single_cog(sharpened_imagery: str) -> str | None:
    """
    Robustly creates a single Cloud-Optimized GeoTIFF with performance optimizations.
    """
    logger = get_animal_logger(__name__)
    
    original_cachemax = os.environ.get("GDAL_CACHEMAX")
    original_num_threads = os.environ.get("NUM_THREADS")
    configured_num_threads = os.environ.get("GAIA_COG_NUM_THREADS", "3")
    configured_cachemax = os.environ.get("GAIA_COG_GDAL_CACHEMAX", "1536")

    configured_blocksizes_raw = os.environ.get("GAIA_COG_BLOCKSIZES", "512,256,128")
    configured_blocksizes = []
    for token in configured_blocksizes_raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            blocksize = int(token)
            if blocksize > 0:
                configured_blocksizes.append(blocksize)
        except ValueError:
            logger.warning(f"[COG] Ignoring invalid blocksize token '{token}' in GAIA_COG_BLOCKSIZES")

    if not configured_blocksizes:
        configured_blocksizes = [512, 256, 128]

    try:
        blocksizes_to_try = configured_blocksizes
        
        for blocksize in blocksizes_to_try:
            try:
                cog_dir = Path(sharpened_imagery).parent.parent / 'cogs'
                cog_dir.mkdir(parents=True, exist_ok=True)
                cog_path = cog_dir / Path(sharpened_imagery).name

                env = os.environ.copy()
                env["NUM_THREADS"] = configured_num_threads
                env["GDAL_CACHEMAX"] = configured_cachemax

                cmd = [
                    'rio', 'cogeo', 'create', '--zoom-level', '20',
                    '--overview-resampling', 'cubic', '--blocksize', str(blocksize),
                    '-w', sharpened_imagery, str(cog_path)
                ]

                logger.info(
                    f"[COG] Creating {sharpened_imagery} -> {cog_path} "
                    f"with blocksize={blocksize}, NUM_THREADS={configured_num_threads}, "
                    f"GDAL_CACHEMAX={configured_cachemax}, blocksize_profile={blocksizes_to_try}"
                )
                subprocess.run(cmd, check=True, env=env)
                
                logger.info(f"[SUCCESS] Created COG: {cog_path} with blocksize={blocksize}")
                return str(cog_path)

            except Exception as e:
                logger.warning(
                    f"[COG ATTEMPT FAILED] Failed with blocksize={blocksize}. Error: {e}"
                )
                if blocksize == blocksizes_to_try[-1]:
                    raise # Re-raise the exception for the @retry decorator

    finally:
        if original_cachemax is None:
            if "GDAL_CACHEMAX" in os.environ: del os.environ["GDAL_CACHEMAX"]
        else:
            os.environ["GDAL_CACHEMAX"] = original_cachemax
        
        if original_num_threads is None:
            if "NUM_THREADS" in os.environ: del os.environ["NUM_THREADS"]
        else:
            os.environ["NUM_THREADS"] = original_num_threads

    return None

def run_cog_creation(primary_list, fallback_list=None, processes=2):
    """
    Creates Cloud Optimized GeoTIFFs from a list. The function signature is
    kept for backward compatibility.
    """
    logger = get_animal_logger(__name__)

    def _normalize_files(value):
        if not value:
            return []
        if isinstance(value, (str, Path)):
            return [str(value)]
        return [str(v) for v in value]

    files_to_process = _normalize_files(primary_list)
    if not files_to_process:
        files_to_process = _normalize_files(fallback_list)

    if not files_to_process:
        logger.warning("[COG] No files provided to process. Skipping.")
        return []

    logger.info(f"[COG] Creating {len(files_to_process)} COGs with {processes} processes...")

    # Celery prefork workers are daemonic and cannot spawn multiprocessing children.
    current_process = multiprocessing.current_process()
    use_serial = current_process.daemon or int(processes) <= 1 or len(files_to_process) == 1

    if use_serial:
        if current_process.daemon and int(processes) > 1:
            logger.info("[COG] Daemon process detected; using serial COG creation.")
        results = [create_single_cog(file_path) for file_path in files_to_process]
    else:
        with Pool(processes=processes) as pool:
            results = pool.map(create_single_cog, files_to_process)

    successful_cogs = [r for r in results if r is not None]
    failed_count = len(files_to_process) - len(successful_cogs)
    
    logger.info(f"[COG] Process complete. Successfully created {len(successful_cogs)} COGs.")
    if failed_count > 0:
        logger.warning(f"[COG] {failed_count} COG(s) failed to create after all retries.")

    return successful_cogs