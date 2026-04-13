# Basic stack
import os
import django
import csv
import logging
import re
from datetime import datetime
from django.shortcuts import render
from django.http import HttpResponse
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from ..models import AGE_CHOICES, Annotations, PointsOfInterest, Classification, Target, Project

# For coordinate transformations
from pyproj import CRS, Transformer

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gaia.settings')
os.environ["CPL_DEBUG"] = "ON" # Should enable GDAL debuggin
django.setup()

# Setup logger
logger = logging.getLogger(__name__)

MONTH_ABBR_TO_NUM = {
    'JAN': 1,
    'FEB': 2,
    'MAR': 3,
    'APR': 4,
    'MAY': 5,
    'JUN': 6,
    'JUL': 7,
    'AUG': 8,
    'SEP': 9,
    'OCT': 10,
    'NOV': 11,
    'DEC': 12,
}

SENSOR_NAME_MAP = {
    'WV02': 'Vantor WorldView-02',
    'WV03': 'Vantor WorldView-03',
}


def _extract_gmt_datetime_from_vendor_id(vendor_id):
    """
    Extract a UTC datetime from vendor_id formats that typically encode date/time.

    Supported patterns:
    - YYMMMDDHHMMSS
    - YYMMMMMDDHHMMSS (extra numeric month block after MMM)
    """
    if not vendor_id:
        return None

    vendor_id_upper = vendor_id.upper()

    patterns = [
        re.compile(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{2})(\d{2})(\d{2})', re.IGNORECASE),
        re.compile(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', re.IGNORECASE),
    ]

    for pattern in patterns:
        match = pattern.search(vendor_id_upper)
        if not match:
            continue

        try:
            year = 2000 + int(match.group(1))
            month = MONTH_ABBR_TO_NUM[match.group(2)]

            if len(match.groups()) == 6:
                day = int(match.group(3))
                hour = int(match.group(4))
                minute = int(match.group(5))
                second = int(match.group(6))
            else:
                # Group 3 is an optional extra numeric month block in this variant.
                day = int(match.group(4))
                hour = int(match.group(5))
                minute = int(match.group(6))
                second = int(match.group(7))

            return datetime(year, month, day, hour, minute, second)
        except Exception:
            continue

    return None


def _expand_sensor_name(sensor):
    if not sensor:
        return ''

    sensor_str = str(sensor).strip()
    if not sensor_str:
        return ''

    return SENSOR_NAME_MAP.get(sensor_str.upper(), sensor_str)

def dissemination_page(request, project_id):
    """ This page is to inform the scientific investigators as to what is currently within
            the data so that they can make decisions based on it (e.g., task for additional
            satellite imagery) (GAIFAGP-46).
    """
    project = Project.objects.get(id=project_id)
    context = {
        'project_id': project_id,
        'project': project,
    }
    return render(request, 'dissemination_page.html', context)

@login_required
def export_whale_annotations_bas(request, project_id):
    """
    Export confirmed whale annotations in BAS CSV format for a specific project
    """
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="whale_annotations_bas_project_{project_id}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    
    # BAS CSV format headers based on the specification
    headers = [
        'observation_id',
        'latitude_wgs84',    # Updated to indicate WGS84 coordinate system
        'longitude_wgs84',   # Updated to indicate WGS84 coordinate system
        'date_time',
        'species',
        'count',
        'confidence',
        'image_source',
        'image_date',
        'observer',
        'comments'
    ]
    writer.writerow(headers)
    
    # Filter for whale annotations with final classification for the specific project
    # Using final_classification and final_species from PointsOfInterest as these represent confirmed annotations
    # Filter for whale-related species (assuming whale species contain 'whale' in their label)
    whale_annotations = PointsOfInterest.objects.filter(
        project_id=project_id,
        final_classification__isnull=False,
        final_species__isnull=False,
    ).select_related('final_classification', 'final_species', 'final_confidence').prefetch_related('annotations')
    
    # Group POIs by EPSG code for efficient coordinate transformation
    pois_by_epsg = {}
    for poi in whale_annotations:
        epsg_key = str(poi.epsg_code) if poi.epsg_code else '4326'
        if epsg_key not in pois_by_epsg:
            pois_by_epsg[epsg_key] = []
        pois_by_epsg[epsg_key].append(poi)
    
    # Create transformers once per EPSG code
    transformers = {}
    for epsg_code in pois_by_epsg.keys():
        if epsg_code != '4326':
            try:
                source_crs = CRS(f"EPSG:{epsg_code}")
                target_crs = CRS("EPSG:4326")
                transformers[epsg_code] = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            except Exception as e:
                logger.warning(f"Could not create transformer for EPSG:{epsg_code}: {e}")
                transformers[epsg_code] = None
    
    # Process POIs by EPSG code and export data
    for epsg_code, poi_group in pois_by_epsg.items():
        transformer = transformers.get(epsg_code)
        
        for poi in poi_group:
            # Get the latitude and longitude from the point geometry
            if poi.point:
                longitude = float(poi.point.x)
                latitude = float(poi.point.y)
                
                # Transform coordinates to WGS84 (EPSG:4326) if needed
                if epsg_code != '4326' and transformer:
                    try:
                        longitude, latitude = transformer.transform(longitude, latitude)
                    except Exception as e:
                        logger.warning(f"Could not transform coordinates for POI {poi.id}: {e}")
            else:
                latitude = ''
                longitude = ''
            
            # Get the most recent annotation for additional details
            latest_annotation = poi.annotations.first() if poi.annotations.exists() else None
            annotation_comments = [
                annotation.comments.strip()
                for annotation in poi.annotations.all()
                if annotation.comments and annotation.comments.strip()
            ]
            all_comments = ' | '.join(annotation_comments)
            export_comments = poi.final_comments.strip() if poi.final_comments and poi.final_comments.strip() else all_comments
            
            # Extract data for BAS format
            row = [
                poi.id,  # observation_id
                latitude,  # latitude (WGS84)
                longitude,  # longitude (WGS84)
                poi.date_image_taken.isoformat() if poi.date_image_taken else '',  # date_time
                poi.final_species.label if poi.final_species else '',  # species
                1,  # count - assuming single animal per POI
                latest_annotation.confidence.label if latest_annotation and latest_annotation.confidence else '',  # confidence
                poi.vendor_id or '',  # image_source
                poi.date_image_taken.isoformat() if poi.date_image_taken else '',  # image_date
                latest_annotation.user.username if latest_annotation and latest_annotation.user else '',  # observer
                export_comments  # comments
            ]
            writer.writerow(row)
    
    return response

@login_required
def export_whale_annotations_whalemap(request, project_id):
    """
    Export confirmed whale annotations in WhaleMap CSV format for a specific project
    WhaleMap format is used for standardized whale sighting data exchange
    """
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="whale_annotations_whalemap_project_{project_id}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    
    # WhaleMap CSV format headers
    headers = [
        'SOURCE',         # Geospatial Artificial Intelligence for Animals
        'Platform Type',  # Platform/source of observation
        'Platform Name',  # Platform Name
        'CATALOG_ID',     # Catalog ID
        'VENDOR_ID',      # Vendor ID
        'ENTITY_ID',      # Entity ID
        'Point ID',       # Unique identifier for the point of interest observation
        'DateTime_GMT',   # Date/time of observation (YYYY-MM-DD HH:MM:SS)
        'LAT_WGS84',      # Latitude in decimal degrees (WGS84/EPSG:4326)
        'LON_WGS84',      # Longitude in decimal degrees (WGS84/EPSG:4326)
        'Scientific Name',# Scientific name
        'Common Name',    # Species code or name
        'Certainty',      # Certainty/confidence level
        'Age',            # Age class
        'Comments',       # Comment field
        'Annotation URL'  # Link to the annotation POI
    ]
    writer.writerow(headers)
    
    # Filter for whale annotations with final classification for the specific project
    whale_annotations = PointsOfInterest.objects.filter(
        project_id=project_id,
        final_classification_id=14,
        final_species__isnull=False,
    ).select_related('final_classification', 'final_species', 'final_confidence').prefetch_related('annotations')
    
    # Group POIs by EPSG code for efficient coordinate transformation
    pois_by_epsg = {}
    for poi in whale_annotations:
        epsg_key = str(poi.epsg_code) if poi.epsg_code else '4326'
        if epsg_key not in pois_by_epsg:
            pois_by_epsg[epsg_key] = []
        pois_by_epsg[epsg_key].append(poi)
    
    # Create transformers once per EPSG code
    transformers = {}
    for epsg_code in pois_by_epsg.keys():
        if epsg_code != '4326':
            try:
                source_crs = CRS(f"EPSG:{epsg_code}")
                target_crs = CRS("EPSG:4326")
                transformers[epsg_code] = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            except Exception as e:
                logger.warning(f"Could not create transformer for EPSG:{epsg_code}: {e}")
                transformers[epsg_code] = None
    
    # Process POIs by EPSG code and export data
    for epsg_code, poi_group in pois_by_epsg.items():
        transformer = transformers.get(epsg_code)
        
        for poi in poi_group:
            # Get the latitude and longitude from the point geometry
            if poi.point:
                longitude = round(float(poi.point.x), 5)
                latitude = round(float(poi.point.y), 5)
                
                # Transform coordinates to WGS84 (EPSG:4326) if needed
                if epsg_code != '4326' and transformer:
                    try:
                        longitude, latitude = transformer.transform(longitude, latitude)
                        longitude = round(longitude, 5)
                        latitude = round(latitude, 5)
                    except Exception as e:
                        logger.warning(f"Could not transform coordinates for POI {poi.id}: {e}")
            else:
                latitude = ''
                longitude = ''
            
            # Get the most recent annotation for additional details
            annotation_comments = [
                annotation.comments.strip()
                for annotation in poi.annotations.all()
                if annotation.comments and annotation.comments.strip()
            ]
            all_comments = ' | '.join(annotation_comments)
            
            # Build URL for direct navigation to the standard annotation page for this POI.
            annotation_url = request.build_absolute_uri(
                reverse(
                    'annotation_item_page',
                    kwargs={
                        'project_id': project_id,
                        'item_id': poi.id
                    }
                )
            )

            observation_date = ''
            vendor_datetime = _extract_gmt_datetime_from_vendor_id(poi.vendor_id)
            if vendor_datetime:
                observation_date = vendor_datetime.strftime('%Y-%m-%d %H:%M:%S')
            elif poi.date_image_taken:
                observation_date = f"{poi.date_image_taken.strftime('%Y-%m-%d')} 00:00:00"

            source = 'Geospatial Artificial Intelligence for Animals'
            platform_type = 'Satellite image'
            platform_name = _expand_sensor_name(poi.sensor)
            scientific_name = poi.final_species.value if poi.final_species else ''
            common_name = poi.final_species.label if poi.final_species else ''
            certainty = poi.final_confidence.label if poi.final_confidence else ''
            age = dict(AGE_CHOICES).get(poi.final_age, '') if poi.final_age else ''
            comments = poi.final_comments.strip() if poi.final_comments and poi.final_comments.strip() else all_comments

            # Extract data for WhaleMap format
            row = [
                source,  # SOURCE
                platform_type,  # Platform Type
                platform_name,  # Platform Name
                poi.catalog_id or '',  # CATALOG_ID
                poi.vendor_id or '',  # VENDOR_ID
                poi.entity_id or '',  # ENTITY_ID
                poi.id,  # Point ID
                observation_date,  # Date_GMT
                latitude,  # LAT_WGS84
                longitude,  # LON_WGS84
                scientific_name,  # Scientific Name
                common_name,  # Common Name
                certainty,  # Certainty
                age,  # Age
                comments,  # Comments
                annotation_url,  # Annotation URL
            ]
            writer.writerow(row)
    
    return response