#!/usr/bin/env python3
# -------------------------------------------------------------------------------
# ----- tiled_processing.py ----------------------------------------------------
# -------------------------------------------------------------------------------
#
#    purpose: Tiled processing implementation for large imagery in memory-constrained
#             environments. Handles pansharpening of 50GB+ images in 6-8GB containers.
#
# -------------------------------------------------------------------------------

import os
import gc
import time
import tempfile
import shutil
from typing import Tuple, List, Optional, Dict
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import numpy as np

try:
    from osgeo import gdal, osr
except ImportError:
    gdal = None

from animal.utils.logging import get_animal_logger
from animal.utils.memory_utils import (
    log_memory_usage, 
    force_garbage_collection, 
    check_memory_pressure,
    MemoryMonitor
)

logger = get_animal_logger(__name__)


class TileInfo:
    """Information about a single tile in the tiled processing grid"""
    def __init__(self, tile_id: str, x_offset: int, y_offset: int, 
                 width: int, height: int, x_overlap: int, y_overlap: int):
        self.tile_id = tile_id
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.width = width
        self.height = height
        self.x_overlap = x_overlap
        self.y_overlap = y_overlap
        self.output_path = None
        
    def __str__(self):
        return (f"Tile {self.tile_id}: offset=({self.x_offset},{self.y_offset}) "
                f"size=({self.width},{self.height}) overlap=({self.x_overlap},{self.y_overlap})")


def calculate_optimal_tile_size(file_path: str, max_memory_mb: int = 1024, 
                               bands: int = 3, dtype_size: int = 2) -> Tuple[int, int]:
    """
    Calculate optimal tile dimensions based on available memory.
    
    Args:
        file_path: Path to input image for size reference
        max_memory_mb: Maximum memory to use per tile in MB
        bands: Number of bands in output image
        dtype_size: Size of data type in bytes (2 for uint16, 4 for float32)
        
    Returns:
        (tile_width, tile_height) in pixels
    """
    # Open the file to get pixel dimensions
    dataset = gdal.Open(file_path, gdal.GA_ReadOnly)
    if not dataset:
        raise ValueError(f"Could not open {file_path}")
    
    raster_x = dataset.RasterXSize
    raster_y = dataset.RasterYSize
    dataset = None  # Close dataset
    
    # Calculate memory per pixel (input PAN + input MSI + output RGB)
    # PAN (1 band) + MSI (4 bands) + Output (3 bands) = 8 bands total
    memory_per_pixel = 8 * dtype_size
    
    # Available pixels within memory constraint
    max_memory_bytes = max_memory_mb * 1024 * 1024
    max_pixels = max_memory_bytes // memory_per_pixel
    
    # Calculate square tile size, but respect image dimensions
    ideal_tile_size = int(np.sqrt(max_pixels))
    
    # Ensure tile size doesn't exceed image dimensions
    tile_width = min(ideal_tile_size, raster_x)
    tile_height = min(ideal_tile_size, raster_y)
    
    # Round down to nearest multiple of 64 for efficient processing
    tile_width = (tile_width // 64) * 64
    tile_height = (tile_height // 64) * 64
    
    # Ensure minimum tile size
    tile_width = max(tile_width, 512)
    tile_height = max(tile_height, 512)
    
    logger.info(f"Calculated tile size: {tile_width}x{tile_height} pixels "
                f"(~{tile_width * tile_height * memory_per_pixel / 1024**2:.1f}MB per tile)")
    
    return tile_width, tile_height


def create_tile_grid(pan_path: str, msi_path: str, tile_width: int, tile_height: int,
                    overlap_percent: float = 10.0) -> List[TileInfo]:
    """
    Create a grid of tiles with overlap for processing large imagery.
    
    Args:
        pan_path: Path to panchromatic image
        msi_path: Path to multispectral image  
        tile_width: Tile width in pixels
        tile_height: Tile height in pixels
        overlap_percent: Overlap percentage between tiles
        
    Returns:
        List of TileInfo objects
    """
    # Open PAN image to get dimensions (should match MSI)
    dataset = gdal.Open(pan_path, gdal.GA_ReadOnly)
    if not dataset:
        raise ValueError(f"Could not open {pan_path}")
    
    raster_x = dataset.RasterXSize
    raster_y = dataset.RasterYSize
    dataset = None
    
    # Calculate overlap in pixels with minimum safeguard for cubic resampling
    min_overlap = 64  # Minimum 64px overlap for cubic interpolation safety
    calculated_x_overlap = int(tile_width * overlap_percent / 100)
    calculated_y_overlap = int(tile_height * overlap_percent / 100)
    
    x_overlap = max(calculated_x_overlap, min_overlap)
    y_overlap = max(calculated_y_overlap, min_overlap)
    
    # Log if minimum overlap safeguard was triggered
    if x_overlap > calculated_x_overlap or y_overlap > calculated_y_overlap:
        logger.info(f"Applied minimum overlap safeguard: {calculated_x_overlap}px -> {x_overlap}px (x), {calculated_y_overlap}px -> {y_overlap}px (y)")
    
    # Ensure overlap doesn't exceed 50% of tile size
    x_overlap = min(x_overlap, tile_width // 2)
    y_overlap = min(y_overlap, tile_height // 2)
    
    # Calculate effective tile step (excluding overlap)
    x_step = tile_width - x_overlap
    y_step = tile_height - y_overlap
    
    tiles = []
    tile_id = 0
    
    y_offset = 0
    while y_offset < raster_y:
        x_offset = 0
        while x_offset < raster_x:
            # Calculate actual tile dimensions (handle edge cases)
            actual_width = min(tile_width, raster_x - x_offset)
            actual_height = min(tile_height, raster_y - y_offset)
            
            # Calculate actual overlap for edge tiles
            actual_x_overlap = x_overlap if x_offset + tile_width < raster_x else 0
            actual_y_overlap = y_overlap if y_offset + tile_height < raster_y else 0
            
            tile = TileInfo(
                tile_id=f"tile_{tile_id:04d}",
                x_offset=x_offset,
                y_offset=y_offset,
                width=actual_width,
                height=actual_height,
                x_overlap=actual_x_overlap,
                y_overlap=actual_y_overlap
            )
            
            tiles.append(tile)
            tile_id += 1
            
            x_offset += x_step
            
        y_offset += y_step
    
    logger.info(f"Created tile grid: {len(tiles)} tiles of {tile_width}x{tile_height} "
                f"with {overlap_percent}% overlap for {raster_x}x{raster_y} image")
    
    return tiles


def extract_tile_data(pan_path: str, msi_path: str, tile: TileInfo, temp_dir: str) -> Tuple[str, str]:
    """
    Extract data for a single tile from the source images.
    
    Args:
        pan_path: Path to panchromatic image
        msi_path: Path to multispectral image
        tile: Tile information
        temp_dir: Temporary directory for tile files
        
    Returns:
        (pan_tile_path, msi_tile_path)
    """
    pan_tile_path = os.path.join(temp_dir, f"{tile.tile_id}_pan.tif")
    msi_tile_path = os.path.join(temp_dir, f"{tile.tile_id}_msi.tif")
    
    # Extract PAN tile
    gdal.Translate(
        pan_tile_path,
        pan_path,
        options=gdal.TranslateOptions(
            srcWin=[tile.x_offset, tile.y_offset, tile.width, tile.height],
            creationOptions=['COMPRESS=LZW', 'TILED=YES']
        )
    )
    
    # Extract MSI tile  
    gdal.Translate(
        msi_tile_path,
        msi_path,
        options=gdal.TranslateOptions(
            srcWin=[tile.x_offset, tile.y_offset, tile.width, tile.height],
            creationOptions=['COMPRESS=LZW', 'TILED=YES']
        )
    )
    
    return pan_tile_path, msi_tile_path


def process_single_tile(pan_tile_path: str, msi_tile_path: str, 
                       output_tile_path: str, tile_id: str,
                       max_memory_mb: int = 512) -> str:
    """
    Process a single tile with pansharpening.
    
    Args:
        pan_tile_path: Path to PAN tile
        msi_tile_path: Path to MSI tile
        output_tile_path: Path for output pansharpened tile
        tile_id: Tile identifier for logging
        max_memory_mb: Memory limit for processing
        
    Returns:
        Path to processed tile
    """
    logger.info(f"Processing {tile_id}")
    
    # Set up GDAL environment for this process
    os.environ.update({
        'GDAL_CACHEMAX': str(max_memory_mb),
        'GDAL_SWATH_SIZE': str(max_memory_mb // 4),
        'GDAL_DISABLE_READDIR_ON_OPEN': 'TRUE',
        'GDAL_NUM_THREADS': '1',  # Single thread per tile
        'PROJ_LIB': '/opt/conda/envs/gaia/share/proj',
        'GDAL_DATA': '/opt/conda/envs/gaia/share/gdal'
    })
    
    try:
        # Import here to avoid issues with multiprocessing
        from osgeo.utils import gdal_pansharpen
    except ImportError:
        from osgeo_utils import gdal_pansharpen
    
    # Run pansharpening on the tile
    result = gdal_pansharpen.main([
        'gdal_pansharpen',
        '-b', '5',  # Blue band
        '-b', '3',  # Green band  
        '-b', '2',  # Red band
        '-r', 'cubic',
        '-co', 'COMPRESS=LZW',
        '-co', 'TILED=YES',
        pan_tile_path, msi_tile_path, output_tile_path
    ])
    
    if result != 0:
        raise RuntimeError(f"Pansharpening failed for {tile_id}")
    
    # Clean up input tiles to save space
    try:
        os.remove(pan_tile_path)
        os.remove(msi_tile_path)
    except:
        pass
    
    logger.info(f"Completed {tile_id}")
    return output_tile_path


def blend_overlapping_regions(tiles: List[TileInfo], temp_dir: str) -> List[str]:
    """
    Blend overlapping regions between tiles for seamless mosaicking.
    
    Args:
        tiles: List of processed tiles with overlap information
        temp_dir: Temporary directory containing tile files
        
    Returns:
        List of paths to blended tiles ready for mosaicking
    """
    logger.info("Blending overlapping regions between tiles...")
    
    blended_tiles = []
    
    for i, tile in enumerate(tiles):
        tile_path = os.path.join(temp_dir, f"{tile.tile_id}_pansharp.tif")
        
        if not os.path.exists(tile_path):
            logger.warning(f"Tile not found: {tile_path}")
            continue
        
        # For now, use simple approach - crop overlap regions for interior tiles
        # More sophisticated blending can be implemented later
        if tile.x_overlap > 0 or tile.y_overlap > 0:
            blended_path = os.path.join(temp_dir, f"{tile.tile_id}_blended.tif")
            
            # Crop the overlap regions from right and bottom edges
            crop_width = tile.width - (tile.x_overlap // 2)
            crop_height = tile.height - (tile.y_overlap // 2)
            
            gdal.Translate(
                blended_path,
                tile_path,
                options=gdal.TranslateOptions(
                    srcWin=[0, 0, crop_width, crop_height],
                    creationOptions=['COMPRESS=LZW', 'TILED=YES']
                )
            )
            
            blended_tiles.append(blended_path)
        else:
            # No overlap to handle
            blended_tiles.append(tile_path)
    
    logger.info(f"Blended {len(blended_tiles)} tiles")
    return blended_tiles


def mosaic_tiles(blended_tiles: List[str], output_path: str, 
                tiles: List[TileInfo]) -> str:
    """
    Mosaic blended tiles into final output image.
    
    Args:
        blended_tiles: List of paths to blended tiles
        output_path: Path for final mosaicked output
        tiles: Original tile information for positioning
        
    Returns:
        Path to final mosaicked image
    """
    logger.info(f"Mosaicking {len(blended_tiles)} tiles into final output...")
    
    # Create VRT file for mosaicking
    vrt_path = output_path.replace('.tif', '_mosaic.vrt')
    
    # Use gdalbuildvrt to create mosaic
    gdal.BuildVRT(
        vrt_path,
        blended_tiles,
        options=gdal.BuildVRTOptions(
            resolution='highest',
            resampleAlg='cubic',
            addAlpha=False
        )
    )
    
    # Convert VRT to final GeoTIFF
    gdal.Translate(
        output_path,
        vrt_path,
        options=gdal.TranslateOptions(
            format='GTiff',
            creationOptions=[
                'COMPRESS=LZW',
                'TILED=YES',
                'BLOCKXSIZE=512',
                'BLOCKYSIZE=512',
                'BIGTIFF=YES'  # Important for large outputs
            ]
        )
    )
    
    # Clean up VRT
    try:
        os.remove(vrt_path)
    except:
        pass
    
    logger.info(f"Mosaicking complete: {output_path}")
    return output_path


def tiled_pansharpen(pan_path: str, msi_path: str, output_path: str,
                    max_memory_mb: int = 1024, max_workers: int = 2,
                    task_name: str = "TILED_PANSHARPEN",
                    chain_id: Optional[str] = None) -> str:
    """
    Perform pansharpening using tiled processing for large imagery.
    
    Args:
        pan_path: Path to panchromatic image
        msi_path: Path to multispectral image
        output_path: Path for pansharpened output
        max_memory_mb: Maximum memory per tile in MB
        max_workers: Maximum number of parallel workers
        task_name: Task name for logging
        chain_id: Chain ID for logging
        
    Returns:
        Path to pansharpened output
    """
    logger.info(f"[{task_name}][{chain_id}] Starting tiled pansharpening")
    logger.info(f"[{task_name}][{chain_id}] PAN: {pan_path}")
    logger.info(f"[{task_name}][{chain_id}] MSI: {msi_path}")
    logger.info(f"[{task_name}][{chain_id}] Output: {output_path}")
    logger.info(f"[{task_name}][{chain_id}] Memory per tile: {max_memory_mb}MB")
    
    start_time = time.time()
    
    # Log initial memory
    log_memory_usage(task_name, chain_id, "tiled_start")
    
    # Create temporary directory for tiles
    temp_dir = tempfile.mkdtemp(prefix="tiled_pansharpen_")
    logger.info(f"[{task_name}][{chain_id}] Temporary directory: {temp_dir}")
    
    try:
        with MemoryMonitor(task_name, chain_id, critical_threshold=90.0):
            # 1. Calculate optimal tile size
            tile_width, tile_height = calculate_optimal_tile_size(
                pan_path, max_memory_mb, bands=3, dtype_size=2
            )
            
            # 2. Create tile grid
            tiles = create_tile_grid(
                pan_path, msi_path, tile_width, tile_height, overlap_percent=10.0
            )
            
            logger.info(f"[{task_name}][{chain_id}] Processing {len(tiles)} tiles")
            
            # 3. Extract and process tiles
            processed_tiles = []
            
            for i, tile in enumerate(tiles):
                logger.info(f"[{task_name}][{chain_id}] Processing tile {i+1}/{len(tiles)}: {tile}")
                
                # Extract tile data
                pan_tile_path, msi_tile_path = extract_tile_data(
                    pan_path, msi_path, tile, temp_dir
                )
                
                # Process tile
                output_tile_path = os.path.join(temp_dir, f"{tile.tile_id}_pansharp.tif")
                
                processed_tile = process_single_tile(
                    pan_tile_path, msi_tile_path, output_tile_path,
                    tile.tile_id, max_memory_mb // 2  # Conservative memory per tile
                )
                
                processed_tiles.append(processed_tile)
                
                # Force garbage collection after each tile
                force_garbage_collection(task_name, chain_id)
                
                # Check memory pressure
                if check_memory_pressure(task_name, chain_id, threshold_percent=85):
                    logger.warning(f"[{task_name}][{chain_id}] High memory pressure after tile {i+1}")
            
            # 4. Blend overlapping regions
            blended_tiles = blend_overlapping_regions(tiles, temp_dir)
            
            # 5. Mosaic final output
            final_output = mosaic_tiles(blended_tiles, output_path, tiles)
            
            # Verify output
            if not os.path.exists(final_output):
                raise RuntimeError(f"Final output not created: {final_output}")
            
            output_size = os.path.getsize(final_output)
            processing_time = time.time() - start_time
            
            logger.info(f"[{task_name}][{chain_id}] Tiled pansharpening completed!")
            logger.info(f"[{task_name}][{chain_id}] Output: {final_output}")
            logger.info(f"[{task_name}][{chain_id}] Size: {output_size / 1024**3:.2f}GB")
            logger.info(f"[{task_name}][{chain_id}] Time: {processing_time:.1f} seconds")
            
            return final_output
            
    except Exception as e:
        logger.error(f"[{task_name}][{chain_id}] Tiled pansharpening failed: {e}")
        raise
        
    finally:
        # Clean up temporary directory
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"[{task_name}][{chain_id}] Cleaned up temporary directory")
        except Exception as e:
            logger.warning(f"[{task_name}][{chain_id}] Failed to clean up temp dir: {e}")
        
        # Final memory log
        log_memory_usage(task_name, chain_id, "tiled_end")
