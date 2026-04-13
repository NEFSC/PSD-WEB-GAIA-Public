# ------------------------------------------------------------------------------
# ----- db_utils.py ------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Database utilities for GAIA fishnet operations.
#
#    tickets:  GAIFAGP-XXX (db_utils decomposition)
#
#    STATUS: PHASE 1 COMPLETE - Dead code quarantined to db_utils_legacy.py
#            PHASE 2 PENDING - Fishnet import stabilization
#
#    SCOPE:
#      IN:  Fishnet GeoDataFrame import to Django models
#      OUT: Everything else - see db_repair.py for schema/introspection/repair
#
#    KNOWN ISSUES (Phase 2 will address):
#      - Signature mismatch: run_async_imports expects project_id but
#        batch_import_fishnets calls it without one
#      - Async pattern (sync_to_async for ORM writes) may reduce reliability
#        without improving throughput
#      - IntegrityError swallowing hides duplicate detection
#      - No explicit batch size control
#
#    PHASE 2 REMEDIATION PLAN:
#      - Verify which version is actually running in production
#      - Convert to synchronous bulk_create with controlled batch size
#      - Explicit conflict strategy (ignore_conflicts or update_conflicts)
#      - Remove async wrappers if not providing measured benefit
#
#    QUARANTINED CODE:
#      The following functions have been moved to db_utils_legacy.py:
#        - validate_updates, update_gegd, update_mgp, update_ee
#        - database_activity, insert_ee, insert_mgp, insert_gegd
#        - insert_pk, select_data, update_aoi, get_aoi
#      
#      These used raw sqlite3.connect() outside Django's connection management.
#      They were only called by broken scripts (load_ee2table.py, 
#      evaluate_repositories.py). Do not re-import them.
#
#    CANONICAL DATABASE OPERATIONS:
#      For schema introspection, comparison, and repair: db_repair.py
#      For POI operations: poi.py
#      For AOI operations: aoi.py
#      For ETL operations: etl.py
#
# ------------------------------------------------------------------------------

import asyncio
import logging
from time import time
from typing import Optional

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
import geopandas as gpd

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Fishnet Import Functions
# ------------------------------------------------------------------------------

def import_fishnet(gdf, model, project_id: int) -> dict:
    """
    Import fishnet geometries from a GeoDataFrame into the provided Django model.
    
    Args:
        gdf: GeoDataFrame with 'vendor_id' and 'geometry' columns
        model: Django model class (e.g., Fishnet)
        project_id: Project ID to assign to created records
    
    Returns:
        dict with 'created' and 'skipped' counts
    
    Note:
        Currently swallows IntegrityError for duplicates. Phase 2 will convert
        to bulk_create with explicit ignore_conflicts=True for transparency.
    """
    created = 0
    skipped = 0
    
    for _, row in gdf.iterrows():
        try:
            model.objects.create(
                vendor_id=row["vendor_id"],
                cell=row["geometry"].wkt,
                project_id=project_id,
            )
            created += 1
        except IntegrityError:
            skipped += 1
            continue
    
    return {'created': created, 'skipped': skipped}


async def import_fishnet_async(path_to_geojson: str, model, project_id: int) -> dict:
    """
    Async wrapper for fishnet import from GeoJSON file.
    
    Args:
        path_to_geojson: Path to GeoJSON file
        model: Django model class
        project_id: Project ID to assign
    
    Returns:
        dict with 'created' and 'skipped' counts
    
    Note:
        Phase 2 may remove async pattern if not providing measured benefit.
        sync_to_async for ORM writes often reduces reliability without
        improving throughput.
    """
    gdf = await sync_to_async(gpd.read_file, thread_sensitive=True)(path_to_geojson)
    result = await sync_to_async(import_fishnet, thread_sensitive=True)(gdf, model, project_id)
    return result


async def run_async_imports(paths: list, model, project_id: int) -> list:
    """
    Run multiple async fishnet imports sequentially.
    
    Args:
        paths: List of GeoJSON file paths
        model: Django model class
        project_id: Project ID to assign
    
    Returns:
        List of result dicts from each import
    
    Note:
        KNOWN ISSUE: Original signature didn't include project_id, but
        import_fishnet_async requires it. This has been corrected.
    """
    results = []
    for path in paths:
        result = await import_fishnet_async(path, model, project_id)
        results.append({'path': path, **result})
    return results


def batch_import_fishnets(
    geojson_paths: list,
    model,
    project_id: int = 2,
    logger_instance=None
) -> dict:
    """
    Batch import multiple fishnet GeoJSON files with transaction wrapping.
    
    Args:
        geojson_paths: List of paths to GeoJSON files
        model: Django model class (e.g., Fishnet)
        project_id: Project ID to assign (default: 2)
        logger_instance: Optional logger for timing output
    
    Returns:
        dict with 'duration_seconds', 'files_processed', and 'null_assignments_updated'
    
    Note:
        Phase 2 will convert to synchronous bulk_create for reliability.
        The async pattern here may not be providing throughput benefit.
    """
    start = time()
    log = logger_instance or logger
    
    log.info("Starting batch fishnet import")
    log.info(f"Files to process: {len(geojson_paths)}")
    log.info(f"Project ID: {project_id}")
    
    with transaction.atomic():
        # Run async imports
        # NOTE: project_id now correctly passed through
        asyncio.run(run_async_imports(geojson_paths, model, project_id))
    
    # Update any records that ended up with null project_id
    # (safety net for edge cases)
    null_updated = model.objects.filter(project_id__isnull=True).update(project_id=project_id)
    
    duration = round(time() - start, 2)
    
    log.info(f"Batch import complete in {duration} seconds")
    if null_updated > 0:
        log.info(f"Updated {null_updated} records with null project_id")
    
    return {
        'duration_seconds': duration,
        'files_processed': len(geojson_paths),
        'null_assignments_updated': null_updated
    }


# ------------------------------------------------------------------------------
# Phase 2 Target: Synchronous Bulk Import
# ------------------------------------------------------------------------------
#
# The following is the target implementation for Phase 2. It replaces the
# async pattern with synchronous bulk_create for reliability.
#
# def bulk_import_fishnet(
#     gdf: gpd.GeoDataFrame,
#     model,
#     project_id: int,
#     batch_size: int = 500,
#     ignore_conflicts: bool = True
# ) -> dict:
#     """
#     Bulk import fishnet geometries using Django's bulk_create.
#     
#     Args:
#         gdf: GeoDataFrame with 'vendor_id' and 'geometry' columns
#         model: Django model class
#         project_id: Project ID to assign
#         batch_size: Records per batch (default: 500)
#         ignore_conflicts: If True, skip duplicates silently (default: True)
#     
#     Returns:
#         dict with 'created' count (note: with ignore_conflicts, this is
#         the number of records attempted, not necessarily inserted)
#     """
#     records = [
#         model(
#             vendor_id=row["vendor_id"],
#             cell=row["geometry"].wkt,
#             project_id=project_id,
#         )
#         for _, row in gdf.iterrows()
#     ]
#     
#     created = model.objects.bulk_create(
#         records,
#         batch_size=batch_size,
#         ignore_conflicts=ignore_conflicts
#     )
#     
#     return {'attempted': len(records), 'created': len(created)}
#
# ------------------------------------------------------------------------------