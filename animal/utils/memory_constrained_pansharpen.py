#!/usr/bin/env python3
# -------------------------------------------------------------------------------
# ----- memory_constrained_pansharpen.py ---------------------------------------
# -------------------------------------------------------------------------------
#
#    purpose: Thin compatibility wrapper.
#             The canonical implementation lives in animal.utils.memory_utils.
#             This module re-exports so existing callers resolve without changes.
#
#    GAIFAGP-476: Consolidated — do NOT add logic here.
#
# -------------------------------------------------------------------------------

from animal.utils.memory_utils import (            # noqa: F401
    memory_constrained_pansharpen,
    get_processing_method,
    MemoryMonitor,
    log_memory_usage,
    check_memory_pressure,
    emergency_memory_cleanup,
    get_optimal_tile_size,
)


def estimate_output_size(pan_size_bytes: int, msi_size_bytes: int) -> int:
    """
    Estimate pansharpened output size.

    Args:
        pan_size_bytes: Panchromatic file size
        msi_size_bytes: Multispectral file size

    Returns:
        Estimated output size in bytes
    """
    # Rough estimate: pansharpened is typically 3-5x MSI size (conservative 4x)
    estimated = msi_size_bytes * 4
    max_estimate = (pan_size_bytes + msi_size_bytes) * 2
    return min(estimated, max_estimate)