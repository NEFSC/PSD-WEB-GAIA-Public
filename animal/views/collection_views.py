# Basic stack
import os
import json
import re
import html
import requests
import uuid
from datetime import datetime
from shapely.geometry import box, Polygon
from shapely.wkt import loads as load_wkt
import pandas as pd
import geopandas as gpd
import django
from django.contrib import messages
from django.db import IntegrityError
from django.utils.safestring import mark_safe
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings

from animal.utils.api_utils import (
    search_imagery,
    search_imagery_by_identifiers,
    ee_login,
)
from animal.utils.config import settings as app_settings
from animal.utils.imagery_request_tracking import create_or_mark_processing
from animal.utils.poi_loader import decode_geojson_payload
from animal.utils.poi_utils import (
    parse_vendor_id_from_geojson_filename,
    parse_vendor_id_from_geojson_payload,
)
from animal.orchestration.workflow_launcher import launch_pipeline_from_payload
from ..models import (
    AreaOfInterest,
    EarthExplorer,
    GEOINTDiscovery,
    MaxarGeospatialPlatform,
    Project,
    StagedImageryGeoJSONUpload,
)
from ..forms import APIQueryForm

from ..query import build_ee_query_payload, query_mgp
from ..utils.logging import get_animal_logger

logger = get_animal_logger(__name__)


def _build_project_display(project_id):
    """Build a stable display label for pipeline monitoring metadata."""
    if not project_id:
        return "Unknown Project"
    try:
        project = Project.objects.only('id', 'label').get(id=project_id)
        return f"{project.label} (#{project.id})"
    except Project.DoesNotExist:
        return f"Project #{project_id}"


def _resolve_aoi_name(aoi_id):
    """Resolve an AOI identifier into a human-friendly name."""
    if aoi_id is None:
        return ""
    try:
        resolved_id = int(float(aoi_id))
    except (TypeError, ValueError):
        return str(aoi_id)

    aoi = AreaOfInterest.objects.filter(id=resolved_id).only('name').first()
    return aoi.name if aoi else str(aoi_id)


def get_vendor_base(vendor_id):
    """Return a stable vendor base used to pair MSI/PAN components.

    Example: 'XYZ-M1BS-ABC' or 'XYZ-P1BS-ABC' -> 'XYZ-ABC'
    """
    try:
        m = re.match(r'^(.*)-(M1BS|P1BS)-(.*)$', str(vendor_id))
        if m:
            return f"{m.group(1)}-{m.group(3)}"
    except Exception:
        pass
    return None


def normalize_sensor_label(sensor_value):
    """Map compact sensor codes to user-friendly names for display."""
    if sensor_value is None:
        return 'Unknown'

    raw = str(sensor_value).strip()
    if not raw:
        return 'Unknown'

    key = raw.upper().replace('_', '').replace(' ', '').replace('-', '')
    mapping = {
        'WV2': 'Vantor WorldView-02',
        'WV02': 'Vantor WorldView-02',
        'WORLDVIEW2': 'Vantor WorldView-02',
        'WORLDVIEW02': 'Vantor WorldView-02',
        'WV3': 'Vantor WorldView-03',
        'WV03': 'Vantor WorldView-03',
        'WORLDVIEW3': 'Vantor WorldView-03',
        'WORLDVIEW03': 'Vantor WorldView-03',
        'GE1': 'Vantor GeoEye-1',
        'GEOEYE': 'Vantor GeoEye-1',
        'GEOEYE1': 'Vantor GeoEye-1',
    }
    return mapping.get(key, raw)


def get_sensor_from_row(row):
    """Pick the best available sensor/platform value from a USGS result row."""
    candidate_fields = [
        'Sensor',
        'sensor',
        'Satellite',
        'satellite',
        'Platform',
        'platform',
    ]

    for field in candidate_fields:
        value = row.get(field, None)
        if value is None or pd.isna(value):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def parse_vendor_id_date(vendor_id):
    """Extract YYMONDDhhmmss token from Vendor ID and return datetime."""
    if vendor_id is None:
        return None

    token_match = re.search(r'(\d{2})([A-Z]{3})(\d{2})\d{6}', str(vendor_id).upper())
    if not token_match:
        return None

    month_lookup = {
        'JAN': 1,
        'FEB': 2,
        'MAR': 3,
        'APR': 4,
        'MAY': 5,
        'JUN': 6,
        'JUL': 7,
        'AUG': 8,
        'SEP': 9,
        'OCT': 10,
        'NOV': 11,
        'DEC': 12,
    }

    yy = int(token_match.group(1))
    mon = token_match.group(2)
    dd = int(token_match.group(3))
    month = month_lookup.get(mon)
    if month is None:
        return None

    # Earth observation IDs in this feed are modern-era; pin to 2000-based years.
    year = 2000 + yy
    try:
        return datetime(year, month, dd)
    except ValueError:
        return None


def normalize_acquisition_date(value, vendor_id=None):
    """Normalize acquisition date to 'Month DD, YYYY' with vendor ID fallback."""
    if value is None or pd.isna(value):
        vendor_dt = parse_vendor_id_date(vendor_id)
        return vendor_dt.strftime('%B %d, %Y') if vendor_dt else 'Unknown'

    try:
        parsed = pd.to_datetime(value, errors='coerce')
        if pd.isna(parsed):
            vendor_dt = parse_vendor_id_date(vendor_id)
            if vendor_dt:
                return vendor_dt.strftime('%B %d, %Y')
            return str(value)[:10]
        return parsed.strftime('%B %d, %Y')
    except Exception:
        vendor_dt = parse_vendor_id_date(vendor_id)
        if vendor_dt:
            return vendor_dt.strftime('%B %d, %Y')
        return str(value)[:10]


def summarize_parent_sensor(sensor_values):
    cleaned = [s for s in sensor_values if s and s != 'Unknown']
    if not cleaned:
        return 'Unknown'

    unique_values = sorted(set(cleaned))
    if len(unique_values) == 1:
        return unique_values[0]
    return 'Mixed'


def _extract_selected_ids_from_post(post_data):
    """Extract selected checkbox IDs from row_data_* inputs with fallback."""
    selected_ids = []
    for key in post_data:
        if key.startswith('row_data_'):
            parts = key.split('_')
            if len(parts) >= 3:
                selected_id = '_'.join(parts[2:-1])
                if selected_id and selected_id not in selected_ids:
                    selected_ids.append(selected_id)

    if not selected_ids:
        for selected_id in post_data.getlist('selected'):
            if selected_id and selected_id not in selected_ids:
                selected_ids.append(selected_id)

    return selected_ids


def _load_search_results_gdf(original_results):
    """Return a GeoDataFrame from session-cached search results payload."""
    if not original_results:
        return None
    if isinstance(original_results, str):
        return gpd.read_file(original_results, driver='GeoJSON')
    return original_results

def login_and_search_sync(aoi_wkt: str, start_date: str, end_date: str):
    """
    Synchronous version of the login_and_search function for use in Django views.
    Uses credentials from Django settings secrets.
    
    Args:
        aoi_wkt (str): WKT string representing the Area of Interest polygon
        start_date (str): Start date in YYYY-MM-DD format  
        end_date (str): End date in YYYY-MM-DD format
        
    Returns:
        geopandas.GeoDataFrame: Search results as a GeoDataFrame
        
    Raises:
        ValueError: If WKT string is invalid or dates are malformed
        requests.exceptions.RequestException: For network-related errors
        RuntimeError: If USGS login fails or search returns no results
    """
    logger.info(f"Starting synchronous login and search")
    logger.info(f"Search parameters: dates={start_date} to {end_date}")
    logger.debug(f"AOI WKT: {aoi_wkt[:100]}...")

    try:
        # Get credentials from Django settings
        usgs_username = settings.USGS_USERNAME
        usgs_token = settings.USGS_TOKEN  # This is an API token, not a password
        
        # Validate credentials are available
        if not usgs_username or not usgs_token:
            raise ValueError(f"USGS credentials not properly configured in settings. Username: {'✓' if usgs_username else '✗'}, Token: {'✓' if usgs_token else '✗'}")
        
        # Login to USGS using token-based authentication
        session = requests.Session()
        session = ee_login(session, usgs_username, usgs_token)  # Use token-based login from api_utils
        logger.info(f"Login successful")
        
        # Debug: Check if auth token is properly set
        auth_token = session.headers.get('X-Auth-Token')
        logger.info(f"Auth token status: {'✓ Present' if auth_token else '✗ Missing'}")

        # Parse and validate AOI
        try:
            aoi_polygon = load_wkt(aoi_wkt)
            logger.info(f"AOI parsed successfully: {aoi_polygon.area:.2f} square degrees")
        except Exception as e:
            logger.error(f"Failed to parse WKT string: {e}")
            raise ValueError(f"Invalid WKT string: {e}")

        # Search for imagery
        logger.info(f"Searching for imagery...")
        results_gdf = search_imagery(aoi_polygon, "crssp_orderable_w3", start_date, end_date, session)
        
        result_count = len(results_gdf)
        logger.info(f"Search complete. Found {result_count} results")
        
        if result_count == 0:
            logger.warning(f"No imagery found for the given parameters")
            return gpd.GeoDataFrame()
        else:
            # Log some stats about the results
            dates = results_gdf['acquisitionDate'].unique() if 'acquisitionDate' in results_gdf.columns else []
            logger.info(f"Results span {len(dates)} unique dates")
            logger.debug(f"Entity IDs: {results_gdf['Entity ID'].tolist()}")

        return results_gdf

    except Exception as e:
        logger.error(f"Synchronous search failed: {e}", exc_info=True)
        raise


def login_and_search_by_ids_sync(
    identifier_values,
    start_date: str,
    end_date: str,
):
    """Synchronous identifier-mode imagery search for collection page."""
    logger.info("Starting synchronous ID-based imagery search")
    logger.info(
        "ID search parameters: %s identifiers, dates=%s to %s",
        len(identifier_values),
        start_date,
        end_date,
    )

    try:
        usgs_username = settings.USGS_USERNAME
        usgs_token = settings.USGS_TOKEN
        if not usgs_username or not usgs_token:
            raise ValueError(
                "USGS credentials not properly configured in settings. "
                f"Username: {'✓' if usgs_username else '✗'}, "
                f"Token: {'✓' if usgs_token else '✗'}"
            )

        session = requests.Session()
        session = ee_login(session, usgs_username, usgs_token)

        results_gdf = search_imagery_by_identifiers(
            identifier_values=identifier_values,
            dataset="crssp_orderable_w3",
            start=start_date,
            end=end_date,
            session=session,
        )

        logger.info(
            "ID search complete. Found %s results",
            len(results_gdf),
        )
        return results_gdf

    except Exception as e:
        logger.error(
            "Synchronous ID search failed: %s",
            e,
            exc_info=True,
        )
        raise

def convert_date_or_none(date_str):
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None

def add_file_sizes_to_results(gdf, form_data):
    """
    Fetch file size information for imagery results from USGS API.
    Adds a 'File Size' column with human-readable file sizes.
    
    Args:
        gdf (GeoDataFrame): Results from USGS imagery search
        form_data (dict): Form data containing credentials and other settings
        
    Returns:
        GeoDataFrame: Updated GeoDataFrame with file size information
    """
    from animal.utils.api_utils import ee_login
    
    if gdf.empty:
        return gdf
        
    logger.info(f"Fetching file size information for {len(gdf)} imagery results...")
    
    # Get credentials from Django settings
    usgs_username = settings.USGS_USERNAME
    usgs_token = settings.USGS_TOKEN
    
    try:
        # Login to USGS
        session = requests.Session()
        session = ee_login(session, usgs_username, usgs_token)
        
        # Add file size column - initialize with None
        gdf['File Size'] = None
        gdf['File Size MB'] = None  # For sorting purposes
        
        def format_file_size(size_bytes):
            """Convert bytes to human-readable format"""
            if not size_bytes or size_bytes == 0:
                return "Unknown"
            
            # Convert to appropriate unit
            if size_bytes < 1024:
                return f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.1f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.1f} MB"
            else:
                return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        
        # Collect all entity IDs we need to process
        entity_ids_to_fetch = []
        entity_to_row_mapping = {}
        
        for index, row in gdf.iterrows():
            row_entity_ids = []
            if 'MSI_Entity_ID' in row and pd.notna(row['MSI_Entity_ID']):
                row_entity_ids.append(str(row['MSI_Entity_ID']))
            if 'PAN_Entity_ID' in row and pd.notna(row['PAN_Entity_ID']):
                row_entity_ids.append(str(row['PAN_Entity_ID']))
            if not row_entity_ids and 'Entity ID' in row:
                row_entity_ids.append(str(row['Entity ID']))
            
            entity_ids_to_fetch.extend(row_entity_ids)
            entity_to_row_mapping[index] = row_entity_ids
        
        # Batch fetch file sizes - USGS API supports multiple entity IDs in one request
        file_size_cache = {}
        
        # Process in batches of 20 to avoid overwhelming the API
        batch_size = 20
        for i in range(0, len(entity_ids_to_fetch), batch_size):
            batch = entity_ids_to_fetch[i:i + batch_size]
            
            try:
                payload = {
                    "datasetName": "crssp_orderable_w3",
                    "entityIds": batch
                }
                
                response = session.post(
                    "https://m2m.cr.usgs.gov/api/api/json/stable/download-options",
                    json=payload
                )
                
                if response.status_code == 200:
                    response_json = response.json()
                    if response_json.get('data'):
                        for option in response_json['data']:
                            entity_id = option.get('entityId')
                            file_size = option.get('filesize', 0)
                            if entity_id and file_size:
                                file_size_cache[entity_id] = int(file_size)
                else:
                    logger.warning(f"Failed to fetch file sizes for batch: {response.status_code}")
                    
            except Exception as e:
                logger.warning(f"Error fetching file sizes for batch: {e}")
                continue
        
        # Now populate the GeoDataFrame with the cached file sizes
        for index, entity_ids in entity_to_row_mapping.items():
            total_size_bytes = 0
            file_sizes = []
            
            for entity_id in entity_ids:
                if entity_id in file_size_cache:
                    file_size = file_size_cache[entity_id]
                    total_size_bytes += file_size
                    file_sizes.append(format_file_size(file_size))
            
            # Update the GeoDataFrame with file size info
            if total_size_bytes > 0:
                if len(file_sizes) > 1:
                    # For MSI/PAN pairs, show combined size
                    gdf.at[index, 'File Size'] = f"{format_file_size(total_size_bytes)} (Combined)"
                else:
                    gdf.at[index, 'File Size'] = format_file_size(total_size_bytes)
                
                # Store MB value for sorting
                gdf.at[index, 'File Size MB'] = total_size_bytes / (1024 * 1024)
            else:
                gdf.at[index, 'File Size'] = "Unknown"
                gdf.at[index, 'File Size MB'] = 0
    
        logger.info(f"File size information fetched for {len(gdf)} results")
        
    except Exception as e:
        logger.error(f"Failed to fetch file size information: {e}")
        # Add default values if file size fetching fails
        gdf['File Size'] = "Unknown"
        gdf['File Size MB'] = 0
    
    return gdf

def collection_page(request, project_id):
    """ A page for consolidated review of satellite imagery collected over
            loaded areas of interest and registration of these images into
            the SpatiaLite database for processing.

        Currently supports USGS EarthExplorer, NGA GEOINT Discovery, and
            Maxar Geospatial Platform satellite imagery repositories.

        DEPENDENCIES:
            - convert_date_or_none
        
        TODO: Split each data repository into a function (GAIFAGP-55).
    """
    project = get_object_or_404(Project, id=project_id) if project_id else None
    results = None
    message = None
    geometry = None
    results_geojson = None
    aoi_bounds = None
    selection_limit = 10

    if request.method == 'POST':
        form = APIQueryForm(request.POST, request.FILES)

        # Print API selected to terminal for troubleshooting
        print("\nREQUEST API: ", request.POST.get('select_api'), '\n\n')

        # Post back to SpatiaLite database if there were selections
        if 'selected' in request.POST:
            # Create a dictionary from the POST
            row_data = {}
            for key in request.POST:
                if key.startswith('row_data_'):
                    row_id = key.split('_')[2]
                    if row_id not in row_data:
                        row_data[row_id] = []
                    row_data[row_id].append(request.POST[key])

            # Print it to terminal
            print("\nROW DATA: ", row_data, '\n\n')

            if request.POST.get('select_api') == 'ee':
                # Get the original search results from session to access real catalog_id values
                original_results = request.session.get('search_results_gdf', None)
                default_aoi_name = _resolve_aoi_name(request.POST.get('aoi'))
                posted_search_mode = (request.POST.get('search_mode') or '').strip().lower()
                staged_upload_id_raw = (request.POST.get('staged_geojson_upload_id') or '').strip()

                # GeoJSON ID-mode: enforce exactly one catalog selection and bind staged upload.
                if posted_search_mode == 'id' and not staged_upload_id_raw:
                    messages.error(
                        request,
                        "A staged GeoJSON upload is required for ID-mode Add Imagery. Re-run View Results.",
                    )
                    return redirect('collection_page', project_id=project_id)

                if posted_search_mode == 'id' and staged_upload_id_raw:
                    selected_catalog_ids = _extract_selected_ids_from_post(request.POST)
                    if len(selected_catalog_ids) != 1:
                        messages.error(
                            request,
                            "Select exactly one catalog ID before adding imagery from a GeoJSON search.",
                        )
                        return redirect('collection_page', project_id=project_id)

                    try:
                        staged_upload_id = int(staged_upload_id_raw)
                    except (TypeError, ValueError):
                        messages.error(request, "The staged GeoJSON reference is invalid. Upload and search again.")
                        return redirect('collection_page', project_id=project_id)

                    staged_upload = StagedImageryGeoJSONUpload.objects.filter(
                        id=staged_upload_id,
                        project_id=project_id,
                        consumed=False,
                    ).first()
                    if not staged_upload:
                        messages.error(
                            request,
                            "The staged GeoJSON upload is no longer available. Upload and search again.",
                        )
                        return redirect('collection_page', project_id=project_id)

                    selected_catalog_id = str(selected_catalog_ids[0])
                    search_gdf = _load_search_results_gdf(original_results)
                    if search_gdf is None:
                        messages.error(
                            request,
                            "Search results were not found in session. Re-run View Results and try again.",
                        )
                        return redirect('collection_page', project_id=project_id)

                    if 'Catalog ID' not in search_gdf.columns:
                        messages.error(
                            request,
                            "Search results did not include catalog IDs. Re-run View Results and try again.",
                        )
                        return redirect('collection_page', project_id=project_id)

                    catalog_matches = search_gdf[
                        search_gdf.get('Catalog ID').astype(str) == selected_catalog_id
                    ]
                    if catalog_matches.empty:
                        messages.error(
                            request,
                            f"Selected catalog ID {selected_catalog_id} was not found in current search results.",
                        )
                        return redirect('collection_page', project_id=project_id)

                    selected_row = catalog_matches.iloc[0]
                    if (
                        'MSI_Entity_ID' in catalog_matches.columns
                        and 'PAN_Entity_ID' in catalog_matches.columns
                    ):
                        paired_rows = catalog_matches[
                            catalog_matches.get('MSI_Entity_ID').notna()
                            & catalog_matches.get('PAN_Entity_ID').notna()
                        ]
                        if not paired_rows.empty:
                            selected_row = paired_rows.iloc[0]

                    primary_entity_id = selected_row.get('MSI_Entity_ID') or selected_row.get('Entity ID')
                    if not primary_entity_id:
                        messages.error(
                            request,
                            f"Unable to resolve a primary Entity ID for catalog {selected_catalog_id}.",
                        )
                        return redirect('collection_page', project_id=project_id)

                    request_group_id = str(uuid.uuid4())
                    request_aoi_name = _resolve_aoi_name(selected_row.get('aoi')) or default_aoi_name

                    try:
                        launch_imagery_processing_pipeline_with_search_data(
                            primary_entity_id,
                            project_id,
                            original_results,
                            requested_by_username=request.user.username,
                            project_display=_build_project_display(project_id),
                            request_group_id=request_group_id,
                            aoi_name=request_aoi_name,
                            catalog_ids=selected_catalog_id,
                            points_upload_id=staged_upload.id,
                            points_catalog_id=selected_catalog_id,
                        )
                        messages.success(
                            request,
                            f"Started processing pipeline for catalog {selected_catalog_id}.",
                        )
                        messages.info(
                            request,
                            "Points from the uploaded GeoJSON will be loaded in the final pipeline step.",
                        )
                    except Exception as exc:
                        messages.error(
                            request,
                            (
                                f"Failed to start processing pipeline for catalog {selected_catalog_id}: {exc}"
                            ),
                        )
                    return redirect('collection_page', project_id=project_id)
                
                # Process each selected checkbox - handle both single Entity IDs and MSI/PAN pairs
                selected_identifiers = _extract_selected_ids_from_post(request.POST)
                
                logger.info(f"Selected identifiers from checkboxes: {selected_identifiers}")

                search_gdf = _load_search_results_gdf(original_results)

                expanded_identifiers = []
                for identifier in selected_identifiers:
                    if '+' in identifier or search_gdf is None:
                        expanded_identifiers.append(identifier)
                        continue

                    if 'Catalog ID' not in search_gdf.columns or 'MSI_Entity_ID' not in search_gdf.columns or 'PAN_Entity_ID' not in search_gdf.columns:
                        expanded_identifiers.append(identifier)
                        continue

                    catalog_matches = search_gdf[
                        (search_gdf.get('Catalog ID').astype(str) == str(identifier)) &
                        search_gdf.get('MSI_Entity_ID').notna() &
                        search_gdf.get('PAN_Entity_ID').notna()
                    ]

                    if catalog_matches.empty:
                        expanded_identifiers.append(identifier)
                        continue

                    for _, matched_row in catalog_matches.iterrows():
                        pair_identifier = f"{matched_row['MSI_Entity_ID']}+{matched_row['PAN_Entity_ID']}"
                        if pair_identifier not in expanded_identifiers:
                            expanded_identifiers.append(pair_identifier)

                logger.info(f"Expanded identifiers for processing: {expanded_identifiers}")
                
                for identifier in expanded_identifiers:
                    logger.info(f"Processing identifier: {identifier}")
                    request_group_id = str(uuid.uuid4())
                    request_aoi_name = default_aoi_name
                    request_catalog_ids = ""
                    
                    # Check if this is an MSI/PAN pair (contains '+')
                    if '+' in identifier:
                        # This is an MSI/PAN pair
                        msi_entity_id, pan_entity_id = identifier.split('+', 1)
                        logger.info(f"Processing MSI/PAN pair - MSI: {msi_entity_id}, PAN: {pan_entity_id}")
                        
                        # Find the corresponding row in the original search results
                        real_catalog_id = "UNKNOWN"
                        real_vendor_id = "UNKNOWN"
                        
                        if search_gdf is not None:
                            # Find the row with matching MSI/PAN Entity IDs
                            matching_rows = search_gdf[
                                (search_gdf.get('MSI_Entity_ID') == msi_entity_id) & 
                                (search_gdf.get('PAN_Entity_ID') == pan_entity_id)
                            ]
                            if not matching_rows.empty:
                                real_catalog_id = matching_rows.iloc[0].get('Catalog ID', 'UNKNOWN')
                                real_vendor_id = matching_rows.iloc[0].get('Vendor ID', 'UNKNOWN')
                                request_aoi_name = _resolve_aoi_name(matching_rows.iloc[0].get('aoi')) or request_aoi_name
                                request_catalog_ids = str(real_catalog_id) if real_catalog_id and str(real_catalog_id) != 'UNKNOWN' else ""
                                logger.info(f"Found real catalog_id for MSI/PAN pair: {real_catalog_id}")
                            else:
                                logger.warning(f"MSI/PAN pair {identifier} not found in search results!")
                        
                        # Use the MSI Entity ID as the primary key for the database record
                        primary_entity_id = msi_entity_id
                        
                    else:
                        # This is a single Entity ID
                        primary_entity_id = identifier
                        logger.info(f"Processing single Entity ID: {primary_entity_id}")

                        # Find the corresponding row in the original search results
                        real_catalog_id = "UNKNOWN"
                        real_vendor_id = "UNKNOWN"
                        if search_gdf is not None:
                            # Find the row with matching Entity ID
                            matching_rows = search_gdf[search_gdf['Entity ID'] == primary_entity_id]
                            if not matching_rows.empty:
                                real_catalog_id = matching_rows.iloc[0].get('Catalog ID', 'UNKNOWN')
                                real_vendor_id = matching_rows.iloc[0].get('Vendor ID', 'UNKNOWN')
                                request_aoi_name = _resolve_aoi_name(matching_rows.iloc[0].get('aoi')) or request_aoi_name
                                request_catalog_ids = str(real_catalog_id) if real_catalog_id and str(real_catalog_id) != 'UNKNOWN' else ""
                                logger.info(f"Found real catalog_id for {primary_entity_id}: {real_catalog_id}")
                            else:
                                logger.warning(f"Entity ID {primary_entity_id} not found in search results!")

                            # If we received an MSI, attempt to find a PAN partner and schedule it too
                            try:
                                # Determine if this primary_entity_id looks like an MSI (contains 'M1BS' or ends with 'M')
                                possible_msi = False
                                if 'M1BS' in str(real_vendor_id) or re.search(r'M\d+$', str(primary_entity_id)):
                                    possible_msi = True

                                if possible_msi:
                                    # Search for a PAN companion in the search results using vendor base
                                    vendor_base = get_vendor_base(real_vendor_id)
                                    if vendor_base is not None:
                                        # Find rows sharing the same vendor base and with P1BS
                                        pan_candidates = search_gdf[search_gdf.get('Vendor ID', '').astype(str).str.contains(vendor_base) & search_gdf.get('Vendor ID', '').astype(str).str.contains('P1BS')]
                                        if not pan_candidates.empty:
                                            pan_entity = pan_candidates.iloc[0].get('Entity ID')
                                            logger.info(f"Automatically detected PAN partner for {primary_entity_id}: {pan_entity}. Launching pipeline for PAN as well.")
                                            # Launch pipeline for PAN partner as well (best-effort, wrapped in try)
                                            try:
                                                launch_imagery_processing_pipeline_with_search_data(
                                                    pan_entity,
                                                    project_id,
                                                    original_results,
                                                    requested_by_username=request.user.username,
                                                    project_display=_build_project_display(project_id),
                                                    request_group_id=request_group_id,
                                                    aoi_name=request_aoi_name,
                                                    catalog_ids=request_catalog_ids,
                                                )
                                                messages.info(request, f"Also started processing for PAN partner {pan_entity}.")
                                            except Exception as e:
                                                logger.warning(f"Failed to start PAN pipeline for {pan_entity}: {e}")
                            except Exception as e:
                                logger.debug(f"Error while attempting to auto-detect PAN partner: {e}")
                    
                    # Use primary_entity_id as the lookup field, and put all other fields in defaults
                    obj, created = EarthExplorer.objects.update_or_create(
                        entity_id=primary_entity_id,
                        defaults={
                            'catalog_id': real_catalog_id,  # Use real catalog_id from search results
                            'vendor_id': real_vendor_id,  # Use real vendor_id from search results
                            # Set minimal defaults for other required fields
                            'acquisition_date': None,
                            'vendor': "UNKNOWN",
                            'cloud_cover': 0,
                            'satellite': "UNKNOWN",
                            'sensor': "UNK",
                            'number_of_bands': 0,
                            'map_projection': "UNK",
                            'datum': "UNK",
                            'processing_level': "UNK",
                            'file_format': "UNKNOWN",
                            'license_id': 0,
                            'sun_azimuth': 0.0,
                            'sun_elevation': 0.0,
                            'pixel_size_x': 0.0,
                            'pixel_size_y': 0.0,
                            'license_uplift_update': None,
                            'event': "UNK",
                            'event_date': None,
                            'date_entered': None,
                            'center_latitude_dec': 0.0,
                            'center_longitude_dec': 0.0,
                            'thumbnail': "UNKNOWN",
                            'publish_date': None,
                            'bounds': "POLYGON((0 0, 0 0, 0 0, 0 0, 0 0))",
                            'aoi_id': get_object_or_404(AreaOfInterest, id=1)  # Default AOI
                        }
                    )
                    
                    # Provide appropriate success message
                    action = "registered" if created else "updated"
                    if '+' in identifier:
                        messages.success(request, f"MSI/PAN pair ({msi_entity_id}, {pan_entity_id}) was {action} in the database successfully!")
                        display_id = f"MSI/PAN pair {msi_entity_id}+{pan_entity_id}"
                    else:
                        messages.success(request, f"Image ID {primary_entity_id} was {action} in the database successfully!")
                        display_id = f"image {primary_entity_id}"
                    
                    # Launch imagery processing pipeline using the original search results data
                    try:
                        launch_imagery_processing_pipeline_with_search_data(
                            primary_entity_id,
                            project_id,
                            original_results,
                            requested_by_username=request.user.username,
                            project_display=_build_project_display(project_id),
                            request_group_id=request_group_id,
                            aoi_name=request_aoi_name,
                            catalog_ids=request_catalog_ids,
                        )
                        processing_action = "Started" if created else "Restarted"
                        messages.info(request, f"{processing_action} processing pipeline for {display_id}. Check the monitoring dashboard for progress.")
                        
                        # If this is an MSI/PAN pair, also launch pipeline for the PAN Entity ID
                        if '+' in identifier:
                            try:
                                logger.info(f"Launching additional pipeline for PAN Entity ID: {pan_entity_id}")
                                launch_imagery_processing_pipeline_with_search_data(
                                    pan_entity_id,
                                    project_id,
                                    original_results,
                                    requested_by_username=request.user.username,
                                    project_display=_build_project_display(project_id),
                                    request_group_id=request_group_id,
                                    aoi_name=request_aoi_name,
                                    catalog_ids=request_catalog_ids,
                                )
                                messages.info(request, f"Also {processing_action.lower()} processing pipeline for PAN component {pan_entity_id}.")
                            except Exception as pan_error:
                                logger.warning(f"Failed to start PAN pipeline for {pan_entity_id}: {pan_error}")
                                messages.warning(request, f"MSI pipeline started successfully, but failed to start PAN pipeline for {pan_entity_id}: {str(pan_error)}")
                                
                    except Exception as e:
                        messages.warning(request, f"{display_id.capitalize()} was {action} but failed to start processing pipeline: {str(e)}")

            elif request.POST.get('select_api') == 'gegd':
                for attributes in row_data.values():
                    attributes = [attribute for attribute in attributes if attribute]

                    [print("\nATT", i , ":", attribute) for i, attribute in enumerate(attributes)]
                    try:
                        GEOINTDiscovery.objects.update_or_create(
                            id = attributes[0],
                            legacy_id = attributes[1],
                            factory_order_number = attributes[2],
                            acquisition_date = convert_date_or_none(attributes[3]),
                            source = attributes[4],
                            source_unit = attributes[5],
                            product_type = attributes[6],
                            cloud_cover = attributes[7],
                            off_nadir_angle = attributes[8],
                            sun_elevation = attributes[9],
                            sun_azimuth = attributes[10],
                            ground_sample_distance = attributes[11],
                            data_layer = attributes[12],
                            legacy_description = attributes[13],
                            color_band_order = attributes[14],
                            asset_name = attributes[15],
                            per_pixel_x = attributes[16],
                            per_pixel_y = attributes[17],
                            crs_from_pixels = attributes[18],
                            age_days = attributes[19],
                            ingest_date = convert_date_or_none(attributes[20]),
                            company_name = attributes[21],
                            copyright = attributes[22],
                            niirs = attributes[23],
                            geometry = attributes[24],
                            aoi_id = get_object_or_404(AreaOfInterest, id=int(float(attributes[25])))
                        )
                        messages.success(request, f"Image ID {attributes[0]} was registered to the database successfully!")
                        
                        # Launch imagery processing pipeline for GEGD images
                        try:
                            entity_id = attributes[0]
                            launch_imagery_processing_pipeline(
                                entity_id,
                                project_id,
                                requested_by_username=request.user.username,
                                project_display=_build_project_display(project_id),
                                request_group_id=str(uuid.uuid4()),
                                aoi_name=_resolve_aoi_name(attributes[25] if len(attributes) > 25 else None),
                                catalog_ids="",
                            )
                            messages.info(request, f"Started processing pipeline for image {entity_id}. Check the monitoring dashboard for progress.")
                        except Exception as e:
                            messages.warning(request, f"Image {entity_id} was registered but failed to start processing pipeline: {str(e)}")
                    
                    except IntegrityError:
                        messages.warning(request, f"Image ID {attributes[0]} failed due to unique constraint violation." + 
                                         f" It has not been added to the database, but likely because some version of the" +
                                         f" record is already there. You should validate that is the case through the Django shell.")

            elif request.POST.get('select_api') == 'mgp':
                for attributes in row_data.values():
                    attributes = [attribute for attribute in attributes if attribute]
                    MaxarGeospatialPlatform.objects.update_or_create(
                        id = attributes[0],
                        platform = attributes[1],
                        instruments = attributes[2],
                        gsd = attributes[3],
                        pan_resolution_avg = attributes[4],
                        multi_resolution_avg = attributes[5],
                        datetime = attributes[6],
                        off_nadir = attributes[7],
                        azimuth = attributes[8],
                        sun_azimuth = attributes[9],
                        sun_elevation = attributes[10],
                        bbox = attributes[11],
                        aoi_id = get_object_or_404(AreaOfInterest, id=int(float(attributes[12])))
                    )
                    messages.success(request, f"Image ID {attributes[0]} was registered to the database successfully!")
            else:
                messages.warning(request, "No items were selected!")
        
        elif form.is_valid():
            # Extract form data for validation and processing
            api = form.cleaned_data['api']
            search_mode = form.cleaned_data.get('search_mode', 'aoi')
            aoi = form.cleaned_data['aoi']
            id_tokens = form.cleaned_data.get('id_tokens', [])
            id_geojson_file = form.cleaned_data.get('id_geojson_file')
            start_date = form.cleaned_data['start_date'].strftime('%Y-%m-%d') if form.cleaned_data['start_date'] else None
            end_date = form.cleaned_data['end_date'].strftime('%Y-%m-%d') if form.cleaned_data['end_date'] else None
            sensor_list = form.cleaned_data.get('sensor', [])  # Get list of selected sensors
            vendor = form.cleaned_data.get('vendor', '')
            staged_geojson_upload_id = None
            parsed_vendor_identifier = None

            if not api:
                messages.error(request, "Please select an API.")
                return render(request, 'collection_page.html', {'form': form,
                                                                'results': results,
                                                                'message': message,
                                                                'area_of_interest_geojson': json.dumps(geometry) if geometry else None,
                                                                'results_geojson': results_geojson,
                                                                'aoi_bounds': aoi_bounds})

            # Process selected sensors (example usage)
            # sensor_list will be a list like ['worldview_2', 'worldview_3'] or ['geoeye']
            # You can use this to filter satellite imagery by sensor type
            print(f"Selected sensors: {sensor_list}")  # Debug print - can be removed
            print(f"Selected vendor: {vendor}")  # Debug print - can be removed

            sensor_choice_map = dict(APIQueryForm.SENSOR_CHOICES)
            selected_sensor_labels = [sensor_choice_map.get(sensor_key, sensor_key) for sensor_key in sensor_list]
            selected_sensor_display = ', '.join(selected_sensor_labels) if selected_sensor_labels else 'Unknown'

            selection_limit = 10 if search_mode == 'aoi' else 1

            # Only AOI mode renders AOI geometry/bounds.
            if search_mode == 'aoi' and aoi:
                geometry = json.loads(aoi.geometry.geojson)
                aoi_bounds = aoi.geometry.buffer(0.5).extent
            else:
                geometry = None
                aoi_bounds = None

            catalog_rows = []

            try:
                if search_mode == 'aoi':
                    request.session.pop('staged_geojson_upload_id', None)
                    request.session.pop('staged_geojson_vendor_id', None)
                    logger.info(
                        "Making AOI-based API call to USGS EarthExplorer for AOI: %s",
                        aoi.name,
                    )
                    aoi_wkt = aoi.geometry.wkt
                    gdf = login_and_search_sync(
                        aoi_wkt=aoi_wkt,
                        start_date=start_date,
                        end_date=end_date,
                    )
                else:
                    if not id_geojson_file:
                        raise ValueError("No GeoJSON upload was provided for ID mode.")

                    id_geojson_file.seek(0)
                    raw_geojson = id_geojson_file.read()
                    payload = decode_geojson_payload(raw_geojson)

                    try:
                        parsed_vendor_identifier = parse_vendor_id_from_geojson_payload(payload)
                    except ValueError as payload_parse_error:
                        try:
                            parsed_vendor_identifier = parse_vendor_id_from_geojson_filename(
                                id_geojson_file.name or ''
                            )
                        except ValueError:
                            form.add_error(
                                'id_geojson_file',
                                (
                                    "Unable to parse vendor identifier from uploaded GeoJSON name field "
                                    "or filename."
                                ),
                            )
                            messages.error(request, str(payload_parse_error))
                            return render(request, 'collection_page.html', {
                                'form': form,
                                'results': results,
                                'message': message,
                                'area_of_interest_geojson': json.dumps(geometry) if geometry else None,
                                'results_geojson': results_geojson,
                                'aoi_bounds': aoi_bounds,
                                'selection_limit': selection_limit,
                                'project': project,
                            })

                    staged_upload = StagedImageryGeoJSONUpload.objects.create(
                        project=project,
                        uploaded_by_user=request.user if request.user.is_authenticated else None,
                        source_filename=id_geojson_file.name or 'uploaded.geojson',
                        parsed_vendor_id=parsed_vendor_identifier,
                        geojson_payload=json.dumps(payload),
                    )
                    staged_geojson_upload_id = staged_upload.id
                    request.session['staged_geojson_upload_id'] = staged_geojson_upload_id
                    request.session['staged_geojson_vendor_id'] = parsed_vendor_identifier

                    id_tokens = [parsed_vendor_identifier]
                    logger.info(
                        "Making ID-based API call to USGS EarthExplorer for %s identifiers",
                        len(id_tokens),
                    )
                    gdf = login_and_search_by_ids_sync(
                        identifier_values=id_tokens,
                        start_date=start_date,
                        end_date=end_date,
                    )
                
                if len(gdf) == 0:
                    message = "No imagery found for the given search parameters."
                    results_geojson = None
                else:
                    # Keep only records that have complete MSI/PAN pairs.
                    vendor_bases = {}
                    for _, row in gdf.iterrows():
                        vendor_id = row.get('Vendor ID', row.get('Entity ID', None))
                        base = get_vendor_base(vendor_id)
                        if not base:
                            continue

                        if base not in vendor_bases:
                            vendor_bases[base] = {'MSI': [], 'PAN': []}

                        vendor_id_text = str(vendor_id)
                        if '-M1BS-' in vendor_id_text:
                            vendor_bases[base]['MSI'].append(row)
                        elif '-P1BS-' in vendor_id_text:
                            vendor_bases[base]['PAN'].append(row)

                    processed_rows = []
                    for _, components in vendor_bases.items():
                        msi_rows = components['MSI']
                        pan_rows = components['PAN']
                        if not msi_rows or not pan_rows:
                            continue

                        for msi_row in msi_rows:
                            msi_catalog = str(msi_row.get('Catalog ID', ''))
                            pan_match = next(
                                (p for p in pan_rows if str(p.get('Catalog ID', '')) == msi_catalog),
                                pan_rows[0],
                            )
                            combined_row = msi_row.copy()
                            combined_row['MSI_Entity_ID'] = msi_row['Entity ID']
                            combined_row['PAN_Entity_ID'] = pan_match['Entity ID']
                            combined_row['Display Date'] = normalize_acquisition_date(
                                msi_row.get('acquisitionDate'),
                                msi_row.get('Vendor ID', None),
                            )
                            combined_row['Display Sensor'] = selected_sensor_display
                            processed_rows.append(combined_row)
                    
                    # Recreate GeoDataFrame from processed rows, preserving geometry and CRS
                    if processed_rows:
                        # Extract geometry column name from original GeoDataFrame
                        geom_col = gdf.geometry.name if hasattr(gdf, 'geometry') else 'geometry'
                        
                        # Create new GeoDataFrame with same CRS and geometry column
                        gdf = gpd.GeoDataFrame(
                            processed_rows, 
                            geometry=geom_col,
                            crs=gdf.crs if hasattr(gdf, 'crs') else None
                        ).reset_index(drop=True)
                    else:
                        # Keep original structure for empty GeoDataFrame
                        gdf = gdf.iloc[0:0].copy()  # Empty GeoDataFrame with same structure
                    
                    # Add required columns
                    gdf['aoi'] = aoi.id if aoi else None
                    
                    # Convert any datetime columns to strings to make them JSON serializable
                    for col in gdf.columns:
                        if gdf[col].dtype == 'datetime64[ns]' or pd.api.types.is_datetime64_any_dtype(gdf[col]):
                            gdf[col] = gdf[col].astype(str)
                    
                    # Create results GeoJSON
                    results_geojson = gdf.to_json()
                    
                    # Store search results in session for later use during processing
                    request.session['search_results_gdf'] = results_geojson

                    # Build one parent row per Catalog ID with vendor sub-rows.
                    catalog_groups = {}
                    for _, row in gdf.iterrows():
                        catalog_id = row.get('Catalog ID', 'Unknown')
                        catalog_key = str(catalog_id)

                        if 'MSI_Entity_ID' in row and pd.notna(row['MSI_Entity_ID']) and 'PAN_Entity_ID' in row and pd.notna(row['PAN_Entity_ID']):
                            result_id = f"{row['MSI_Entity_ID']}+{row['PAN_Entity_ID']}"
                        else:
                            continue

                        child_date = row.get(
                            'Display Date',
                            normalize_acquisition_date(row.get('acquisitionDate'), row.get('Vendor ID', None))
                        )
                        child_sensor = row.get(
                            'Display Sensor',
                            selected_sensor_display,
                        )

                        if catalog_key not in catalog_groups:
                            catalog_groups[catalog_key] = {
                                'catalog_id': catalog_id,
                                'result_ids': [],
                                'dates': [],
                                'sensors': [],
                            }

                        catalog_groups[catalog_key]['result_ids'].append(result_id)
                        catalog_groups[catalog_key]['dates'].append(child_date)
                        catalog_groups[catalog_key]['sensors'].append(child_sensor)

                    catalog_rows = []
                    for _, group in catalog_groups.items():
                        valid_dates = [d for d in group['dates'] if d and d != 'Unknown']
                        parent_date = valid_dates[0] if valid_dates else 'Unknown'
                        parent_sensor = selected_sensor_display
                        catalog_rows.append({
                            'catalog_id': group['catalog_id'],
                            'date': parent_date,
                            'sensor': parent_sensor,
                            'count': len(group['result_ids']),
                        })

                    message = (
                        f"Your query returned {len(catalog_rows)} catalog IDs and "
                        f"{len(gdf)} PAN/MSI imagery pairs from USGS EarthExplorer"
                    )
                    if search_mode == 'id' and parsed_vendor_identifier:
                        message += f" using vendor identifier '{parsed_vendor_identifier}' parsed from the uploaded GeoJSON"
                    
            except Exception as e:
                logger.error(f"Error during API search: {e}", exc_info=True)
                
                # Provide more specific error messages based on the error type
                error_message = str(e)
                if "not authorized to access dataset" in error_message:
                    user_message = f"Access denied: The USGS account does not have permission to access the required satellite imagery dataset (crssp_orderable_w3). This dataset requires special permissions from USGS."
                elif "UNAUTHORIZED_USER" in error_message:
                    user_message = f"USGS API authorization failed: The account may not have the required permissions for this dataset."
                else:
                    user_message = f"Failed to retrieve imagery data: {error_message}"
                
                messages.error(request, user_message)
                # Re-raise the exception to see the full error details
                raise

            # Create HTML table for all API types using the same loaded data
            results_html = f'<input type="hidden" id="api-hidden-input" name="select_api" value="{api}">'
            results_html += f'<input type="hidden" name="search_mode" value="{html.escape(str(search_mode))}">'
            if search_mode == 'id' and staged_geojson_upload_id:
                results_html += (
                    f'<input type="hidden" name="staged_geojson_upload_id" '
                    f'value="{int(staged_geojson_upload_id)}">'
                )
            
            # USWDS Table with proper classes and structure
            results_html += '<div>'
            results_html += '<table class="usa-table usa-table--striped" style="display: block; max-height: 370px; overflow-y: auto; width: 100%;">'
            results_html += '<thead>'
            results_html += '<tr>'
            # Add select all checkbox with JS handler
            results_html += '<th scope="col"><input type="checkbox" id="select-all-checkbox" aria-label="Select all results" onclick="toggleAllCheckboxes(this)"></th>'

            display_columns = ['Catalog ID', 'Date', 'Sensor']
            for col in display_columns:
                results_html += f'<th scope="col">{col}</th>'
            results_html += '</tr>'
            results_html += '</thead>'
            results_html += '<tbody>'

            for idx, catalog in enumerate(catalog_rows):
                catalog_value = html.escape(str(catalog.get('catalog_id', 'Unknown')))
                parent_date = html.escape(str(catalog.get('date', 'Unknown')))
                parent_sensor = html.escape(str(catalog.get('sensor', 'Unknown')))
                child_count = int(catalog.get('count', 0))
                checkbox_id = f"catalog-select-{idx}"

                results_html += '<tr class="catalog-parent-row">'
                results_html += (
                    f'<td><input type="checkbox" id="{checkbox_id}" name="selected" value="{catalog_value}" '
                    f'aria-label="Select catalog {catalog_value}"></td>'
                )
                results_html += (
                    f'<td><label for="{checkbox_id}">{catalog_value}</label></td>'
                )
                results_html += f'<td>{parent_date}</td>'
                results_html += f'<td>{parent_sensor}</td>'
                results_html += '</tr>'
            results_html += '</tbody>'

            results_html += '</table>'
            results_html += '</div>'
            results = mark_safe(results_html)
            
            # After successful processing, create a fresh form with preserved values
            # This preserves the user's selections for potential follow-up queries
            preserved_data = {
                'aoi': aoi.id if aoi else None,
                'api': api,
                'search_mode': search_mode,
                'id_input': '',
                'vendor': vendor,
                'start_date': form.cleaned_data.get('start_date'),
                'end_date': form.cleaned_data.get('end_date'),
                'sensor': sensor_list,  # Preserve selected sensors
            }
            form = APIQueryForm(initial=preserved_data)

        else:
            # Form validation failed (Django built-in validation)
            # Keep the form with submitted data and validation errors
            messages.error(request, "Please correct the errors in the form.")

    else:
        form = APIQueryForm()

    return render(request, 'collection_page.html', {
        'form': form,
        'results': results,
        'message': message,
        'area_of_interest_geojson': json.dumps(geometry) if geometry else None,
        'results_geojson': results_geojson,
        'aoi_bounds': aoi_bounds,
        'selection_limit': selection_limit,
        'project': project,
    })


def launch_imagery_processing_pipeline(
    entity_id,
    project_id,
    requested_by_username=None,
    project_display=None,
    request_group_id=None,
    aoi_name=None,
    catalog_ids=None,
    points_upload_id=None,
    points_catalog_id=None,
):
    """
    Launch the GAIA imagery processing pipeline starting from step 2 (download).
    This function creates a Celery chain to process a single entity ID through:
    - Download imagery from USGS
    - Organize and calibrate 
    - Pansharpen
    - Create COGs
    - Upload to Azure
    - Cleanup local files
    
    Args:
        entity_id (str): The USGS Entity ID to process
        project_id (int): The project ID for organization
    """
    from animal.utils.logging import get_animal_logger
    from shapely.geometry import Point
    
    logger = get_animal_logger(__name__)
    
    # Generate unique chain ID for tracking
    chain_id = str(uuid.uuid4())
    logger.info(f"[{chain_id}] Starting imagery processing pipeline for entity {entity_id}")
    
    try:
        # Get the registered image from the database to get its geometry
        try:
            earth_explorer_record = EarthExplorer.objects.get(entity_id=entity_id)
            # Use the center point as geometry
            center_lon = earth_explorer_record.center_longitude_dec
            center_lat = earth_explorer_record.center_latitude_dec
            geometry = Point(center_lon, center_lat)
        except EarthExplorer.DoesNotExist:
            # Try GEGD table
            try:
                gegd_record = GEOINTDiscovery.objects.get(id=entity_id)
                # Use a default point if no specific geometry available
                geometry = Point(-70.0, 42.0)  # Default coordinate
            except GEOINTDiscovery.DoesNotExist:
                logger.warning(f"[{chain_id}] Entity {entity_id} not found in database, using default geometry")
                geometry = Point(-70.0, 42.0)  # Default coordinate
    
        # Create a proper GeoDataFrame like the search task would return
        # Include catalog_id from the database for fallback resolution
        try:
            earth_explorer_record = EarthExplorer.objects.get(entity_id=entity_id)
            catalog_id = earth_explorer_record.catalog_id
        except EarthExplorer.DoesNotExist:
            catalog_id = None
            
        entity_data = {
            'Entity ID': [entity_id],
            'Catalog ID': [catalog_id] if catalog_id else [None],
            'acquisitionDate': ['2024-01-01'],  # Placeholder date
            'displayId': [entity_id],
            'geometry': [geometry]
        }
        
        # Create GeoDataFrame with proper geometry column
        results_gdf = gpd.GeoDataFrame(entity_data, geometry='geometry', crs='EPSG:4326')
        
        # Convert to GeoJSON format (this creates proper GeoJSON that gpd.read_file can handle)
        results_json = results_gdf.to_json()
        
        # Create the payload expected by the download task
        payload = {
            "results": results_json,
            "usgs_username": settings.USGS_USERNAME,
            "token": settings.USGS_TOKEN
        }
        payload_json = json.dumps(payload)
        
        # Delegate to canonical launcher so queue/timeouts/retries remain centralized.
        result = launch_pipeline_from_payload(
            results_payload_json=payload_json,
            img_dir=str(app_settings.img_dir),
            azure_credentials={
                "account_name": app_settings.azure_account_name,
                "account_key": app_settings.azure_account_key,
                "container_name": app_settings.azure_container_name,
            },
            dem_path=str(app_settings.dem_file),
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
            points_upload_id=points_upload_id,
            points_catalog_id=points_catalog_id,
        )
        logger.info(f"[{chain_id}] Processing chain launched with task ID: {result.id}")

        project = Project.objects.filter(id=project_id).first()
        resolved_catalog_ids = catalog_ids
        if not resolved_catalog_ids and catalog_id:
            resolved_catalog_ids = str(catalog_id)
        if project:
            create_or_mark_processing(
                chain_id=chain_id,
                project=project,
                requested_by_username=requested_by_username,
                request_group_id=request_group_id,
                aoi_name=aoi_name,
                catalog_ids=resolved_catalog_ids,
            )
        
        return chain_id
        
    except Exception as e:
        logger.error(f"[{chain_id}] Failed to launch processing pipeline: {e}", exc_info=True)
        raise


def launch_imagery_processing_pipeline_with_search_data(
    entity_id,
    project_id,
    original_search_results,
    requested_by_username=None,
    project_display=None,
    request_group_id=None,
    aoi_name=None,
    catalog_ids=None,
    points_upload_id=None,
    points_catalog_id=None,
):
    """
    Launch the imagery processing pipeline using original search results data.
    This bypasses database lookup and uses the real entity_id from search results.
    Uses Celery tasks but passes Entity ID instead of Vendor ID for downloads.
    
    Args:
        entity_id (str): The entity ID to process
        project_id (int): The project ID
        original_search_results: Original search results GeoDataFrame or JSON string
    
    Returns:
        str: The chain ID for tracking
    """
    import uuid
    
    # Generate unique chain ID
    chain_id = str(uuid.uuid4())
    logger.info(f"[{chain_id}] Starting Celery pipeline with Entity ID for entity {entity_id}")
    
    try:
        # Convert search results to GeoDataFrame if needed
        if isinstance(original_search_results, str):
            search_gdf = gpd.read_file(original_search_results, driver='GeoJSON')
        else:
            search_gdf = original_search_results
        
        # Find the specific entity in the search results
        # First try direct Entity ID match
        matching_rows = search_gdf[search_gdf['Entity ID'] == entity_id]
        
        # If not found, check if this entity is part of an MSI/PAN pair in combined records
        if matching_rows.empty:
            # Look for MSI_Entity_ID or PAN_Entity_ID matches in combined records
            msi_matches = search_gdf[search_gdf.get('MSI_Entity_ID') == entity_id]
            pan_matches = search_gdf[search_gdf.get('PAN_Entity_ID') == entity_id]
            
            if not msi_matches.empty:
                # Found as MSI component of a pair
                matching_rows = msi_matches
                logger.info(f"[{chain_id}] Found {entity_id} as MSI component in combined record")
            elif not pan_matches.empty:
                # Found as PAN component of a pair
                matching_rows = pan_matches
                logger.info(f"[{chain_id}] Found {entity_id} as PAN component in combined record")
        
        if matching_rows.empty:
            raise ValueError(f"Entity {entity_id} not found in original search results")
            
        entity_data = matching_rows.iloc[0].to_dict()
        
        # For combined records, extract the specific component data we need
        if entity_id == entity_data.get('MSI_Entity_ID'):
            # This is an MSI request, use MSI-specific data
            actual_entity_id = entity_data.get('MSI_Entity_ID', entity_id)
            logger.info(f"[{chain_id}] Processing MSI component: {actual_entity_id}")
        elif entity_id == entity_data.get('PAN_Entity_ID'): 
            # This is a PAN request, use PAN-specific data
            actual_entity_id = entity_data.get('PAN_Entity_ID', entity_id)
            logger.info(f"[{chain_id}] Processing PAN component: {actual_entity_id}")
        else:
            # Single record or direct match
            actual_entity_id = entity_data.get('Entity ID', entity_id)
            logger.info(f"[{chain_id}] Processing single record: {actual_entity_id}")
            
        logger.info(f"[{chain_id}] Found entity data - Entity ID: {actual_entity_id}")
        logger.info(f"[{chain_id}] Catalog ID: {entity_data.get('Catalog ID', 'UNKNOWN')}")
        logger.info(f"[{chain_id}] Using Entity ID directly from search results")
        logger.info(f"[{chain_id}] This Entity ID should be the correct download identifier")
        
        # Create payload with real search result data, emphasizing Entity ID.
        # If this came from a combined MSI/PAN record, preserve both component
        # IDs so the download task can fetch both files.
        entity_data_for_pipeline = {
            'Entity ID': actual_entity_id,
            'Catalog ID': entity_data.get('Catalog ID', 'UNKNOWN'),
            'Vendor ID': entity_data.get('Vendor ID', 'UNKNOWN'),
            'Cloud Cover': entity_data.get('Cloud Cover', 0),
            'geometry': entity_data.get('geometry', None)
        }

        msi_entity_id = entity_data.get('MSI_Entity_ID')
        pan_entity_id = entity_data.get('PAN_Entity_ID')
        if pd.notna(msi_entity_id) and pd.notna(pan_entity_id):
            entity_data_for_pipeline['MSI_Entity_ID'] = msi_entity_id
            entity_data_for_pipeline['PAN_Entity_ID'] = pan_entity_id
            logger.info(f"[{chain_id}] Preserving MSI/PAN pair for download: {msi_entity_id}, {pan_entity_id}")
        
        # Create a single-row GeoDataFrame with the entity data
        entity_gdf = gpd.GeoDataFrame([entity_data_for_pipeline], geometry='geometry')
        
        # Create the correct payload structure that download_imagery task expects
        payload = {
            "results": entity_gdf.to_json(),
            "img_dir": str(app_settings.img_dir),
            "dataset": "crssp_orderable_w3",
            "use_entity_id": True,  # Flag to tell Celery task to use Entity ID
            "usgs_username": settings.USGS_USERNAME,
            "token": settings.USGS_TOKEN
        }
        
        # Delegate to canonical launcher so all chain operational controls are consistent.
        result = launch_pipeline_from_payload(
            results_payload_json=json.dumps(payload),
            img_dir=str(app_settings.img_dir),
            azure_credentials={
                "account_name": app_settings.azure_account_name,
                "account_key": app_settings.azure_account_key,
                "container_name": app_settings.azure_container_name,
            },
            dem_path=str(app_settings.dem_file),
            chain_id=chain_id,
            project_id=project_id,
            requested_by_username=requested_by_username,
            project_display=project_display,
            points_upload_id=points_upload_id,
            points_catalog_id=points_catalog_id,
        )
        logger.info(f"[{chain_id}] Celery processing chain launched with Entity ID. Task ID: {result.id}")

        project = Project.objects.filter(id=project_id).first()
        resolved_catalog_ids = catalog_ids
        if not resolved_catalog_ids:
            source_catalog_id = entity_data.get('Catalog ID', None)
            if source_catalog_id and str(source_catalog_id) != 'UNKNOWN':
                resolved_catalog_ids = str(source_catalog_id)
        if project:
            create_or_mark_processing(
                chain_id=chain_id,
                project=project,
                requested_by_username=requested_by_username,
                request_group_id=request_group_id,
                aoi_name=aoi_name,
                catalog_ids=resolved_catalog_ids,
            )
        
        return chain_id
        
    except Exception as e:
        logger.error(f"[{chain_id}] Failed to launch Celery pipeline: {e}", exc_info=True)
        raise
