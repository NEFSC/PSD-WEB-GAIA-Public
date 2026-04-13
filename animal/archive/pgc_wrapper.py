# -------------------------------------------------------------------------------
# ----- pgc_wrapper.py ----------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    author:  John Wall (john.wall@noaa.gov)
#
#    purpose: Standardizes functions from the Polar Geospatial Center's
#             "imagery_utils" for use within GAIA and supports local troubleshooting.
#             Includes wrappers for orthorectification and pansharpening using
#             Maxar WV imagery.
#
#    notes:
#        - External script: `pgc_ortho.py` must be present in /external/imagery_utils
#        - Output images are saved in automatically generated subdirectories
#        - Consider renaming to `pgc_utils.py` in the future
#
#    references:
#        https://www.pgc.umn.edu/guides/pgc-coding-and-utilities/using-pgc-github-orthorectification/
#        https://github.com/PolarGeospatialCenter/imagery_utils/blob/main/doc/pgc_ortho.txt
#
# -------------------------------------------------------------------------------

import os
import sys
import subprocess
from glob import glob
from time import time
from pathlib import Path
from typing import Tuple, Optional
import math

from osgeo import gdal
from osgeo_utils.gdal_pansharpen import gdal_pansharpen

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from animal.utils.logging import get_animal_logger
from animal.utils.utils import ensure_pgc_utils

logger = get_animal_logger(__name__)

# -------------------------------------------------------------------------------
# Locate external PGC script
# -------------------------------------------------------------------------------

try:
    base_dir = Path(__file__).resolve().parent.parent
except NameError:
    base_dir = Path(os.getenv("PROJECT_ROOT", Path.cwd()))

external_dir = base_dir / "external" / "imagery_utils"
print(f"[INFO] Imagery utilities path: {external_dir}")
sys.path.append(str(external_dir))


# -------------------------------------------------------------------------------
# Main Functions
# -------------------------------------------------------------------------------
def calibrate_pair(imagery_tuple: Tuple[str, str], dem: str) -> Tuple[str, str]:
    """
    Calibrates both PAN and MSI images using the provided DEM and PGC scripts.

    Args:
        imagery_tuple (Tuple[str, str]): Tuple containing paths to the PAN and MSI TIFFs.
        dem (str): Path to the DEM file to use for orthorectification.

    Returns:
        Tuple[str, str]: Calibrated PAN and MSI image paths.
    """
    pan_image, msi_image = imagery_tuple
    calibrated_pan = calibrate_image(pan_image, dem)
    calibrated_msi = calibrate_image(msi_image, dem)
    return calibrated_pan, calibrated_msi


def _build_clean_env() -> dict:
    """Return a sanitized environment for GDAL/PROJ calls inside the container."""
    env = os.environ.copy()
    conda_prefix = Path(sys.executable).resolve().parents[1]
    proj_dir = conda_prefix / "share" / "proj"
    gdal_dir = conda_prefix / "share" / "gdal"
    for var in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
        env.pop(var, None)
    env["PROJ_LIB"] = str(proj_dir)
    env["GDAL_DATA"] = str(gdal_dir)
    env.setdefault("CPL_DEBUG", "OFF")
    return env


def _determine_utm_epsg_from_rpc(tiff: str) -> Optional[int]:
    """Determine a UTM EPSG code from RPC metadata (fallback for minimal warp)."""
    try:
        ds = gdal.Open(tiff)
        if not ds:
            return None
        md = ds.GetMetadata("RPC") or {}
        lat = float(md.get("LAT_OFF")) if md.get("LAT_OFF") else None
        lon = float(md.get("LONG_OFF")) if md.get("LONG_OFF") else None
        if lat is None or lon is None:
            return None
        zone = int(math.floor((lon + 180) / 6) + 1)
        if lat >= 0:
            return 32600 + zone  # WGS84 UTM North
        else:
            return 32700 + zone  # WGS84 UTM South
    except Exception:
        return None


def _simple_rpc_warp(tiff: str, output_dir: Path, epsg: Optional[int]) -> Path:
    """Perform a minimal orthorectification using gdalwarp + RPC without DEM."""
    logger = get_animal_logger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = Path(tiff).stem
    dst = output_dir / f"{base}_rpcwarp.tif"
    epsg_code = epsg or 4326
    cmd = [
        "gdalwarp",
        "-of", "GTiff",
        "-rpc",
        "-t_srs", f"EPSG:{epsg_code}",
        "-wo", "SAMPLE_STEPS=64",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=YES",
        "-dstalpha",
        "-overwrite",
        tiff,
        str(dst)
    ]
    env = _build_clean_env()
    logger.warning(f"[CALIBRATE][FALLBACK] Running SIMPLE RPC warp (no DEM) -> {dst}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    if result.returncode != 0:
        logger.error(result.stderr)
        raise RuntimeError("Simple RPC warp failed")
    if not dst.exists():
        raise FileNotFoundError("RPC warp output not created")
    return dst


def calibrate_image(tiff: str, dem: str) -> str:
    """Attempt full PGC orthorectification with DEM; fallback to constant-height; then to simple RPC warp."""
    logger = get_animal_logger(__name__)
    filename = Path(tiff).name
    input_dir = Path(tiff).parent
    output_dir = input_dir.parent / "calibrated"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[CALIBRATE] Image={filename}")
    env = _build_clean_env()
    logger.info(f"[CALIBRATE_ENV] PROJ_LIB={env['PROJ_LIB']} GDAL_DATA={env['GDAL_DATA']}")

    pgc_script = str(external_dir / "pgc_ortho.py")

    def run_pgc(args_list, label: str) -> bool:
        cmd = [sys.executable, pgc_script] + args_list + [str(input_dir), str(output_dir)]
        logger.info(f"[CALIBRATE][PGC] {label} cmd={' '.join(cmd)}")
        try:
            res = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            logger.debug(res.stdout)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(f"[CALIBRATE][PGC_FAIL:{label}] rc={e.returncode}\nSTDERR:\n{e.stderr}")
            return False

    # 1. Try DEM-based if exists and not empty
    dem_path = Path(dem) if dem else None
    if dem_path and dem_path.exists() and dem_path.stat().st_size > 0:
        args_dem = [
            "-p", "utm",
            "-c", "mr",
            "-f", "GTiff",
            "-t", "Byte",
            "-d", str(dem_path),
            "--skip-dem-overlap-check",
            "--resample", "cubic"
        ]
        if run_pgc(args_dem, "DEM"):
            match = list(output_dir.glob(f"{filename.split('.')[0]}*.tif"))
            if match:
                logger.info(f"[CALIBRATED][DEM] {match[0]}")
                return str(match[0])
            logger.warning("[CALIBRATE] DEM run succeeded but output not found; continuing fallbacks")
    else:
        logger.info("[CALIBRATE] DEM missing/unusable; skipping DEM attempt")

    # 2. Constant-height orthorectification (PGC internal avg height logic OR explicit ortho-height 0)
    args_const = [
        "-p", "utm",
        "-c", "mr",
        "-f", "GTiff",
        "-t", "Byte",
        "--ortho-height", "0",
        "--resample", "cubic"
    ]
    if run_pgc(args_const, "CONST_HEIGHT"):
        match = list(output_dir.glob(f"{filename.split('.')[0]}*.tif"))
        if match:
            logger.info(f"[CALIBRATED][CONST] {match[0]}")
            return str(match[0])
        logger.warning("[CALIBRATE] Constant-height run succeeded but output not found; will try RPC warp")

    # 3. Simple GDAL RPC warp fallback
    try:
        epsg = _determine_utm_epsg_from_rpc(tiff)
        rpc_out = _simple_rpc_warp(tiff, output_dir, epsg)
        logger.info(f"[CALIBRATED][RPC_SIMPLE] {rpc_out}")
        return str(rpc_out)
    except Exception as e:
        logger.error(f"[CALIBRATE][FATAL] All calibration strategies failed for {tiff}: {e}")
        raise