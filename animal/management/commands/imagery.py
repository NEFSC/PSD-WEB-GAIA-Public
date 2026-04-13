# ------------------------------------------------------------------------------
# ----- imagery.py -------------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    created:  2026-01-20
#    revised:  2026-01-21 (added --force-refresh for PGC repo management)
#              2026-01-22 (Phase 4: added upload action)
#              2026-01-22 (added --pan-only for batch PAN calibration)
#              2026-01-22 (added --from-calibrated for PAN-only COG workflow)
#              2026-02-18 (flag consistency: credential overrides, dataset
#                          choices validation, --input-dir alias on organize)
#              2026-02-18 (GAIFAGP-479: moved filter_wv3_swir_cavis to
#                          api_utils.py — canonical utility location)
#    ticket:   GAIFAGP-439
#
#    purpose:  Unified CLI for GAIA imagery acquisition and processing.
#              Consolidates fragmented utilities into a single entry point
#              while preserving async/Celery capability.
#
#    usage:
#        python manage.py imagery auth --provider=usgs
#        python manage.py imagery search --aoi=6 --start=2024-01-01 --end=2024-12-31
#        python manage.py imagery search --geojson=/path/to/aoi.geojson --start=2024-01-01 --end=2024-12-31
#        python manage.py imagery calibrate --input-dir=... --dem=... --force-refresh
#        python manage.py imagery calibrate --input-dir=... --dem=... --pan-only
#        python manage.py imagery cog --input-dir=... --from-calibrated  # PAN-only workflow
#        python manage.py imagery upload --input-dir=.../cogs --azure-dir=processed/wv3
#        python manage.py imagery pipeline --aoi=6 --start=2024-01-01 --end=2024-12-31
#
#    phases:
#        Phase 1: auth, search (this file)
#        Phase 2: download, organize, pair, status
#        Phase 3: calibrate, pansharpen, cog
#        Phase 4: upload (footprint/fishnet removed - duplicative)
#        Phase 5: async integration, pipeline
#        Phase 6: consolidation, cleanup
#
#    related:
#        GAIFAGP-165 (VRT DEM support - spike closed)
#        GAIFAGP-180 (PAN-only workflow)
#        GAIFAGP-445 (VRT DEM Windows validation)
#        GAIFAGP-446 (VRT DEM Linux production deployment)
#
#    SOURCE OF TRUTH ASSUMPTIONS:
#      - USGS M2M API is authoritative for scene search results
#      - Local filesystem is working copy; Azure Blob is durable store
#      - api_utils.py owns all USGS API interaction logic
#      - Preprocessing delegates to PGC imagery_utils (external)
#
# ------------------------------------------------------------------------------

import json
import sys
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings as django_settings

# Deferred imports - these happen inside methods to avoid module-level failures
# from animal.utils.logging import get_animal_logger
# from animal.utils.config import settings as pipeline_settings

# Use standard logging at module level; switch to animal logger in methods
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------

# Dataset name mapping (short name -> USGS API dataset name)
DATASET_MAP = {
    'wv3': 'crssp_orderable_w3',
    'wv2': 'crssp_orderable_w2',
    'wv1': 'crssp_orderable_wv',
    'ge1': 'crssp_orderable_ge',
    'qb2': 'crssp_orderable_qb',
    'ik2': 'crssp_orderable_ik',
    'aw': 'crssp_orderable_aw',
    # Also allow full names (pass-through)
    'crssp_orderable_w3': 'crssp_orderable_w3',
    'crssp_orderable_w2': 'crssp_orderable_w2',
    'crssp_orderable_wv': 'crssp_orderable_wv',
    'crssp_orderable_ge': 'crssp_orderable_ge',
    'crssp_orderable_qb': 'crssp_orderable_qb',
    'crssp_orderable_ik': 'crssp_orderable_ik',
    'crssp_orderable_aw': 'crssp_orderable_aw',
}

# All available datasets
ALL_DATASETS = ['wv3', 'wv2', 'wv1', 'ge1', 'qb2', 'ik2', 'aw']


# ------------------------------------------------------------------------------
# Error Types
# ------------------------------------------------------------------------------

class ImageryCommandError(CommandError):
    """Base error for imagery command failures."""
    pass


class AuthenticationError(ImageryCommandError):
    """Failed to authenticate to external service."""
    pass


class SearchError(ImageryCommandError):
    """Failed to search imagery catalog."""
    pass


class ValidationError(ImageryCommandError):
    """Input validation failed."""
    pass


# ------------------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------------------

def get_logger():
    """Get the appropriate logger, falling back to standard logging."""
    try:
        from animal.utils.logging import get_animal_logger
        return get_animal_logger(__name__)
    except ImportError:
        return logging.getLogger(__name__)


def get_pipeline_settings():
    """Get pipeline settings, with fallback to Django settings."""
    try:
        from animal.utils.config import settings as pipeline_settings
        return pipeline_settings
    except ImportError:
        return None


def load_aoi_geometry(aoi_id: Optional[int] = None, geojson_path: Optional[str] = None):
    """
    Load AOI geometry from database or local GeoJSON file.
    
    Args:
        aoi_id: Database AOI ID (mutually exclusive with geojson_path)
        geojson_path: Path to local GeoJSON file (mutually exclusive with aoi_id)
        
    Returns:
        tuple: (geometry, source_description)
        
    Raises:
        ValidationError: If neither or both sources provided, or if loading fails
    """
    import geopandas as gpd
    
    log = get_logger()
    
    if aoi_id is not None and geojson_path is not None:
        raise ValidationError("Specify either --aoi or --geojson, not both")
    
    if aoi_id is None and geojson_path is None:
        raise ValidationError("Must specify either --aoi or --geojson")
    
    if aoi_id is not None:
        # Load from database
        try:
            from animal.models import AreaOfInterest
            from shapely import wkt as shapely_wkt
            
            aoi = AreaOfInterest.objects.get(id=aoi_id)
            # Convert Django GEOS geometry to Shapely geometry
            # Django GEOS geometry has .wkt property
            geometry = shapely_wkt.loads(aoi.geometry.wkt)
            source = f"database AOI ID {aoi_id}"
            log.info(f"[SEARCH] Loaded AOI from database: ID={aoi_id}, name='{aoi.name}'")
            return geometry, source
        except Exception as e:
            if "DoesNotExist" in str(type(e).__name__):
                raise ValidationError(f"AreaOfInterest with ID {aoi_id} not found in database")
            raise ValidationError(f"Failed to load AreaOfInterest {aoi_id} from database: {e}")
    
    else:
        # Load from GeoJSON file
        geojson_path = Path(geojson_path)
        if not geojson_path.exists():
            raise ValidationError(f"GeoJSON file not found: {geojson_path}")
        
        try:
            gdf = gpd.read_file(geojson_path)
            if len(gdf) == 0:
                raise ValidationError(f"GeoJSON file is empty: {geojson_path}")
            
            # Use first geometry (or unary_union if multiple)
            if len(gdf) == 1:
                geometry = gdf.geometry.iloc[0]
            else:
                geometry = gdf.geometry.unary_union
                log.warning(f"[SEARCH] GeoJSON has {len(gdf)} features; using union")
            
            source = f"GeoJSON file {geojson_path}"
            log.info(f"[SEARCH] Loaded AOI from GeoJSON: {geojson_path}")
            return geometry, source
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(f"Failed to load GeoJSON {geojson_path}: {e}")


def export_geojson(gdf, output_path, crs: str = 'EPSG:4326'):
    """
    Export GeoDataFrame as GeoJSON using GAIA spatial standards.
    
    Args:
        gdf: GeoDataFrame to export
        output_path: Destination path
        crs: Output CRS (default EPSG:4326 for CLI/testing/database)
        
    Returns:
        Path to written file, or None if empty/invalid
    """
    log = get_logger()
    
    # Handle empty GeoDataFrame
    if len(gdf) == 0:
        log.warning(f"[EXPORT] Empty GeoDataFrame, skipping export to {output_path}")
        return None
    
    # Ensure GeoDataFrame has a valid active geometry column
    try:
        geom_col = gdf.geometry
        if geom_col is None or geom_col.isna().all():
            log.warning(f"[EXPORT] No valid geometry data, skipping export to {output_path}")
            return None
    except AttributeError:
        # No active geometry column set - try to find one
        if 'bounds' in gdf.columns:
            gdf = gdf.set_geometry('bounds')
        elif 'geometry' in gdf.columns:
            gdf = gdf.set_geometry('geometry')
        else:
            log.warning(f"[EXPORT] No geometry column found, skipping export to {output_path}")
            return None
    
    if gdf.crs is None:
        gdf = gdf.set_crs('EPSG:4326')
    
    if str(gdf.crs) != crs:
        gdf = gdf.to_crs(crs)
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver='GeoJSON')
    
    log.info(f"[EXPORT] Wrote {len(gdf)} features to {output_path} (CRS: {crs})")
    
    return output_path


# ------------------------------------------------------------------------------
# Main Command
# ------------------------------------------------------------------------------

class Command(BaseCommand):
    help = 'GAIA imagery acquisition and processing CLI'

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest='action', help='Available actions')
        
        # ----- auth -----
        auth_parser = subparsers.add_parser('auth', help='Test authentication to imagery providers')
        auth_parser.add_argument(
            '--provider', 
            choices=['usgs', 'mgp', 'all'], 
            default='usgs',
            help='Provider to authenticate with (default: usgs)'
        )
        auth_parser.add_argument('--username', help='Override username from settings')
        auth_parser.add_argument('--token', help='Override token from settings')
        
        # ----- search -----
        search_parser = subparsers.add_parser('search', help='Search imagery catalogs')
        
        # AOI input sources (mutually exclusive)
        aoi_group = search_parser.add_mutually_exclusive_group(required=True)
        aoi_group.add_argument('--aoi', type=int, help='AOI ID from database')
        aoi_group.add_argument('--geojson', type=str, help='Path to local GeoJSON file')
        
        search_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
        search_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
        search_parser.add_argument(
            '--dataset', 
            nargs='+',
            choices=[
                'wv3', 'wv2', 'wv1', 'ge1', 'qb2', 'ik2', 'aw',
                'crssp_orderable_w3', 'crssp_orderable_w2', 'crssp_orderable_wv',
                'crssp_orderable_ge', 'crssp_orderable_qb', 'crssp_orderable_ik',
                'crssp_orderable_aw', 'all'
            ],
            default=['wv3'],
            help='Dataset(s) to search. Use short names (wv3, wv2, ge1) or full names. '
                 'Specify multiple: --dataset wv3 wv2 ge1. Use "all" for all datasets.'
        )
        
        # SWIR/CAVIS filtering (default: exclude both)
        search_parser.add_argument(
            '--include-swir', 
            action='store_true',
            help='Include SWIR catalog IDs (104A*) - excluded by default'
        )
        search_parser.add_argument(
            '--include-cavis', 
            action='store_true',
            help='Include CAVIS catalog IDs (104C*) - excluded by default'
        )
        
        # Output options
        search_parser.add_argument('--output', '-o', help='Output GeoJSON path')
        search_parser.add_argument(
            '--crs',
            default='EPSG:4326',
            help='Output CRS (default: EPSG:4326, use EPSG:3857 for web display)'
        )
        
        # Credential override
        search_parser.add_argument('--username', help='Override username from settings')
        search_parser.add_argument('--token', help='Override token from settings')
        
        # Async dispatch (Phase 5)
        search_parser.add_argument(
            '--async', 
            action='store_true', 
            dest='run_async',
            help='Dispatch to Celery worker (not yet implemented)'
        )
        
        # ----- download ----- (Phase 2)
        download_parser = subparsers.add_parser('download', help='Download imagery by entity ID or catalog ID')
        
        # Input options (mutually exclusive: entity-id OR catalog-id+search-results)
        download_input = download_parser.add_mutually_exclusive_group(required=True)
        download_input.add_argument('--entity-id', help='Single USGS entity ID to download')
        download_input.add_argument('--catalog-id', help='Catalog ID - downloads all entities (PAN+MSI) from search results')
        
        download_parser.add_argument(
            '--search-results',
            help='GeoJSON with search results (required with --catalog-id)'
        )
        download_parser.add_argument('--output-dir', help='Download destination')
        download_parser.add_argument(
            '--dataset',
            default='wv3',
            choices=[
                'wv3', 'wv2', 'wv1', 'ge1', 'qb2', 'ik2', 'aw',
                'crssp_orderable_w3', 'crssp_orderable_w2', 'crssp_orderable_wv',
                'crssp_orderable_ge', 'crssp_orderable_qb', 'crssp_orderable_ik',
                'crssp_orderable_aw',
            ],
            help='Dataset to download from (default: wv3)'
        )
        download_parser.add_argument(
            '--max-retries',
            type=int,
            default=8,
            help='Max polling attempts for USGS staging (default: 8)'
        )
        download_parser.add_argument(
            '--poll-interval',
            type=int,
            default=30,
            help='Seconds between polling attempts (default: 30)'
        )
        download_parser.add_argument('--username', help='Override username from settings')
        download_parser.add_argument('--token', help='Override token from settings')
        download_parser.add_argument('--async', action='store_true', dest='run_async')
        
        # ----- calibrate ----- (Phase 3)
        calibrate_parser = subparsers.add_parser('calibrate', help='Calibrate imagery using DEM (orthorectification)')
        calibrate_input = calibrate_parser.add_mutually_exclusive_group(required=True)
        calibrate_input.add_argument('--input-dir', help='Directory with raw imagery (auto-finds pairs)')
        calibrate_input.add_argument('--pairs-file', help='JSON file from pair action')
        calibrate_input.add_argument('--single-file', help='Single file to calibrate (PAN-only or MSI-only workflow)')
        calibrate_parser.add_argument(
            '--dem', 
            help='DEM path - local file, Azure blob URL, or VRT. Default: from config'
        )
        calibrate_parser.add_argument('--processes', type=int, default=1, help='Parallel calibrations (default: 1)')
        calibrate_parser.add_argument(
            '--force-refresh',
            action='store_true',
            help='Force re-clone of PGC imagery_utils (ignores staleness check)'
        )
        calibrate_parser.add_argument(
            '--pan-only',
            action='store_true',
            help='Process all PAN files individually (no MSI pairing required). Use with --input-dir.'
        )
        
        # ----- organize ----- (Phase 2)
        organize_parser = subparsers.add_parser('organize', help='Organize downloaded zips into catalog structure and unzip')
        organize_parser.add_argument('--img-dir', '--input-dir', required=True, dest='img_dir', help='Directory containing downloaded zips')
        organize_parser.add_argument('--results', required=True, help='GeoJSON with search results')
        organize_parser.add_argument(
            '--no-unzip',
            action='store_true',
            help='Skip unzipping (only organize into catalog folders)'
        )
        
        # ----- pair ----- (Phase 2)
        pair_parser = subparsers.add_parser('pair', help='Match PAN/MSI pairs from raster files (NITF or GeoTIFF)')
        pair_parser.add_argument('--input-dir', required=True, help='Directory containing raster files')
        pair_parser.add_argument('--output', '-o', help='Output JSON file for pairs')
        
        # ----- status ----- (Phase 2)
        status_parser = subparsers.add_parser('status', help='Check USGS download status')
        status_parser.add_argument('--label', required=True, help='Download label from previous request')
        status_parser.add_argument('--username', help='Override username from settings')
        status_parser.add_argument('--token', help='Override token from settings')
        
        # ----- pansharpen ----- (Phase 3)
        pansharpen_parser = subparsers.add_parser('pansharpen', help='Pansharpen calibrated imagery')
        pansharpen_input = pansharpen_parser.add_mutually_exclusive_group(required=True)
        pansharpen_input.add_argument('--input-dir', help='Directory with calibrated imagery')
        pansharpen_input.add_argument('--calibrated-file', help='JSON file from calibrate action')
        pansharpen_parser.add_argument(
            '--bands', nargs=3, type=int, default=[5, 3, 2],
            help='RGB bands to use. Defaults: WV-3=[5,3,2], WV-2=[5,3,2], GE-1=[3,2,1], QB=[3,2,1]'
        )
        pansharpen_parser.add_argument(
            '--sensor',
            choices=['wv3', 'wv2', 'ge1', 'qb', 'ik'],
            help='Use preset bands for sensor (overrides --bands)'
        )
        pansharpen_parser.add_argument('--processes', type=int, default=1, help='Parallel processes (default: 1)')
        
        # ----- cog ----- (Phase 3)
        cog_parser = subparsers.add_parser('cog', help='Create Cloud Optimized GeoTIFFs')
        cog_input = cog_parser.add_mutually_exclusive_group(required=True)
        cog_input.add_argument('--input', help='Single pansharpened file')
        cog_input.add_argument('--input-dir', help='Directory with pansharpened imagery')
        cog_parser.add_argument('--output-dir', help='COG output directory')
        cog_parser.add_argument('--processes', type=int, default=2, help='Parallel processes (default: 2)')
        cog_parser.add_argument(
            '--from-calibrated',
            action='store_true',
            help='Process calibrated files directly (skip pansharpen step, for PAN-only workflows)'
        )
        
        # ----- upload ----- (Phase 4)
        upload_parser = subparsers.add_parser('upload', help='Upload COGs to Azure Blob Storage')
        upload_parser.add_argument('--input-dir', required=True, help='Directory containing COG .tif files')
        upload_parser.add_argument(
            '--azure-dir',
            default='cogs',
            help='Blob path prefix within container (default: cogs)'
        )
        upload_parser.add_argument(
            '--container',
            help='Override container name from settings'
        )
        upload_parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List files without uploading'
        )
        
        # ----- pipeline ----- (Phase 5)
        pipeline_parser = subparsers.add_parser('pipeline', help='Launch full processing pipeline (Phase 5)')
        pipeline_parser.add_argument('--aoi', type=int, required=True)
        pipeline_parser.add_argument('--start', required=True)
        pipeline_parser.add_argument('--end', required=True)
        pipeline_parser.add_argument('--chain-id', help='Chain ID for tracking')

    def handle(self, *args, **options):
        action = options.get('action')
        if not action:
            self.print_help('manage.py', 'imagery')
            return
        
        handler = getattr(self, f'handle_{action}', None)
        if handler is None:
            raise CommandError(f"Unknown action: {action}")
        
        try:
            return handler(**options)
        except ImageryCommandError:
            raise
        except Exception as e:
            log = get_logger()
            log.error(f"[{action.upper()}] Unexpected error: {e}", exc_info=True)
            raise CommandError(f"Command failed: {e}")

    # --------------------------------------------------------------------------
    # Credential Helpers
    # --------------------------------------------------------------------------
    
    def _get_usgs_credentials(self, options):
        """Get USGS credentials from options or settings."""
        username = options.get('username')
        token = options.get('token')
        
        # Try pipeline settings first
        pipeline_settings = get_pipeline_settings()
        
        if username is None:
            if pipeline_settings:
                username = getattr(pipeline_settings, 'USGS_USERNAME', None)
            if username is None:
                username = getattr(django_settings, 'USGS_USERNAME', None)
        
        if token is None:
            if pipeline_settings:
                token = getattr(pipeline_settings, 'USGS_TOKEN', None)
            if token is None:
                token = getattr(django_settings, 'USGS_TOKEN', None)
        
        if not username or not token:
            raise AuthenticationError(
                "USGS credentials not configured. Set USGS_USERNAME and USGS_TOKEN in secrets.json"
            )
        
        return username, token

    # --------------------------------------------------------------------------
    # Action Handlers
    # --------------------------------------------------------------------------

    def handle_auth(self, **options):
        """
        Test authentication to imagery providers.
        
        Uses token-based authentication (USGS M2M standard).
        Password-based auth is deprecated.
        """
        import requests
        
        provider = options.get('provider', 'usgs')
        
        self.stdout.write(f"[AUTH] Testing authentication for provider: {provider}")
        
        if provider in ('usgs', 'all'):
            username, token = self._get_usgs_credentials(options)
            self._test_usgs_auth(username, token)
        
        if provider in ('mgp', 'all'):
            self.stdout.write(self.style.WARNING("[AUTH] MGP authentication not yet implemented"))
        
        self.stdout.write(self.style.SUCCESS("[AUTH] Authentication test completed"))

    def _test_usgs_auth(self, username: str, token: str):
        """Test USGS EarthExplorer authentication using token-based auth."""
        import requests
        from animal.utils.api_utils import ee_login
        
        self.stdout.write(f"[AUTH] Authenticating to USGS as: {username}")
        
        try:
            session = requests.Session()
            session = ee_login(session, username, token)
            self.stdout.write(self.style.SUCCESS("[AUTH] USGS authentication successful"))
        except Exception as e:
            raise AuthenticationError(f"USGS authentication failed: {e}")

    def handle_search(self, **options):
        """
        Search imagery catalogs with SWIR/CAVIS filtering.
        
        Supports AOI from database or local GeoJSON file.
        Supports multiple datasets (WorldView-3, WorldView-2, GeoEye-1, etc.)
        Default: excludes SWIR (104A*) and CAVIS (104C*) catalog IDs.
        """
        import requests
        import pandas as pd
        from animal.utils.api_utils import ee_login, search_imagery, filter_wv3_swir_cavis
        
        log = get_logger()
        
        # Check for async dispatch (Phase 5)
        if options.get('run_async'):
            raise CommandError("Async dispatch not yet implemented (Phase 5)")
        
        # Load AOI geometry
        aoi_id = options.get('aoi')
        geojson_path = options.get('geojson')
        geometry, aoi_source = load_aoi_geometry(aoi_id=aoi_id, geojson_path=geojson_path)
        
        # Parse dates
        start_date = options['start']
        end_date = options['end']
        
        # Resolve datasets
        datasets_input = options.get('dataset', ['wv3'])
        if 'all' in datasets_input:
            datasets = ALL_DATASETS
        else:
            datasets = datasets_input
        
        # Map short names to full USGS names
        dataset_names = []
        for ds in datasets:
            if ds in DATASET_MAP:
                dataset_names.append(DATASET_MAP[ds])
            else:
                self.stdout.write(self.style.WARNING(f"[SEARCH] Unknown dataset '{ds}', skipping"))
        
        if not dataset_names:
            raise ValidationError("No valid datasets specified")
        
        # Validate date format
        try:
            datetime.strptime(start_date, '%Y-%m-%d')
            datetime.strptime(end_date, '%Y-%m-%d')
        except ValueError as e:
            raise ValidationError(f"Invalid date format. Use YYYY-MM-DD: {e}")
        
        self.stdout.write(f"[SEARCH] AOI source: {aoi_source}")
        self.stdout.write(f"[SEARCH] Date range: {start_date} to {end_date}")
        self.stdout.write(f"[SEARCH] Datasets: {', '.join(dataset_names)}")
        
        # Get credentials
        username, token = self._get_usgs_credentials(options)
        
        # Authenticate
        self.stdout.write(f"[SEARCH] Authenticating to USGS as: {username}")
        try:
            session = requests.Session()
            session = ee_login(session, username, token)
        except Exception as e:
            raise AuthenticationError(f"USGS authentication failed: {e}")
        
        # Search each dataset and combine results
        self.stdout.write("[SEARCH] Searching USGS EarthExplorer...")
        all_results = []
        
        for dataset_name in dataset_names:
            self.stdout.write(f"[SEARCH]   Searching {dataset_name}...")
            try:
                results_gdf = search_imagery(geometry, dataset_name, start_date, end_date, session)
                if len(results_gdf) > 0:
                    # Add dataset column for clarity
                    results_gdf['dataset'] = dataset_name
                    all_results.append(results_gdf)
                    self.stdout.write(f"[SEARCH]   Found {len(results_gdf)} results in {dataset_name}")
                else:
                    self.stdout.write(f"[SEARCH]   No results in {dataset_name}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"[SEARCH]   Error searching {dataset_name}: {e}"))
        
        # Combine all results
        if all_results:
            import geopandas as gpd
            combined_gdf = gpd.GeoDataFrame(pd.concat(all_results, ignore_index=True))
            # Re-set geometry column (lost during concat)
            if 'bounds' in combined_gdf.columns:
                combined_gdf = combined_gdf.set_geometry('bounds')
            elif 'geometry' in combined_gdf.columns:
                combined_gdf = combined_gdf.set_geometry('geometry')
        else:
            # Return empty GeoDataFrame with expected columns
            import geopandas as gpd
            combined_gdf = gpd.GeoDataFrame()
        
        self.stdout.write(f"[SEARCH] Found {len(combined_gdf)} total results before filtering")
        
        # Apply SWIR/CAVIS filter
        exclude_swir = not options.get('include_swir', False)
        exclude_cavis = not options.get('include_cavis', False)
        
        if len(combined_gdf) > 0 and (exclude_swir or exclude_cavis):
            filter_desc = []
            if exclude_swir:
                filter_desc.append("SWIR (104A*)")
            if exclude_cavis:
                filter_desc.append("CAVIS (104C*)")
            self.stdout.write(f"[SEARCH] Filtering: excluding {', '.join(filter_desc)}")
            
            try:
                combined_gdf = filter_wv3_swir_cavis(
                    combined_gdf,
                    exclude_swir=exclude_swir,
                    exclude_cavis=exclude_cavis
                )
            except ValueError as e:
                self.stdout.write(self.style.WARNING(f"[SEARCH] Could not apply filter: {e}"))
        
        self.stdout.write(self.style.SUCCESS(f"[SEARCH] Final result count: {len(combined_gdf)}"))
        
        # Output results
        output_path = options.get('output')
        if output_path:
            if len(combined_gdf) == 0:
                self.stdout.write(self.style.WARNING(f"[SEARCH] No results to export"))
            else:
                output_crs = options.get('crs', 'EPSG:4326')
                output_path = Path(output_path)
                result = export_geojson(combined_gdf, output_path, crs=output_crs)
                if result:
                    self.stdout.write(self.style.SUCCESS(f"[SEARCH] Results saved to: {output_path}"))
                else:
                    self.stdout.write(self.style.WARNING(f"[SEARCH] Export failed - no valid geometry"))
        else:
            # Print summary to stdout
            if len(combined_gdf) > 0:
                self.stdout.write("\n[SEARCH] Results summary:")
                for idx, row in combined_gdf.head(10).iterrows():
                    entity_id = row.get('Entity ID', row.get('entity_id', 'N/A'))
                    catalog_id = row.get('Catalog ID', row.get('catalog_id', 'N/A'))
                    acq_date = row.get('acquisitionDate', row.get('Acquisition Date', 'N/A'))
                    dataset = row.get('dataset', 'N/A')
                    self.stdout.write(f"  {dataset} | {catalog_id} | {entity_id} | {acq_date}")
                
                if len(combined_gdf) > 10:
                    self.stdout.write(f"  ... and {len(combined_gdf) - 10} more")
                
                self.stdout.write("\n[SEARCH] Use -o/--output to save full results as GeoJSON")

    def handle_download(self, **options):
        """
        Download imagery by entity ID or catalog ID.
        
        Two modes:
        1. --entity-id: Download a single entity
        2. --catalog-id + --search-results: Download all entities for a catalog from search results
        
        The USGS API has a staging workflow:
        1. Request download -> may be immediately available OR need preparation
        2. If preparing, poll until ready (up to max_retries * poll_interval seconds)
        3. Download the actual file when available
        """
        import requests
        import geopandas as gpd
        from animal.utils.api_utils import ee_login, download_imagery, TemporaryDataUnavailableError
        
        log = get_logger()
        
        # Check for async dispatch (Phase 5)
        if options.get('run_async'):
            raise CommandError("Async dispatch not yet implemented (Phase 5)")
        
        entity_id = options.get('entity_id')
        catalog_id = options.get('catalog_id')
        search_results_path = options.get('search_results')
        
        # Validate catalog-id mode requires search-results
        if catalog_id and not search_results_path:
            raise ValidationError("--catalog-id requires --search-results GeoJSON file")
        
        # Build list of entity IDs to download
        entity_ids_to_download = []
        
        if entity_id:
            # Single entity mode
            if entity_id.endswith('.'):
                self.stdout.write(self.style.WARNING(
                    f"[DOWNLOAD] Warning: Entity ID ends with a period - possible typo?"
                ))
            entity_ids_to_download.append(entity_id)
            
        elif catalog_id:
            # Catalog ID mode - filter search results
            search_results_path = Path(search_results_path)
            if not search_results_path.exists():
                raise ValidationError(f"Search results file not found: {search_results_path}")
            
            self.stdout.write(f"[DOWNLOAD] Loading search results: {search_results_path}")
            try:
                gdf = gpd.read_file(search_results_path)
            except Exception as e:
                raise ValidationError(f"Failed to load search results: {e}")
            
            # Normalize column names
            col_map = {}
            for col in gdf.columns:
                if col.lower() == 'catalog id':
                    col_map[col] = 'Catalog ID'
                elif col.lower() == 'entity id':
                    col_map[col] = 'Entity ID'
            if col_map:
                gdf = gdf.rename(columns=col_map)
            
            if 'Catalog ID' not in gdf.columns:
                raise ValidationError("Search results must have 'Catalog ID' column")
            if 'Entity ID' not in gdf.columns:
                raise ValidationError("Search results must have 'Entity ID' column")
            
            # Filter by catalog ID
            filtered = gdf[gdf['Catalog ID'] == catalog_id]
            
            if len(filtered) == 0:
                self.stdout.write(self.style.ERROR(f"[DOWNLOAD] No entities found for Catalog ID: {catalog_id}"))
                self.stdout.write("[DOWNLOAD] Available Catalog IDs in search results:")
                for cid in gdf['Catalog ID'].unique()[:10]:
                    self.stdout.write(f"  {cid}")
                if len(gdf['Catalog ID'].unique()) > 10:
                    self.stdout.write(f"  ... and {len(gdf['Catalog ID'].unique()) - 10} more")
                return
            
            entity_ids_to_download = filtered['Entity ID'].tolist()
            sensors = filtered['Sensor'].tolist() if 'Sensor' in filtered.columns else ['unknown'] * len(filtered)
            
            self.stdout.write(f"[DOWNLOAD] Catalog ID: {catalog_id}")
            self.stdout.write(f"[DOWNLOAD] Found {len(entity_ids_to_download)} entities:")
            for eid, sensor in zip(entity_ids_to_download, sensors):
                self.stdout.write(f"  {eid} ({sensor})")
        
        # Get output directory
        output_dir = options.get('output_dir')
        if output_dir:
            output_dir = Path(output_dir)
        else:
            pipeline_settings = get_pipeline_settings()
            if pipeline_settings and hasattr(pipeline_settings, 'IMAGERY_PATH'):
                output_dir = Path(pipeline_settings.IMAGERY_PATH)
            else:
                output_dir = Path.cwd() / 'downloads'
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Resolve dataset
        dataset_input = options.get('dataset', 'wv3')
        if dataset_input in DATASET_MAP:
            dataset_name = DATASET_MAP[dataset_input]
        else:
            dataset_name = dataset_input
        
        # Get retry settings
        max_retries = options.get('max_retries', 8)
        poll_interval = options.get('poll_interval', 30)
        
        self.stdout.write(f"[DOWNLOAD] Dataset: {dataset_name}")
        self.stdout.write(f"[DOWNLOAD] Output directory: {output_dir}")
        self.stdout.write(f"[DOWNLOAD] Max retries: {max_retries}, Poll interval: {poll_interval}s")
        
        # Get credentials and authenticate
        username, token = self._get_usgs_credentials(options)
        
        self.stdout.write(f"[DOWNLOAD] Authenticating to USGS as: {username}")
        try:
            session = requests.Session()
            session = ee_login(session, username, token)
        except Exception as e:
            raise AuthenticationError(f"USGS authentication failed: {e}")
        
        # Download each entity
        self.stdout.write(f"[DOWNLOAD] Starting downloads ({len(entity_ids_to_download)} files)...")
        self.stdout.write(f"[DOWNLOAD] Note: USGS may need to stage files (can take several minutes each)")
        
        successful = []
        failed = []
        
        for i, eid in enumerate(entity_ids_to_download, 1):
            self.stdout.write(f"\n[DOWNLOAD] [{i}/{len(entity_ids_to_download)}] Downloading: {eid}")
            
            try:
                local_path = download_imagery(
                    entity_id=eid,
                    session=session,
                    datasetName=dataset_name,
                    out_dir=str(output_dir),
                    max_retries=max_retries,
                    poll_interval=poll_interval
                )
                
                if local_path:
                    self.stdout.write(self.style.SUCCESS(f"[DOWNLOAD] Success: {local_path}"))
                    successful.append((eid, local_path))
                else:
                    self.stdout.write(self.style.ERROR(f"[DOWNLOAD] Failed: {eid}"))
                    failed.append(eid)
                    
            except TemporaryDataUnavailableError as e:
                self.stdout.write(self.style.WARNING(f"[DOWNLOAD] USGS staging delay for {eid}: {e}"))
                failed.append(eid)
                
            except Exception as e:
                log.error(f"[DOWNLOAD] Error for {eid}: {e}", exc_info=True)
                self.stdout.write(self.style.ERROR(f"[DOWNLOAD] Error: {e}"))
                failed.append(eid)
        
        # Summary
        self.stdout.write(f"\n[DOWNLOAD] Complete: {len(successful)} succeeded, {len(failed)} failed")
        
        if failed:
            self.stdout.write(self.style.WARNING("[DOWNLOAD] Failed entities:"))
            for eid in failed:
                self.stdout.write(f"  {eid}")

    def handle_calibrate(self, **options):
        """
        Calibrate imagery pairs using DEM.
        
        Uses pgc_wrapper.calibrate_pair() for pairs or calibrate_image() for single files.
        PGC orthorectification with fallback strategies (DEM -> constant-height -> RPC warp).
        
        Modes:
        - --input-dir: Auto-finds PAN/MSI pairs in directory
        - --pairs-file: Loads pairs from JSON (output of 'pair' action)
        - --single-file: Calibrate single file (PAN-only workflow, e.g., WV-1)
        
        DEM can be local file, Azure blob URL, or VRT in the cloud.
        Output: calibrated GeoTIFFs in 'calibrated/' subdirectory
        """
        import json
        from functools import partial
        from multiprocessing import Pool
        from animal.utils.utils import collect_geotiffs, match_pan_ms_pairs
        from animal.utils.pgc_wrapper import calibrate_pair, calibrate_image
        
        log = get_logger()
        
        input_dir = options.get('input_dir')
        pairs_file = options.get('pairs_file')
        single_file = options.get('single_file')
        dem_path = options.get('dem')
        processes = options.get('processes', 1)
        force_refresh = options.get('force_refresh', False)
        pan_only = options.get('pan_only', False)
        
        # Get DEM from config if not provided
        if not dem_path:
            pipeline_settings = get_pipeline_settings()
            if pipeline_settings and hasattr(pipeline_settings, 'dem_file'):
                dem_path = pipeline_settings.dem_file
            else:
                raise ValidationError("--dem is required (or set in config)")
        
        # Check DEM exists (only for local paths, not URLs/VRTs)
        dem_str = str(dem_path)
        is_cloud_dem = dem_str.startswith('http') or dem_str.startswith('/vsicurl/')
        if not is_cloud_dem:
            dem_path = Path(dem_path)
            if not dem_path.exists():
                raise ValidationError(f"DEM file not found: {dem_path}")
            dem_str = str(dem_path)
        
        self.stdout.write(f"[CALIBRATE] DEM: {dem_str}")
        if is_cloud_dem:
            self.stdout.write(f"[CALIBRATE] (Cloud DEM - will be accessed via GDAL)")
        
        # Ensure PGC imagery_utils is available (uses C:/gis/external/ on Windows)
        try:
            from animal.utils.git_utils import ensure_imagery_utils
            external_dir = ensure_imagery_utils(force=force_refresh)
            if external_dir:
                self.stdout.write(f"[CALIBRATE] PGC imagery_utils available at: {external_dir}")
            else:
                self.stdout.write(self.style.WARNING("[CALIBRATE] Could not ensure imagery_utils"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"[CALIBRATE] Could not clone imagery_utils: {e}"))
        
        # SINGLE FILE MODE - calibrate one file without pairing
        if single_file:
            single_file = Path(single_file)
            if not single_file.exists():
                raise ValidationError(f"Input file not found: {single_file}")
            
            self.stdout.write(f"[CALIBRATE] Single file mode: {single_file.name}")
            
            try:
                result = calibrate_image(str(single_file), dem=dem_str)
                if result:
                    self.stdout.write(self.style.SUCCESS(f"[CALIBRATE] Y Calibrated: {result}"))
                    
                    # Output info for next step
                    output_data = {
                        'calibrated_files': [result],
                        'dem_used': dem_str,
                        'source': str(single_file),
                        'mode': 'single'
                    }
                    output_path = single_file.parent / 'calibrated_files.json'
                    with open(output_path, 'w') as f:
                        json.dump(output_data, f, indent=2)
                    self.stdout.write(f"[CALIBRATE] Output info saved to: {output_path}")
                else:
                    self.stdout.write(self.style.ERROR(f"[CALIBRATE] âœ— Failed (no output)"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"[CALIBRATE] âœ— Error: {e}"))
                log.error(f"Calibration failed: {e}", exc_info=True)
            return
        
        # PAN-ONLY BATCH MODE - calibrate all PAN files individually
        if pan_only and input_dir:
            import re
            from multiprocessing import Pool
            
            input_dir = Path(input_dir)
            if not input_dir.exists():
                raise ValidationError(f"Input directory not found: {input_dir}")
            
            self.stdout.write(f"[CALIBRATE] PAN-only batch mode: {input_dir}")
            
            # Find all raster files
            raster_files = collect_geotiffs(input_dir)
            if not raster_files:
                raise ValidationError(f"No raster files found in {input_dir}")
            
            # Filter for PAN files only (P1BS, P00, P1B tokens)
            PAN_TOKENS = ["P1BS", "P00", "P1B"]
            pan_files = []
            for f in raster_files:
                stem = f.stem
                for tok in PAN_TOKENS:
                    pattern = rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])"
                    if re.search(pattern, stem):
                        pan_files.append(f)
                        break
            
            if not pan_files:
                raise ValidationError(f"No PAN files found in {input_dir}. Looking for files with P1BS, P00, or P1B in name.")
            
            self.stdout.write(f"[CALIBRATE] Found {len(pan_files)} PAN files to process")
            for f in pan_files[:5]:
                self.stdout.write(f"  {f.name}")
            if len(pan_files) > 5:
                self.stdout.write(f"  ... and {len(pan_files) - 5} more")
            
            # Process PAN files
            calibrated = []
            failed = []
            
            if processes > 1:
                # Parallel processing
                from functools import partial
                process_func = partial(calibrate_image, dem=dem_str)
                pan_paths = [str(f) for f in pan_files]
                
                with Pool(processes=processes) as pool:
                    results = pool.map(process_func, pan_paths)
                
                for f, result in zip(pan_files, results):
                    if result:
                        calibrated.append(result)
                        self.stdout.write(f"[CALIBRATE] ✓ {f.name}")
                    else:
                        failed.append(str(f))
                        self.stdout.write(self.style.ERROR(f"[CALIBRATE] ✗ {f.name}"))
            else:
                # Serial processing with progress
                for i, f in enumerate(pan_files, 1):
                    self.stdout.write(f"[CALIBRATE] [{i}/{len(pan_files)}] {f.name}")
                    try:
                        result = calibrate_image(str(f), dem=dem_str)
                        if result:
                            calibrated.append(result)
                            self.stdout.write(self.style.SUCCESS(f"[CALIBRATE]   ✓ Output: {Path(result).name}"))
                        else:
                            failed.append(str(f))
                            self.stdout.write(self.style.ERROR(f"[CALIBRATE]   ✗ Failed (no output)"))
                    except Exception as e:
                        failed.append(str(f))
                        self.stdout.write(self.style.ERROR(f"[CALIBRATE]   ✗ Error: {e}"))
                        log.error(f"Calibration failed for {f}: {e}", exc_info=True)
            
            # Summary
            self.stdout.write(f"\n[CALIBRATE] Complete: {len(calibrated)} succeeded, {len(failed)} failed")
            
            if calibrated:
                # Output info for next step
                output_data = {
                    'calibrated_files': calibrated,
                    'dem_used': dem_str,
                    'source': str(input_dir),
                    'mode': 'pan_only_batch',
                    'total_processed': len(pan_files),
                    'succeeded': len(calibrated),
                    'failed': len(failed)
                }
                output_path = input_dir / 'calibrated_files.json'
                with open(output_path, 'w') as f:
                    json.dump(output_data, f, indent=2)
                self.stdout.write(self.style.SUCCESS(f"[CALIBRATE] Results saved to: {output_path}"))
            
            if failed:
                self.stdout.write(self.style.WARNING("[CALIBRATE] Failed files:"))
                for f in failed[:5]:
                    self.stdout.write(f"  {Path(f).name}")
            return
        
        # PAIR MODE - get pairs from directory or file
        if input_dir:
            input_dir = Path(input_dir)
            if not input_dir.exists():
                raise ValidationError(f"Input directory not found: {input_dir}")
            
            self.stdout.write(f"[CALIBRATE] Scanning for pairs in: {input_dir}")
            
            # Find raster files and match pairs
            raster_files = collect_geotiffs(input_dir)
            if not raster_files:
                raise ValidationError(f"No raster files found in {input_dir}")
            
            self.stdout.write(f"[CALIBRATE] Found {len(raster_files)} raster files")
            
            pairs_dict, unmatched_pan, unmatched_msi = match_pan_ms_pairs([str(p) for p in raster_files])
            
            if not pairs_dict:
                raise ValidationError("No PAN/MSI pairs found")
            
            self.stdout.write(f"[CALIBRATE] Matched {len(pairs_dict)} pairs")
            
        elif pairs_file:
            pairs_file = Path(pairs_file)
            if not pairs_file.exists():
                raise ValidationError(f"Pairs file not found: {pairs_file}")
            
            self.stdout.write(f"[CALIBRATE] Loading pairs from: {pairs_file}")
            
            with open(pairs_file) as f:
                pairs_data = json.load(f)
            
            # Support both formats: {pan: msi} or {"pairs_full_paths": {pan: msi}}
            if 'pairs_full_paths' in pairs_data:
                pairs_dict = pairs_data['pairs_full_paths']
            elif 'pairs' in pairs_data:
                pairs_dict = pairs_data['pairs']
            else:
                pairs_dict = pairs_data
            
            if not pairs_dict:
                raise ValidationError("No pairs found in file")
            
            self.stdout.write(f"[CALIBRATE] Loaded {len(pairs_dict)} pairs")
        
        # Convert pairs dict to list of tuples for processing
        pairs_list = list(pairs_dict.items())
        
        self.stdout.write(f"[CALIBRATE] Starting calibration of {len(pairs_list)} pairs...")
        self.stdout.write(f"[CALIBRATE] Processes: {processes}")
        
        # Process pairs
        calibrated = []
        failed = []
        
        if processes > 1:
            # Parallel processing
            process_func = partial(calibrate_pair, dem=dem_str)
            with Pool(processes=processes) as pool:
                results = pool.map(process_func, pairs_list)
            
            for pair, result in zip(pairs_list, results):
                if result:
                    calibrated.append(result)
                    self.stdout.write(f"[CALIBRATE] Y {Path(pair[0]).name}")
                else:
                    failed.append(pair)
                    self.stdout.write(self.style.ERROR(f"[CALIBRATE] âœ— {Path(pair[0]).name}"))
        else:
            # Serial processing with progress
            for i, (pan, msi) in enumerate(pairs_list, 1):
                self.stdout.write(f"[CALIBRATE] [{i}/{len(pairs_list)}] {Path(pan).name}")
                try:
                    result = calibrate_pair((pan, msi), dem=dem_str)
                    if result:
                        calibrated.append(result)
                        self.stdout.write(self.style.SUCCESS(f"[CALIBRATE]   Y Success"))
                    else:
                        failed.append((pan, msi))
                        self.stdout.write(self.style.ERROR(f"[CALIBRATE]   âœ— Failed (no output)"))
                except Exception as e:
                    failed.append((pan, msi))
                    self.stdout.write(self.style.ERROR(f"[CALIBRATE]   âœ— Error: {e}"))
                    log.error(f"Calibration failed for {pan}: {e}", exc_info=True)
        
        # Summary
        self.stdout.write(f"\n[CALIBRATE] Complete: {len(calibrated)} succeeded, {len(failed)} failed")
        
        if calibrated:
            # Output calibrated pairs for next step
            output_data = {
                'calibrated_pairs': [(str(p), str(m)) for p, m in calibrated],
                'dem_used': dem_str,
                'source': str(input_dir or pairs_file)
            }
            
            # Determine output path
            if input_dir:
                output_path = input_dir / 'calibrated_pairs.json'
            else:
                output_path = pairs_file.parent / 'calibrated_pairs.json'
            
            with open(output_path, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            self.stdout.write(self.style.SUCCESS(f"[CALIBRATE] Calibrated pairs saved to: {output_path}"))
        
        if failed:
            self.stdout.write(self.style.WARNING("[CALIBRATE] Failed pairs:"))
            for pan, msi in failed[:5]:
                self.stdout.write(f"  {Path(pan).name}")

    def handle_organize(self, **options):
        """
        Organize downloaded zip files into catalog directory structure and unzip.
        
        1. Moves zip files from flat download directory into subdirectories named by Catalog ID
        2. Unzips the files in place (unless --no-unzip specified)
        
        Can use either:
        - A GeoJSON with 'local_path' column (from batch download)
        - Auto-match zip files to Entity IDs in the GeoJSON
        """
        import geopandas as gpd
        import shutil
        import zipfile
        
        log = get_logger()
        
        img_dir = Path(options['img_dir'])
        results_path = Path(options['results'])
        do_unzip = not options.get('no_unzip', False)
        
        if not img_dir.exists():
            raise ValidationError(f"Image directory not found: {img_dir}")
        
        if not results_path.exists():
            raise ValidationError(f"Results file not found: {results_path}")
        
        self.stdout.write(f"[ORGANIZE] Image directory: {img_dir}")
        self.stdout.write(f"[ORGANIZE] Results file: {results_path}")
        self.stdout.write(f"[ORGANIZE] Unzip after organizing: {do_unzip}")
        
        # Load results GeoJSON
        try:
            gdf = gpd.read_file(results_path)
        except Exception as e:
            raise ValidationError(f"Failed to load results: {e}")
        
        # Normalize column names
        col_map = {}
        for col in gdf.columns:
            if col.lower() == 'catalog id':
                col_map[col] = 'Catalog ID'
            elif col.lower() == 'entity id':
                col_map[col] = 'Entity ID'
        if col_map:
            gdf = gdf.rename(columns=col_map)
        
        # Check for required columns
        if 'Catalog ID' not in gdf.columns:
            raise ValidationError("Results file must have 'Catalog ID' column")
        
        if 'Entity ID' not in gdf.columns:
            raise ValidationError("Results file must have 'Entity ID' column")
        
        # Check if local_path exists; if not, auto-detect from directory
        if 'local_path' not in gdf.columns:
            self.stdout.write("[ORGANIZE] No 'local_path' column - auto-matching zip files to Entity IDs...")
            
            # Find all zip files in img_dir
            zip_files = list(img_dir.glob("*.zip"))
            self.stdout.write(f"[ORGANIZE] Found {len(zip_files)} zip files in {img_dir}")
            
            # Build entity ID to zip path mapping
            zip_map = {}
            for zf in zip_files:
                entity_id = zf.stem
                zip_map[entity_id] = str(zf)
            
            # Match to GeoJSON rows
            local_paths = []
            matched = 0
            for _, row in gdf.iterrows():
                entity_id = row['Entity ID']
                if entity_id in zip_map:
                    local_paths.append(zip_map[entity_id])
                    matched += 1
                else:
                    local_paths.append(None)
            
            gdf['local_path'] = local_paths
            self.stdout.write(f"[ORGANIZE] Matched {matched}/{len(gdf)} records to zip files")
            
            if matched == 0:
                self.stdout.write(self.style.WARNING("[ORGANIZE] No matches found!"))
                self.stdout.write("[ORGANIZE] Zip files found:")
                for zf in zip_files[:5]:
                    self.stdout.write(f"  {zf.name}")
                self.stdout.write("[ORGANIZE] Entity IDs in GeoJSON:")
                for eid in gdf['Entity ID'].head(5):
                    self.stdout.write(f"  {eid}")
                return
        
        self.stdout.write(f"[ORGANIZE] Processing {len(gdf)} records...")
        
        # Phase 1: Move zips to catalog directories
        organized = 0
        skipped = 0
        catalog_dirs = set()
        
        for _, row in gdf.iterrows():
            catalog_id = row['Catalog ID']
            local_path = row.get('local_path')
            
            if not local_path or local_path in ["NON_USGS_SKIP", None]:
                skipped += 1
                continue
            
            local_path = Path(local_path)
            if not local_path.exists():
                # Maybe already moved?
                catalog_dir = img_dir / catalog_id
                potential_dest = catalog_dir / local_path.name
                if potential_dest.exists():
                    self.stdout.write(f"[ORGANIZE] Already organized: {local_path.name}")
                    catalog_dirs.add(catalog_dir)
                    organized += 1
                    continue
                else:
                    self.stdout.write(self.style.WARNING(f"[ORGANIZE] File not found: {local_path}"))
                    skipped += 1
                    continue
            
            # Create catalog directory and move file
            catalog_dir = img_dir / catalog_id
            catalog_dir.mkdir(parents=True, exist_ok=True)
            catalog_dirs.add(catalog_dir)
            
            dest_path = catalog_dir / local_path.name
            shutil.move(str(local_path), str(dest_path))
            self.stdout.write(f"[ORGANIZE] {local_path.name} -> {catalog_id}/")
            organized += 1
        
        self.stdout.write(self.style.SUCCESS(f"[ORGANIZE] Organized: {organized} files, {skipped} skipped"))
        
        # Phase 2: Unzip files in catalog directories
        if do_unzip and catalog_dirs:
            self.stdout.write(f"\n[UNZIP] Unzipping files in {len(catalog_dirs)} catalog directories...")
            
            total_unzipped = 0
            total_errors = 0
            
            for catalog_dir in sorted(catalog_dirs):
                zip_files = list(catalog_dir.glob("*.zip"))
                if not zip_files:
                    continue
                    
                self.stdout.write(f"[UNZIP] {catalog_dir.name}: {len(zip_files)} zip files")
                
                for zf in zip_files:
                    try:
                        with zipfile.ZipFile(zf, 'r') as ref:
                            ref.extractall(catalog_dir)
                        self.stdout.write(f"[UNZIP]   Extracted: {zf.name}")
                        total_unzipped += 1
                    except (zipfile.BadZipFile, zipfile.LargeZipFile) as e:
                        self.stdout.write(self.style.ERROR(f"[UNZIP]   Error: {zf.name} - {e}"))
                        total_errors += 1
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"[UNZIP]   Error: {zf.name} - {e}"))
                        total_errors += 1
            
            self.stdout.write(self.style.SUCCESS(f"[UNZIP] Complete: {total_unzipped} extracted, {total_errors} errors"))
        
        self.stdout.write(self.style.SUCCESS("\n[ORGANIZE] All done!"))

    def handle_pair(self, **options):
        """
        Match PAN and MSI image pairs.
        
        Scans a directory for raster files (GeoTIFF and NITF) and matches 
        panchromatic (PAN) images with their corresponding multispectral (MSI) 
        counterparts using naming conventions (P1BS/M1BS, P00/M00, etc.).
        
        Works with:
        - Raw NITF files (.ntf) from USGS after unzip
        - Calibrated GeoTIFFs (.tif) after calibration
        """
        import json
        from animal.utils.utils import match_pan_ms_pairs, collect_geotiffs
        
        log = get_logger()
        
        input_dir = Path(options['input_dir'])
        output_path = options.get('output')
        
        if not input_dir.exists():
            raise ValidationError(f"Input directory not found: {input_dir}")
        
        self.stdout.write(f"[PAIR] Scanning: {input_dir}")
        
        # Use collect_geotiffs which handles .tif, .ntif, .ntf
        raster_files = collect_geotiffs(input_dir)
        
        if not raster_files:
            self.stdout.write(self.style.WARNING("[PAIR] No raster files found (.tif, .ntf, .ntif)"))
            # Show what files ARE there
            all_files = list(input_dir.rglob("*"))
            extensions = set(f.suffix.lower() for f in all_files if f.is_file())
            self.stdout.write(f"[PAIR] File extensions found: {extensions}")
            return
        
        self.stdout.write(f"[PAIR] Found {len(raster_files)} raster files")
        
        # Show file types found
        extensions = {}
        for f in raster_files:
            ext = f.suffix.lower()
            extensions[ext] = extensions.get(ext, 0) + 1
        self.stdout.write(f"[PAIR] File types: {extensions}")
        
        # Match pairs
        try:
            pairs, unmatched_pan, unmatched_msi = match_pan_ms_pairs([str(p) for p in raster_files])
        except Exception as e:
            raise CommandError(f"Pair matching failed: {e}")
        
        # Report results
        self.stdout.write(self.style.SUCCESS(f"[PAIR] Matched {len(pairs)} pairs"))
        
        if unmatched_pan:
            self.stdout.write(self.style.WARNING(f"[PAIR] Unmatched PAN: {len(unmatched_pan)}"))
        if unmatched_msi:
            self.stdout.write(self.style.WARNING(f"[PAIR] Unmatched MSI: {len(unmatched_msi)}"))
        
        # Output results
        if output_path:
            output_path = Path(output_path)
            results = {
                'pairs': pairs,
                'unmatched_pan': unmatched_pan,
                'unmatched_msi': unmatched_msi
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                json.dump(results, f, indent=2)
            self.stdout.write(self.style.SUCCESS(f"[PAIR] Results saved to: {output_path}"))
        else:
            # Print summary
            if pairs:
                self.stdout.write("\n[PAIR] Matched pairs:")
                for pan, msi in list(pairs.items())[:5]:
                    self.stdout.write(f"  PAN: {Path(pan).name}")
                    self.stdout.write(f"  MSI: {Path(msi).name}")
                    self.stdout.write("")
                if len(pairs) > 5:
                    self.stdout.write(f"  ... and {len(pairs) - 5} more pairs")
                self.stdout.write("\n[PAIR] Use -o/--output to save full results as JSON")

    def handle_status(self, **options):
        """
        Check USGS download status by label.
        
        Queries the USGS API to check if a previously requested download
        is ready. The label is returned from the download request.
        """
        import requests
        from animal.utils.api_utils import ee_login, retrieve_download
        
        log = get_logger()
        
        label = options['label']
        
        self.stdout.write(f"[STATUS] Checking download status for label: {label}")
        
        # Get credentials and authenticate
        username, token = self._get_usgs_credentials(options)
        
        try:
            session = requests.Session()
            session = ee_login(session, username, token)
        except Exception as e:
            raise AuthenticationError(f"USGS authentication failed: {e}")
        
        # Check status
        try:
            download_ids = retrieve_download(session, label)
            
            if download_ids:
                self.stdout.write(self.style.SUCCESS(f"[STATUS] Downloads available: {len(download_ids)}"))
                for did in download_ids:
                    self.stdout.write(f"  Download ID: {did}")
            else:
                self.stdout.write(self.style.WARNING("[STATUS] No downloads available yet"))
                
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"[STATUS] Could not retrieve status: {e}"))

    def handle_pansharpen(self, **options):
        """
        Pansharpen calibrated imagery pairs.
        
        Uses imagery_ops.pansharpen_imagery() which wraps GDAL pansharpening
        with memory-aware blocksize fallback.
        
        Band presets by sensor:
        - WV-3, WV-2: bands 5, 3, 2 (Red, Green, Blue from 8-band MSI)
        - GeoEye-1, QuickBird, IKONOS: bands 3, 2, 1 (4-band MSI)
        
        Input: calibrated GeoTIFFs from --input-dir or --calibrated-file
        Output: pansharpened GeoTIFFs in 'pansharpened/' subdirectory
        """
        import json
        from glob import glob
        from functools import partial
        from multiprocessing import Pool
        from animal.utils.utils import collect_geotiffs, match_pan_ms_pairs
        from animal.utils.imagery_ops import pansharpen_imagery
        
        log = get_logger()
        
        # Sensor band presets (RGB order)
        SENSOR_BANDS = {
            'wv3': [5, 3, 2],  # WorldView-3: 8-band MSI
            'wv2': [5, 3, 2],  # WorldView-2: 8-band MSI
            'ge1': [3, 2, 1],  # GeoEye-1: 4-band MSI
            'qb': [3, 2, 1],   # QuickBird: 4-band MSI
            'ik': [3, 2, 1],   # IKONOS: 4-band MSI
        }
        
        input_dir = options.get('input_dir')
        calibrated_file = options.get('calibrated_file')
        sensor = options.get('sensor')
        bands = options.get('bands', [5, 3, 2])
        processes = options.get('processes', 1)
        
        # Apply sensor preset if specified
        if sensor:
            bands = SENSOR_BANDS.get(sensor, bands)
            self.stdout.write(f"[PANSHARPEN] Using {sensor.upper()} preset bands: {bands}")
        else:
            self.stdout.write(f"[PANSHARPEN] Bands: {bands}")
        
        # Get calibrated pairs
        if calibrated_file:
            calibrated_file = Path(calibrated_file)
            if not calibrated_file.exists():
                raise ValidationError(f"Calibrated file not found: {calibrated_file}")
            
            self.stdout.write(f"[PANSHARPEN] Loading pairs from: {calibrated_file}")
            
            with open(calibrated_file) as f:
                data = json.load(f)
            
            if 'calibrated_pairs' in data:
                pairs_list = [tuple(p) for p in data['calibrated_pairs']]
            else:
                raise ValidationError("Expected 'calibrated_pairs' key in JSON")
            
            self.stdout.write(f"[PANSHARPEN] Loaded {len(pairs_list)} pairs")
            
        elif input_dir:
            input_dir = Path(input_dir)
            if not input_dir.exists():
                raise ValidationError(f"Input directory not found: {input_dir}")
            
            self.stdout.write(f"[PANSHARPEN] Scanning for calibrated pairs in: {input_dir}")
            
            # Look for calibrated TIFFs (in 'calibrated' subdirectories)
            calibrated_tifs = list(input_dir.rglob('**/calibrated/*.tif'))
            
            if not calibrated_tifs:
                # Fall back to all TIFs in directory
                calibrated_tifs = collect_geotiffs(input_dir)
            
            if not calibrated_tifs:
                raise ValidationError(f"No calibrated GeoTIFFs found in {input_dir}")
            
            self.stdout.write(f"[PANSHARPEN] Found {len(calibrated_tifs)} calibrated files")
            
            # Match pairs
            pairs_dict, unmatched_pan, unmatched_msi = match_pan_ms_pairs([str(p) for p in calibrated_tifs])
            
            if not pairs_dict:
                raise ValidationError("No PAN/MSI pairs found in calibrated files")
            
            pairs_list = list(pairs_dict.items())
            self.stdout.write(f"[PANSHARPEN] Matched {len(pairs_list)} pairs")
        else:
            raise ValidationError("Either --input-dir or --calibrated-file is required")
        
        # Validate pairs exist
        for pan, msi in pairs_list:
            if not Path(pan).exists():
                raise ValidationError(f"PAN file not found: {pan}")
            if not Path(msi).exists():
                raise ValidationError(f"MSI file not found: {msi}")
        
        self.stdout.write(f"[PANSHARPEN] Starting pansharpening of {len(pairs_list)} pairs...")
        self.stdout.write(f"[PANSHARPEN] Processes: {processes}")
        
        # Process pairs
        pansharpened = []
        failed = []
        
        if processes > 1:
            # Parallel processing
            process_func = partial(pansharpen_imagery, bands=bands)
            with Pool(processes=processes) as pool:
                results = pool.map(process_func, pairs_list)
            
            for pair, result in zip(pairs_list, results):
                if result:
                    pansharpened.append(result)
                    self.stdout.write(f"[PANSHARPEN] Y {Path(pair[0]).name}")
                else:
                    failed.append(pair)
                    self.stdout.write(self.style.ERROR(f"[PANSHARPEN] âœ— {Path(pair[0]).name}"))
        else:
            # Serial processing with progress
            for i, (pan, msi) in enumerate(pairs_list, 1):
                self.stdout.write(f"[PANSHARPEN] [{i}/{len(pairs_list)}] {Path(pan).name}")
                try:
                    result = pansharpen_imagery((pan, msi), bands=bands)
                    if result:
                        pansharpened.append(result)
                        self.stdout.write(self.style.SUCCESS(f"[PANSHARPEN]   Y Output: {Path(result).name}"))
                    else:
                        failed.append((pan, msi))
                        self.stdout.write(self.style.ERROR(f"[PANSHARPEN]   âœ— Failed (no output)"))
                except Exception as e:
                    failed.append((pan, msi))
                    self.stdout.write(self.style.ERROR(f"[PANSHARPEN]   âœ— Error: {e}"))
                    log.error(f"Pansharpening failed for {pan}: {e}", exc_info=True)
        
        # Summary
        self.stdout.write(f"\n[PANSHARPEN] Complete: {len(pansharpened)} succeeded, {len(failed)} failed")
        
        if pansharpened:
            # Output pansharpened paths for next step
            output_data = {
                'pansharpened_files': pansharpened,
                'bands_used': bands,
                'source': str(input_dir or calibrated_file)
            }
            
            # Determine output path
            if input_dir:
                output_path = input_dir / 'pansharpened_files.json'
            else:
                output_path = calibrated_file.parent / 'pansharpened_files.json'
            
            with open(output_path, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            self.stdout.write(self.style.SUCCESS(f"[PANSHARPEN] Results saved to: {output_path}"))
        
        if failed:
            self.stdout.write(self.style.WARNING("[PANSHARPEN] Failed pairs:"))
            for pan, msi in failed[:5]:
                self.stdout.write(f"  {Path(pan).name}")

    def handle_cog(self, **options):
        """
        Create Cloud Optimized GeoTIFFs from pansharpened or calibrated imagery.
        
        Uses imagery_ops.create_single_cog() / run_cog_creation() which wraps
        rio cogeo with retry logic and blocksize fallback.
        
        Input: pansharpened GeoTIFFs from --input or --input-dir
               OR calibrated GeoTIFFs with --from-calibrated (PAN-only workflow)
        Output: COGs in 'cogs/' subdirectory
        """
        import json
        from glob import glob
        from multiprocessing import Pool
        from animal.utils.imagery_ops import create_single_cog, run_cog_creation
        
        log = get_logger()
        
        single_input = options.get('input')
        input_dir = options.get('input_dir')
        output_dir = options.get('output_dir')
        processes = options.get('processes', 2)
        from_calibrated = options.get('from_calibrated', False)
        
        # Collect files to process
        if single_input:
            single_input = Path(single_input)
            if not single_input.exists():
                raise ValidationError(f"Input file not found: {single_input}")
            
            files_to_process = [str(single_input)]
            self.stdout.write(f"[COG] Single file: {single_input}")
            
        elif input_dir:
            input_dir = Path(input_dir)
            if not input_dir.exists():
                raise ValidationError(f"Input directory not found: {input_dir}")
            
            if from_calibrated:
                # PAN-only workflow: process calibrated files directly
                self.stdout.write(f"[COG] Scanning for calibrated files in: {input_dir}")
                
                calibrated_files = list(input_dir.rglob('**/calibrated/*.tif'))
                
                if not calibrated_files:
                    raise ValidationError(f"No calibrated GeoTIFF files found in {input_dir}")
                
                # Filter out existing COGs only
                files_to_process = []
                for f in calibrated_files:
                    if 'cogs' not in str(f):
                        files_to_process.append(str(f))
                
                if not files_to_process:
                    self.stdout.write(self.style.WARNING("[COG] All calibrated files appear to already be COGs"))
                    return
                
                self.stdout.write(f"[COG] Found {len(files_to_process)} calibrated files to process")
            else:
                # Normal workflow: process pansharpened files
                self.stdout.write(f"[COG] Scanning for pansharpened files in: {input_dir}")
                
                # Look for pansharpened TIFFs
                pansharpened_files = list(input_dir.rglob('**/pansharpened/*.tif'))
                
                if not pansharpened_files:
                    # Fall back to all TIFs in directory
                    pansharpened_files = list(input_dir.rglob('*.tif'))
                
                if not pansharpened_files:
                    raise ValidationError(f"No GeoTIFF files found in {input_dir}")
                
                # Filter out existing COGs and calibrated files
                files_to_process = []
                for f in pansharpened_files:
                    if 'cogs' not in str(f) and 'calibrated' not in str(f):
                        files_to_process.append(str(f))
                
                if not files_to_process:
                    self.stdout.write(self.style.WARNING("[COG] All files appear to already be COGs or calibrated"))
                    self.stdout.write("[COG] Hint: Use --from-calibrated for PAN-only workflows")
                    return
                
                self.stdout.write(f"[COG] Found {len(files_to_process)} files to process")
        else:
            raise ValidationError("Either --input or --input-dir is required")
        
        # Check for existing COGs to skip
        if input_dir:
            existing_cogs = list(input_dir.rglob('**/cogs/*.tif'))
            existing_cog_names = {Path(c).name for c in existing_cogs}
            
            remaining = []
            for f in files_to_process:
                if Path(f).name not in existing_cog_names:
                    remaining.append(f)
                else:
                    self.stdout.write(f"[COG] Skipping (already exists): {Path(f).name}")
            
            if not remaining:
                self.stdout.write(self.style.SUCCESS("[COG] All files already have COGs"))
                return
            
            files_to_process = remaining
            self.stdout.write(f"[COG] {len(files_to_process)} files remaining after skip check")
        
        self.stdout.write(f"[COG] Starting COG creation for {len(files_to_process)} files...")
        self.stdout.write(f"[COG] Processes: {processes}")
        
        # Use run_cog_creation for batch processing
        if len(files_to_process) > 1 and processes > 1:
            self.stdout.write("[COG] Using batch processing...")
            try:
                run_cog_creation(primary_list=files_to_process, processes=processes)
                self.stdout.write(self.style.SUCCESS(f"[COG] Batch processing complete"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"[COG] Batch processing failed: {e}"))
                log.error(f"COG batch creation failed: {e}", exc_info=True)
        else:
            # Serial processing with progress
            created = []
            failed = []
            
            for i, f in enumerate(files_to_process, 1):
                self.stdout.write(f"[COG] [{i}/{len(files_to_process)}] {Path(f).name}")
                try:
                    result = create_single_cog(f)
                    if result:
                        created.append(result)
                        self.stdout.write(self.style.SUCCESS(f"[COG]   Y Created: {Path(result).name}"))
                    else:
                        failed.append(f)
                        self.stdout.write(self.style.ERROR(f"[COG]   âœ— Failed (no output)"))
                except Exception as e:
                    failed.append(f)
                    self.stdout.write(self.style.ERROR(f"[COG]   âœ— Error: {e}"))
                    log.error(f"COG creation failed for {f}: {e}", exc_info=True)
            
            # Summary
            self.stdout.write(f"\n[COG] Complete: {len(created)} succeeded, {len(failed)} failed")
            
            if created:
                self.stdout.write(self.style.SUCCESS(f"[COG] COGs created in 'cogs/' subdirectories"))
            
            if failed:
                self.stdout.write(self.style.WARNING("[COG] Failed files:"))
                for f in failed[:5]:
                    self.stdout.write(f"  {Path(f).name}")

    def handle_upload(self, **options):
        """
        Upload COGs to Azure Blob Storage.
        
        Uses utils.upload_to_azure() which handles:
        - Filtering for .tif files (excludes 'tmp' in filename)
        - Strips '_cog.tif' suffix for cleaner blob names
        - Sets content_type='image/tiff'
        - Overwrites existing blobs
        
        Credentials come from settings (not CLI) per security policy.
        """
        from animal.utils.utils import upload_to_azure
        
        log = get_logger()
        
        input_dir = Path(options.get('input_dir'))
        azure_dir = options.get('azure_dir', 'cogs')
        container_override = options.get('container')
        dry_run = options.get('dry_run', False)
        
        # Validate input directory
        if not input_dir.exists():
            raise ValidationError(f"Input directory not found: {input_dir}")
        
        if not input_dir.is_dir():
            raise ValidationError(f"Path is not a directory: {input_dir}")
        
        # Collect .tif files (matching upload_to_azure filter logic)
        candidates = list(input_dir.glob("*.tif"))
        tif_files = [p for p in candidates if "tmp" not in p.name.lower()]
        
        if not tif_files:
            self.stdout.write(self.style.WARNING(f"[UPLOAD] No .tif files found in {input_dir}"))
            return
        
        self.stdout.write(f"[UPLOAD] Found {len(tif_files)} .tif files in {input_dir}")
        
        # Get Azure credentials from settings
        account_name, account_key, container_name = self._get_azure_credentials()
        
        # Allow container override
        if container_override:
            container_name = container_override
            self.stdout.write(f"[UPLOAD] Using container override: {container_name}")
        
        # Show what will be uploaded
        self.stdout.write(f"[UPLOAD] Target: {account_name}/{container_name}/{azure_dir}/")
        self.stdout.write("[UPLOAD] Files to upload:")
        for f in tif_files:
            # Show the blob name that will be created (matches upload_to_azure logic)
            blob_name = f"{azure_dir}/{f.name}"
            if "_cog.tif" in blob_name:
                blob_name = blob_name.replace("_cog.tif", ".tif")
            self.stdout.write(f"  {f.name} -> {blob_name}")
        
        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"\n[UPLOAD] Dry run complete. {len(tif_files)} files would be uploaded."))
            return
        
        # Execute upload
        self.stdout.write(f"\n[UPLOAD] Starting upload of {len(tif_files)} files...")
        
        try:
            upload_to_azure(
                account_name=account_name,
                account_key=account_key,
                container_name=container_name,
                local_dir=input_dir,
                azure_dir=azure_dir
            )
            self.stdout.write(self.style.SUCCESS(f"[UPLOAD] Successfully uploaded {len(tif_files)} files"))
            log.info(f"[UPLOAD] Completed: {len(tif_files)} files to {container_name}/{azure_dir}/")
        except Exception as e:
            log.error(f"[UPLOAD] Failed: {e}", exc_info=True)
            raise CommandError(f"Upload failed: {e}")

    def _get_azure_credentials(self):
        """
        Get Azure Storage credentials from settings.
        
        Checks Django settings first, then falls back to pipeline config.
        Never accepts credentials from CLI arguments (security policy).
        
        Returns:
            tuple: (account_name, account_key, container_name)
        """
        pipeline_settings = get_pipeline_settings()
        
        # Try Django settings first
        account_name = getattr(django_settings, 'AZURE_STORAGE_ACCOUNT_NAME', None)
        account_key = getattr(django_settings, 'AZURE_STORAGE_ACCOUNT_KEY', None)
        container_name = getattr(django_settings, 'AZURE_CONTAINER_NAME', None)
        
        # Fall back to pipeline settings
        if account_name is None and pipeline_settings:
            account_name = getattr(pipeline_settings, 'azure_storage_account', None)
        if account_key is None and pipeline_settings:
            account_key = getattr(pipeline_settings, 'azure_storage_key', None)
        if container_name is None and pipeline_settings:
            container_name = getattr(pipeline_settings, 'azure_container', 'data')
        
        # Default container if still None
        if container_name is None:
            container_name = 'data'
        
        if not account_name or not account_key:
            raise AuthenticationError(
                "Azure Storage credentials not configured. "
                "Set AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_ACCOUNT_KEY in secrets.json"
            )
        
        return account_name, account_key, container_name

    def handle_pipeline(self, **options):
        """Launch full pipeline. (Phase 5 - Not yet implemented)"""
        raise CommandError("Pipeline action not yet implemented (Phase 5)")