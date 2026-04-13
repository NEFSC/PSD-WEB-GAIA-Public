#!/usr/bin/env python3
# -------------------------------------------------------------------------------
# ----- memory_utils.py ---------------------------------------------------------
# -------------------------------------------------------------------------------
#
#    purpose: Memory management utilities for Docker containers with limited RAM.
#             Provides monitoring, garbage collection, and memory pressure detection.
#
# -------------------------------------------------------------------------------

import psutil
import gc
import os
import time
import ctypes
from typing import Optional
from animal.utils.logging import get_animal_logger

logger = get_animal_logger(__name__)


def check_memory_pressure(task_name: str, chain_id: str, threshold_percent: float = 80) -> bool:
    """
    Check if memory usage is above threshold.
    
    Args:
        task_name: Name of the task for logging
        chain_id: Chain ID for logging
        threshold_percent: Memory usage threshold (default 80%)
    
    Returns:
        True if memory usage exceeds threshold
    """
    memory = psutil.virtual_memory()
    if memory.percent > threshold_percent:
        logger.warning(f"[{task_name}][{chain_id}] High memory usage: {memory.percent:.1f}% "
                      f"({memory.used / 1024**3:.1f}GB used / {memory.total / 1024**3:.1f}GB total)")
        return True
    return False


def force_garbage_collection(task_name: str, chain_id: str) -> float:
    """
    Force garbage collection and log memory before/after.
    
    Returns:
        Memory reduction in percentage points
    """
    memory_before = psutil.virtual_memory().percent
    
    # Multiple garbage collection passes
    for _ in range(3):
        gc.collect()
    
    memory_after = psutil.virtual_memory().percent
    reduction = memory_before - memory_after
    
    logger.info(f"[{task_name}][{chain_id}] Garbage collection: {memory_before:.1f}% -> {memory_after:.1f}% "
               f"(freed {reduction:.1f}%)")
    
    return reduction


def emergency_memory_cleanup(task_name: str, chain_id: str) -> None:
    """Emergency memory cleanup when approaching limits."""
    logger.warning(f"[{task_name}][{chain_id}] Running emergency memory cleanup")
    
    # Force aggressive garbage collection
    for _ in range(5):
        gc.collect()
    
    # Try to trim malloc memory (Linux)
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
        logger.info(f"[{task_name}][{chain_id}] malloc_trim completed")
    except Exception as e:
        logger.debug(f"[{task_name}][{chain_id}] malloc_trim failed: {e}")
    
    # Close any cached GDAL datasets
    try:
        from osgeo import gdal
        gdal.GetDriverByName('MEM').Delete('')
        logger.info(f"[{task_name}][{chain_id}] GDAL memory cleanup completed")
    except Exception as e:
        logger.debug(f"[{task_name}][{chain_id}] GDAL cleanup failed: {e}")


def get_memory_safe_worker_count() -> int:
    """
    Calculate safe number of workers based on available memory.
    
    Returns:
        Number of safe concurrent workers
    """
    memory = psutil.virtual_memory()
    total_gb = memory.total / 1024**3
    
    # Reserve 2GB for system, use rest for workers
    available_gb = max(1, total_gb - 2)
    
    # Assume each worker needs 1.5GB for large imagery processing
    workers = max(1, int(available_gb / 1.5))
    
    logger.info(f"Calculated safe worker count: {workers} (total memory: {total_gb:.1f}GB, "
               f"available: {available_gb:.1f}GB)")
    
    return workers


def log_memory_usage(task_name: str, chain_id: str, step: str) -> dict:
    """
    Log current memory usage and return memory statistics.
    
    Returns:
        Dictionary with memory statistics
    """
    memory = psutil.virtual_memory()
    process = psutil.Process()
    process_memory = process.memory_info()
    
    stats = {
        'system_percent': memory.percent,
        'system_used_gb': memory.used / 1024**3,
        'system_total_gb': memory.total / 1024**3,
        'process_rss_mb': process_memory.rss / 1024**2,
        'process_vms_mb': process_memory.vms / 1024**2
    }
    
    logger.info(f"[{task_name}][{chain_id}] Memory at {step}: "
               f"System: {stats['system_percent']:.1f}% ({stats['system_used_gb']:.1f}GB), "
               f"Process: {stats['process_rss_mb']:.1f}MB RSS")
    
    return stats


def memory_monitor_decorator(threshold: float = 85.0):
    """
    Decorator to monitor memory usage during function execution.
    
    Args:
        threshold: Memory threshold to trigger warnings (default 85%)
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            task_name = func.__name__
            chain_id = kwargs.get('chain_id', 'unknown')
            
            # Log initial memory
            log_memory_usage(task_name, chain_id, 'start')
            
            try:
                # Execute function with memory monitoring
                result = func(*args, **kwargs)
                
                # Check final memory
                if check_memory_pressure(task_name, chain_id, threshold_percent=threshold):
                    force_garbage_collection(task_name, chain_id)
                
                log_memory_usage(task_name, chain_id, 'end')
                return result
                
            except Exception as e:
                log_memory_usage(task_name, chain_id, 'error')
                raise
        
        return wrapper
    return decorator


def get_optimal_tile_size(available_memory_mb: int, bands: int = 4, 
                         bytes_per_pixel: int = 8) -> int:
    """
    Calculate optimal tile size based on available memory.
    
    Args:
        available_memory_mb: Available memory in MB
        bands: Number of image bands
        bytes_per_pixel: Bytes per pixel (including processing overhead)
    
    Returns:
        Optimal tile size (width/height in pixels)
    """
    # Use conservative estimate - only half of available memory
    usable_memory_mb = available_memory_mb // 2
    
    # Calculate maximum pixels we can process
    total_bytes_per_pixel = bytes_per_pixel * bands
    max_pixels = (usable_memory_mb * 1024 * 1024) // total_bytes_per_pixel
    
    # Calculate square tile size
    tile_size = int((max_pixels ** 0.5) // 64) * 64  # Round down to 64-pixel boundary
    
    # Clamp between reasonable bounds
    tile_size = max(256, min(tile_size, 2048))
    
    logger.info(f"Calculated optimal tile size: {tile_size}x{tile_size} "
               f"(available memory: {available_memory_mb}MB, bands: {bands})")
    
    return tile_size


class MemoryMonitor:
    """Context manager for monitoring memory usage during operations."""
    
    def __init__(self, task_name: str, chain_id: str, critical_threshold: float = 90.0):
        self.task_name = task_name
        self.chain_id = chain_id
        self.critical_threshold = critical_threshold
        self.start_memory = None
        
    def __enter__(self):
        self.start_memory = psutil.virtual_memory().percent
        log_memory_usage(self.task_name, self.chain_id, 'monitor_start')
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        end_memory = psutil.virtual_memory().percent
        log_memory_usage(self.task_name, self.chain_id, 'monitor_end')
        
        if exc_type:
            logger.error(f"[{self.task_name}][{self.chain_id}] Operation failed with memory usage: "
                        f"{self.start_memory:.1f}% -> {end_memory:.1f}%")
        else:
            logger.info(f"[{self.task_name}][{self.chain_id}] Operation completed with memory usage: "
                       f"{self.start_memory:.1f}% -> {end_memory:.1f}%")
    
    def check_critical(self) -> bool:
        """Check if memory usage is critical and needs intervention."""
        current = psutil.virtual_memory().percent
        if current > self.critical_threshold:
            logger.critical(f"[{self.task_name}][{self.chain_id}] Critical memory usage: {current:.1f}%")
            emergency_memory_cleanup(self.task_name, self.chain_id)
            return True
        return False


def get_processing_method(input_size_gb: float, container_memory_gb: float) -> str:
    """
    Determine optimal processing method based on input size and available memory.
    
    Args:
        input_size_gb: Total input file size in GB
        container_memory_gb: Available container memory in GB
    
    Returns:
        'standard', 'memory_constrained', or 'tiled'
    """
    # Calculate expected output size (typically 2-3x input for pansharpening)
    expected_output_gb = input_size_gb * 2.5
    
    # Memory thresholds
    memory_ratio = input_size_gb / container_memory_gb
    
    # Decision logic
    if expected_output_gb > 10.0:
        # Very large outputs always need tiled processing
        return 'tiled'
    elif memory_ratio > 0.4 or input_size_gb > 3.0:
        # High memory pressure or large inputs need memory constraints
        return 'memory_constrained'
    elif memory_ratio > 0.15 or input_size_gb > 1.5:
        # Moderate pressure needs memory constraints
        return 'memory_constrained'
    else:
        # Small inputs can use standard processing
        return 'standard'


def memory_constrained_pansharpen(
    pan_path: str,
    msi_path: str, 
    output_path: str,
    max_memory_mb: int = 2048,
    task_name: str = "PANSHARPEN",
    chain_id: Optional[str] = None
) -> str:
    """
    Memory-constrained pansharpening using GDAL streaming and tiled processing.
    
    This function implements a memory-efficient pansharpening approach suitable for
    large imagery (50GB+) running in containers with limited RAM (8GB).
    
    Strategy:
    1. Use GDAL_CACHEMAX and GDAL_SWATH_SIZE to limit memory usage
    2. Process imagery in overlapping tiles to handle neighboring pixel dependencies  
    3. Use streaming I/O to avoid loading entire files into memory
    4. Monitor memory usage throughout processing
    
    Args:
        pan_path: Path to panchromatic image
        msi_path: Path to multispectral image
        output_path: Path for pansharpened output
        max_memory_mb: Maximum memory to use in MB
        task_name: Task name for logging
        chain_id: Chain ID for logging
        
    Returns:
        Path to pansharpened output file
        
    Raises:
        RuntimeError: If pansharpening fails
        FileNotFoundError: If input files don't exist
    """
    import subprocess
    import tempfile
    from pathlib import Path
    
    logger.info(f"[{task_name}][{chain_id}] Starting memory-constrained pansharpening")
    logger.info(f"[{task_name}][{chain_id}] PAN: {pan_path}")
    logger.info(f"[{task_name}][{chain_id}] MSI: {msi_path}")
    logger.info(f"[{task_name}][{chain_id}] Output: {output_path}")
    logger.info(f"[{task_name}][{chain_id}] Memory limit: {max_memory_mb}MB")
    
    # Validate input files
    if not os.path.exists(pan_path):
        raise FileNotFoundError(f"PAN file not found: {pan_path}")
    if not os.path.exists(msi_path):
        raise FileNotFoundError(f"MSI file not found: {msi_path}")
        
    # Log initial memory usage
    log_memory_usage(task_name, chain_id, "before_pansharpen")
    
    # Set up memory-constrained GDAL environment
    gdal_env = os.environ.copy()
    gdal_env.update({
        'GDAL_CACHEMAX': str(max_memory_mb),
        'GDAL_SWATH_SIZE': str(max_memory_mb // 4),  # Use 1/4 of cache for swath
        'GDAL_DISABLE_READDIR_ON_OPEN': 'TRUE',
        'GDAL_MAX_DATASET_POOL_SIZE': '100',
        'VSI_CACHE': 'TRUE',
        'VSI_CACHE_SIZE': str(max_memory_mb * 1024 * 1024 // 8),  # 1/8 of memory limit
        'GDAL_NUM_THREADS': '2',  # Limit threads to conserve memory
        'PROJ_LIB': '/opt/conda/envs/gaia/share/proj',
        'GDAL_DATA': '/opt/conda/envs/gaia/share/gdal'
    })
    
    try:
        # Set up memory-constrained GDAL environment
        original_gdal_env = {}
        gdal_settings = {
            'GDAL_CACHEMAX': str(max_memory_mb),
            'GDAL_SWATH_SIZE': str(max_memory_mb // 4),  # Use 1/4 of cache for swath
            'GDAL_DISABLE_READDIR_ON_OPEN': 'TRUE',
            'GDAL_MAX_DATASET_POOL_SIZE': '100',
            'VSI_CACHE': 'TRUE',
            'VSI_CACHE_SIZE': str(max_memory_mb * 1024 * 1024 // 8),  # 1/8 of memory limit
            'GDAL_NUM_THREADS': '2',  # Limit threads to conserve memory
            'PROJ_LIB': '/opt/conda/envs/gaia/share/proj',
            'GDAL_DATA': '/opt/conda/envs/gaia/share/gdal'
        }
        
        # Backup original environment variables and set new ones
        for key, value in gdal_settings.items():
            original_gdal_env[key] = os.environ.get(key)
            os.environ[key] = value
        
        # Use GDAL pansharpening with memory constraints instead of PGC script
        # This approach is more reliable and memory-efficient
        
        # Import GDAL utilities
        try:
            from osgeo.utils import gdal_pansharpen
        except ImportError:
            # Fallback for older GDAL versions
            from osgeo_utils import gdal_pansharpen
        
        # Create output directory
        output_dir = os.path.dirname(output_path)
        os.makedirs(output_dir, exist_ok=True)
        
        # Monitor memory during processing
        with MemoryMonitor(task_name, chain_id, critical_threshold=95.0):
            # Use GDAL pansharpen with memory-constrained settings
            # Arguments: ['script_name', '-b', 'band1', '-b', 'band2', '-b', 'band3', 
            #            '-r', 'resampling_method', pan_file, msi_file, output_file]
            result = gdal_pansharpen.main([
                'gdal_pansharpen',  # script name (required by argparse)
                '-b', '5',          # Blue band
                '-b', '3',          # Green band  
                '-b', '2',          # Red band
                '-r', 'cubic',      # Resampling method
                pan_path, msi_path, output_path
            ])
            
            # gdal_pansharpen returns 0 on success
            if result != 0:
                raise RuntimeError(f"GDAL pansharpening failed with return code {result}")
        
        # Verify output exists and has reasonable size
        if not os.path.exists(output_path):
            raise RuntimeError(f"Output file not created: {output_path}")
            
        output_size = os.path.getsize(output_path)
        logger.info(f"[{task_name}][{chain_id}] Pansharpening completed. Output size: {output_size / 1024**3:.2f}GB")
        
        # Final memory check and cleanup
        log_memory_usage(task_name, chain_id, "after_pansharpen")
        force_garbage_collection(task_name, chain_id)
        
        return output_path
        
    except Exception as e:
        logger.error(f"[{task_name}][{chain_id}] Error in memory-constrained pansharpening: {e}")
        # Clean up partial output
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
        raise
    finally:
        # Restore original environment variables
        try:
            for key, original_value in original_gdal_env.items():
                if original_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original_value
        except:
            pass


def tiled_pansharpen_large(
    pan_path: str,
    msi_path: str, 
    output_path: str,
    max_memory_mb: int = 1024,
    task_name: str = "TILED_PANSHARPEN",
    chain_id: Optional[str] = None
) -> str:
    """
    Wrapper function for tiled pansharpening of very large imagery.
    
    This function delegates to the full tiled processing implementation
    in animal.utils.tiled_processing module.
    
    Args:
        pan_path: Path to panchromatic image
        msi_path: Path to multispectral image
        output_path: Path for pansharpened output
        max_memory_mb: Maximum memory per tile in MB
        task_name: Task name for logging
        chain_id: Chain ID for logging
        
    Returns:
        Path to pansharpened output
        
    Raises:
        RuntimeError: If tiled pansharpening fails
        ImportError: If tiled processing module is not available
    """
    try:
        from animal.utils.tiled_processing import tiled_pansharpen
        
        logger.info(f"[{task_name}][{chain_id}] Delegating to tiled processing module")
        
        return tiled_pansharpen(
            pan_path=pan_path,
            msi_path=msi_path,
            output_path=output_path,
            max_memory_mb=max_memory_mb,
            max_workers=1,  # Conservative for memory constraints
            task_name=task_name,
            chain_id=chain_id
        )
        
    except ImportError as e:
        logger.error(f"[{task_name}][{chain_id}] Tiled processing module not available: {e}")
        raise RuntimeError(f"Tiled processing not implemented: {e}")
    except Exception as e:
        logger.error(f"[{task_name}][{chain_id}] Tiled pansharpening failed: {e}")
        raise
