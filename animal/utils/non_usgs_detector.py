"""
Non-USGS Vendor ID Detection Utilities

This module identifies vendor IDs that don't correspond to USGS imagery
and should be skipped during USGS download attempts.
"""

import re
import logging
from typing import Optional
from animal.models import PointsOfInterest

logger = logging.getLogger(__name__)

# Known patterns for non-USGS vendor IDs
NON_USGS_PATTERNS = [
    r'.*-S1BS-.*',      # Sentinel-1 pattern (S1BS = Sentinel-1 B Single look)
    r'.*-S2[AB]-.*',    # Sentinel-2 pattern  
    r'^S1[AB]_.*',      # ESA Sentinel-1 naming
    r'^S2[AB]_.*',      # ESA Sentinel-2 naming
    r'.*_S1_.*',        # Alternative Sentinel-1 format
]

def is_usgs_vendor_id(vendor_id: str) -> bool:
    """
    Check if a vendor ID corresponds to USGS-available imagery.
    
    Args:
        vendor_id: Vendor ID to check
        
    Returns:
        True if likely USGS imagery, False if non-USGS
    """
    if not vendor_id:
        return False
    
    # Check database flag first (if exists)
    try:
        poi_record = PointsOfInterest.objects.filter(vendor_id=vendor_id).first()
        if poi_record and hasattr(poi_record, 'is_usgs_available'):
            if poi_record.is_usgs_available is False:
                logger.info(f"Vendor ID {vendor_id} marked as non-USGS in database")
                return False
            elif poi_record.is_usgs_available is True:
                return True
    except Exception as e:
        logger.debug(f"Database check failed for {vendor_id}: {e}")
    
    # Pattern-based detection for known non-USGS formats
    for pattern in NON_USGS_PATTERNS:
        if re.match(pattern, vendor_id, re.IGNORECASE):
            logger.info(f"Vendor ID {vendor_id} matches non-USGS pattern: {pattern}")
            return False
    
    # Default to assuming USGS unless proven otherwise
    return True

def classify_vendor_id_source(vendor_id: str) -> str:
    """
    Classify the likely data source for a vendor ID.
    
    Args:
        vendor_id: Vendor ID to classify
        
    Returns:
        String indicating likely source: 'USGS', 'SENTINEL', 'UNKNOWN'
    """
    if not vendor_id:
        return 'UNKNOWN'
    
    # Sentinel patterns
    if re.match(r'.*-S1BS-.*', vendor_id, re.IGNORECASE):
        return 'SENTINEL_1'
    if re.match(r'.*-S2[AB]-.*', vendor_id, re.IGNORECASE):
        return 'SENTINEL_2'
    if re.match(r'^S1[AB]_.*', vendor_id, re.IGNORECASE):
        return 'SENTINEL_1'
    if re.match(r'^S2[AB]_.*', vendor_id, re.IGNORECASE):
        return 'SENTINEL_2'
    
    # USGS/Maxar patterns (typical format: DDMMMYY??????-[MP]1BS-??????_??_P???)
    if re.match(r'^\d{2}[A-Z]{3}\d{2}\d{6}-(M|P)1BS-\d+_\d+_P\d+$', vendor_id):
        return 'USGS_MAXAR'
    
    # Landsat patterns
    if re.match(r'^LC\d{2}_L\d[A-Z]{2}_\d{6}_\d{8}_\d{8}_\d{2}_T[12]$', vendor_id):
        return 'USGS_LANDSAT'
    
    return 'UNKNOWN'

def get_download_skip_reason(vendor_id: str) -> Optional[str]:
    """
    Get reason why a vendor ID should be skipped for USGS download.
    
    Args:
        vendor_id: Vendor ID to check
        
    Returns:
        Skip reason string if should skip, None if downloadable
    """
    if not is_usgs_vendor_id(vendor_id):
        source = classify_vendor_id_source(vendor_id)
        return f"Non-USGS imagery source: {source}"
    
    return None

def mark_vendor_id_non_usgs(vendor_id: str, reason: str = None):
    """
    Mark a vendor ID as non-USGS in the database.
    
    Args:
        vendor_id: Vendor ID to mark
        reason: Optional reason for marking
    """
    try:
        updated_count = PointsOfInterest.objects.filter(
            vendor_id=vendor_id
        ).update(is_usgs_available=False)
        
        logger.info(f"Marked {updated_count} POI records for vendor_id {vendor_id} as non-USGS")
        if reason:
            logger.info(f"Reason: {reason}")
            
    except Exception as e:
        logger.error(f"Failed to mark vendor_id {vendor_id} as non-USGS: {e}")

# Quick utility functions
def should_skip_usgs_download(vendor_id: str) -> bool:
    """Quick check if vendor ID should skip USGS download."""
    return not is_usgs_vendor_id(vendor_id)

def get_vendor_id_type(vendor_id: str) -> dict:
    """Get comprehensive information about a vendor ID."""
    return {
        'vendor_id': vendor_id,
        'is_usgs': is_usgs_vendor_id(vendor_id),
        'source': classify_vendor_id_source(vendor_id),
        'skip_reason': get_download_skip_reason(vendor_id),
        'should_skip': should_skip_usgs_download(vendor_id)
    }
