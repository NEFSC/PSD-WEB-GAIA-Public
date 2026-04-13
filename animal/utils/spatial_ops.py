# ------------------------------------------------------------------------------
# ----- spatial_ops.py ---------------------------------------------------------
# ------------------------------------------------------------------------------
#
#    authors:  John Wall (john.wall@noaa.gov)
#              
#    purpose:  Contains reusable global spatial methods
#
# ------------------------------------------------------------------------------



# ------------------------------------------------------------------------------
# Import libraries
# ------------------------------------------------------------------------------
import os
import sys
import math
import psutil
import shutil
import tempfile
import traceback
import subprocess
from time import time
from pyproj import CRS
from osgeo import gdal
from pathlib import Path
import multiprocessing as mp
from functools import partial
from shapely.geometry import box, Polygon
from shapely.ops import unary_union
import pandas as pd
import geopandas as gpd
from osgeo_utils.gdal_pansharpen import gdal_pansharpen

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from animal.utils.logging import get_animal_logger
from animal.utils.utils import retry

logger = get_animal_logger(__name__)


# ------------------------------------------------------------------------------
# Spatial methods
# ------------------------------------------------------------------------------
def is_projected_in_meters(crs: CRS) -> bool:
    return crs.axis_info[0].unit_name.lower() in ["metre", "meter"]


def create_hexagon(cx, cy, r):
    """Create a flat-top hexagon centered at (cx, cy) with radius r."""
    # Calculate the 6 vertices of the hexagon
    angles = [math.radians(a) for a in range(0, 360, 60)]
    points = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]
    
    # Create a Polygon object directly from the list of vertices.
    # This is the modern and robust way to create the geometry.
    return Polygon(points)


def _safe_create(cog_path, out_dir, max_retries):
    for attempt in range(max_retries):
        result = create_fishnet(cog_path, out_dir=out_dir)
        if result and os.path.exists(result):
            return result
    return None


def process_fishnet_batch_parallel(cogs: list, out_dir: str, processes: int,
                                    cell_width: float = 600, cell_height: float = 400,
                                    shape: str = "rectangle", threads: int = 4):
    if not cogs:
        logger.info("[INFO] No new COGs found for fishnet creation.")
        return [], []
    
    logger.info(f"[INFO] Generating fishnets for {len(cogs)} COGs...")

    os.makedirs(out_dir, exist_ok=True)
    start = time()

    wrapper = partial(
        create_fishnet, 
        out_dir=out_dir, 
        cell_width=cell_width, 
        cell_height=cell_height, 
        shape=shape,
        threads=threads
    )
    with mp.Pool(processes=processes) as pool:
        results = pool.map(wrapper, cogs)

    successes = [r for r in results if r]
    failures = [c for c, r in zip(cogs, results) if not r]

    logger.info(f"[INFO] Batch fishnet creation complete in {round(time() - start, 2)} seconds")

    return successes, failures


@retry(max_retries=3, wait_seconds=10)
def create_single_footprint(cog_path: str, out_dir: str, threads: int) -> str | None:
    """Runs gdal_footprint on a single COG file."""
    logger = get_animal_logger(__name__)
    try:
        footprints_dir = Path(out_dir) / "footprints"
        footprints_dir.mkdir(parents=True, exist_ok=True)
        cog_base = Path(cog_path).stem
        fp_geojson = footprints_dir / f"{cog_base}_fp.geojson"

        logger.info(f"Creating footprint for {cog_base}...")
        
        env = os.environ.copy()
        env["GDAL_CACHEMAX"] = str(int(psutil.virtual_memory().total // (1024*1024) * 0.50))
        env["NUM_THREADS"] = str(threads) # Use the thread count passed from the command

        subprocess.run(
            ['gdal_footprint', '-srcnodata', '0', '-overwrite', cog_path, str(fp_geojson)],
            check=True, env=env
        )
        logger.info(f"Successfully created footprint: {fp_geojson}")
        return str(fp_geojson)
    except Exception as e:
        logger.error(f"Failed to create footprint for {cog_path}: {e}")
        raise


def generate_grid_from_footprint(footprint_path: str, out_dir: str, 
                                 cell_width: float, cell_height: float, shape: str):
    """Generates a fishnet grid from a pre-computed footprint GeoJSON."""
    logger = get_animal_logger(__name__)
    try:
        fishnets_dir = Path(out_dir)
        fishnets_dir.mkdir(parents=True, exist_ok=True)
        footprint_base = Path(footprint_path).stem.replace('_fp', '')
        fishnet_geojson = fishnets_dir / f"{footprint_base}_fishnet_{shape}_{cell_width}x{cell_height}.geojson"

        logger.info(f"Generating {shape} grid for {footprint_base}...")
        
        gdf = gpd.read_file(footprint_path)

        # Reproject to local UTM to work with meters
        utm_proj = footprint_base.split('_')[-1].split('mr')[-1]
        gdf_proj = gdf.to_crs(f"EPSG:{utm_proj}")

        bbox = box(*gdf_proj.geometry[0].bounds)

        grid = []
        if shape == "rectangle":
            xmin, ymin, xmax, ymax = bbox.bounds
            x = xmin
            while x < xmax:
                y = ymin
                while y < ymax:
                    cell = box(x, y, x + cell_width, y + cell_height)
                    if cell.intersects(gdf_proj.geometry[0]):
                        grid.append(cell)
                    y += cell_height
                x += cell_width
        else:
            xmin, ymin, xmax, ymax = bbox.bounds
            dx = cell_width * 3/4
            dy = cell_height * math.sqrt(3)/2
            row = 0
            x = xmin
            while x < xmax + cell_width:
                y = ymin - (dy / 2 if row % 2 else 0)
                while y < ymax + cell_height:
                    hexagon = create_hexagon(x, y, cell_width / 2)
                    if hexagon.intersects(gdf_proj.geometry[0]):
                        grid.append(hexagon)
                    y += dy
                x += dx
                row += 1

        fishnet_gdf = gpd.GeoDataFrame(geometry=grid, crs=gdf_proj.crs)

        logger.info(f"Fishnet GeoDataFrame created with {len(fishnet_gdf)} features.")

        fishnet_gdf["vendor_id"] = footprint_base
        
        # Reproject final result to Web Mercator for web map display
        fishnet_gdf = fishnet_gdf.to_crs("EPSG:3857")
        fishnet_gdf.to_file(fishnet_geojson, driver="GeoJSON")

        return str(fishnet_geojson)

    except Exception as e:
        logger.error(f"Failed to generate fishnet from {footprint_path}: {e}", exc_info=True)
        # Re-raise to let the main command know this specific file failed
        raise