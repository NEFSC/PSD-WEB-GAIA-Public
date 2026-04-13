"""
POI generation — run Microsoft's generate_interesting_points on a GeoTIFF.

Clones microsoft/whales if needed, runs detection via subprocess,
returns path to output GeoJSON for ingestion by poi.py load.
"""
# -----------------------------------------------------------------------
# ----- poi_generate.py -------------------------------------------------
# -----------------------------------------------------------------------
#
#    author:   John Wall (john.wall@noaa.gov)
#    purpose:  Subprocess wrapper for Microsoft AI for Good
#              generate_interesting_points.py (Caleb Robinson).
#              Clones repo via git_utils, runs detection, returns
#              GeoJSON path for poi.py load ingestion.
#
#    tickets:  GAIFAGP-573 (poi.py consolidation — restore capability)
#              GAIFAGP-452 (SPIKE: automate Caleb's method)
#              GAIFAGP-427 (SPIKE: automate interesting points)
#
#    references:
#      https://github.com/microsoft/whales
#      DL-022 — POI Data Contract (output format)
#
# -----------------------------------------------------------------------

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT = 3600  # 1 hour max


def generate_interesting_points(
    input_fn: str,
    output_fn: str,
    method: str = "big_window",
    difference_threshold: Optional[float] = None,
    auto_difference_threshold: bool = False,
    area_threshold: Optional[float] = None,
    big_window_size: Optional[int] = None,
    land_mask_fn: Optional[str] = None,
    study_area_fn: Optional[str] = None,
    bands: Optional[str] = None,
    overwrite: bool = False,
) -> dict:
    """
    Run generate_interesting_points.py on a GeoTIFF.

    Clones microsoft/whales repo if not present, then invokes the
    detection script via subprocess.

    Args:
        input_fn: URL or path to Cloud Optimized GeoTIFF.
        output_fn: Output GeoJSON file path.
        method: Detection method (big_window, rolling_window, gmm).
        difference_threshold: Threshold in standard deviations.
        auto_difference_threshold: Auto-calculate threshold from data.
        area_threshold: Minimum feature size in map units.
        big_window_size: Window size for big_window method.
        land_mask_fn: Path to land mask vector file.
        study_area_fn: Path to study area vector file.
        bands: Comma-separated 1-based band indices.
        overwrite: Overwrite existing output file.

    Returns:
        dict with keys: success, output_fn, returncode, stdout,
        stderr, script_path, error.
    """
    from animal.utils.git_utils import get_whales_script_path

    script_path = get_whales_script_path()
    if script_path is None:
        logger.error(
            "generate_interesting_points: script not available",
            extra={"ticket": "GAIFAGP-573"},
        )
        return _error_result(
            output_fn,
            "Cannot locate generate_interesting_points.py. "
            "Clone of microsoft/whales failed or git not available.",
        )

    Path(output_fn).parent.mkdir(parents=True, exist_ok=True)

    cmd = _build_detection_cmd(
        script_path, input_fn, output_fn, method,
        difference_threshold=difference_threshold,
        auto_difference_threshold=auto_difference_threshold,
        area_threshold=area_threshold,
        big_window_size=big_window_size,
        land_mask_fn=land_mask_fn,
        study_area_fn=study_area_fn,
        bands=bands,
        overwrite=overwrite,
    )

    return _run_detection(cmd, output_fn, str(script_path))


def _build_detection_cmd(
    script_path: Path, input_fn: str, output_fn: str,
    method: str, **kwargs,
) -> list:
    """Build subprocess command list for generate_interesting_points."""
    cmd = [
        sys.executable, str(script_path),
        "--input_fn", str(input_fn),
        "--output_fn", str(output_fn),
        "--method", method,
    ]
    if kwargs.get("difference_threshold") is not None:
        cmd.extend(["--difference_threshold",
                     str(kwargs["difference_threshold"])])
    if kwargs.get("auto_difference_threshold"):
        cmd.append("--auto_difference_threshold")
    if kwargs.get("area_threshold") is not None:
        cmd.extend(["--area_threshold", str(kwargs["area_threshold"])])
    if kwargs.get("big_window_size") is not None:
        cmd.extend(["--big_window_size", str(kwargs["big_window_size"])])
    if kwargs.get("land_mask_fn"):
        cmd.extend(["--land_mask_fn", str(kwargs["land_mask_fn"])])
    if kwargs.get("study_area_fn"):
        cmd.extend(["--study_area_fn", str(kwargs["study_area_fn"])])
    if kwargs.get("bands"):
        cmd.extend(["--bands", str(kwargs["bands"])])
    if kwargs.get("overwrite"):
        cmd.append("--overwrite")
    return cmd


def _run_detection(cmd: list, output_fn: str, script_path: str) -> dict:
    """Execute detection subprocess and return structured result."""
    logger.info(
        "Running generate_interesting_points",
        extra={"output_fn": output_fn, "ticket": "GAIFAGP-573"},
    )

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )

        if proc.returncode == 0 and Path(output_fn).exists():
            logger.info(
                "generate_interesting_points completed",
                extra={"output_fn": output_fn, "ticket": "GAIFAGP-573"},
            )
            return {
                "success": True, "output_fn": output_fn,
                "returncode": 0, "stdout": proc.stdout,
                "stderr": proc.stderr, "script_path": script_path,
                "error": None,
            }

        error = (
            f"Script exited with code {proc.returncode}. "
            f"stderr: {proc.stderr[:500]}"
        )
        logger.error(
            "generate_interesting_points failed",
            extra={"returncode": proc.returncode, "ticket": "GAIFAGP-573"},
        )
        return _error_result(output_fn, error, script_path,
                             proc.returncode, proc.stdout, proc.stderr)

    except subprocess.TimeoutExpired:
        logger.error(
            "generate_interesting_points timed out",
            extra={"timeout": SUBPROCESS_TIMEOUT, "ticket": "GAIFAGP-573"},
        )
        return _error_result(
            output_fn,
            f"Script timed out after {SUBPROCESS_TIMEOUT} seconds",
            script_path,
        )
    except Exception as e:
        logger.error(
            "generate_interesting_points exception",
            extra={"error": str(e), "ticket": "GAIFAGP-573"},
        )
        return _error_result(output_fn, str(e), script_path)


def _error_result(
    output_fn: str, error: str, script_path: str = None,
    returncode: int = -1, stdout: str = "", stderr: str = "",
) -> dict:
    """Build a failure result dict."""
    return {
        "success": False, "output_fn": output_fn,
        "returncode": returncode, "stdout": stdout,
        "stderr": stderr, "script_path": script_path,
        "error": error,
    }
