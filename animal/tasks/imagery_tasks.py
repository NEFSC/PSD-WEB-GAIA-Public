# -------------------------------------------------------------------------------
# ----- imagery_tasks.py --------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  John Wall
#    email:    john.wall@noaa.gov
#    created:  2025-08-15
#
#    purpose:  Celery tasks for the full GAIA imagery processing pipeline.
#              Processes satellite imagery from USGS EarthExplorer through
#              calibration, pansharpening, and final delivery to Azure storage.
#
#    workflow: 1. Login and search USGS EE
#              2. Download imagery ZIPs
#              3. Organize, unzip, match pairs, and calibrate
#              4. Pansharpen calibrated images
#              5. Generate Cloud Optimized GeoTIFFs (COGs)
#              6. Upload final outputs to Azure
#              7. Clean up local data
#
#    inputs:   - USGS credentials
#              - Area of Interest (AOI) in WKT format
#              - Date range for imagery search
#              - DEM file for calibration
#              - Azure storage credentials
#
#    outputs:  - Cloud Optimized GeoTIFFs in Azure storage
#
#    notes:    Requires active Celery workers and Redis broker.
#              All tasks are idempotent and can be safely retried.
#              Each task includes detailed logging for troubleshooting.
#
# -------------------------------------------------------------------------------

# Standard library
import os
import sys
import re
import json
import stat
import time
import shutil
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Union, Callable, Any
from functools import partial
from multiprocessing import Pool
import shutil

# Third-party libraries
import requests
import pandas as pd
import geopandas as gpd
from celery import shared_task
from celery.exceptions import Retry, Ignore
from kombu.exceptions import OperationalError
from shapely.wkt import loads as load_wkt
from shapely.geometry import Polygon

@shared_task(bind=True, name='gaia.imagery.prepare_workspace')
def prepare_workspace(
    self,
    base_dir_to_prepare: str = None,
    chain_id: str = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """
    Prepare workspace by ensuring directories exist and are empty.
    This is the inverse of cleanup_local_data - it prepares a clean workspace.
    
    Args:
        base_dir_to_prepare: Base directory path to prepare
        chain_id: Unique identifier for this processing chain
        
    Returns:
        str: Status message indicating success
    """
    tag = "PREPARE"
    logger.info(f"[{tag}][{chain_id}][PROJECT {project_id}] prepare_workspace task started")
    
    try:
        if not base_dir_to_prepare:
            base_dir_to_prepare = "/app/gis/data"
            
        base_path = Path(base_dir_to_prepare)
        
        # Define directories to ensure exist and are clean
        dirs_to_prepare = [
            base_path / "imagery" / "belugas",
            base_path / "imagery" / "belugas" / "cogs",
            base_path / "geojson" / "belugas",
            base_path / "shapefiles"
        ]
        
        # Create or clean each directory
        for dir_path in dirs_to_prepare:
            logger.info(f"[{tag}][{chain_id}] Preparing directory: {dir_path}")
            
            # Ensure directory exists
            dir_path.mkdir(parents=True, exist_ok=True)
            
            # Clean directory if it exists
            if dir_path.exists():
                for item in dir_path.glob("*"):
                    try:
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)
                    except Exception as e:
                        logger.warning(f"[{tag}][{chain_id}] Could not remove {item}: {e}")
            
        logger.info(f"[{tag}][{chain_id}] Workspace preparation completed successfully")
        return "Workspace prepared successfully"
        
    except Exception as e:
        logger.error(f"[{tag}][{chain_id}] Error in prepare_workspace task: {e}", exc_info=True)
        raise


# Task retry settings
DEFAULT_RETRY_POLICY = {
    'max_retries': 3,
    'interval_start': 0,
    'interval_step': 1,
    'interval_max': 60,
}

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from animal.utils.logging import get_animal_logger
from animal.utils.api_utils import download_imagery as download_imagery_from_usgs
from animal.utils.git_utils import clone_imagery_utils
from animal.utils.pgc_wrapper import calibrate_pair
from animal.utils.imagery_ops import create_single_cog, pansharpen_imagery
from animal.utils.utils import upload_to_azure, collect_geotiffs, move_zip_to_catalog, unzip_to_loading_events, match_pan_ms_pairs, determine_safe_pool_size, clone_imagery_utils, get_task_tag
from animal.utils.api_utils import ee_login, search_imagery, robust_download, TemporaryDataUnavailableError
from animal.utils.memory_utils import (
    log_memory_usage, 
    check_memory_pressure, 
    get_processing_method,
    force_garbage_collection
)

logger = get_animal_logger(__name__)


def ensure_dem_available(
    dem_path: Union[str, Path],
    *,
    chain_id: Optional[str] = None,
    tag: str = "CALIBRATE",
) -> Path:
    """Return a local DEM path, downloading from configured blob storage if needed."""
    dem = Path(dem_path)
    if dem.exists():
        return dem

    logger.warning(f"[{tag}][{chain_id}] DEM missing at {dem}. Attempting blob download fallback.")

    try:
        from animal.utils.config import settings as app_settings
        from animal.utils.azure_utils import get_blob_service_client, download_blob_to_path

        dem_blob_uri = getattr(app_settings, "dem_blob_uri", None)
        if not dem_blob_uri:
            raise FileNotFoundError(
                f"DEM not found: {dem}. No dem_blob_uri configured for fallback download."
            )

        blob_service = get_blob_service_client(
            app_settings.azure_account_name,
            app_settings.azure_account_key,
        )
        downloaded = Path(download_blob_to_path(blob_service, dem_blob_uri, dem))
        if downloaded.exists():
            logger.info(f"[{tag}][{chain_id}] Downloaded DEM fallback to {downloaded}")
            return downloaded

    except Exception as exc:
        raise FileNotFoundError(
            f"DEM not found: {dem}. Fallback download failed."
        ) from exc

    raise FileNotFoundError(f"DEM not found: {dem}")


def enhanced_robust_download(entity_id: str, session, datasetName: str, out_dir, chain_id: str, tag: str, max_retries: int = 8):
    """
    Simplified enhanced download function using user-selected dataset.
    
    Args:
        entity_id: Entity ID to download
        session: Authenticated USGS session
        datasetName: User-selected dataset (e.g., WorldView-3)
        out_dir: Output directory for download
        chain_id: Chain ID for logging
        tag: Tag for logging
        max_retries: Maximum retry attempts for USGS staging delays
        
    Returns:
        Path to downloaded file, or None if failed
    """
    logger.info(f"[{tag}][{chain_id}] Starting download for: {entity_id} using dataset: {datasetName}")
    
    try:
        result = download_imagery_from_usgs(
            entity_id=entity_id,
            session=session,
            datasetName=datasetName,
            out_dir=out_dir,
            max_retries=max_retries
        )
        
        if result:
            logger.info(f"[{tag}][{chain_id}] Download successful: {entity_id} -> {result}")
        else:
            logger.warning(f"[{tag}][{chain_id}] Download failed for: {entity_id}")
            
        return result
        
    except TemporaryDataUnavailableError as e:
        logger.warning(f"[{tag}][{chain_id}] USGS staging delay for {entity_id}: {e}")
        return None
        
    except Exception as e:
        logger.error(f"[{tag}][{chain_id}] Download failed for {entity_id}: {e}")
        return None


# -------------------------------------------------------------------------------
# 1. Login and search for imagery from USGS EarthExplorer
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.login_and_search",
    autoretry_for=(
        requests.exceptions.RequestException,  # Network-related errors
        requests.exceptions.Timeout,          # Timeout errors
        requests.exceptions.ConnectionError,   # Connection failures
        OperationalError                      # Redis/broker errors
    ),
    retry_kwargs={
        'max_retries': 3,
        'interval_start': 1,
        'interval_step': 2,
        'interval_max': 30,
    },
    rate_limit='10/m',
    acks_late=True,
    reject_on_worker_lost=True
)
def login_and_search(
    self,
    aoi_wkt: str,
    start_date: str,
    end_date: str,
    usgs_username: str,
    token: str,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """
    Login to USGS EarthExplorer and search for imagery within an AOI and date range.

    Args:
        aoi_wkt (str): WKT string representing the Area of Interest polygon
        start_date (str): Start date in YYYY-MM-DD format
        end_date (str): End date in YYYY-MM-DD format
        usgs_username (str): USGS EarthExplorer username
        token (str): USGS authentication token
        chain_id (str, optional): Chain ID for tracking task progress

    Returns:
        str: JSON string containing search results and session info for next task

    Raises:
        ValueError: If WKT string is invalid or dates are malformed
        requests.exceptions.RequestException: For network-related errors
        RuntimeError: If USGS login fails or search returns no results
    """
    tag = get_task_tag(self)
    logger.info(f"[{tag}][{chain_id}] Starting login and search task")
    logger.info(f"[{tag}][{chain_id}] Search parameters: dates={start_date} to {end_date}")
    logger.debug(f"[{tag}][{chain_id}] AOI WKT: {aoi_wkt[:100]}...")

    try:
        # Login to USGS
        logger.info(f"[{tag}][{chain_id}] Logging in to USGS EarthExplorer as {usgs_username}")
        session = ee_login(requests.Session(), usgs_username, token)
        logger.info(f"[{tag}][{chain_id}] Login successful")

        # Parse and validate AOI
        try:
            aoi_polygon = load_wkt(aoi_wkt)
            logger.info(f"[{tag}][{chain_id}] AOI parsed successfully: {aoi_polygon.area:.2f} square degrees")
        except Exception as e:
            logger.error(f"[{tag}][{chain_id}] Failed to parse WKT string: {e}")
            raise ValueError(f"Invalid WKT string: {e}")

        # Search for imagery using user-selected dataset (typically WorldView-3 CRSSP)
        logger.info(f"[{tag}][{chain_id}] Searching for imagery...")
        results_gdf = search_imagery(aoi_polygon, "crssp_orderable_w3", start_date, end_date, session)
        
        result_count = len(results_gdf)
        logger.info(f"[{tag}][{chain_id}] Search complete. Found {result_count} results")
        
        if result_count == 0:
            logger.warning(f"[{tag}][{chain_id}] No imagery found for the given parameters")
            logger.warning(f"[{tag}][{chain_id}] This might indicate an issue with the search criteria")
        else:
            # Log some stats about the results
            dates = results_gdf['acquisitionDate'].unique() if 'acquisitionDate' in results_gdf.columns else []
            logger.info(f"[{tag}][{chain_id}] Results span {len(dates)} unique dates")
            logger.debug(f"[{tag}][{chain_id}] Entity IDs: {results_gdf['Entity ID'].tolist()}")

        # Prepare return payload
        results_json = results_gdf.to_json()
        payload = {
            "results": results_json,
            "usgs_username": usgs_username,
            "token": token,
            "project_id": project_id,
        }
        logger.debug(f"[{tag}][{chain_id}] Payload size: {len(str(payload))} bytes")
        
        return json.dumps(payload)

    except Exception as e:
        logger.error(f"[{tag}][{chain_id}] Task failed: {e}", exc_info=True)
        raise


# -------------------------------------------------------------------------------
# 2. Download ZIPs for the selected imagery
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.download",
    autoretry_for=(requests.exceptions.RequestException, IOError, OperationalError),
    retry_kwargs=DEFAULT_RETRY_POLICY,
    retry_backoff=True,
    rate_limit='2/m',  # Limit to 2 downloads per minute to avoid overwhelming USGS
    time_limit=3600    # 1 hour timeout for large downloads
)
def download_imagery(
    self,
    results_payload_json: str,
    img_dir: str,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """
    Downloads imagery ZIP files from USGS EarthExplorer based on search results.
    For MSI/PAN pairs, downloads both the multispectral and panchromatic images.

    Args:
        results_payload_json (str): JSON string containing search results and session info
        img_dir (str): Directory path where downloaded files will be saved
        chain_id (str, optional): Chain ID for tracking task progress

    Returns:
        str: JSON string containing GeoDataFrame with local paths added

    Raises:
        RuntimeError: If any downloads fail
        ValueError: If input JSON is invalid
        IOError: If directory creation/access fails
    """
    logger = get_animal_logger(__name__)
    tag = get_task_tag(self)
    
    logger.info(f"[{tag}][{chain_id}][PROJECT {project_id}] Starting download task")
    start_time = time.time()

    try:
        logger.debug(f"[{tag}][{chain_id}] Parsing input JSON payload")
        payload = json.loads(results_payload_json)
        results_gdf = gpd.read_file(payload["results"])
        dataset_name = payload.get("dataset", "crssp_orderable_w3")
        
        total_images = len(results_gdf)
        logger.info(f"[{tag}][{chain_id}] Found {total_images} image records to download (may include MSI/PAN pairs)")
        logger.info(f"[{tag}][{chain_id}] Download dataset: {dataset_name}")
        
        if total_images == 0:
            raise ValueError("No images to download in the provided GeoDataFrame")
            
        logger.info(f"[{tag}][{chain_id}] Re-establishing USGS session for downloads")
        session = ee_login(requests.Session(), payload["usgs_username"], payload["token"])
        logger.info(f"[{tag}][{chain_id}] USGS session established successfully")

        # Prepare output directory
        img_dir = Path(img_dir)
        if not img_dir.exists():
            logger.info(f"[{tag}][{chain_id}] Creating output directory: {img_dir}")
            img_dir.mkdir(parents=True, exist_ok=True)

        pool_size = determine_safe_pool_size()
        logger.info(f"[{tag}][{chain_id}] Using pool size {pool_size} to download imagery")

        # Download files - now handling MSI/PAN pairs
        local_paths = []
        
        # Check if we have catalog_id information in the search results
        has_catalog_id = 'Catalog ID' in results_gdf.columns
        use_entity_id_priority = payload.get("use_entity_id", False)  # Flag from view
        logger.info(f"[{tag}][{chain_id}] Search results include Catalog ID: {has_catalog_id}")
        logger.info(f"[{tag}][{chain_id}] Use Entity ID priority (like api_utils): {use_entity_id_priority}")
        
        for i, row in results_gdf.iterrows():
            # Check if this is an MSI/PAN pair or single entity
            has_msi_entity = 'MSI_Entity_ID' in row and pd.notna(row['MSI_Entity_ID'])
            has_pan_entity = 'PAN_Entity_ID' in row and pd.notna(row['PAN_Entity_ID'])
            
            if has_msi_entity and has_pan_entity:
                # This is an MSI/PAN pair - download both
                msi_entity_id = row['MSI_Entity_ID']
                pan_entity_id = row['PAN_Entity_ID']
                
                logger.info(f"[{tag}][{chain_id}] Processing MSI/PAN pair ({i+1}/{len(results_gdf)})")
                logger.info(f"[{tag}][{chain_id}] MSI Entity ID: {msi_entity_id}")
                logger.info(f"[{tag}][{chain_id}] PAN Entity ID: {pan_entity_id}")
                
                # Skip non-USGS patterns for both MSI and PAN
                non_usgs_patterns = [
                    r'.*-S1BS-.*',      # Sentinel-1
                    r'.*-S2[AB]-.*',    # Sentinel-2
                    r'^S1[AB]_.*',      # ESA Sentinel-1
                    r'^S2[AB]_.*',      # ESA Sentinel-2
                ]
                
                msi_is_non_usgs = any(re.match(pattern, msi_entity_id) for pattern in non_usgs_patterns)
                pan_is_non_usgs = any(re.match(pattern, pan_entity_id) for pattern in non_usgs_patterns)
                
                if msi_is_non_usgs or pan_is_non_usgs:
                    logger.warning(f"[{tag}][{chain_id}] Skipping non-USGS MSI/PAN pair")
                    logger.warning(f"[{tag}][{chain_id}] MSI: {msi_entity_id} (non-USGS: {msi_is_non_usgs})")
                    logger.warning(f"[{tag}][{chain_id}] PAN: {pan_entity_id} (non-USGS: {pan_is_non_usgs})")
                    local_paths.append("NON_USGS_SKIP")
                    continue
                
                # Download MSI
                logger.info(f"[{tag}][{chain_id}] Downloading MSI component...")
                msi_path = enhanced_robust_download(
                    entity_id=msi_entity_id,
                    session=session,
                    datasetName=dataset_name,
                    out_dir=img_dir,
                    chain_id=chain_id,
                    tag=tag
                )
                
                # Download PAN
                logger.info(f"[{tag}][{chain_id}] Downloading PAN component...")
                pan_path = enhanced_robust_download(
                    entity_id=pan_entity_id,
                    session=session,
                    datasetName=dataset_name,
                    out_dir=img_dir,
                    chain_id=chain_id,
                    tag=tag
                )
                
                # Store both paths (semicolon-separated for downstream processing)
                if msi_path and pan_path:
                    local_paths.append(f"{msi_path};{pan_path}")  # Semicolon-separated
                    logger.info(f"[{tag}][{chain_id}] ✅ MSI/PAN pair download successful")
                else:
                    local_paths.append(None)
                    logger.error(f"[{tag}][{chain_id}] ❌ MSI/PAN pair download failed")
                    logger.error(f"[{tag}][{chain_id}]    MSI result: {msi_path}")
                    logger.error(f"[{tag}][{chain_id}]    PAN result: {pan_path}")
            
            else:
                # Single entity ID (fallback to original behavior)
                entity_id = row.get('Entity ID') or row.get('entity_id')
                catalog_id = row.get('Catalog ID') if has_catalog_id else None
                
                logger.info(f"[{tag}][{chain_id}] Processing single Entity ID ({i+1}/{len(results_gdf)}): {entity_id}")
                logger.info(f"[{tag}][{chain_id}] This Entity ID from search results: {entity_id}")
                if catalog_id:
                    logger.info(f"[{tag}][{chain_id}] Available catalog_id: {catalog_id}")
                
                # STEP 1: Check for non-USGS patterns and skip
                non_usgs_patterns = [
                    r'.*-S1BS-.*',      # Sentinel-1
                    r'.*-S2[AB]-.*',    # Sentinel-2
                    r'^S1[AB]_.*',      # ESA Sentinel-1
                    r'^S2[AB]_.*',      # ESA Sentinel-2
                ]
                
                is_non_usgs = any(re.match(pattern, entity_id) for pattern in non_usgs_patterns)
                if is_non_usgs:
                    logger.warning(f"[{tag}][{chain_id}] Skipping non-USGS entity ID: {entity_id}")
                    local_paths.append("NON_USGS_SKIP")
                    continue
                
                # STEP 2: Skip database lookups - use only search results as requested
                # Note: Explicitly avoiding EarthExplorer table per user requirements
                
                # STEP 3: Use Entity ID directly from search results (like api_utils pattern)
                local_path = None
                
                # SIMPLIFIED: Just use the Entity ID from search results (e.g., WV320230622152241M00)
                # This is the correct value for download requests per user feedback
                logger.info(f"[{tag}][{chain_id}] Using Entity ID directly from search results: {entity_id}")
                
                try:
                    # Use the Entity ID directly - no fallbacks, no alternatives
                    local_path = enhanced_robust_download(
                        entity_id=entity_id,  # Use the search results Entity ID directly
                        session=session,
                        datasetName=dataset_name,
                        out_dir=img_dir,
                        chain_id=chain_id,
                        tag=tag
                    )
                    
                    if local_path:
                        logger.info(f"[{tag}][{chain_id}] ✅ Download successful with Entity ID: {entity_id}")
                    else:
                        logger.error(f"[{tag}][{chain_id}] ❌ Download failed for Entity ID: {entity_id}")
                        
                except Exception as download_error:
                    logger.error(f"[{tag}][{chain_id}] ❌ Download error with Entity ID {entity_id}: {download_error}")
                
                local_paths.append(local_path)
                
                # Log final result
                if local_path:
                    logger.info(f"[{tag}][{chain_id}] ✅ Download completed for Entity ID {entity_id}")
                else:
                    logger.error(f"[{tag}][{chain_id}] ❌ Download failed for Entity ID {entity_id}")
                    logger.error(f"[{tag}][{chain_id}]    Used Entity ID from search results: {entity_id}")
            
        # Check for failed downloads
        failed_downloads = []
        for i, path in enumerate(local_paths):
            if path is None:
                # Get the entity ID(s) for this row
                row = results_gdf.iloc[i]
                if 'MSI_Entity_ID' in row and pd.notna(row['MSI_Entity_ID']):
                    failed_downloads.append(f"MSI/PAN pair: {row['MSI_Entity_ID']}, {row['PAN_Entity_ID']}")
                else:
                    failed_downloads.append(row.get('Entity ID', f"row_{i}"))

        if failed_downloads:
            logger.error(f"[{tag}][{chain_id}] The following downloads failed: {failed_downloads}")
            raise RuntimeError(f"Failed to download {len(failed_downloads)} image(s).")
        # Add local paths to GeoDataFrame and return
        # Convert Path objects to strings to make them JSON serializable
        local_paths_str = []
        for path in local_paths:
            if path is None:
                local_paths_str.append(None)
            elif hasattr(path, '__fspath__') or isinstance(path, (str, bytes)):
                # Convert Path-like objects to strings
                local_paths_str.append(str(path))
            else:
                # Already a string or other serializable type
                local_paths_str.append(path)
        
        results_gdf['local_path'] = local_paths_str
        logger.info(f"[{tag}][{chain_id}] Added 'local_path' column to GeoDataFrame")
        
        # Log completion
        elapsed_time = time.time() - start_time
        logger.info(f"[{tag}][{chain_id}] Download task completed in {elapsed_time:.2f} seconds")
        return results_gdf.to_json()

    except Exception as e:
        logger.error(f"[{tag}][{chain_id}] Task failed: {e}", exc_info=True)
        raise

    return results_gdf.to_json()


# -------------------------------------------------------------------------------
# 3. Organize, unzip, match PAN/MS pairs, and calibrate
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.calibrate"
    # Removed auto-retry - CPU/disk bound task should fail fast
)
def organize_and_calibrate(
    self,
    augmented_gdf_json: str,
    img_dir: str,
    dem_path: str,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
    entity_ids: Optional[list] = None,
    **kwargs
) -> List[Tuple[str, str]]:
    """
    Organizes downloaded imagery, matches PAN/MS pairs, and calibrates them using PGC tools.

    This task performs several steps:
    1. Organizes downloaded files into appropriate directories
    2. Unzips archives into a loading area
    3. Matches panchromatic and multispectral image pairs
    4. Calibrates matched pairs using a DEM

    Args:
        augmented_gdf_json (str): JSON string containing GeoDataFrame with local paths
        img_dir (str): Directory containing downloaded imagery
        dem_path (str): Path to DEM file for calibration
        chain_id (str, optional): Chain ID for tracking task progress
        entity_ids (list, optional): (Ignored) backwards-compatible placeholder for callers that pass
            an 'entity_ids' keyword. Kept for robustness.
        **kwargs: Catch-all to ignore unexpected keyword args from upstream tasks.

    Returns:
        List[Tuple[str, str]]: List of (PAN, MSI) calibrated image path pairs

    Raises:
        FileNotFoundError: If DEM file is missing
        RuntimeError: If calibration fails or produces no outputs
        ValueError: If input JSON is invalid
    """
    logger = get_animal_logger(__name__)
    # Log any backward-compatible fields that callers may pass (harmless)
    if entity_ids is not None or kwargs:
        logger.info(f"[{get_task_tag(self)}][{chain_id}]: Received extra args entity_ids={entity_ids}, extra_keys={list(kwargs.keys())}")
    # Directly load the augmented GeoDataFrame from the previous task's JSON output
    gdf = gpd.read_file(augmented_gdf_json)
    logger.info(f"[{chain_id}][PROJECT {project_id}]: Received GeoDataFrame with {len(gdf)} records for calibration.")

    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Received input type: {type(gdf)}")

    img_dir = Path(img_dir)
    dem = ensure_dem_available(
        dem_path,
        chain_id=chain_id,
        tag=get_task_tag(self),
    )

    # Organize only the zips relevant to this task
    affected_catalogs = set(gdf['Catalog ID'].unique()) if 'Catalog ID' in gdf.columns else set()
    move_zip_to_catalog(img_dir, gdf)
    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Unzipping downloaded archives (scoped) for catalogs: {sorted(list(affected_catalogs))}")
    unzip_to_loading_events(img_dir, limit_catalogs=affected_catalogs, fail_fast=False, lock=True)
    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Unzip done")

    # Match pairs
    tifs = collect_geotiffs(img_dir)
    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Preparing to find PAN/MSI pairs for: {tifs}")
    # match_pan_ms_pairs returns: (pairs_dict, unmatched_pan_list, unmatched_ms_list)
    pairs, unmatched_pan, unmatched_ms = match_pan_ms_pairs([str(t) for t in tifs])
    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Matched {len(pairs)} pairs.")
    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Found {len(unmatched_pan)} unmatched panchromatic images.")
    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Found {len(unmatched_ms)} unmatched multispectral images.")

    # Ensure PGC Utils is available
    base_dir = Path(__file__).resolve().parent.parent
    external_dir = base_dir / "external" / "imagery_utils"
    clone_imagery_utils(external_dir)

    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Will calibrate {len(pairs)} pairs using DEM {dem}")

    # ---- Serial calibration for clear errors ----
    calibrated_paths = []
    total = len(pairs)
    for idx, (pan, msi) in enumerate(pairs.items(), 1):
        try:
            logger.info(f"[{get_task_tag(self)}][{chain_id}]: Calibrating pair {idx}/{total} "
                        f"PAN={pan} MSI={msi}")
            out = calibrate_pair((pan, msi), dem=dem)
            calibrated_paths.append(out)
            logger.info(f"[{get_task_tag(self)}][{chain_id}]: Calibrated pair {idx}/{total}")
        except Exception as e:
            logger.error(f"[{get_task_tag(self)}][{chain_id}]: Calibration failed for "
                         f"PAN={pan} MSI={msi}: {e}", exc_info=True)
            raise

    if not calibrated_paths or any(c is None for c in calibrated_paths):
        raise RuntimeError("Calibration produced no outputs / had failures")

    normalized = []
    for pair in calibrated_paths:
        pan, msi = pair
        normalized.append((str(pan), str(msi)))

    logger.info(f"[{get_task_tag(self)}][{chain_id}]: Returning {len(normalized)} calibrated pairs")

    logger.debug(f"[{get_task_tag(self)}][{chain_id}]: Sample return[0]: {normalized[0] if normalized else 'EMPTY'}")
    return normalized

# -------------------------------------------------------------------------------
# 4. Pansharpen
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.pansharpen"
    # Removed auto-retry - CPU/disk bound task should fail fast
)
def run_pansharpen(
    self,
    calibrated_files: List[Tuple[str, str]],
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> List[str]:
    """
    Runs GDAL pansharpening on calibrated image pairs.

    This task:
    1. Validates input file pairs
    2. Ensures correct PAN/MSI order
    3. Performs pansharpening using GDAL
    4. Returns paths to pansharpened outputs

    Args:
        calibrated_files (List[Tuple[str, str]]): List of (PAN, MSI) file path pairs
        chain_id (str, optional): Chain ID for tracking task progress

    Returns:
        List[str]: Paths to pansharpened output images

    Raises:
        ValueError: If input format is invalid
        RuntimeError: If pansharpening fails
        FileNotFoundError: If input files don't exist
    """
    """
    Runs GDAL pansharpening on a list of (PAN, MSI) image path tuples.

    Each tuple must contain one 'P1BS' (PAN) file and one 'M1BS' (MSI) file.
    If the tuple is reversed, it will be automatically corrected.

    Returns:
        List[str]: Paths to the pansharpened output images.
    """
    logger = get_animal_logger(__name__)

    logger.info(f"[{get_task_tag(self)}][{chain_id}][PROJECT {project_id}]: Received input: {calibrated_files}")

    if not isinstance(calibrated_files, (list, tuple)):
        logger.error(f"[{get_task_tag(self)}][{chain_id}]: calibrated_files is not a list or tuple. Got: {type(calibrated_files)}")
        raise ValueError("calibrated_files must be a list of (PAN, MSI) tuples.")

    pansharpened = []

    for idx, pair in enumerate(calibrated_files):
        # Validate structure
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Invalid input (not a 2-tuple): {pair}")
            continue

        pan_path, msi_path = pair

        # Validate types
        if not isinstance(pan_path, (str, os.PathLike)) or not isinstance(msi_path, (str, os.PathLike)):
            logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Invalid input types: ({type(pan_path)}, {type(msi_path)}) for {pair}")
            continue

        # Convert to string for checks and downstream use
        pan_path = str(pan_path)
        msi_path = str(msi_path)

        # Auto-flip if paths are reversed
        if 'M1BS' in os.path.basename(pan_path) and 'P1BS' in os.path.basename(msi_path):
            logger.warning(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Auto-correcting flipped pair: ({pan_path}, {msi_path})")
            pan_path, msi_path = msi_path, pan_path

        # Final validation
        if 'P1BS' not in os.path.basename(pan_path):
            logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] PAN file must contain 'P1BS': {pan_path}")
            continue
        if 'M1BS' not in os.path.basename(msi_path):
            logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] MSI file must contain 'M1BS': {msi_path}")
            continue

        # Run pansharpening with memory optimization
        try:
            # Log memory status before processing
            log_memory_usage(f"{get_task_tag(self)}", chain_id, "before_pansharpen")
            
            # Check file sizes to determine processing method
            pan_size = Path(pan_path).stat().st_size
            msi_size = Path(msi_path).stat().st_size
            total_size_gb = (pan_size + msi_size) / (1024**3)
            
            # Assume 8GB container (can be made configurable)
            container_memory_gb = 8.0
            processing_method = get_processing_method(total_size_gb, container_memory_gb)
            
            logger.info(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Input files: "
                       f"PAN {pan_size/1024/1024:.1f}MB, MSI {msi_size/1024/1024:.1f}MB, "
                       f"total {total_size_gb:.2f}GB, method: {processing_method}")
            
            if processing_method == 'tiled':
                # For very large images, use tiled processing
                logger.info(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] "
                           f"Using tiled processing for large image ({total_size_gb:.1f}GB input, "
                           f"expected {total_size_gb * 2.5:.1f}GB output)")
                
                # Generate output path
                base_name = os.path.basename(pan_path).replace('_P1BS', '').replace('P1BS', '').replace('.tif', '')
                output_dir = os.path.join(os.path.dirname(pan_path), "..", "pansharpened")
                os.makedirs(output_dir, exist_ok=True)
                tiled_output_path = os.path.join(output_dir, f"{base_name}_tiled_pansharpened.tif")
                
                # Calculate memory per tile (use conservative 1/8 of container memory)
                max_memory_per_tile = int(container_memory_gb * 1024 / 8)
                
                try:
                    from animal.utils.memory_utils import tiled_pansharpen_large
                    
                    output = tiled_pansharpen_large(
                        pan_path=pan_path,
                        msi_path=msi_path,
                        output_path=tiled_output_path,
                        max_memory_mb=max_memory_per_tile,
                        task_name=get_task_tag(self),
                        chain_id=chain_id
                    )
                    
                except ImportError as e:
                    logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] "
                               f"Tiled processing not available: {e}")
                    logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] "
                               f"Image too large for current memory constraints. "
                               f"Consider using larger container or wait for tiled processing implementation.")
                    continue
                except Exception as e:
                    logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] "
                               f"Tiled processing failed: {e}")
                    continue
                
            elif processing_method == 'memory_constrained' or total_size_gb > 1.0:
                # Use memory-constrained approach for moderately large files
                
                # Calculate memory for memory-constrained processing (use conservative 1/4 of container memory)
                max_memory_mb = int(container_memory_gb * 1024 / 4)
                
                # Set up memory-constrained GDAL environment
                original_gdal_env = {}
                gdal_settings = {
                    'GDAL_CACHEMAX': str(max_memory_mb),
                    'GDAL_SWATH_SIZE': str(max_memory_mb // 4),
                    'GDAL_DISABLE_READDIR_ON_OPEN': 'TRUE',
                    'GDAL_MAX_DATASET_POOL_SIZE': '100',
                    'VSI_CACHE': 'TRUE',
                    'VSI_CACHE_SIZE': str(max_memory_mb * 1024 * 1024 // 8),
                    'GDAL_NUM_THREADS': '2',
                    'PROJ_LIB': '/opt/conda/envs/gaia/share/proj',
                    'GDAL_DATA': '/opt/conda/envs/gaia/share/gdal'
                }
                
                try:
                    # Backup original environment and set memory constraints
                    for key, value in gdal_settings.items():
                        original_gdal_env[key] = os.environ.get(key)
                        os.environ[key] = value
                    
                    logger.info(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Using memory-constrained "
                               f"pansharpening with {max_memory_mb}MB GDAL cache")
                    
                    # Force garbage collection before processing
                    force_garbage_collection(get_task_tag(self), chain_id)
                    
                    output = pansharpen_imagery((pan_path, msi_path))
                    
                finally:
                    # Restore original environment variables
                    for key, original_value in original_gdal_env.items():
                        if original_value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = original_value
            else:
                # Use standard approach for smaller files
                logger.info(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Using standard pansharpening")
                output = pansharpen_imagery((pan_path, msi_path))
            
            if processing_method == 'memory_constrained' or total_size_gb > 2.0:
                # Use memory-constrained approach for large files or small containers
                
                # Calculate memory limit (use 1/4 of container memory for safety)
                max_memory_mb = int(container_memory_gb * 1024 / 4)
                
                # Set up GDAL memory constraints
                original_gdal_env = {}
                gdal_settings = {
                    'GDAL_CACHEMAX': str(max_memory_mb),
                    'GDAL_SWATH_SIZE': str(max_memory_mb // 4),
                    'GDAL_DISABLE_READDIR_ON_OPEN': 'TRUE',
                    'GDAL_MAX_DATASET_POOL_SIZE': '100',
                    'VSI_CACHE': 'TRUE',
                    'VSI_CACHE_SIZE': str(max_memory_mb * 1024 * 1024 // 8),
                    'GDAL_NUM_THREADS': '2',
                    'PROJ_LIB': '/opt/conda/envs/gaia/share/proj',
                    'GDAL_DATA': '/opt/conda/envs/gaia/share/gdal'
                }
                
                # Backup and set environment variables
                for key, value in gdal_settings.items():
                    original_gdal_env[key] = os.environ.get(key)
                    os.environ[key] = value
                
                logger.info(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Using memory-constrained "
                           f"pansharpening with {max_memory_mb}MB GDAL cache")
                
                try:
                    # Use standard pansharpening with memory constraints
                    output = pansharpen_imagery((pan_path, msi_path))
                finally:
                    # Restore environment variables
                    for key, original_value in original_gdal_env.items():
                        if original_value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = original_value
            else:
                # Use standard approach for smaller files
                logger.info(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Using standard pansharpening")
                output = pansharpen_imagery((pan_path, msi_path))
            
            pansharpened.append(str(output))
            
            # Log memory after processing
            log_memory_usage(f"{get_task_tag(self)}", chain_id, "after_pansharpen")
            
            # Check if we need emergency cleanup
            if check_memory_pressure(get_task_tag(self), chain_id, threshold_percent=80):
                logger.warning(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] High memory pressure detected")
            
        except Exception as e:
            logger.error(f"[{get_task_tag(self)}][{chain_id}]: [#{idx}] Failed for ({pan_path}, {msi_path}): {e}", exc_info=True)
            raise

    return pansharpened


# -------------------------------------------------------------------------------
# 5. Create Cloud Optimized GeoTIFFs
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.create_cogs"
    # Removed auto-retry - CPU/disk bound task should fail fast
)
def run_cog_creation_task(
    self,
    pansharpened_files: List[str],
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """
    Converts pansharpened GeoTIFFs to Cloud Optimized GeoTIFFs (COGs).

    This task:
    1. Validates input files
    2. Creates COGs with internal tiling and overviews
    3. Organizes outputs in a dedicated directory
    4. Verifies COG validity

    Args:
        pansharpened_files (List[str]): Paths to pansharpened GeoTIFFs
        chain_id (str, optional): Chain ID for tracking task progress

    Returns:
        str: Path to directory containing generated COGs

    Raises:
        ValueError: If input is not a list of file paths
        RuntimeError: If COG creation fails
        FileNotFoundError: If input files don't exist
    """
    logger = get_animal_logger(__name__)
    tag = get_task_tag(self)

    if not isinstance(pansharpened_files, (list, tuple)):
        raise ValueError("Input must be a list of file paths")

    cogs = []
    failed_inputs = []
    for file in pansharpened_files:
        try:
            # Avoid nested multiprocessing inside Celery worker children.
            cog_path = create_single_cog(file)
            if cog_path:
                cogs.append(cog_path)
            else:
                failed_inputs.append(file)
                logger.warning(f"[{tag}][{chain_id}] COG creation returned no output for {file}")
        except Exception as e:
            logger.error(f"[{tag}][{chain_id}] Failed to create COG for {file}: {e}", exc_info=True)
            raise

    if not cogs:
        raise RuntimeError(
            f"COG creation failed to produce any output files. "
            f"Inputs attempted: {failed_inputs or list(pansharpened_files)}"
        )

    if failed_inputs:
        logger.warning(
            f"[{tag}][{chain_id}] COG creation produced partial output: "
            f"{len(cogs)} succeeded, {len(failed_inputs)} failed"
        )

    # All COGs are in the same directory, so we can get the path from the first one.
    cog_output_dir = os.path.dirname(cogs[0])
    logger.info(f"[{tag}][{chain_id}][PROJECT {project_id}] All COGs saved in {cog_output_dir}. Passing directory to next task.")
    
    # Return the path to the DIRECTORY, not the list of files
    return str(cog_output_dir)


# -------------------------------------------------------------------------------
# 6. Upload to Azure
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.upload_azure",
    autoretry_for=(IOError, ConnectionError),
    max_retries=3,
    retry_backoff=True
)
def upload_to_azure_task(
    self,
    cog_directory_path: str,
    account_name: str,
    account_key: str,
    container_name: str,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """
    Uploads Cloud Optimized GeoTIFFs to Azure Blob Storage.

    This task:
    1. Validates input directory
    2. Authenticates with Azure
    3. Uploads all COGs to specified container
    4. Verifies successful upload

    Args:
        cog_directory_path (str): Path to directory containing COGs
        account_name (str): Azure storage account name
        account_key (str): Azure storage account key
        container_name (str): Azure container name
        chain_id (str, optional): Chain ID for tracking task progress

    Returns:
        str: Path to the uploaded directory (for cleanup task)

    Raises:
        NotADirectoryError: If input path is not a directory
        RuntimeError: If upload fails
        ValueError: If Azure credentials are invalid
    """
    logger = get_animal_logger(__name__)
    tag = get_task_tag(self)
    
    local_dir = Path(cog_directory_path)

    if not local_dir.is_dir():
        logger.error(f"[{tag}][{chain_id}] Input path is not a directory: {local_dir}")
        raise NotADirectoryError(f"Input path from previous task is not a directory: {local_dir}")

    logger.info(f"[{tag}][{chain_id}][PROJECT {project_id}] upload_to_azure_task started for {cog_directory_path}")

    try:
        # List files to upload
        files_to_upload = [p.name for p in local_dir.iterdir() if p.is_file()]
        logger.debug(f"[{tag}][{chain_id}] Files to upload from {cog_directory_path}: {files_to_upload}")

        # Call upload_to_azure without deleting files
        logger.info(f"[{tag}][{chain_id}] Starting Azure upload from {cog_directory_path}")
        upload_to_azure(
            local_dir=local_dir,
            account_name=account_name,
            account_key=account_key,
            container_name=container_name
        )

        logger.info(f"[{tag}][{chain_id}] upload_to_azure_task completed, returning {cog_directory_path}")
        return cog_directory_path

    except Exception as e:
        logger.error(f"[{tag}][{chain_id}] upload_to_azure_task failed: {e}", exc_info=True)
        raise

# -------------------------------------------------------------------------------
# 7. Load points from staged GeoJSON
# -------------------------------------------------------------------------------
@shared_task(
    bind=True,
    name="gaia.imagery.load_points",
)
def load_points_from_staged_geojson(
    self,
    _prev_result: Any,
    points_upload_id: Optional[int] = None,
    points_catalog_id: Optional[str] = None,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """Load POIs from staged GeoJSON upload as final imagery-chain step."""
    logger = get_animal_logger(__name__)
    tag = get_task_tag(self)

    if not points_upload_id or not points_catalog_id:
        logger.info(
            f"[{tag}][{chain_id}] No staged GeoJSON payload provided. Skipping final point-load step."
        )
        return "No staged GeoJSON point load requested."

    from animal.models import StagedImageryGeoJSONUpload
    from animal.utils.poi_loader import load_pois_from_geojson_upload

    staged_upload = StagedImageryGeoJSONUpload.objects.filter(
        id=points_upload_id,
        project_id=project_id,
    ).first()
    if not staged_upload:
        raise ValueError(
            f"Staged GeoJSON upload id={points_upload_id} was not found for project {project_id}."
        )

    if staged_upload.consumed:
        logger.info(
            f"[{tag}][{chain_id}] Staged GeoJSON upload {points_upload_id} was already consumed."
        )
        return f"Staged GeoJSON upload {points_upload_id} was already consumed."

    file_name = staged_upload.source_filename or f"staged_{staged_upload.id}.geojson"
    file_bytes = staged_upload.geojson_payload.encode('utf-8')
    uploaded_file = SimpleUploadedFile(
        file_name,
        file_bytes,
        content_type="application/geo+json",
    )

    result = load_pois_from_geojson_upload(
        uploaded_file=uploaded_file,
        project_identifier=str(project_id),
        id_type="catalog",
        target_id=str(points_catalog_id),
        dry_run=False,
    )

    staged_upload.consumed = True
    staged_upload.consumed_at = timezone.now()
    staged_upload.save(update_fields=["consumed", "consumed_at"])

    logger.info(
        f"[{tag}][{chain_id}] Final point-load completed: loaded={result.get('loaded', 0)}, "
        f"skipped={result.get('skipped', 0)}, duplicates={result.get('duplicates', 0)}"
    )
    return (
        f"Loaded {result.get('loaded', 0)} POIs from staged GeoJSON "
        f"for catalog {points_catalog_id}."
    )


# -------------------------------------------------------------------------------
# 8. Clean up local
# -------------------------------------------------------------------------------
def _chmod_then_retry(func: Callable, path: str, exc_info: Tuple) -> None:
    """
    Helper function to handle permission issues during file deletion.

    Args:
        func (Callable): The original function that failed (usually os.remove or similar)
        path (str): Path to the file that couldn't be deleted
        exc_info (Tuple): Exception information from the failed attempt

    Raises:
        Exception: If deletion fails even after changing permissions
    """
    logger = get_animal_logger(__name__)
    try:
        logger.debug(f"Attempting to chmod and retry deletion for {path} (current permissions: {oct(os.stat(path).st_mode)})")
        os.chmod(path, stat.S_IWUSR | stat.S_IREAD | stat.S_IWGRP | stat.S_IWOTH)
        func(path)
        logger.debug(f"Successfully deleted {path} after chmod")
    except Exception as e:
        logger.error(f"Failed to delete {path} after chmod retry: {e}", exc_info=True)
        raise

@shared_task(
    bind=True,
    name="gaia.imagery.cleanup",
    autoretry_for=(IOError, OSError),
    max_retries=2,
    retry_backoff=True
)
def cleanup_local_data(
    self,
    _prev_result: Any,
    base_dir_to_clean: str,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
) -> str:
    """
    Safely removes all processed files from local storage.

    This task:
    1. Validates the cleanup path for safety
    2. Recursively removes all contents
    3. Handles permission issues
    4. Verifies complete cleanup

    Args:
        _prev_result (Any): Result from previous task (not used)
        base_dir_to_clean (str): Path to directory to clean
        chain_id (str, optional): Chain ID for tracking task progress

    Returns:
        str: Confirmation message

    Raises:
        RuntimeError: If cleanup fails or path is unsafe
        NotADirectoryError: If path doesn't exist or isn't a directory
    """
    logger = get_animal_logger(__name__)
    tag = get_task_tag(self)

    logger.info(f"[{tag}][{chain_id}][PROJECT {project_id}] cleanup_local_data task started")

    # Resolve the path to clean
    base_dir = Path(base_dir_to_clean).resolve()
    safe_root = Path('/app/gis')

    # Safety check
    if not str(base_dir).startswith(str(safe_root)) or base_dir == safe_root:
        logger.critical(f"[{tag}][{chain_id}] Refusing to delete outside/equal to {safe_root}: {base_dir}")
        raise RuntimeError("Unsafe cleanup path")

    logger.info(f"[{tag}][{chain_id}] Cleaning up all contents in {base_dir} (mimicking rm -rf {base_dir}/*)")

    try:
        if base_dir.exists() and base_dir.is_dir():
            # Log directory details
            contents = list(base_dir.iterdir())
            logger.debug(f"[{tag}][{chain_id}] Directory permissions: {oct(os.stat(base_dir).st_mode)}")
            logger.debug(f"[{tag}][{chain_id}] Contents to delete: {[str(p) for p in contents] if contents else 'none'}")

            # Delete all contents
            for item_path in base_dir.iterdir():
                logger.debug(f"[{tag}][{chain_id}] Deleting item: {item_path} (is_dir: {item_path.is_dir()}, permissions: {oct(os.stat(item_path).st_mode)})")
                try:
                    if item_path.is_dir():
                        shutil.rmtree(item_path, onerror=_chmod_then_retry)
                    else:
                        item_path.unlink()
                    logger.debug(f"[{tag}][{chain_id}] Successfully deleted {item_path}")
                except Exception as e:
                    logger.error(f"[{tag}][{chain_id}] Failed to delete {item_path}: {e}", exc_info=True)
                    raise

            # Verify emptiness
            remaining_items = list(base_dir.iterdir())
            if remaining_items:
                logger.error(f"[{tag}][{chain_id}] Cleanup incomplete, remaining items: {[str(p) for p in remaining_items]}")
                raise RuntimeError(f"Failed to clean all contents of {base_dir}")
            else:
                logger.info(f"[{tag}][{chain_id}] Successfully cleaned all contents of {base_dir}")

        else:
            logger.warning(f"[{tag}][{chain_id}] Directory {base_dir} does not exist or is not a directory")

        logger.info(f"[{tag}][{chain_id}] cleanup_local_data task completed")
        return f"[{chain_id}] Local data cleanup complete"

    except Exception as e:
        logger.error(f"[{tag}][{chain_id}] Cleanup failed: {e}", exc_info=True)
        raise