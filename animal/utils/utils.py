  # ------------------------------------------------------------------------------
  # ----- utils.py ---------------------------------------------------------------
  # ------------------------------------------------------------------------------
  #
  #    author:   John Wall (john.wall@noaa.gov)
  #
  #    purpose:  Reusable utility functions for the GAIA pipeline. Retry
  #              decorator, file management for imagery downloads, GeoTIFF
  #              organization, and COG copying.
  #
  #    tickets: GAIFAGP-473 (datetime.now() -> timezone.now())
  #             GAIFAGP-522 (TeeWriter extraction target)
  #
  #    SOURCE OF TRUTH ASSUMPTIONS:
  #      - IMAGERY_DIR (settings) is the base path for all file operations
  #      - COG output directory structure mirrors input structure
  #      - Retry decorator assumes transient failures (network, I/O)
  #
  #    usage:    from animal.utils.utils import retry
  #
  # ------------------------------------------------------------------------------

import os
import re
import sys
import time
import shutil
import zipfile
from zipfile import BadZipFile, LargeZipFile
import multiprocessing
import math
import json
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from glob import glob
from typing import List, Tuple, Optional
from pathlib import Path
import geopandas as gpd

from django.utils import timezone

# Optional psutil import for system monitoring
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
from azure.storage.blob import BlobServiceClient, ContentSettings

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from animal.utils.git_utils import clone_imagery_utils
from animal.utils.logging import get_animal_logger

logger = get_animal_logger(__name__)


class TeeWriter:
    """
    Write to both console and file simultaneously.

    Strips ANSI codes when writing to file for clean logs.
    When quiet=True, suppresses console output (file only).
    """

    _ansi_pattern = re.compile(r'\x1b\[[0-9;]*m')

    def __init__(self, console, file, quiet=False):
        """Initialize dual-output writer.

        Args:
            console: Primary output stream (stdout).
            file: Secondary file output stream.
            quiet: If True, suppress console output.
        """
        self.console = console
        self.file = file
        self.quiet = quiet

    def write(self, message):
        """Write message to console and file.

        Args:
            message: Text to write. ANSI codes are
                stripped for file output.
        """
        if not self.quiet:
            self.console.write(message)
        clean_message = self._ansi_pattern.sub('', message)
        self.file.write(clean_message + "\n")

    def flush(self):
        """Flush both output streams."""
        if hasattr(self.console, 'flush'):
            self.console.flush()
        self.file.flush()


def retry(max_retries: int = 3, wait_seconds: int = 10):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Re-import inside the wrapper to ensure it's available in Celery subprocesses
            from animal.utils.logging import get_animal_logger
            logger = get_animal_logger(__name__)
            
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"[Retry {attempt}/{max_retries}] {func.__name__} failed with: {e}")
                    time.sleep(wait_seconds)
            logger.error(f"{func.__name__} failed after {max_retries} retries. Returning None.")
            return None
        return wrapper
    return decorator


def get_task_tag(self):
    return self.name.split('.')[-1].upper()


def ensure_pgc_utils(func):
    def wrapper(*args, **kwargs):

        # Ensure project root is in sys.path
        project_root = Path(__file__).resolve().parents[1]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from animal.utils.git_utils import clone_imagery_utils
        from pathlib import Path

        base_dir = Path(__file__).resolve().parent.parent
        external_dir = base_dir / "external" / "imagery_utils"

        # Ensure parent directory exists before cloning
        external_dir.parent.mkdir(parents=True, exist_ok=True)

        clone_imagery_utils(external_dir)
        return func(*args, **kwargs)
    return wrapper


def move_zip_to_catalog(img_dir: Path, gdf):
    """
    Organizes .zip files into subdirectories by Catalog ID.
    Handles both single zip files and MSI/PAN pairs (semicolon-separated paths).

    Args:
        img_dir (Path): Path to imagery directory containing .zip files.
        gdf (GeoDataFrame): Metadata containing 'Catalog ID', 'Entity ID', and 'local_path'.
    """
    logger = get_animal_logger(__name__)

    for _, row in gdf.iterrows():
        catalog_id = row['Catalog ID']
        local_path = row.get('local_path')
        
        if not local_path or local_path in ["NON_USGS_SKIP", None]:
            logger.info(f"[ORGANIZED] Skipping row with no valid local_path: {local_path}")
            continue

        catalog_dir = img_dir / catalog_id
        catalog_dir.mkdir(parents=True, exist_ok=True)

        # Handle semicolon-separated MSI/PAN pairs
        if ';' in str(local_path):
            paths = str(local_path).split(';')
            logger.info(f"[ORGANIZED] Processing MSI/PAN pair for catalog {catalog_id}")
            for path_str in paths:
                path_str = path_str.strip()
                if path_str:
                    zip_path = Path(path_str)
                    if zip_path.exists():
                        dest_path = catalog_dir / zip_path.name
                        shutil.move(str(zip_path), str(dest_path))
                        logger.info(f"[ORGANIZED] Moved {zip_path.name} to {catalog_id}")
                    else:
                        logger.warning(f"[ORGANIZED] Zip file not found: {zip_path}")
        else:
            # Handle single zip file (original behavior)
            zip_path = Path(local_path)
            if zip_path.exists():
                dest_path = catalog_dir / zip_path.name
                shutil.move(str(zip_path), str(dest_path))
                logger.info(f"[ORGANIZED] Moved {zip_path.name} to {catalog_id}")
            else:
                # Fallback to old entity_id naming convention
                entity_id = row['Entity ID']
                zip_path = img_dir / f"{entity_id}.zip"
                if zip_path.exists():
                    dest_path = catalog_dir / zip_path.name
                    shutil.move(str(zip_path), str(dest_path))
                    logger.info(f"[ORGANIZED] Moved {zip_path.name} to {catalog_id} (fallback)")
                else:
                    logger.warning(f"[ORGANIZED] Zip file not found: {zip_path} or {local_path}")
                    
    logger.info(f"[ORGANIZED] Zipped files have been organized")


    
def verify_zip_file(path: Path) -> bool:
    """Return True if path is a readable ZIP archive with a valid header."""
    try:
        if not path.exists() or not path.is_file():
            return False
        # Quick signature check
        with open(path, 'rb') as f:
            sig = f.read(4)
            if sig != b'PK\x03\x04':
                return False
        # Deep check
        with zipfile.ZipFile(path, 'r') as ref:
            ref.namelist()  # force read central directory
        return True
    except Exception:
        return False


def unzip_to_loading_events(
    img_dir: Path,
    limit_catalogs: Optional[set] = None,
    fail_fast: bool = False,
    lock: bool = True,
    lock_timeout_sec: int = 30
):
    """Unzip only the catalog directories relevant to current task.

    Args:
        img_dir: Base imagery directory.
        limit_catalogs: Optional set of catalog IDs to restrict processing.
        fail_fast: If True, raise immediately on first bad zip. Default False (skip and continue).
        lock: Acquire a simple process lock to avoid concurrent unzip collisions.
        lock_timeout_sec: Seconds to wait acquiring lock before proceeding without it.
    """
    logger = get_animal_logger(__name__)

    lock_path = img_dir / ".unzip.lock"
    lock_fd = None
    if lock:
        for attempt in range(lock_timeout_sec):
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(lock_fd, str(os.getpid()).encode())
                break
            except FileExistsError:
                time.sleep(1)
        else:
            logger.warning(f"[UNZIP] Could not acquire lock after {lock_timeout_sec}s; proceeding anyway")

    try:
        cat_id_dirs = []
        for d in img_dir.iterdir():
            if not d.is_dir():
                continue
            name = d.name
            # Skip non-catalog directories (catalog IDs are 16 digit strings)
            if not (name.isdigit() and len(name) == 16):
                # If limit is provided and explicit, still allow explicit names
                if limit_catalogs and name in limit_catalogs:
                    pass
                else:
                    continue
            if limit_catalogs and name not in limit_catalogs:
                continue
            cat_id_dirs.append(d)

        logger.info(f"[UNZIP] Scanning {len(cat_id_dirs)} catalog dirs under {img_dir}")

        total_bad = 0
        total_ok = 0
        bad_examples = []

        for cat_dir in cat_id_dirs:
            zips = list(cat_dir.glob("**/*.zip"))
            logger.info(f"[UNZIP] {cat_dir.name}: found {len(zips)} zips")
            for z in zips:
                try:
                    with zipfile.ZipFile(z, 'r') as ref:
                        ref.extractall(cat_dir)
                    total_ok += 1
                except (BadZipFile, LargeZipFile) as e:
                    total_bad += 1
                    if len(bad_examples) < 3:
                        bad_examples.append(str(z))
                    logger.error(f"[UNZIP] Bad/large zip {z}: {e}")
                    if fail_fast:
                        raise
                    # Skip and continue
                    continue
                except Exception as e:
                    logger.error(f"[UNZIP] Failed extracting {z}: {e}", exc_info=True)
                    if fail_fast:
                        raise
                    total_bad += 1
                    continue

            vendor_dirs = [d for d in cat_dir.iterdir() if d.is_dir()]
            loading_codes = set(
                d.name.split('-')[-1].split('_')[0]
                for d in vendor_dirs if d.is_dir()
            )
            digits_only = [code for code in loading_codes if code.isdigit()]
            logger.info(f"[UNZIP] {cat_dir.name}: grouping into {len(digits_only)} loading codes")

            for code in digits_only:
                group_dir = cat_dir / code
                group_dir.mkdir(exist_ok=True)
                for vendor_dir in vendor_dirs:
                    if code in vendor_dir.name and vendor_dir != group_dir:
                        try:
                            shutil.move(str(vendor_dir), str(group_dir))
                        except shutil.Error as e:
                            logger.warning(f"[UNZIP] Skip moving {vendor_dir} -> {group_dir}: {e}")

        logger.info(f"[UNZIPPED] Completed unzip. ok={total_ok} bad={total_bad}")
        if total_ok == 0 and total_bad > 0:
            raise RuntimeError(f"All zips failed to extract. Examples: {bad_examples}")
    except Exception:
        logger.error(f"[UNZIP] Fatal error in unzip_to_loading_events({img_dir})", exc_info=True)
        raise
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass


def filter_to_pan_msi_pair(results_json: str, usgs_username: str, token: str) -> str:
    """
    Filters GeoJSON results to the first PAN/MSI image pair and returns a new payload.

    Args:
        results_json (str): JSON string from the search task, containing the 'results' path to a GeoJSON file.
        usgs_username (str): EarthExplorer username to retain in payload.
        token (str): EarthExplorer token for session reuse.

    Returns:
        str: Serialized JSON string with filtered results and credentials for next task.
    """
    # Parse and load the results
    results = json.loads(results_json)
    results_gdf = gpd.read_file(results["results"])

    # --- Identify PAN/MSI Pair ---
    first = results_gdf.iloc[0]
    first_vendor_id = first["Vendor ID"]

    if "M1BS" in first_vendor_id:
        img_pair = first_vendor_id.replace("M1BS", "P1BS")
    elif "P1BS" in first_vendor_id:
        img_pair = first_vendor_id.replace("P1BS", "M1BS")
    else:
        raise ValueError(f"Cannot identify PAN/MSI pair from Vendor ID: {first_vendor_id}")

    if img_pair not in results_gdf["Vendor ID"].values:
        raise ValueError(f"Expected PAN/MSI pair {img_pair} not found in results")

    # --- Filter to the pair ---
    subset_gdf = results_gdf[
        (results_gdf["Vendor ID"] == first_vendor_id) | (results_gdf["Vendor ID"] == img_pair)
    ].copy()

    # Log filtering results
    logging_msg = f"Selected PAN/MSI pair: {first_vendor_id} and {img_pair}"
    logger.info(f"[FILTER] {logging_msg}")
    logger.info(f"[FILTER] Filtered from {len(results_gdf)} images to {len(subset_gdf)} images")

    # --- Convert datetime columns to ISO format ---
    datetime_cols = subset_gdf.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns
    for col in datetime_cols:
        subset_gdf[col] = subset_gdf[col].dt.strftime("%Y-%m-%dT%H:%M:%S")

    # --- Save filtered results to a new GeoJSON file ---
    # FIX (GAIFAGP-473 / Utilities Audit CRIT-002):
    # Was: datetime.now().strftime(...) — naive datetime
    # Now: timezone.now() for consistent timezone-aware timestamps
    current_date = timezone.now().strftime('%Y-%m-%d')
    filtered_filename = f"belugas_filtered_{current_date}.geojson"
    filtered_path = Path("/app/gis/data/geojson/belugas") / filtered_filename
    filtered_path.parent.mkdir(parents=True, exist_ok=True)
    
    subset_gdf.to_file(filtered_path, driver='GeoJSON')
    logger.info(f"[FILTER] Filtered PAN/MSI pair saved to: {filtered_path}")  # Already logged pair info above

    # --- Prepare new payload ---
    new_payload = {
        "results": str(filtered_path),
        "usgs_username": usgs_username,
        "token": token
    }

    return json.dumps(new_payload)


def match_pan_ms_pairs(geotiffs: List[str]) -> Tuple[dict, List[str], List[str]]:
    """Match PAN & MSI geotiffs across multiple naming conventions.

    Supported patterns (case-sensitive substring checks):
      * Maxar standard: P1BS / M1BS
      * Alternate entity style: P00  / M00 (observed in WV3 bundles)
      * Fallback: P1B / M1B (if truncated)

    Returns
    -------
    dict : {pan_path: msi_path}
    list : unmatched pan paths
    list : unmatched msi paths
    """
    logger = get_animal_logger(__name__)
    from pathlib import Path
    import re

    # Define token groups so we can transform between PAN and MSI tokens
    PAN_TOKENS = ["P1BS", "P00", "P1B"]
    MSI_TOKENS = ["M1BS", "M00", "M1B"]
    TOKEN_PAIRS = list(zip(PAN_TOKENS, MSI_TOKENS))  # positional correspondence

    def classify(stem: str) -> str:
        """Classify filename stem as PAN / MSI / UNKNOWN.

        Uses regex with token boundary rules so we don't get false positives
        from substrings (e.g. PAN token 'P00' incorrectly matching the scene
        designator 'P004' at the end of many filenames). A token must be
        bounded by a non-alphanumeric character (start/end, '-', '_') on both
        sides. This keeps support for the alternate naming styles while
        preventing over-classification of MSI files as PAN.
        """
        # Delimiters considered to bound a token. Start/end of string also ok.
        for tok in PAN_TOKENS:
            pattern = rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])"
            if re.search(pattern, stem):
                return "PAN"
        for tok in MSI_TOKENS:
            pattern = rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])"
            if re.search(pattern, stem):
                return "MSI"
        return "UNKNOWN"

    pans_map: dict[str, str] = {}
    multis_map: dict[str, str] = {}
    unknown: list[str] = []

    for tif in geotiffs:
        p = Path(tif)
        stem = p.stem
        role = classify(stem)
        if role == "PAN":
            pans_map[stem] = str(p)
        elif role == "MSI":
            multis_map[stem] = str(p)
        else:
            unknown.append(str(p))

    pairs: dict[str, str] = {}
    unmatched_pan: list[str] = []
    unmatched_ms: list[str] = list(multis_map.values())

    # Attempt matching for each PAN by substituting its token with the aligned MSI token
    for pan_stem, pan_full in pans_map.items():
        matched = False
        for pan_tok, msi_tok in TOKEN_PAIRS:
            if pan_tok in pan_stem:
                candidate_stem = pan_stem.replace(pan_tok, msi_tok)
                match_full = multis_map.get(candidate_stem)
                if match_full:
                    pairs[pan_full] = match_full
                    try:
                        unmatched_ms.remove(match_full)
                    except ValueError:
                        pass
                    matched = True
                    break
        if not matched:
            unmatched_pan.append(pan_full)

    logger.info(
        f"[MATCHING] total_input={len(geotiffs)} pan={len(pans_map)} msi={len(multis_map)} unknown={len(unknown)} matched={len(pairs)}"
    )
    if unknown:
        sample = unknown[:5]
        logger.warning(f"[MATCHING] Unknown TIFF naming (first {len(sample)} shown): {sample}")
    logger.warning(f"[UNMATCHED] PAN missing MSI: {len(unmatched_pan)} | MSI missing PAN: {len(unmatched_ms)}")
    return pairs, unmatched_pan, unmatched_ms


def copy_subdir_tiffs_to_flat_dir(data_dir: Path, subdir: str, outdir_name: str):
    """
    Copies all .tif files found under */[subdir]/*.tif to a flat directory [data_dir]/[outdir_name].

    Args:
        data_dir (Path): Root imagery directory.
        subdir (str): Name of subdirectory to filter (e.g., 'pansharpened', 'cogs').
        outdir_name (str): Name of flat output directory (e.g., 'pan', 'cogs').
    """
    logger = get_animal_logger(__name__)

    search_path = str(data_dir / '**' / subdir / '*.tif')
    tif_paths = glob(search_path, recursive=True)
    out_dir = data_dir / outdir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    for tif_path in tif_paths:
        tif_path = Path(tif_path)
        out_path = out_dir / tif_path.name
        shutil.copy2(tif_path, out_path)
        logger.info(f"[COPY] {tif_path.name} -> {out_path}")


def upload_to_azure(account_name: str, account_key: str, container_name: str, local_dir: Path, azure_dir: str = 'cogs'):
    logger = get_animal_logger(__name__)
    local_dir = Path(local_dir)

    try:
        account_url = f"https://{account_name}.blob.core.windows.net"
        blob_service_client = BlobServiceClient(account_url=account_url, credential=account_key)
        container_client = blob_service_client.get_container_client(container_name)

        if not local_dir.exists() or not local_dir.is_dir():
            logger.error(f"[AZURE] Provided path is not a valid directory: {local_dir}")
            print(f"[ERROR] [AZURE] Provided path is not a valid directory: {local_dir}")
            raise NotADirectoryError(f"Provided path is not a valid directory: {local_dir}")

        candidates = list(local_dir.glob("*.tif"))
        local_tifs = [p for p in candidates if "tmp" not in p.name.lower()]
        logger.info(f"[AZURE] Uploading {[os.path.basename(local_tif.name) for local_tif in local_tifs]}")
        print(f"[INFO] [AZURE] Uploading {[os.path.basename(local_tif.name) for local_tif in local_tifs]}")

        if not local_tifs:
            logger.warning(f"[AZURE] No .tif files found in {local_dir}")
            print(f"[WARNING] [AZURE] No .tif files found in {local_dir}")
            return

        for local_file in local_tifs:
            blob_name = f"{azure_dir}/{local_file.name}"
            if "_cog.tif" in blob_name:
                blob_name = blob_name.replace("_cog.tif", ".tif")

            with open(local_file, 'rb') as data:
                blob_client = container_client.get_blob_client(blob=blob_name)
                content_settings = ContentSettings(content_type='image/tiff')
                blob_client.upload_blob(data, content_settings=content_settings, overwrite=True, timeout=900)
            logger.info(f"[AZURE] Uploaded {local_file.name} to blob '{blob_name}'")
            print(f"[INFO] [AZURE] Uploaded {local_file.name} to blob '{blob_name}'")

    except Exception as e:
        logger.critical(f"[AZURE] Upload process failed entirely: {e}", exc_info=True)
        print(f"[CRITICAL] [AZURE] Upload process failed entirely: {e}")
        raise


def collect_geotiffs(path: Path, subdir: Optional[str] = None, extensions=(".tif", ".ntif", ".ntf")) -> List[Path]:
    """
    Recursively collects raster files under the given directory.
    Optionally filters to files within subdirectories matching `subdir`.
    Excludes already processed/calibrated files to avoid re-processing.

    Args:
        path (Path): Root directory to search.
        subdir (Optional[str]): If provided, only include files under subdirectories with this name.
        extensions (tuple): File extensions to include (case-insensitive).

    Returns:
        List[Path]: List of matching raster file paths.
    """
    extensions = tuple(ext.lower() for ext in extensions)
    collected = []

    for p in path.resolve().rglob("*"):
        if p.is_file() and p.suffix.lower() in extensions:
            # Skip files that are already calibrated (have processed suffix patterns)
            if any(pattern in p.name for pattern in ['_u08mr', '_ortho', '_calibrated']):
                continue
                
            # Skip files in calibrated/processed directories
            if any(dirname in ['calibrated', 'pansharpened', 'cogs'] for dirname in p.parts):
                continue
                
            if subdir is None or subdir in p.parts:
                collected.append(p)

    return collected

def determine_safe_pool_size(min_pool=1, max_pool=None):
    total_cores = multiprocessing.cpu_count()
    
    if HAS_PSUTIL:
        available_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        # Estimate per-process RAM usage (very conservatively)
        est_per_process_ram_gb = 2  # Adjust based on profiling if needed
        ram_limited_pool = math.floor(available_ram_gb / est_per_process_ram_gb)
    else:
        # Fallback when psutil is not available - assume conservative RAM usage
        # Use a conservative estimate based on CPU count
        ram_limited_pool = max(1, total_cores // 2)

    cpu_limited_pool = total_cores - 1  # Leave 1 core for OS

    # Find the lowest of the two constraints
    recommended_pool = max(min_pool, min(ram_limited_pool, cpu_limited_pool))

    if max_pool:
        recommended_pool = min(recommended_pool, max_pool)

    return recommended_pool