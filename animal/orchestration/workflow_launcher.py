# -------------------------------------------------------------------------------
# ----- workflow_launcher.py ----------------------------------------------------
# -------------------------------------------------------------------------------
#
#    authors:  John Wall
#
#    purpose:  Orchestrates and launches the full GAIA Celery imagery processing
#              pipeline using Celery chains. This is the single orchestration
#              module — all entry points delegate here.
#
#              Two launch functions:
#
#              launch_pipeline()
#                  Full pipeline from search. Called by process_imagery management
#                  command or Django shell.
#                  9 steps: prepare → search → download → calibrate → pansharpen
#                           → cog → upload → cleanup → load_points
#
#              launch_pipeline_from_payload()
#                  Pipeline from a pre-built download payload. Called by
#                  collection_views.py after the user selects entities in the UI.
#                  8 steps: prepare → download → calibrate → pansharpen → cog
#                           → upload → cleanup → load_points
#
#              Canonical operational controls (single source of truth):
#                  queue:           imagery
#                  retry_policy:    3 retries, 0/0.2/0.5 backoff
#                  soft_time_limit: 3600 (1 hour)
#                  time_limit:      7200 (2 hours)
#                  expires:         7200 (2 hours)
#                  compression:     gzip
#                  priority:        5
#                  link_error:      cleanup_local_data
#
#    tickets:  GAIFAGP-449, GAIFAGP-490, GAIFAGP-491, GAIFAGP-492, GAIFAGP-493
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - This module is the canonical pipeline orchestrator. All entry points
#        (CLI, UI, shell) delegate chain construction here.
#      - Operational controls defined in this file (queue, timeouts, retries,
#        compression, link_error) are authoritative. Other modules must not
#        define their own.
#      - Task imports come from animal.tasks.imagery_tasks. The chain contract
#        is: each task returns a value consumed by the next task's first
#        positional arg (via Celery result passing), except where .si() is used
#        to break the chain injection.
#      - launch_pipeline() owns the full 9-step search-to-final-load path.
#      - launch_pipeline_from_payload() owns the 8-step payload-to-final-load path.
#      - collection_views.py constructs the results_payload_json format that
#        launch_pipeline_from_payload() consumes. That format is owned by the
#        views; this module accepts it as-is.
#
# -------------------------------------------------------------------------------

import sys
import uuid
import json
import logging
from pathlib import Path
from typing import Optional

from celery import chain
from celery.result import AsyncResult

logger = logging.getLogger("gaia")

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from animal.tasks.imagery_tasks import (
    prepare_workspace,
    login_and_search,
    download_imagery,
    organize_and_calibrate,
    run_pansharpen,
    run_cog_creation_task,
    upload_to_azure_task,
    cleanup_local_data,
    load_points_from_staged_geojson,
)

# Regression guard for BUG-1 (GAIFAGP-490): run_cog_creation_task must be
# a Celery task, not the utility function from imagery_ops.
assert hasattr(run_cog_creation_task, "delay"), (
    "run_cog_creation_task is not a Celery task. "
    "Check imports — the utility function run_cog_creation was likely imported instead."
)

assert hasattr(load_points_from_staged_geojson, "delay"), (
    "load_points_from_staged_geojson is not a Celery task. "
    "Check imports — the final staged point-load step must be a task."
)


def launch_pipeline(
    aoi_geojson_str: str,
    start_date: str,
    end_date: str,
    usgs_username: str,
    token: str,
    azure_credentials: dict,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
    points_upload_id: Optional[int] = None,
    points_catalog_id: Optional[str] = None,
) -> AsyncResult:
    """
    Launches the complete GAIA Celery pipeline as a task chain.
    This is the primary entry point for the imagery processing pipeline
    when a full USGS search is needed.

    Args:
        aoi_geojson_str (str): Serialized GeoJSON defining area of interest
        start_date (str): Start date for imagery search (YYYY-MM-DD)
        end_date (str): End date for imagery search (YYYY-MM-DD)
        usgs_username (str): USGS EarthExplorer username
        token (str): USGS API token (valid for this session)
        azure_credentials (dict): Dictionary with keys:
            - account_name
            - account_key
            - container_name
        chain_id (str, optional): Unique identifier for this chain. If not provided,
                                a UUID will be generated.
        project_id (int, optional): Project ID to scope this pipeline run.

    Returns:
        celery.result.AsyncResult: Result object for monitoring the chain

    Assumptions:
        - USGS token is valid for the duration of the chain (sessions expire).
        - aoi_geojson_str is serialized GeoJSON, not WKT (parameter naming is
          a pre-existing mismatch with the task's aoi_wkt param name).
        - azure_credentials dict has account_name, account_key, container_name.
        - settings.img_dir and settings.dem_file are populated in config.
    """
    if not chain_id:
        chain_id = str(uuid.uuid4())

    logger.info(
        "Launching full pipeline (9-step, from search)",
        extra={
            "chain_id": chain_id,
            "project_id": project_id,
            "requested_by_username": requested_by_username,
            "project_display": project_display,
            "entry_point": "launch_pipeline",
            "steps": 9,
        },
    )

    # Set static references from config
    from animal.utils.config import settings
    img_dir = str(settings.img_dir)
    dem_path = str(settings.dem_file)

    # Build the complete processing chain
    workflow_chain = chain(
        # Step 1: Prepare clean workspace
        prepare_workspace.si(
            base_dir_to_prepare=str(Path(img_dir).parent),
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:prepare"),

        # Step 2: Search for imagery
        # .si() — all args are explicit; prepare_workspace return is not consumed
        login_and_search.si(
            aoi_wkt=aoi_geojson_str,
            start_date=start_date,
            end_date=end_date,
            usgs_username=usgs_username,
            token=token,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:search"),

        # Step 3: Download imagery
        download_imagery.s(
            img_dir=img_dir,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:download"),

        # Step 4: Organize and calibrate
        organize_and_calibrate.s(
            img_dir,
            dem_path,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:calibrate"),

        # Step 5: Run pansharpening
        run_pansharpen.s(
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:pansharpen"),

        # Step 6: Create Cloud Optimized GeoTIFFs
        run_cog_creation_task.s(
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:cog"),

        # Step 7: Upload to Azure
        upload_to_azure_task.s(
            account_name=azure_credentials["account_name"],
            account_key=azure_credentials["account_key"],
            container_name=azure_credentials["container_name"],
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:upload"),

        # Step 8: Clean up
        cleanup_local_data.s(
            base_dir_to_clean=str(Path(img_dir).parent),
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:cleanup"),

        # Step 9: Load staged GeoJSON points (no-op when no staged upload is provided)
        load_points_from_staged_geojson.s(
            points_upload_id=points_upload_id,
            points_catalog_id=points_catalog_id,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:load_points"),
    )

    # Launch the chain with optimized settings
    result = workflow_chain.apply_async(
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 0.2,
            "interval_max": 0.5,
        },
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 3,
            "interval_start": 1,
            "interval_step": 1,
            "interval_max": 3,
        },
        time_limit=7200,
        soft_time_limit=3600,
        expires=7200,
        queue="imagery",
        priority=5,
        compression="gzip",
        link_error=[
            cleanup_local_data.s(
                base_dir_to_clean=str(Path(img_dir).parent),
                chain_id=chain_id,
                project_id=project_id,
                requested_by_username=requested_by_username,
                project_display=project_display,
            )
        ],
    )

    return result


def launch_pipeline_from_payload(
    results_payload_json: str,
    img_dir: str,
    azure_credentials: dict,
    dem_path: Optional[str] = None,
    chain_id: Optional[str] = None,
    project_id: Optional[int] = None,
    requested_by_username: Optional[str] = None,
    project_display: Optional[str] = None,
    points_upload_id: Optional[int] = None,
    points_catalog_id: Optional[str] = None,
) -> AsyncResult:
    """
    Launches the GAIA Celery pipeline from a pre-built download payload,
    skipping login_and_search. This is the entry point for the web UI,
    where the user has already searched and selected entities.

    The payload format is the same JSON string that collection_views.py
    already produces — no transformation needed.

    Call pattern for collection_views.py integration::

        from animal.orchestration.workflow_launcher import launch_pipeline_from_payload

        result = launch_pipeline_from_payload(
            results_payload_json=filtered_payload_json,   # from build_download_payload()
            img_dir=str(settings.img_dir),
            azure_credentials={
                "account_name": settings.azure_account_name,
                "account_key": settings.azure_account_key,
                "container_name": settings.azure_container_name,
            },
            dem_path=str(settings.dem_file),
        )
        chain_id = result.id  # or parse from result

    8-step chain: prepare → download → calibrate → pansharpen → cog → upload → cleanup → load_points

    Args:
        results_payload_json (str): JSON string containing the download payload.
            Expected keys: results (path to GeoJSON), usgs_username, token,
            and any other fields needed by download_imagery.
        img_dir (str): Directory for downloaded imagery.
        azure_credentials (dict): Dictionary with keys:
            - account_name
            - account_key
            - container_name
        dem_path (str, optional): Path to DEM file for calibration. If not
            provided, uses settings.dem_file.
        chain_id (str, optional): Unique identifier for this chain. If not
            provided, a UUID will be generated.
        project_id (int, optional): Project ID to scope this pipeline run.

    Returns:
        celery.result.AsyncResult: Result object for monitoring the chain.

    Assumptions:
        - results_payload_json contains a valid GeoJSON path under the "results"
          key, plus usgs_username and token for re-authentication during download.
        - The payload format matches what collection_views.py produces. This
          function does not validate or transform the payload.
        - azure_credentials dict has account_name, account_key, container_name.
        - If dem_path is not provided, settings.dem_file must be populated.
    """
    if not chain_id:
        chain_id = str(uuid.uuid4())

    logger.info(
        "Launching pipeline from payload (8-step, search skipped)",
        extra={
            "chain_id": chain_id,
            "project_id": project_id,
            "requested_by_username": requested_by_username,
            "project_display": project_display,
            "entry_point": "launch_pipeline_from_payload",
            "steps": 8,
        },
    )

    # Keep project scope in payload metadata for downstream tasks that inspect payload.
    if project_id is not None:
        payload_obj = json.loads(results_payload_json)
        payload_obj["project_id"] = project_id
        payload_obj["requested_by_username"] = requested_by_username
        payload_obj["project_display"] = project_display
        results_payload_json = json.dumps(payload_obj)

    if not dem_path:
        from animal.utils.config import settings
        dem_path = str(settings.dem_file)

    # Build the 8-step chain (no login_and_search — payload already has results)
    workflow_chain = chain(
        # Step 1: Prepare clean workspace
        prepare_workspace.si(
            base_dir_to_prepare=str(Path(img_dir).parent),
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:prepare"),

        # Step 2: Download imagery (payload passed as first positional arg)
        download_imagery.si(
            results_payload_json,
            img_dir=img_dir,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:download"),

        # Step 3: Organize and calibrate
        organize_and_calibrate.s(
            img_dir,
            dem_path,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:calibrate"),

        # Step 4: Run pansharpening
        run_pansharpen.s(
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:pansharpen"),

        # Step 5: Create Cloud Optimized GeoTIFFs
        run_cog_creation_task.s(
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:cog"),

        # Step 6: Upload to Azure
        upload_to_azure_task.s(
            account_name=azure_credentials["account_name"],
            account_key=azure_credentials["account_key"],
            container_name=azure_credentials["container_name"],
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:upload"),

        # Step 7: Clean up
        cleanup_local_data.s(
            base_dir_to_clean=str(Path(img_dir).parent),
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:cleanup"),

        # Step 8: Load staged GeoJSON points (no-op when no staged upload is provided)
        load_points_from_staged_geojson.s(
            points_upload_id=points_upload_id,
            points_catalog_id=points_catalog_id,
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
        ).set(task_id=f"{chain_id}:load_points"),
    )

    # Launch with same operational controls as launch_pipeline()
    result = workflow_chain.apply_async(
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 0.2,
            "interval_max": 0.5,
        },
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 3,
            "interval_start": 1,
            "interval_step": 1,
            "interval_max": 3,
        },
        time_limit=7200,
        soft_time_limit=3600,
        expires=7200,
        queue="imagery",
        priority=5,
        compression="gzip",
        link_error=[
            cleanup_local_data.s(
                base_dir_to_clean=str(Path(img_dir).parent),
                chain_id=chain_id,
                project_id=project_id,
                requested_by_username=requested_by_username,
                project_display=project_display,
            )
        ],
    )

    return result