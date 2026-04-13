"""
Vendor ID Resolution and Auto-Mapping Utilities

This module handles cases where vendor IDs exist in POI records but have no corresponding
USGS entity IDs. It attempts various strategies to resolve these mappings.
"""

import re
import logging
from typing import Optional, List, Tuple, Dict
from django.conf import settings
from animal.models import EarthExplorer, PointsOfInterest
from animal.utils.api_utils import ee_login, search_imagery
from shapely.geometry import Point, Polygon
import requests
import geopandas as gpd
from datetime import datetime

logger = logging.getLogger(__name__)

class VendorIDResolver:
    """Resolves vendor IDs to USGS entity IDs using various strategies."""
    
    def __init__(self):
        self.session = None
        
    def get_session(self):
        """Get or create authenticated USGS session."""
        if self.session is None:
            self.session = requests.Session()
            self.session = ee_login(self.session, settings.USGS_USERNAME, settings.USGS_TOKEN)
        return self.session
    
    def parse_vendor_id_date(self, vendor_id: str) -> Optional[datetime]:
        """
        Extract date from vendor ID format like '21MAR21152113-...'
        
        Args:
            vendor_id: Vendor ID string
            
        Returns:
            datetime object or None if parsing fails
        """
        try:
            # Match format: DDMMMYY... (e.g., 21MAR21...)
            match = re.match(r'^(\d{2})([A-Z]{3})(\d{2})', vendor_id)
            if not match:
                return None
                
            day_str, month_str, year_str = match.groups()
            
            month_map = {
                'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
                'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
            }
            
            day = int(day_str)
            month = month_map.get(month_str.upper())
            year = 2000 + int(year_str)  # Assume 20XX
            
            if month is None:
                return None
                
            return datetime(year, month, day)
            
        except (ValueError, AttributeError) as e:
            logger.debug(f"Failed to parse date from vendor_id {vendor_id}: {e}")
            return None
    
    def get_poi_spatial_bounds(self, vendor_id: str) -> Optional[Polygon]:
        """
        Get spatial bounds from POI records with this vendor ID.
        
        Args:
            vendor_id: Vendor ID to search for
            
        Returns:
            Polygon representing the bounding box of all POIs, or None
        """
        try:
            poi_records = PointsOfInterest.objects.filter(vendor_id=vendor_id)
            if not poi_records.exists():
                return None
                
            # Get all points
            points = []
            for poi in poi_records:
                if poi.point:
                    points.append((poi.point.x, poi.point.y))
            
            if len(points) < 2:
                return None
                
            # Create bounding box
            min_x = min(p[0] for p in points)
            max_x = max(p[0] for p in points)
            min_y = min(p[1] for p in points)
            max_y = max(p[1] for p in points)
            
            # Add small buffer (0.01 degrees ~ 1km)
            buffer = 0.01
            bbox = Polygon([
                (min_x - buffer, min_y - buffer),
                (max_x + buffer, min_y - buffer),
                (max_x + buffer, max_y + buffer),
                (min_x - buffer, max_y + buffer),
                (min_x - buffer, min_y - buffer)
            ])
            
            logger.info(f"Created spatial bounds for {vendor_id}: {len(points)} POIs -> bbox")
            return bbox
            
        except Exception as e:
            logger.error(f"Failed to get spatial bounds for {vendor_id}: {e}")
            return None
    
    def search_usgs_by_date_location(self, vendor_id: str) -> List[str]:
        """
        Search USGS for imagery matching the vendor ID's date and location.
        
        Args:
            vendor_id: Vendor ID to resolve
            
        Returns:
            List of potential USGS entity IDs
        """
        try:
            # Parse date from vendor ID
            image_date = self.parse_vendor_id_date(vendor_id)
            if not image_date:
                logger.warning(f"Cannot parse date from vendor_id: {vendor_id}")
                return []
            
            # Get spatial bounds from POI records
            bbox = self.get_poi_spatial_bounds(vendor_id)
            if not bbox:
                logger.warning(f"Cannot determine spatial bounds for vendor_id: {vendor_id}")
                return []
            
            # Search USGS for imagery in date range ±7 days
            start_date = image_date.strftime("%Y-%m-%d")
            end_date = image_date.strftime("%Y-%m-%d")  # Same day for now
            
            logger.info(f"Searching USGS for {vendor_id}: date={start_date}, bbox area={bbox.area:.6f}")
            
            session = self.get_session()
            results_gdf = search_imagery(
                aoi=bbox,
                dataset="crssp_orderable_w3", 
                start=start_date,
                end=end_date,
                session=session
            )
            
            if len(results_gdf) == 0:
                logger.info(f"No USGS imagery found for {vendor_id} on {start_date}")
                return []
            
            entity_ids = results_gdf['Entity ID'].tolist()
            logger.info(f"Found {len(entity_ids)} potential entity IDs for {vendor_id}")
            return entity_ids
            
        except Exception as e:
            logger.error(f"USGS search failed for {vendor_id}: {e}")
            return []
    
    def resolve_vendor_to_entity(self, vendor_id: str, auto_create_mapping: bool = False) -> Optional[str]:
        """
        Attempt to resolve a vendor ID to a USGS entity ID.
        
        Args:
            vendor_id: Vendor ID to resolve
            auto_create_mapping: Whether to automatically create database mappings
            
        Returns:
            USGS entity ID if found, None otherwise
        """
        logger.info(f"🔍 Attempting to resolve vendor_id: {vendor_id}")
        
        # Strategy 1: Check existing database mappings
        existing_mapping = self.check_existing_mappings(vendor_id)
        if existing_mapping:
            logger.info(f"✅ Found existing mapping: {vendor_id} -> {existing_mapping}")
            return existing_mapping
        
        # Strategy 2: Search USGS by date/location
        potential_entities = self.search_usgs_by_date_location(vendor_id)
        
        if not potential_entities:
            logger.warning(f"❌ No USGS entity IDs found for vendor_id: {vendor_id}")
            return None
        
        if len(potential_entities) == 1:
            entity_id = potential_entities[0]
            logger.info(f"✅ Single match found: {vendor_id} -> {entity_id}")
            
            if auto_create_mapping:
                self.create_mapping(vendor_id, entity_id)
                
            return entity_id
        
        # Multiple matches - need manual review
        logger.warning(f"⚠️ Multiple matches for {vendor_id}: {potential_entities[:5]}")
        return potential_entities[0]  # Return first match for now
    
    def check_existing_mappings(self, vendor_id: str) -> Optional[str]:
        """Check if vendor_id already has entity_id mapping in database."""
        try:
            # Check EarthExplorer table
            ee_records = EarthExplorer.objects.filter(vendor_id=vendor_id)
            if ee_records.exists():
                return ee_records.first().entity_id
            
            # Check POI table for any records with entity_id set
            poi_records = PointsOfInterest.objects.filter(
                vendor_id=vendor_id
            ).exclude(entity_id__isnull=True).exclude(entity_id='')
            
            if poi_records.exists():
                return poi_records.first().entity_id
                
            return None
            
        except Exception as e:
            logger.error(f"Error checking existing mappings for {vendor_id}: {e}")
            return None
    
    def create_mapping(self, vendor_id: str, entity_id: str):
        """Create vendor_id -> entity_id mapping in database."""
        try:
            # Update POI records to include the entity_id
            updated_count = PointsOfInterest.objects.filter(
                vendor_id=vendor_id,
                entity_id__isnull=True
            ).update(entity_id=entity_id)
            
            logger.info(f"✅ Updated {updated_count} POI records with entity_id {entity_id}")
            
        except Exception as e:
            logger.error(f"Failed to create mapping {vendor_id} -> {entity_id}: {e}")

# Utility functions for use in other modules
def resolve_vendor_id(vendor_id: str) -> Optional[str]:
    """
    Quick utility to resolve a vendor ID to entity ID.
    
    Args:
        vendor_id: Vendor ID to resolve
        
    Returns:
        USGS entity ID if found, None otherwise
    """
    resolver = VendorIDResolver()
    return resolver.resolve_vendor_to_entity(vendor_id, auto_create_mapping=False)

def bulk_resolve_vendor_ids(vendor_ids: List[str]) -> Dict[str, Optional[str]]:
    """
    Resolve multiple vendor IDs to entity IDs.
    
    Args:
        vendor_ids: List of vendor IDs to resolve
        
    Returns:
        Dict mapping vendor_id -> entity_id (or None if not found)
    """
    resolver = VendorIDResolver()
    results = {}
    
    for vendor_id in vendor_ids:
        try:
            entity_id = resolver.resolve_vendor_to_entity(vendor_id)
            results[vendor_id] = entity_id
        except Exception as e:
            logger.error(f"Failed to resolve {vendor_id}: {e}")
            results[vendor_id] = None
    
    return results
