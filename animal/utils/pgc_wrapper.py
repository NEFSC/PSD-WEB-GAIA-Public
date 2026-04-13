# -------------------------------------------------------------------------------
# ----- pgc_wrapper.py ----------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    Author:  John Wall (john.wall@noaa.gov)
#    Revised: January 21, 2026 (GAIFAGP-439, GAIFAGP-165)
#
#    Purpose: Standardizes Polar Geospatial Center (PGC) orthorectification
#             for use within GAIA. Provides calibration of satellite imagery
#             with layered fallback strategies for operational continuity.
#
#    Capabilities:
#        - Single image calibration (calibrate_image)
#        - Pair calibration for PAN+MSI (calibrate_pair)
#        - Cloud DEM support (Azure blob URLs via /vsiaz/, VRTs)
#        - Cross-platform GDAL/PROJ environment handling (Windows + Linux)
#        - Fallback strategies: DEM → constant-height → RPC warp
#
#    External Dependencies:
#        imagery_utils repo is stored OUTSIDE the project to avoid git
#        tracking and Windows file locking issues:
#        - Windows: C:/gis/external/imagery_utils/
#        - Linux: /opt/gaia/external/imagery_utils/
#        - Override: Set GAIA_EXTERNAL_DIR environment variable
#
#    GAIFAGP-165 Fix (2026-01-21):
#        Azure blob URLs now use /vsiaz/ instead of /vsicurl/.
#        This is required for VRT files with relative paths.
#        See Claire's spike documentation for details.
#
#    Dependencies:
#        - {GAIA_EXTERNAL_DIR}/imagery_utils/pgc_ortho.py (PGC tooling)
#        - GDAL/PROJ (via conda or system)
#        - animal.utils.logging (GAIA logger)
#        - animal.utils.git_utils (clone_imagery_utils)
#
#    References:
#        https://www.pgc.umn.edu/guides/pgc-coding-and-utilities/
#        https://github.com/PolarGeospatialCenter/imagery_utils
#
# -------------------------------------------------------------------------------

from __future__ import annotations

import math
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple, Union

from osgeo import gdal

# Enable GDAL exceptions for cleaner error handling
gdal.UseExceptions()

# -------------------------------------------------------------------------------
# Logging Setup
# -------------------------------------------------------------------------------

try:
    from animal.utils.logging import get_animal_logger
    logger = get_animal_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------------

# Calibration output subdirectory name
CALIBRATED_SUBDIR = "calibrated"

# PGC ortho script relative path
PGC_ORTHO_SCRIPT = "pgc_ortho.py"

# Supported raster extensions
RASTER_EXTENSIONS = {'.tif', '.tiff', '.ntf', '.nitf', '.TIF', '.TIFF', '.NTF', '.NITF'}


# -------------------------------------------------------------------------------
# Environment Configuration
# -------------------------------------------------------------------------------

def _detect_gdal_proj_paths() -> Tuple[Optional[Path], Optional[Path]]:
    """
    Detect GDAL_DATA and PROJ_LIB paths based on the current environment.
    
    Handles:
    - Windows conda: {prefix}/Library/share/proj, {prefix}/Library/share/gdal
    - Linux/macOS conda: {prefix}/share/proj, {prefix}/share/gdal
    - Docker containers with custom layouts
    - System installations
    
    Returns:
        Tuple of (proj_path, gdal_path), either may be None if not found.
    """
    # Get conda prefix from Python executable
    python_path = Path(sys.executable).resolve()
    
    # Typical conda layouts
    if platform.system() == 'Windows':
        # Windows conda: C:/Users/xxx/.conda/envs/gaia/python.exe
        # Data dirs at: C:/Users/xxx/.conda/envs/gaia/Library/share/
        conda_prefix = python_path.parent
        proj_candidates = [
            conda_prefix / "Library" / "share" / "proj",
            conda_prefix / "share" / "proj",
        ]
        gdal_candidates = [
            conda_prefix / "Library" / "share" / "gdal",
            conda_prefix / "share" / "gdal",
        ]
    else:
        # Linux/macOS conda: /opt/conda/envs/gaia/bin/python
        # Data dirs at: /opt/conda/envs/gaia/share/
        conda_prefix = python_path.parents[1] if python_path.parent.name == 'bin' else python_path.parent
        proj_candidates = [
            conda_prefix / "share" / "proj",
            Path("/usr/share/proj"),
            Path("/usr/local/share/proj"),
        ]
        gdal_candidates = [
            conda_prefix / "share" / "gdal",
            Path("/usr/share/gdal"),
            Path("/usr/local/share/gdal"),
        ]
    
    # Find first existing path
    proj_path = None
    for candidate in proj_candidates:
        proj_db = candidate / "proj.db"
        if proj_db.exists():
            proj_path = candidate
            break
    
    gdal_path = None
    for candidate in gdal_candidates:
        if candidate.exists() and candidate.is_dir():
            gdal_path = candidate
            break
    
    return proj_path, gdal_path


def _build_clean_env() -> dict:
    """
    Build a sanitized environment for GDAL/PROJ subprocess calls.
    
    This function:
    1. Copies the current environment
    2. Removes potentially conflicting PROJ/GDAL variables
    3. Sets correct paths based on OS and conda layout
    4. Validates paths exist before setting them
    
    Returns:
        dict: Environment dictionary safe for subprocess calls.
        
    Raises:
        RuntimeError: If PROJ paths cannot be determined.
    """
    env = os.environ.copy()
    
    # Remove potentially conflicting variables
    for var in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA", "PROJ_NETWORK"):
        env.pop(var, None)
    
    # Detect correct paths
    proj_path, gdal_path = _detect_gdal_proj_paths()
    
    if proj_path:
        env["PROJ_LIB"] = str(proj_path)
        logger.debug(f"[ENV] PROJ_LIB={proj_path}")
    else:
        # Fall back to environment if detection failed
        if "PROJ_LIB" in os.environ:
            env["PROJ_LIB"] = os.environ["PROJ_LIB"]
            logger.warning(f"[ENV] Using inherited PROJ_LIB={os.environ['PROJ_LIB']}")
        else:
            logger.error("[ENV] Cannot determine PROJ_LIB path")
            raise RuntimeError(
                "Cannot determine PROJ_LIB path. "
                "Set PROJ_LIB environment variable or ensure conda environment is properly configured."
            )
    
    if gdal_path:
        env["GDAL_DATA"] = str(gdal_path)
        logger.debug(f"[ENV] GDAL_DATA={gdal_path}")
    elif "GDAL_DATA" in os.environ:
        env["GDAL_DATA"] = os.environ["GDAL_DATA"]
        logger.warning(f"[ENV] Using inherited GDAL_DATA={os.environ['GDAL_DATA']}")
    
    # Disable PROJ network for reproducibility
    env["PROJ_NETWORK"] = "OFF"
    
    # Reduce GDAL debug noise
    env.setdefault("CPL_DEBUG", "OFF")
    
    return env


def validate_environment() -> bool:
    """
    Validate that GDAL/PROJ environment is correctly configured.
    
    This can be called at startup to fail fast rather than during processing.
    
    Returns:
        bool: True if environment is valid.
        
    Raises:
        RuntimeError: If environment validation fails.
    """
    try:
        env = _build_clean_env()
        proj_lib = env.get("PROJ_LIB")
        
        if proj_lib:
            proj_db = Path(proj_lib) / "proj.db"
            if not proj_db.exists():
                raise RuntimeError(f"proj.db not found at {proj_db}")
        
        # Quick GDAL test
        from osgeo import osr
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        
        logger.info("[ENV] GDAL/PROJ environment validated successfully")
        return True
        
    except Exception as e:
        logger.error(f"[ENV] Environment validation failed: {e}")
        raise


# -------------------------------------------------------------------------------
# PGC Utilities Location
# -------------------------------------------------------------------------------

def _get_pgc_script_path(*, force_refresh: bool = False) -> Path:
    """
    Locate the PGC ortho script, cloning repo if necessary.
    
    Uses git_utils to manage the external imagery_utils repository.
    The repo is stored OUTSIDE the project (C:/gis/external/ on Windows,
    /opt/gaia/external/ on Linux) to avoid git tracking and file locking.
    
    Args:
        force_refresh: If True, force re-clone of imagery_utils repo.
    
    Returns:
        Path to pgc_ortho.py
        
    Raises:
        FileNotFoundError: If script cannot be located after clone attempt.
    """
    # Primary method: use git_utils with external directory
    try:
        from animal.utils.git_utils import get_pgc_script_path
        
        script_path = get_pgc_script_path(force=force_refresh)
        if script_path and script_path.exists():
            return script_path
            
    except ImportError:
        logger.warning("[PGC] git_utils not available, trying legacy path")
    except Exception as e:
        logger.warning(f"[PGC] git_utils clone failed: {e}")
    
    # Legacy fallback: check project-relative path
    try:
        base_dir = Path(__file__).resolve().parent.parent
    except NameError:
        base_dir = Path(os.getenv("PROJECT_ROOT", Path.cwd()))
    
    legacy_path = base_dir / "external" / "imagery_utils" / PGC_ORTHO_SCRIPT
    if legacy_path.exists():
        logger.info(f"[PGC] Using legacy path: {legacy_path}")
        return legacy_path
    
    raise FileNotFoundError(
        f"PGC ortho script not found. "
        f"Expected at C:/gis/external/imagery_utils/{PGC_ORTHO_SCRIPT} (Windows) "
        f"or /opt/gaia/external/imagery_utils/{PGC_ORTHO_SCRIPT} (Linux). "
        f"Set GAIA_EXTERNAL_DIR to override."
    )


# -------------------------------------------------------------------------------
# DEM Handling
# -------------------------------------------------------------------------------

def _resolve_dem_path(dem: str) -> Tuple[str, bool]:
    """
    Resolve DEM path, handling local files and cloud URLs.
    
    Args:
        dem: DEM path - can be local file, Azure blob URL, or GDAL VRT
        
    Returns:
        Tuple of (resolved_path, is_cloud) where:
        - resolved_path: Path usable by GDAL
        - is_cloud: True if DEM is accessed via network
        
    Note:
        Azure blob URLs are converted to /vsiaz/ format (not /vsicurl/)
        because VRT files with relative paths require the Azure driver.
        
        Requires AZURE_STORAGE_ACCOUNT environment variable to be set.
        
    Example:
        https://blob.core.windows.net/data/rasters/dem.vrt
        → /vsiaz/data/rasters/dem.vrt
    """
    import re
    
    if not dem:
        return "", False
    
    dem_str = str(dem)
    
    # Check for cloud/virtual paths
    is_cloud = any([
        dem_str.startswith("http://"),
        dem_str.startswith("https://"),
        dem_str.startswith("/vsicurl/"),
        dem_str.startswith("/vsiaz/"),
        dem_str.startswith("/vsigs/"),
        dem_str.startswith("/vsis3/"),
    ])
    
    if is_cloud:
        # Check for Azure blob URLs - convert to /vsiaz/ (not /vsicurl/)
        # /vsiaz/ handles VRTs with relative paths correctly
        azure_match = re.match(
            r'https://([^.]+)\.blob\.core\.windows\.net/(.+)',
            dem_str
        )
        
        if azure_match:
            storage_account = azure_match.group(1)
            blob_path = azure_match.group(2)  # container/path/to/file
            
            # Set storage account env var if not already set
            current_account = os.environ.get('AZURE_STORAGE_ACCOUNT', '')
            if not current_account:
                os.environ['AZURE_STORAGE_ACCOUNT'] = storage_account
                logger.info(f"[DEM] Set AZURE_STORAGE_ACCOUNT={storage_account}")
            elif current_account != storage_account:
                logger.warning(
                    f"[DEM] AZURE_STORAGE_ACCOUNT mismatch: "
                    f"env={current_account}, url={storage_account}"
                )
            
            resolved = f"/vsiaz/{blob_path}"
            logger.info(f"[DEM] Using Azure DEM via /vsiaz/: {resolved}")
            return resolved, True
        
        # Non-Azure HTTP URLs - use /vsicurl/
        if dem_str.startswith("http") and not dem_str.startswith("/vsi"):
            resolved = f"/vsicurl/{dem_str}"
            logger.info(f"[DEM] Using cloud DEM via /vsicurl/: {resolved}")
        else:
            resolved = dem_str
            logger.info(f"[DEM] Using cloud DEM: {resolved}")
        
        return resolved, True
    
    # Local file
    dem_path = Path(dem_str)
    if not dem_path.exists():
        logger.warning(f"[DEM] Local DEM not found: {dem_path}")
        return "", False
    
    if dem_path.stat().st_size == 0:
        logger.warning(f"[DEM] DEM file is empty: {dem_path}")
        return "", False
    
    return str(dem_path.resolve()), False


# -------------------------------------------------------------------------------
# UTM Zone Detection
# -------------------------------------------------------------------------------

def _determine_utm_epsg_from_rpc(tiff: str) -> Optional[int]:
    """
    Determine UTM EPSG code from RPC metadata in imagery.
    
    Uses LAT_OFF and LONG_OFF from RPC metadata to calculate the
    appropriate UTM zone.
    
    Args:
        tiff: Path to raster file with RPC metadata.
        
    Returns:
        EPSG code (326xx for north, 327xx for south) or None if unavailable.
    """
    try:
        ds = gdal.Open(tiff, gdal.GA_ReadOnly)
        if not ds:
            logger.warning(f"[RPC] Cannot open {tiff}")
            return None
        
        md = ds.GetMetadata("RPC") or {}
        ds = None  # Close dataset
        
        lat_off = md.get("LAT_OFF")
        lon_off = md.get("LONG_OFF")
        
        if lat_off is None or lon_off is None:
            logger.warning(f"[RPC] No RPC metadata found in {Path(tiff).name}")
            return None
        
        lat = float(lat_off)
        lon = float(lon_off)
        
        # Calculate UTM zone
        zone = int(math.floor((lon + 180) / 6) + 1)
        
        if lat >= 0:
            epsg = 32600 + zone  # WGS84 UTM North
        else:
            epsg = 32700 + zone  # WGS84 UTM South
        
        logger.debug(f"[RPC] Derived EPSG:{epsg} from lat={lat:.2f}, lon={lon:.2f}")
        return epsg
        
    except Exception as e:
        logger.warning(f"[RPC] Failed to determine UTM from RPC: {e}")
        return None


# -------------------------------------------------------------------------------
# Calibration Strategies
# -------------------------------------------------------------------------------

def _run_pgc_ortho(
    input_dir: Path,
    output_dir: Path,
    args: list,
    env: dict,
    label: str
) -> bool:
    """
    Execute PGC ortho script with given arguments.
    
    Args:
        input_dir: Directory containing input imagery
        output_dir: Directory for calibrated output
        args: Additional arguments for pgc_ortho.py
        env: Environment dictionary
        label: Label for logging (e.g., "DEM", "CONST_HEIGHT")
        
    Returns:
        True if successful, False otherwise.
    """
    try:
        pgc_script = _get_pgc_script_path()
    except FileNotFoundError as e:
        logger.error(f"[PGC] {e}")
        return False
    
    cmd = [
        sys.executable,
        str(pgc_script),
    ] + args + [
        str(input_dir),
        str(output_dir)
    ]
    
    logger.info(f"[CALIBRATE][PGC:{label}] {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=3600  # 1 hour timeout for large images
        )
        
        if result.returncode == 0:
            logger.debug(f"[PGC:{label}] stdout: {result.stdout[:500] if result.stdout else '(empty)'}")
            return True
        else:
            logger.warning(f"[CALIBRATE][PGC_FAIL:{label}] rc={result.returncode}")
            if result.stderr:
                logger.warning(f"[PGC:{label}] stderr: {result.stderr[:1000]}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"[CALIBRATE][PGC:{label}] Timeout after 1 hour")
        return False
    except Exception as e:
        logger.error(f"[CALIBRATE][PGC:{label}] Exception: {e}")
        return False


def _simple_rpc_warp(
    tiff: str,
    output_dir: Path,
    epsg: Optional[int],
    env: dict
) -> Optional[Path]:
    """
    Perform minimal orthorectification using gdalwarp with RPC.
    
    This is the last-resort fallback when PGC tools are unavailable
    or fail. Uses RPC metadata to warp to a geographic/UTM projection.
    
    Args:
        tiff: Input raster path
        output_dir: Output directory
        epsg: Target EPSG code (defaults to 4326 if None)
        env: Environment dictionary
        
    Returns:
        Path to output file or None if failed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    base = Path(tiff).stem
    dst = output_dir / f"{base}_rpcwarp.tif"
    epsg_code = epsg or 4326
    
    cmd = [
        "gdalwarp",
        "-of", "GTiff",
        "-rpc",
        "-t_srs", f"EPSG:{epsg_code}",
        "-r", "cubic",
        "-wo", "SAMPLE_STEPS=64",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=IF_SAFER",
        "-co", "COMPRESS=LZW",
        "-dstalpha",
        "-overwrite",
        tiff,
        str(dst)
    ]
    
    logger.warning(f"[CALIBRATE][FALLBACK:RPC] Warping to EPSG:{epsg_code} -> {dst.name}")
    
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=1800  # 30 min timeout
        )
        
        if result.returncode != 0:
            logger.error(f"[RPC_WARP] Failed: {result.stderr[:500] if result.stderr else 'no stderr'}")
            return None
        
        if not dst.exists():
            logger.error(f"[RPC_WARP] Output not created: {dst}")
            return None
        
        logger.info(f"[CALIBRATE][RPC_WARP] Success: {dst.name}")
        return dst
        
    except subprocess.TimeoutExpired:
        logger.error("[RPC_WARP] Timeout after 30 minutes")
        return None
    except Exception as e:
        logger.error(f"[RPC_WARP] Exception: {e}")
        return None


# -------------------------------------------------------------------------------
# Main Calibration Functions
# -------------------------------------------------------------------------------

def calibrate_image(tiff: str, dem: str = "") -> Optional[str]:
    """
    Calibrate a single image using PGC orthorectification with fallbacks.
    
    Attempts calibration strategies in order:
    1. DEM-based orthorectification (highest fidelity)
    2. Constant-height orthorectification (ortho-height=0)
    3. Simple RPC warp via gdalwarp (last resort)
    
    Args:
        tiff: Path to input raster (TIF, NTF, etc.)
        dem: Optional DEM path (local file, Azure URL, or VRT)
        
    Returns:
        Path to calibrated output, or None if all strategies failed.
        
    Example:
        >>> calibrated = calibrate_image("WV03_pan.ntf", dem="/data/cop30.tif")
        >>> print(calibrated)
        '/data/calibrated/WV03_pan_ortho.tif'
    """
    tiff_path = Path(tiff).resolve()
    filename = tiff_path.name
    
    # Validate input exists
    if not tiff_path.exists():
        logger.error(f"[CALIBRATE] Input file not found: {tiff_path}")
        return None
    
    # Setup directories
    input_dir = tiff_path.parent
    output_dir = input_dir.parent / CALIBRATED_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"[CALIBRATE] Image={filename}")
    logger.info(f"[CALIBRATE] Output dir={output_dir}")
    
    # Build environment
    try:
        env = _build_clean_env()
    except RuntimeError as e:
        logger.error(f"[CALIBRATE] Environment setup failed: {e}")
        return None
    
    logger.info(f"[CALIBRATE_ENV] PROJ_LIB={env.get('PROJ_LIB', 'NOT SET')}")
    logger.info(f"[CALIBRATE_ENV] GDAL_DATA={env.get('GDAL_DATA', 'NOT SET')}")
    
    # Resolve DEM
    dem_path, is_cloud_dem = _resolve_dem_path(dem)
    
    # Stem for output matching (handle multi-extension like .tar.gz)
    stem = tiff_path.stem.split('.')[0] if '.' in tiff_path.stem else tiff_path.stem
    
    def find_output() -> Optional[str]:
        """Search for calibrated output file."""
        patterns = [
            f"{stem}*.tif",
            f"{stem}*.TIF",
        ]
        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            if matches:
                # Prefer non-rpcwarp if both exist
                non_rpc = [m for m in matches if '_rpcwarp' not in m.name]
                return str(non_rpc[0] if non_rpc else matches[0])
        return None
    
    # Strategy 1: DEM-based orthorectification
    if dem_path:
        args = [
            "-p", "utm",
            "-c", "mr",          # Multi-resolution
            "-f", "GTiff",
            "-t", "Byte",
            "-d", dem_path,
            "--skip-dem-overlap-check",
            "--resample", "cubic"
        ]
        
        if _run_pgc_ortho(input_dir, output_dir, args, env, "DEM"):
            output = find_output()
            if output:
                logger.info(f"[CALIBRATED][DEM] {output}")
                return output
            logger.warning("[CALIBRATE] DEM run succeeded but output not found")
    else:
        logger.info("[CALIBRATE] No DEM available, skipping DEM strategy")
    
    # Strategy 2: Constant-height orthorectification
    args = [
        "-p", "utm",
        "-c", "mr",
        "-f", "GTiff",
        "-t", "Byte",
        "--ortho-height", "0",
        "--resample", "cubic"
    ]
    
    if _run_pgc_ortho(input_dir, output_dir, args, env, "CONST_HEIGHT"):
        output = find_output()
        if output:
            logger.info(f"[CALIBRATED][CONST_HEIGHT] {output}")
            return output
        logger.warning("[CALIBRATE] Constant-height run succeeded but output not found")
    
    # Strategy 3: Simple RPC warp (last resort)
    epsg = _determine_utm_epsg_from_rpc(str(tiff_path))
    rpc_output = _simple_rpc_warp(str(tiff_path), output_dir, epsg, env)
    
    if rpc_output:
        logger.info(f"[CALIBRATED][RPC] {rpc_output}")
        return str(rpc_output)
    
    # All strategies failed
    logger.error(f"[CALIBRATE][FATAL] All calibration strategies failed for {filename}")
    return None


def calibrate_pair(
    imagery_tuple: Tuple[str, str],
    dem: str = ""
) -> Optional[Tuple[str, str]]:
    """
    Calibrate both PAN and MSI images using the provided DEM.
    
    Processes both images of a pair using the same DEM and calibration
    settings for consistency.
    
    Args:
        imagery_tuple: Tuple of (pan_path, msi_path)
        dem: Optional DEM path (local file, Azure URL, or VRT)
        
    Returns:
        Tuple of (calibrated_pan, calibrated_msi) paths, or None if either fails.
        
    Example:
        >>> result = calibrate_pair(("WV03_pan.ntf", "WV03_msi.ntf"), dem="/data/cop30.tif")
        >>> if result:
        ...     cal_pan, cal_msi = result
    """
    pan_image, msi_image = imagery_tuple
    
    logger.info(f"[CALIBRATE_PAIR] PAN={Path(pan_image).name}")
    logger.info(f"[CALIBRATE_PAIR] MSI={Path(msi_image).name}")
    
    # Calibrate PAN
    calibrated_pan = calibrate_image(pan_image, dem)
    if not calibrated_pan:
        logger.error("[CALIBRATE_PAIR] PAN calibration failed")
        return None
    
    # Calibrate MSI
    calibrated_msi = calibrate_image(msi_image, dem)
    if not calibrated_msi:
        logger.error("[CALIBRATE_PAIR] MSI calibration failed")
        return None
    
    logger.info(f"[CALIBRATE_PAIR] Success: PAN={Path(calibrated_pan).name}, MSI={Path(calibrated_msi).name}")
    return (calibrated_pan, calibrated_msi)


# -------------------------------------------------------------------------------
# Batch Processing
# -------------------------------------------------------------------------------

def calibrate_batch(
    pairs: list[Tuple[str, str]],
    dem: str = "",
    processes: int = 1
) -> list[Tuple[str, str]]:
    """
    Calibrate multiple PAN/MSI pairs.
    
    Args:
        pairs: List of (pan_path, msi_path) tuples
        dem: Optional DEM path
        processes: Number of parallel processes (1 = serial)
        
    Returns:
        List of (calibrated_pan, calibrated_msi) tuples for successful pairs.
    """
    if processes > 1:
        from functools import partial
        from multiprocessing import Pool
        
        process_func = partial(calibrate_pair, dem=dem)
        
        with Pool(processes=processes) as pool:
            results = pool.map(process_func, pairs)
        
        return [r for r in results if r is not None]
    else:
        results = []
        for i, pair in enumerate(pairs, 1):
            logger.info(f"[BATCH] Processing pair {i}/{len(pairs)}")
            result = calibrate_pair(pair, dem=dem)
            if result:
                results.append(result)
        return results


# -------------------------------------------------------------------------------
# Module Initialization
# -------------------------------------------------------------------------------

def _init_module():
    """
    Initialize module - locate PGC utilities and add to sys.path.
    
    Uses git_utils.get_imagery_utils_dir() to find the external repo.
    The repo is stored OUTSIDE the project to avoid git tracking and
    Windows file locking issues.
    """
    try:
        from animal.utils.git_utils import get_imagery_utils_dir
        external_dir = get_imagery_utils_dir()
    except ImportError:
        # Fallback to legacy project-relative path
        try:
            base_dir = Path(__file__).resolve().parent.parent
        except NameError:
            base_dir = Path(os.getenv("PROJECT_ROOT", Path.cwd()))
        external_dir = base_dir / "external" / "imagery_utils"
    
    if external_dir.exists():
        if str(external_dir) not in sys.path:
            sys.path.append(str(external_dir))
        logger.info(f"[INIT] Imagery utilities: {external_dir}")
    else:
        logger.warning(f"[INIT] Imagery utilities not found at {external_dir}")
        logger.warning("[INIT] Will clone on first use")


# Run initialization
_init_module()


# -------------------------------------------------------------------------------
# CLI Entry Point (for testing)
# -------------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="PGC Calibration Wrapper")
    parser.add_argument("input", help="Input raster file")
    parser.add_argument("--dem", help="DEM file path")
    parser.add_argument("--validate-env", action="store_true", help="Validate environment and exit")
    
    args = parser.parse_args()
    
    if args.validate_env:
        try:
            validate_environment()
            print("Environment validation: PASSED")
        except Exception as e:
            print(f"Environment validation: FAILED - {e}")
            sys.exit(1)
    else:
        result = calibrate_image(args.input, dem=args.dem or "")
        if result:
            print(f"Calibrated output: {result}")
        else:
            print("Calibration failed")
            sys.exit(1)