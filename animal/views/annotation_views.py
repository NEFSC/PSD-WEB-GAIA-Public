"""
Annotation Views Module

This module contains views for handling animal annotations, multiview operations, and related functionality.

CACHING IMPLEMENTATION:
- multiview_list() view uses Django cache to store vendor ID lists per project
- Cache key format: 'multiview_vendor_list_{project_id}'
- Cache timeout: 30 minutes (1800 seconds)
- Cache is automatically invalidated when POIs are created/deleted via web interface
- For bulk operations, cache should be manually invalidated using invalidate_multiview_vendor_cache()

CACHE MANAGEMENT FUNCTIONS:
- invalidate_multiview_vendor_cache(project_id): Clears cache for a specific project
- warm_multiview_vendor_cache(project_id): Pre-populates cache for a project
- clear_multiview_cache(request, project_id): Admin view to manually clear cache

PERFORMANCE NOTES:
- Caching reduces database query time from ~seconds to milliseconds for large datasets
- Cache hit/miss status is logged and visible to superusers in debug mode
- Query performance is monitored and logged
"""

import requests
import math
from datetime import datetime, timedelta
from pyproj import CRS, Transformer
from azure.core.credentials import AzureNamedKeyCredential
from azure.storage.blob import generate_blob_sas, BlobSasPermissions, BlobServiceClient
from django.core.cache import cache
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect
from django.db.models import Q, Count, Prefetch, Value, IntegerField, Case, When
from django.db.models.functions import Abs, Cast
from django.db import models, IntegrityError
from django.utils import timezone
import json
from django.contrib.gis.geos import Point

# Try to import numpy for bulk coordinate transformation
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None

from ..models import PointsOfInterest, Annotations, Fishnet, FishnetReviews, Classification, Target, Confidence, Project
from ..forms import AnnotationForm, FishnetForm, PointsOfInterestForm
from django.core.paginator import Paginator
import logging
from django.contrib.gis.geos import Polygon

logger = logging.getLogger('animal')  # use your app name here


def _serialize_detect_point(poi, user):
    if not poi.point:
        return None

    owner_id = poi.created_by_id
    owner_username = poi.created_by.username if poi.created_by else None
    return {
        'id': poi.id,
        'catalog_id': poi.catalog_id,
        'vendor_id': poi.vendor_id,
        'longitude': poi.point.x,
        'latitude': poi.point.y,
        'created_by_id': owner_id,
        'created_by_username': owner_username,
        'is_owner': bool(owner_id and owner_id == user.id),
    }


def _serialize_detect_submitted_points(project_id, vendor_id, user):
    if not vendor_id:
        return []

    queryset = PointsOfInterest.objects.filter(
        project_id=project_id,
        vendor_id=vendor_id,
        generation_method='manual',
    ).exclude(point__isnull=True).select_related('created_by').order_by('id')

    points = []
    for poi in queryset:
        serialized = _serialize_detect_point(poi, user)
        if serialized:
            points.append(serialized)

    return points


def _can_modify_poi(user, poi):
    if not user.is_authenticated:
        return False

    return user.is_superuser or (
        poi.created_by_id is not None and poi.created_by_id == user.id
    )


def _mark_fishnet_review(fishnet, user):
    try:
        review, created = FishnetReviews.objects.get_or_create(
            fishnet=fishnet,
            user=user,
            defaults={'date': timezone.now().date()},
        )
    except IntegrityError:
        review = FishnetReviews.objects.filter(fishnet=fishnet, user=user).first()
        created = False

    if review and review.date is None:
        review.date = timezone.now().date()
        review.save(update_fields=['date'])

    return created


def _is_ajax_json_request(request):
    accept_header = request.headers.get('Accept', '')
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in accept_header
    )

def annotation_page(request, project_id, item_id=None):
    # Initialize default coordinates (Fisherman's Wharf, Provincetown, MA)
    longitude, latitude = -70.183762, 42.049081
    id = item_id
    project = Project.objects.get(id=project_id)
    user = request.user
    annotation = None
    annotations = None
    form = AnnotationForm(instance=annotation, initial={})
    vendor_id = None
    # Count total annotations already submitted by this user in this project
    total_annotations_by_user_in_project = Annotations.objects.filter(
        user_id=user.id,
        poi__project_id=project_id
    ).count()

    def cog_exists(vendor_id):
        cached_result = cache.get(f'cog_existence_{vendor_id}')
        if cached_result is not None:
            return cached_result

        blob_name = check_cog_existence(vendor_id, directory='cogs/')
        cache.set(f'cog_existence_{vendor_id}', (blob_name), timeout=300)  
        return blob_name 

    def get_next_poi(user, project_id, current_poi_id=None, use_validation_queue=False):
        if use_validation_queue and user.is_superuser:
            from django.db import connection

            # Match the validation() base query so superusers advance through that queue.
            base_query = """
                SELECT DISTINCT poi.id
                FROM animal_pointsofinterest poi
                INNER JOIN animal_annotations ann ON poi.id = ann.poi_id
                WHERE poi.project_id = %s
                AND EXISTS (
                    SELECT 1 FROM animal_annotations a1
                    WHERE a1.poi_id = poi.id AND a1.classification_id = 14
                )
                AND (
                    SELECT COUNT(*) FROM animal_annotations a2
                    WHERE a2.poi_id = poi.id
                ) > 2
                AND poi.final_classification_id IS NULL
            """

            with connection.cursor() as cursor:
                cursor.execute("PRAGMA busy_timeout = 25000")

                # Prefer the next higher id after the current POI to preserve list order.
                if current_poi_id is not None:
                    cursor.execute(
                        base_query + " AND poi.id > %s ORDER BY poi.id LIMIT 1",
                        [project_id, current_poi_id]
                    )
                    row = cursor.fetchone()
                    if row:
                        return PointsOfInterest.objects.filter(id=row[0]).first()

                # Wrap to the first remaining validation item when at end of list.
                cursor.execute(base_query + " ORDER BY poi.id LIMIT 1", [project_id])
                row = cursor.fetchone()

            return PointsOfInterest.objects.filter(id=row[0]).first() if row else None

        # Get IDs of POIs the user has already annotated
        annotated_poi_ids = set(Annotations.objects.filter(
            user_id=user.id
        ).values_list('poi_id', flat=True))
        
        # Get IDs of POIs with 3+ annotations
        full_poi_ids = set(Annotations.objects.values('poi_id')
            .annotate(count=Count('poi_id'))
            .filter(count__gte=3)
            .values_list('poi_id', flat=True))
        
        query = PointsOfInterest.objects.filter(project_id=project_id)
        
        # Apply exclusions and get first available POI
        return query.exclude(
            id__in=annotated_poi_ids | full_poi_ids
        ).order_by('id').first()

    if id is None:
        poi = get_next_poi(user, project_id)
        if poi:
            return redirect(f'/project/{project_id}/annotation/{poi.id}')
        else:
            return render(request, 'annotation_page.html')
    elif id:
        try:
            poi = PointsOfInterest.objects.get(id=id)
            vendor_id = poi.vendor_id
            
            if user.is_superuser:
                annotations = Annotations.objects.filter(poi=poi)
            try:
                annotation = Annotations.objects.select_related(
                    'classification', 
                    'target',
                    'confidence'
                ).get(poi=poi, user_id=user.id)
            except Annotations.DoesNotExist:
                annotation = Annotations(poi=poi, user_id=user.id)
        except PointsOfInterest.DoesNotExist:
            poi = None
    form = AnnotationForm(instance=annotation, initial={})

    if request.method == "POST":
        form = AnnotationForm(request.POST, instance=annotation)
        if form.is_valid():
            current_poi_id = poi.id if poi else None
            superuser_validation_submit = False

            if user.is_superuser and annotations.count() > 2:
                superuser_validation_submit = True
                poi.final_review_date = datetime.now()
                poi.final_classification = form.cleaned_data['classification']
                poi.final_species = form.cleaned_data['target']
                poi.final_confidence = form.cleaned_data['confidence']
                poi.final_age = form.cleaned_data['age']
                poi.final_comments = request.POST.get('final_comments', '').strip() or None
                poi.save(update_fields=['final_species', 'final_classification', 'final_confidence', 'final_age', 'final_comments', 'final_review_date'])
                invalidate_deduplication_cache(project_id)
                poi = get_next_poi(
                    user,
                    project_id,
                    current_poi_id=current_poi_id,
                    use_validation_queue=True
                )
            else:
                annotation = form.save(commit=False)
                annotation.full_clean()
                annotation.save()
                invalidate_deduplication_cache(project_id)
                poi = get_next_poi(user, project_id)

            if poi:
                return redirect(f'/project/{project_id}/annotation/{poi.id}')
            else:
                if superuser_validation_submit:
                    messages.success(request, "You're all caught up! No further validations to review.")
                    return redirect('landing_page')
                return render(request, 'annotation_page.html')

    # Since the points were generated from projected imagery, we need to transform them to
    #      geographic coordinates (i.e., EPSG:4326) to show them.
    if poi and poi.point and poi.epsg_code:
        logger.info(f"Your geometry is: {poi.point} and your EPSG code is: {poi.epsg_code}")
        source_crs = CRS(f"EPSG:{poi.epsg_code}")
        target_crs = CRS("EPSG:4326")
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
        easting, northing = poi.point.coords
        print(f"easting: {easting}, northing: {northing}")
        longitude, latitude = transformer.transform(easting, northing)

    cogurl = cog_exists(poi.vendor_id) if poi else None
    return render(request, 'annotation_page.html', {
        'poi': poi,
        'annotation': annotation,
        'annotations': annotations,
        'total_annotations_by_user_in_project': total_annotations_by_user_in_project,
        'project': project,
        'user_is_superuser': user.is_superuser,
        'form': form,
        'vendor_id': vendor_id,
        'longitude': longitude,
        'latitude': latitude,
        'error_message': form.errors,
        'final_age': poi.final_age if poi else '',
        'final_comments': poi.final_comments if poi else '',
        'cogurl': cogurl
    })

def cog_view(request, vendor_id=None):
    try:
        requested_blob = (vendor_id or '').strip().strip('/')
        resolved_blob = requested_blob

        # Project pages often pass vendor IDs without extension; resolve to actual blob path first.
        lowered = requested_blob.lower()
        has_tiff_extension = lowered.endswith('.tif') or lowered.endswith('.tiff')
        if not has_tiff_extension:
            matched_blob = check_cog_existence(requested_blob, directory='cogs/')
            if not matched_blob:
                return HttpResponse(
                    f"COG not found for vendor_id '{requested_blob}' in Azure cogs directory.",
                    status=404,
                )
            resolved_blob = matched_blob

        blob_url = generate_sas_token(resolved_blob)
        if not blob_url:
            return HttpResponse(
                f"Failed to generate SAS URL for COG '{resolved_blob}'.",
                status=500,
            )

        session = requests.Session()
        retries = requests.adapters.Retry(total = 5, backoff_factor = 1, status_forcelist = [500, 502, 503, 504])
        adapter = requests.adapters.HTTPAdapter(max_retries = retries)
        session.mount('https://', adapter)

        range_header = request.META.get('HTTP_RANGE', None)
        headers = {}

        if range_header:
            # Preserve valid byte ranges verbatim, including suffix forms (e.g., bytes=-16384).
            cleaned_range = range_header.strip()
            if cleaned_range.lower().startswith('bytes='):
                headers['Range'] = cleaned_range

        response = session.get(blob_url, headers=headers, timeout=20)

        if response.status_code in [200, 206]:
            print(f"Successful status code {response.status_code}")

            status_code = 206 if response.status_code == 206 else 200
            tile_response = HttpResponse(response.content, content_type='image/tiff', status=status_code)

            content_range = response.headers.get('Content-Range')
            content_length = response.headers.get('Content-Length')
            accept_ranges = response.headers.get('Accept-Ranges', 'bytes')

            if content_range:
                tile_response['Content-Range'] = content_range
            tile_response['Accept-Ranges'] = accept_ranges
            tile_response['Content-Length'] = content_length if content_length else str(len(response.content))

            return tile_response
        else: 
            return HttpResponseForbidden(f"Error fetching COG: {response.status_code}")
            
    except requests.exceptions.RequestException as e:
        return HttpResponse(f"Network error: {str(e)}", status = 503)
    except Exception as e:
        return HttpResponse(f"Error: {str(e)}", status=403)

def proxy_openlayers_js(request):
    """ Proxy view for serving OpenLayers supporting COG viewing. """
    # Use the proper distribution path including /dist/
    url = "https://cdn.jsdelivr.net/npm/ol@6.15.1/dist/ol.js"
    response = requests.get(url)
    return HttpResponse(response.content, content_type="application/javascript")

def proxy_webgls_js(request):
    """ Proxy view for serving WebGLS supporting COG viewing. """
    url = "https://cdn.jsdelivr.net/npm/ol-webgl/dist/ol-webgl.min.js"
    response = requests.get(url)
    return HttpResponse(response.content, content_type="application/javascript")

def convert_date_or_none(date_str):
    """ Used to convert date formats from USGS EarthExplorer and NGA GEGD. """
    success = False
    
    if date_str and date_str != "None":
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                result = datetime.datetime.strptime(date_str, fmt)
                success = True
                return result
            except ValueError:
                continue
        if not success:
            return datetime.datetime.strptime(date_str, "%Y/%m/%d").strftime("%Y-%m-%d")
        raise ValueError(f"Date string {date_str} does not match supported formats!")
    return None

def generate_sas_token(blob_name):
    """ Generates a Shared Access Signature (SAS) Token on-the-fly. """
    try:
        account_name = settings.AZURE_STORAGE_ACCOUNT_NAME
        account_key = settings.AZURE_STORAGE_ACCOUNT_KEY
        container_name = settings.AZURE_CONTAINER_NAME

        normalized_blob_name = (blob_name or '').lstrip('/')
        blob_path = normalized_blob_name if normalized_blob_name.startswith('cogs/') else f'cogs/{normalized_blob_name}'
        print(f"Your blob path is: {blob_path}")
        
        sas_token = generate_blob_sas(
            account_name = account_name,
            container_name = container_name,
            blob_name = blob_path,
            account_key = account_key,
            permission = BlobSasPermissions(read=True),
            expiry = datetime.now() + timedelta(hours=2)
        )
    
        blob_url = f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_path}?{sas_token}"
    
        return blob_url

    except Exception as e:
        print(f"Error generating SAS token for blob '{blob_name}': {e}")
        return None

def check_cog_existence(vendor_id, directory=None):
    """ Checks if a Cloud Optimized GeoTIFF exists in Azure. """

    account_name = settings.AZURE_STORAGE_ACCOUNT_NAME
    account_key = settings.AZURE_STORAGE_ACCOUNT_KEY
    container_name = settings.AZURE_CONTAINER_NAME
    vendor_id = vendor_id.replace('P1BS', 'S1BS')

    try:
        credential = AzureNamedKeyCredential(account_name, account_key)
    
        blob_service_client = BlobServiceClient(
            account_url = f"https://{account_name}.blob.core.windows.net/",
            credential=credential
        )
        container_client = blob_service_client.get_container_client(container_name)
        prefix = directory if directory else ""
        
        blobs = container_client.list_blobs(name_starts_with=prefix)
        candidates = []
        for blob in blobs:
            blob_name = blob.name
            blob_name_lower = blob_name.lower()
            if vendor_id not in blob_name:
                continue
            if not (blob_name_lower.endswith('.tif') or blob_name_lower.endswith('.tiff')):
                continue

            base_name = blob_name.rsplit('/', 1)[-1]
            score = 0
            if base_name.startswith(vendor_id):
                score += 10
            if base_name == vendor_id or base_name == f"{vendor_id}.tif" or base_name == f"{vendor_id}.tiff":
                score += 20

            candidates.append({
                'name': blob_name,
                'score': score,
                'size': getattr(blob, 'size', 0) or 0,
                'last_modified': getattr(blob, 'last_modified', None),
            })

        if candidates:
            candidates.sort(
                key=lambda c: (c['score'], c['size'], c['last_modified'] or datetime.min),
                reverse=True,
            )
            return candidates[0]['name']
        return None

    except Exception as e:
        print(f"An error occurred: {e}")
        return False, None

def validation(request, project_id):
    project = Project.objects.get(id=project_id)
    sort_order = request.GET.get('sort', 'asc')
    show_final_reviews = request.GET.get('showfinals', 'false')
    page_number = request.GET.get('page')

    try:
        # Use raw SQL for better performance on validation queries
        from django.db import connection
        
        with connection.cursor() as cursor:
            # Set query timeout
            cursor.execute("PRAGMA busy_timeout = 25000")  # 25 second timeout
            
            # Build the base query for POIs with classification=14 annotations
            base_query = """
                SELECT DISTINCT poi.id
                FROM animal_pointsofinterest poi
                INNER JOIN animal_annotations ann ON poi.id = ann.poi_id
                WHERE poi.project_id = %s
                AND EXISTS (
                    SELECT 1 FROM animal_annotations a1 
                    WHERE a1.poi_id = poi.id AND a1.classification_id = 14
                )
                AND (
                    SELECT COUNT(*) FROM animal_annotations a2 
                    WHERE a2.poi_id = poi.id
                ) > 2
            """
            
            # Add final classification filter if needed
            if show_final_reviews == 'false':
                base_query += " AND poi.final_classification_id IS NULL"
            else: 
                base_query += " AND poi.final_classification_id = 14"
            base_query += " ORDER BY poi.id"
            
            cursor.execute(base_query, [project_id])
            poi_ids = [row[0] for row in cursor.fetchall()]
        
        # Convert to Django QuerySet for pagination compatibility
        if poi_ids:
            POIs = PointsOfInterest.objects.filter(id__in=poi_ids).select_related('project')
            
            # Get related annotations efficiently
            three_reviews = Annotations.objects.filter(
                poi_id__in=poi_ids
            ).select_related('classification', 'target', 'confidence', 'poi')
            
            # Use prefetch_related for better performance
            POIs = POIs.prefetch_related(
                Prefetch('annotations', queryset=three_reviews, to_attr='three_reviews')
            ).order_by('id')
        else:
            # Empty queryset if no POIs found
            POIs = PointsOfInterest.objects.none()

        # Paginate the results
        paginator = Paginator(POIs, 100)
        page_obj = paginator.get_page(page_number)

    except Exception as e:
        logger.error(f"Error in validation view for project {project_id}: {str(e)}")
        # Return empty page as fallback
        POIs = PointsOfInterest.objects.none()
        paginator = Paginator(POIs, 100)
        page_obj = paginator.get_page(1)

    return render(request, 'validation_page.html', {
        'page_obj': page_obj, 
        'sort_order': sort_order, 
        'project': project
    })

def detect_page(request, project_id, id=None):
    # Initialize default coordinates (Fisherman's Wharf, Provincetown, MA)
    longitude, latitude = -70.183762, 42.049081
    user = request.user
    project = Project.objects.get(id=project_id)

    def get_next_cell(user, project_id):
        # Get IDs of Fishnets the user has already annotated
        reviewed_fishnet_ids = set(FishnetReviews.objects.filter(
            user_id=user.id
        ).values_list('fishnet_id', flat=True))
        
        # A cell is complete only after 3 distinct users review it.
        full_fishnet_ids = set(FishnetReviews.objects.filter(user_id__isnull=False)
            .values('fishnet_id')
            .annotate(reviewer_count=Count('user_id', distinct=True))
            .filter(reviewer_count__gte=3)
            .values_list('fishnet_id', flat=True))
        
        # Base query filtered by project if needed
        query = Fishnet.objects.filter(project_id=project_id)
        
        # Apply exclusions and get first available fishnet cell
        return query.exclude(
            id__in=list(reviewed_fishnet_ids | full_fishnet_ids)
        ).order_by('id').first()

    def cog_exists(vendor_id):
        cached_result = cache.get(f'cog_existence_{vendor_id}')
        if cached_result is not None:
            return cached_result

        blob_name = check_cog_existence(vendor_id, directory='cogs/')
        cache.set(f'cog_existence_{vendor_id}', (blob_name), timeout=300)  
        return blob_name 

    def build_fishnet_response(fishnet):
        source_crs = CRS("EPSG:3857")
        target_crs = CRS("EPSG:4326")
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)

        centroid = fishnet.cell.centroid
        longitude, latitude = transformer.transform(centroid.x, centroid.y)

        exterior_ring = fishnet.cell.exterior_ring
        transformed_coords = []
        for point in exterior_ring:
            lon, lat = transformer.transform(point[0], point[1])
            transformed_coords.append([lon, lat])

        return {
            'success': True,
            'fishnet_id': fishnet.id,
            'vendor_id': fishnet.vendor_id,
            'longitude': longitude,
            'latitude': latitude,
            'cell_coordinates': transformed_coords,
            'project': str(project),
            'cogurl': cog_exists(fishnet.vendor_id),
            'submitted_points': _serialize_detect_submitted_points(project_id, fishnet.vendor_id, user),
        }

    # Handle AJAX request for next cell FIRST, before any other logic
    if request.method == "POST" and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        logger.info(f"AJAX POST request received for project {project_id}, id={id}")
        logger.info(f"POST data: {dict(request.POST)}")
        
        # Check if this is a "next" request
        if request.POST.get('action') == 'next':
            current_fishnet_id = request.POST.get('fishnet_id')
            logger.info(f"Processing 'next' action for fishnet_id: {current_fishnet_id}")
            
            # Record the review for the current fishnet
            if current_fishnet_id:
                try:
                    current_fishnet = Fishnet.objects.get(id=current_fishnet_id, project_id=project_id)
                    was_created = _mark_fishnet_review(current_fishnet, user)
                    logger.info(
                        "Recorded review for fishnet %s (%s)",
                        current_fishnet_id,
                        "created" if was_created else "already reviewed by user",
                    )
                    
                except Fishnet.DoesNotExist:
                    logger.warning(f"Fishnet {current_fishnet_id} not found")
                    pass
            
            # Get the next cell
            next_fishnet = get_next_cell(user, project_id)
            
            if next_fishnet is None:
                logger.info("No more cells left to review")
                return JsonResponse({
                    'success': False,
                    'message': 'No more cells left to review.',
                    'finished': True
                })
            
            logger.info(f"Found next fishnet: {next_fishnet.id}")

            response_data = build_fishnet_response(next_fishnet)
            logger.info(f"Returning JSON response: {response_data}")
            return JsonResponse(response_data)

        if request.POST.get('action') == 'load':
            target_fishnet_id = request.POST.get('fishnet_id')
            logger.info(f"Processing 'load' action for fishnet_id: {target_fishnet_id}")

            try:
                target_fishnet_id = int(target_fishnet_id)
            except (TypeError, ValueError):
                return JsonResponse({
                    'success': False,
                    'message': 'Invalid fishnet id.'
                }, status=400)

            try:
                requested_fishnet = Fishnet.objects.get(id=target_fishnet_id, project_id=project_id)
            except Fishnet.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'message': 'Fishnet not found for this project.'
                }, status=404)

            response_data = build_fishnet_response(requested_fishnet)
            logger.info(f"Returning load response for fishnet {target_fishnet_id}")
            return JsonResponse(response_data)

    if id is None:
        fishnet = get_next_cell(user, project_id)
        if fishnet is None:
            return render(request, 'detect_page.html', {
            'info_message': 'No points cells left to review.', 'project': project
        })
        else:
            return redirect(f'/project/{project_id}/detect/{fishnet.id}')

    fishnet = Fishnet.objects.get(id=id)
    vendor_id = fishnet.vendor_id

    # Handle traditional form submission (fallback)
    if request.method == "POST":
        form = FishnetForm(request.POST, instance=fishnet)
        if form.is_valid():
            _mark_fishnet_review(fishnet, user)
            
            next_fishnet = get_next_cell(user, project_id)
            if next_fishnet:
                return redirect(f'/project/{project_id}/detect/{next_fishnet.id}')
            else:
                return render(request, 'detect_page.html', {
                    'info_message': 'No more cells left to review.',
                    'project': project
                })

    source_crs = CRS(f"EPSG:3857")
    target_crs = CRS("EPSG:4326")
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    centroid = fishnet.cell.centroid
    easting = centroid.x
    northing = centroid.y
    print(f"easting: {easting}, northing: {northing}")
    longitude, latitude = transformer.transform(easting, northing)

    # Transform the fishnet cell polygon
    # Get the exterior ring of the polygon
    exterior_ring = fishnet.cell.exterior_ring
    transformed_coords = []
    # Transform each point in the polygon
    for point in exterior_ring:
        lon, lat = transformer.transform(point[0], point[1])
        transformed_coords.append((lon, lat))
    # Create a new polygon with transformed coordinates
    transformed_polygon = Polygon(transformed_coords)
    # Store the transformed polygon for rendering
    fishnet.transformed_cell = transformed_polygon

    cogurl = cog_exists(vendor_id) if fishnet else None
    return render(request, 'detect_page.html', {
        'id': fishnet.id,
        'cell': fishnet.transformed_cell,
        'vendor_id': vendor_id,
        'longitude': longitude,
        'latitude': latitude,
        'cogurl': cogurl,
        'submitted_points_json': json.dumps(_serialize_detect_submitted_points(project_id, vendor_id, user)),
        'project_id': project_id,
        'project': project,
    })

def create_point(request, project_id):
    if request.method == "POST":
        # Accept both JSON and form-data
        points_data = None

        # Try to get points from form-data (as in your JS)
        if 'points' in request.POST:
            # The frontend sends a single stringified JSON array under 'points'
            try:
                points_data = json.loads(request.POST['points'])
            except Exception as e:
                return JsonResponse({'error': f'Invalid points data: {e}'}, status=400)
        else:
            # Try to parse JSON body (for application/json requests)
            try:
                body = request.body.decode('utf-8')
                data = json.loads(body)
                points_data = data.get('points')
            except Exception:
                pass

        if not points_data or not isinstance(points_data, list):
            return JsonResponse({'error': 'No valid points provided.'}, status=400)

        created_points = []
        for point_data in points_data:
            try:
                geom = point_data.get('geometry')
                vendor_id = point_data.get('vendor_id')
                # Accept geometry as GeoJSON
                if geom and geom.get('type') == 'Point':
                    coords = geom.get('coordinates')
                    point_geom = Point(coords[0], coords[1])
                else:
                    return JsonResponse({'error': 'Invalid geometry.'}, status=400)

                poi = PointsOfInterest.objects.create(
                    point=point_geom,
                    vendor_id=vendor_id,
                    project_id=project_id,
                    epsg_code=4326,
                    generation_method='manual',
                    created_by=request.user,
                )
                serialized_poi = _serialize_detect_point(poi, request.user)
                if serialized_poi:
                    created_points.append(serialized_poi)
                logger.info(f"Point {poi.id} created")
            except Exception as e:
                logger.error(f"Error creating point: {e}")
                return JsonResponse({'error': str(e)}, status=400)

        # Invalidate the multiview vendor list cache since we created new POIs
        if created_points:
            invalidate_multiview_vendor_cache(project_id)
            # Also invalidate deduplication cache since new POIs might be duplicates
            invalidate_deduplication_cache(project_id)

        return JsonResponse({'points': created_points, 'count': len(created_points)})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


def move_point(request, project_id, poi_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        poi = PointsOfInterest.objects.select_related('created_by').get(id=poi_id, project_id=project_id)
    except PointsOfInterest.DoesNotExist:
        return JsonResponse({'error': 'Point not found.'}, status=404)

    if not _can_modify_poi(request.user, poi):
        return JsonResponse({'error': 'You do not have permission to move this point.'}, status=403)

    geometry = None
    if request.POST.get('geometry'):
        try:
            geometry = json.loads(request.POST.get('geometry'))
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid geometry payload.'}, status=400)
    else:
        try:
            payload = json.loads(request.body.decode('utf-8')) if request.body else {}
            geometry = payload.get('geometry')
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    if not geometry or geometry.get('type') != 'Point':
        return JsonResponse({'error': 'Invalid geometry.'}, status=400)

    coords = geometry.get('coordinates') or []
    if len(coords) != 2:
        return JsonResponse({'error': 'Point geometry requires [longitude, latitude].'}, status=400)

    try:
        longitude = float(coords[0])
        latitude = float(coords[1])
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Coordinates must be numeric.'}, status=400)

    poi.point = Point(longitude, latitude)
    poi.save(update_fields=['point'])

    invalidate_multiview_vendor_cache(project_id)
    invalidate_deduplication_cache(project_id)

    serialized = _serialize_detect_point(poi, request.user)
    return JsonResponse({'success': True, 'point': serialized})


def delete_point(request, project_id, poi_id):
    if request.method != 'POST':
        if _is_ajax_json_request(request):
            return JsonResponse({'error': 'Method not allowed.'}, status=405)
        messages.error(request, 'Invalid request method.')
        return redirect('project_detail', project_id=project_id)
    
    try:
        poi = PointsOfInterest.objects.select_related('created_by').get(id=poi_id, project_id=project_id)

        if not _can_modify_poi(request.user, poi):
            if _is_ajax_json_request(request):
                return JsonResponse({'error': 'You do not have permission to remove this point.'}, status=403)
            messages.error(request, 'You do not have permission to remove this point.')
            return redirect('project_detail', project_id=project_id)

        poi.delete()
        logger.info(f"Point {poi_id} deleted by user {request.user.username}")

        # Invalidate related caches when a POI is removed.
        invalidate_multiview_vendor_cache(project_id)
        invalidate_deduplication_cache(project_id)

        if _is_ajax_json_request(request):
            return JsonResponse({'success': True, 'deleted_point_id': poi_id})

        messages.success(request, f'Point {poi_id} has been successfully removed.')
        
    except PointsOfInterest.DoesNotExist:
        if _is_ajax_json_request(request):
            return JsonResponse({'error': 'Point not found.'}, status=404)
        messages.error(request, 'Point not found.')
    except Exception as e:
        logger.error(f"Error deleting point: {e}")
        if _is_ajax_json_request(request):
            return JsonResponse({'error': f'Error removing point: {str(e)}'}, status=400)
        messages.error(request, f'Error removing point: {str(e)}')
    
    return redirect('project_detail', project_id=project_id)

def deduplication_list(request, project_id):
    from django.contrib.gis.measure import D
    from django.contrib.gis.db.models.functions import Distance
    from django.contrib.gis.geos import Point
    import math
    
    sort_order = request.GET.get('sort', 'asc')
    page_number = request.GET.get('page')
    project = Project.objects.get(id=project_id)

    try:
        # Check cache first
        cache_key = f'deduplication_list_v2_{project_id}_{sort_order}'
        cached_duplicates = cache.get(cache_key)
        
        if cached_duplicates is not None:
            logger.info(f"Cache hit for deduplication list, project {project_id}")
            duplicates = cached_duplicates
        else:
            logger.info(f"Cache miss for deduplication list, project {project_id}")
            start_time = datetime.now()
            
            # Use raw SQL with spatial queries for much better performance
            from django.db import connection
            
            with connection.cursor() as cursor:
                # Set query timeout
                cursor.execute("PRAGMA busy_timeout = 30000")  # 30 second timeout
                
                # Use spatial SQL to find duplicates efficiently
                # This query finds all POI pairs within 100 meters
                cursor.execute("""
                    SELECT DISTINCT 
                        p1.id as main_poi_id,
                        p2.id as nearby_poi_id,
                        ST_Distance(p1.point, p2.point) as distance_m
                    FROM animal_pointsofinterest p1
                    INNER JOIN animal_pointsofinterest p2 ON p1.id < p2.id
                    WHERE p1.project_id = %s 
                    AND p2.project_id = %s
                    AND p1.final_classification_id = 14 
                    AND p2.final_classification_id = 14
                    AND COALESCE(p1.duplicate_reviewed_valid, 0) = 0
                    AND COALESCE(p2.duplicate_reviewed_valid, 0) = 0
                    AND p1.point IS NOT NULL 
                    AND p2.point IS NOT NULL
                    AND ST_Distance(p1.point, p2.point) <= 100
                    ORDER BY p1.id, distance_m
                """, [project_id, project_id])
                
                duplicate_pairs = cursor.fetchall()
            
            # Group the pairs by main POI
            duplicates_dict = {}
            poi_ids = set()
            
            for main_poi_id, nearby_poi_id, distance_m in duplicate_pairs:
                poi_ids.add(main_poi_id)
                poi_ids.add(nearby_poi_id)
                
                if main_poi_id not in duplicates_dict:
                    duplicates_dict[main_poi_id] = []
                
                duplicates_dict[main_poi_id].append({
                    'nearby_poi_id': nearby_poi_id,
                    'distance': float(distance_m)
                })
            
            # Get POI objects efficiently in bulk
            if poi_ids:
                poi_objects = {
                    poi.id: poi for poi in PointsOfInterest.objects.filter(
                        id__in=poi_ids
                    ).select_related('final_classification', 'final_species', 'final_confidence')
                }
                
                # Build final duplicate groups structure
                duplicates = []
                for main_poi_id, nearby_list in duplicates_dict.items():
                    if main_poi_id in poi_objects:
                        nearby_pois = []
                        for nearby_info in nearby_list:
                            nearby_poi_id = nearby_info['nearby_poi_id']
                            if nearby_poi_id in poi_objects:
                                nearby_pois.append({
                                    'poi': poi_objects[nearby_poi_id],
                                    'distance': nearby_info['distance']
                                })
                        
                        if nearby_pois:  # Only include groups that have nearby POIs
                            # Sort nearby POIs by distance
                            nearby_pois.sort(key=lambda x: x['distance'])
                            duplicates.append({
                                'main_poi': poi_objects[main_poi_id],
                                'nearby_pois': nearby_pois
                            })
            else:
                duplicates = []
            
            query_time = (datetime.now() - start_time).total_seconds()
            
            # Cache the results for 2 hours
            cache.set(cache_key, duplicates, timeout=7200)
            logger.info(f"Cached deduplication list for project {project_id}, {len(duplicates)} duplicate groups, query took {query_time:.3f}s")

        # Apply sorting to cached data
        if sort_order == 'desc':
            duplicates.sort(key=lambda x: x['main_poi'].id, reverse=True)
        else:
            duplicates.sort(key=lambda x: x['main_poi'].id)

    except Exception as e:
        logger.error(f"Error in deduplication_list for project {project_id}: {str(e)}")
        # Return empty list as fallback
        duplicates = []

    paginator = Paginator(duplicates, 50)  # Show 50 duplicate groups per page
    page_obj = paginator.get_page(page_number)

    # Render a list of duplicate groups, each with a link to the deduplication page for the main POI
    return render(request, 'deduplication_list_page.html', {
        'page_obj': page_obj, 
        'duplicates': page_obj.object_list,
        'sort_order': sort_order,
        'is_duplicate_view': True,
        'project': project
    })

def deduplication(request, project_id, poi_id):
    """
    Display a specific POI and its nearby duplicates (within 100 meters) on a single map.
    This allows users to view and manage duplicate POIs from the deduplication list.
    Similar to multiview but focused on duplicate POI groups.
    """
    logger.info(f"deduplication called with project_id={project_id}, poi_id={poi_id}")
    logger.info(f"Request method: {request.method}, Is AJAX: {request.headers.get('X-Requested-With') == 'XMLHttpRequest'}")
    
    user = request.user
    project = Project.objects.get(id=project_id)
    
    # Get the main POI
    try:
        main_poi = PointsOfInterest.objects.get(id=poi_id, project_id=project_id)
    except PointsOfInterest.DoesNotExist:
        return render(request, 'deduplication_page.html', {
            'error_message': f'POI with ID {poi_id} not found in project {project_id}.',
            'project_id': project_id,
            'project': project,
        })
    
    # Calculate center coordinates from the main POI
    longitude = float(main_poi.point.x)
    latitude = float(main_poi.point.y)
    
    # Check if coordinates need transformation from projected to geographic
    if main_poi.epsg_code and str(main_poi.epsg_code) != '4326':
        try:
            source_crs = CRS(f"EPSG:{main_poi.epsg_code}")
            target_crs = CRS("EPSG:4326")
            transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            longitude, latitude = transformer.transform(longitude, latitude)
        except Exception as e:
            logger.warning(f"Could not transform coordinates for POI {main_poi.id}: {e}")
    
    def haversine_distance(lat1, lon1, lat2, lon2):
        """Calculate the great circle distance between two points in meters."""
        R = 6371000  # Earth's radius in meters
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) * math.sin(delta_lat / 2) +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) * math.sin(delta_lon / 2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c

    def get_nearby_duplicate_pois(main_poi_obj):
        """Return nearby animal POIs within 100m that are not already reviewed valid."""
        nearby_pois_query = PointsOfInterest.objects.filter(
            project_id=project_id,
            final_classification=14,
            duplicate_reviewed_valid=False,
            point__isnull=False,
        ).exclude(id=main_poi_obj.id).select_related('final_classification', 'final_species', 'final_confidence')

        nearby_results = []
        for nearby_poi in nearby_pois_query:
            is_geographic = (
                str(main_poi_obj.epsg_code) == '4326' or
                str(nearby_poi.epsg_code) == '4326' or
                main_poi_obj.epsg_code == 4326 or
                nearby_poi.epsg_code == 4326
            )

            if is_geographic:
                distance_m = haversine_distance(
                    main_poi_obj.point.y, main_poi_obj.point.x,
                    nearby_poi.point.y, nearby_poi.point.x
                )
            else:
                distance_m = main_poi_obj.point.distance(nearby_poi.point)

            if distance_m <= 100:
                nearby_results.append({'poi': nearby_poi, 'distance': distance_m})

        nearby_results.sort(key=lambda x: x['distance'])
        return nearby_results

    # Handle form submission for POI deletion/mark reviewed.
    if request.method == "POST":
        selected_pois = request.POST.getlist('selected_pois')
        action = request.POST.get('action')

        # Check if this is an AJAX request
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        logger.info(f"POST data received - selected_pois: {selected_pois}, action: {action}, is_ajax: {is_ajax}")

        try:
            if action == 'delete':
                if not selected_pois:
                    raise ValueError('No POIs were selected.')

                deleted_count = 0
                for poi_id_to_delete in selected_pois:
                    try:
                        poi_to_delete = PointsOfInterest.objects.get(id=poi_id_to_delete, project_id=project_id)
                        poi_to_delete.delete()
                        deleted_count += 1
                        logger.info(f"Deleted POI {poi_id_to_delete}")
                    except PointsOfInterest.DoesNotExist:
                        logger.warning(f"POI {poi_id_to_delete} not found for deletion")

                if deleted_count > 0:
                    invalidate_multiview_vendor_cache(project_id)
                    invalidate_deduplication_cache(project_id)

                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'message': f'Deleted {deleted_count} POI(s).',
                        'deleted_count': deleted_count,
                        'deleted_pois': selected_pois,
                    })

                messages.success(request, f'Deleted {deleted_count} POI(s).')
                return redirect('deduplication_list_page', project_id=project_id)

            if action == 'mark_reviewed':
                nearby_for_review = get_nearby_duplicate_pois(main_poi)
                poi_ids_to_review = [main_poi.id] + [item['poi'].id for item in nearby_for_review]

                reviewed_count = PointsOfInterest.objects.filter(
                    id__in=poi_ids_to_review,
                    project_id=project_id,
                    final_classification=14,
                ).update(
                    duplicate_reviewed_valid=True,
                    duplicate_reviewed_at=timezone.now(),
                    duplicate_reviewed_by_id=user.id,
                )

                invalidate_deduplication_cache(project_id)

                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'message': f'Marked {reviewed_count} POI(s) as reviewed valid.',
                        'reviewed_count': reviewed_count,
                        'reviewed_pois': poi_ids_to_review,
                    })

                messages.success(request, f'Marked {reviewed_count} POI(s) as reviewed valid.')
                return redirect('deduplication_list_page', project_id=project_id)

            raise ValueError('No valid action was specified.')

        except Exception as e:
            logger.error(f"Error in deduplication action: {e}")

            if is_ajax:
                return JsonResponse({
                    'success': False,
                    'error': f'Error performing action: {str(e)}'
                }, status=400)

            messages.error(request, f'Error performing action: {str(e)}')

    # Find nearby POIs within 100 meters (same logic as deduplication_list)
    
    # Get all POIs with final_classification=14 (animals) in this project except the main one
    nearby_pois = get_nearby_duplicate_pois(main_poi)
    
    # Prepare all POIs (main + nearby) for the template
    all_pois = [main_poi] + [item['poi'] for item in nearby_pois]
    
    # Check if COG exists for this vendor_id
    def cog_exists(vendor_id):
        cached_result = cache.get(f'cog_existence_{vendor_id}')
        if cached_result is not None:
            return cached_result

        blob_name = check_cog_existence(vendor_id, directory='cogs/')
        cache.set(f'cog_existence_{vendor_id}', blob_name, timeout=300)  
        return blob_name 
    
    cogurl = cog_exists(main_poi.vendor_id)
    
    # Prepare POI data for the template with coordinate transformation
    poi_list = []
    
    # Group POIs by EPSG code for efficient transformation
    pois_by_epsg = {}
    for poi in all_pois:
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
    
    # Process POIs by EPSG code
    for epsg_code, poi_group in pois_by_epsg.items():
        transformer = transformers.get(epsg_code)
        
        for poi in poi_group:
            # Extract coordinates
            poi_longitude = float(poi.point.x)
            poi_latitude = float(poi.point.y)
            
            # Transform coordinates if needed
            if epsg_code != '4326' and transformer:
                try:
                    poi_longitude, poi_latitude = transformer.transform(poi_longitude, poi_latitude)
                except Exception as e:
                    logger.warning(f"Could not transform coordinates for POI {poi.id}: {e}")
            
            # Find distance for nearby POIs
            distance = None
            if poi.id != main_poi.id:
                for nearby_item in nearby_pois:
                    if nearby_item['poi'].id == poi.id:
                        distance = nearby_item['distance']
                        break
            
            poi_data = {
                'id': poi.id,
                'catalog_id': poi.catalog_id or '',
                'longitude': poi_longitude,
                'latitude': poi_latitude,
                'final_classification': poi.final_classification.label if poi.final_classification else None,
                'final_species': poi.final_species.label if poi.final_species else None,
                'final_confidence': poi.final_confidence.label if poi.final_confidence else None,
                'distance': distance,  # Distance from main POI (None for main POI)
                'is_main': poi.id == main_poi.id
            }
            poi_list.append(poi_data)
    
    # Convert POI list to JSON for JavaScript
    import json
    pois_json = json.dumps(poi_list)
    
    # Handle AJAX requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            return JsonResponse({
                'pois': poi_list,
                'poi_count': len(poi_list),
                'main_poi_id': main_poi.id,
                'nearby_count': len(nearby_pois)
            })
        except Exception as e:
            logger.error(f"Error in AJAX response: {e}")
            return JsonResponse({'error': str(e)}, status=500)
    
    context = {
        'main_poi': main_poi,
        'nearby_pois': nearby_pois,
        'vendor_id': main_poi.vendor_id,
        'project_id': project_id,
        'longitude': longitude,
        'latitude': latitude,
        'cogurl': cogurl,
        'pois': poi_list,
        'pois_json': pois_json,
        'poi_count': len(poi_list),
        'nearby_count': len(nearby_pois),
        'user_is_superuser': request.user.is_superuser,
        'user': user,
        'project': project,
    }
    
    return render(request, 'deduplication_page.html', context)

def multiview(request, project_id, vendor_id, poi_id=None):
    """
    Display all Points of Interest that share the same vendor_id on a single map.
    This allows users to view and annotate multiple POIs from the same satellite image together.
    
    PROGRESSIVE LOADING IMPLEMENTATION:
    - Initial load: 10 POIs for fast page rendering
    - Background loading: Additional POIs loaded in 25-POI batches automatically
    - User controls: Users can pause/resume background loading
    - Performance: Reduces initial page load time from seconds to milliseconds
    - Memory efficient: Processes POIs in smaller chunks to avoid memory issues
    """
    logger.info(f"multiview called with project_id={project_id}, vendor_id={vendor_id}, poi_id={poi_id}")
    logger.info(f"Request method: {request.method}, Is AJAX: {request.headers.get('X-Requested-With') == 'XMLHttpRequest'}")
    logger.info(f"Request GET params: {dict(request.GET)}")
    
    user = request.user
    project = Project.objects.get(id=project_id)
    
    # Handle form submission for multiple POI annotations
    if request.method == "POST":
        selected_pois = request.POST.getlist('selected_pois')
        classification_id = request.POST.get('classification')
        target_id = request.POST.get('target')
        confidence_id = request.POST.get('confidence')
        age = request.POST.get('age') or None
        comments = request.POST.get('comments', '')
        
        # Check if this is an AJAX request
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        # Debug logging
        logger.info(f"POST data received - selected_pois: {selected_pois}, classification: {classification_id}, target: {target_id}, confidence: {confidence_id}, age: {age}, is_ajax: {is_ajax}")
        
        if selected_pois and classification_id:
            try:
                classification = Classification.objects.get(id=classification_id)
                target = Target.objects.get(id=target_id) if target_id else None
                confidence = Confidence.objects.get(id=confidence_id) if confidence_id else None

                has_new_annotations = PointsOfInterest.objects.filter(
                    id__in=selected_pois,
                    project_id=project_id,
                ).exclude(annotations__user=user).exists()

                if classification.id == 14 and has_new_annotations and not age:
                    if is_ajax:
                        return JsonResponse({
                            'success': False,
                            'error': 'Age is required when Classification is Animal.'
                        }, status=400)
                    messages.error(request, 'Age is required when Classification is Animal.')
                    return redirect(request.path)

                if classification.id != 14:
                    target = None
                    confidence = None
                    age = None
                
                # Create or update annotations for selected POIs
                created_count = 0
                updated_count = 0
                
                for poi_id in selected_pois:
                    poi = PointsOfInterest.objects.get(id=poi_id, project_id=project_id)
                    
                    # Check if annotation already exists for this user and POI
                    annotation, created = Annotations.objects.get_or_create(
                        poi=poi,
                        user=user,
                        defaults={
                            'classification': classification,
                            'target': target,
                            'confidence': confidence,
                            'age': age,
                            'comments': comments,
                            'date': datetime.now().date()
                        }
                    )
                    
                    if not created:
                        # Update existing annotation
                        annotation.classification = classification
                        annotation.target = target
                        annotation.confidence = confidence
                        annotation.age = age
                        annotation.comments = comments
                        annotation.date = datetime.now().date()
                        annotation.save()
                        updated_count += 1
                    else:
                        created_count += 1
                
                # Increment session annotation counter for new annotations
                if created_count > 0:
                    current_count = request.session.get('annotation_count', 0)
                    request.session['annotation_count'] = current_count + created_count

                # Annotation updates can trigger final classification changes via adjudication logic.
                if created_count > 0 or updated_count > 0:
                    invalidate_deduplication_cache(project_id)
                
                # Handle AJAX response
                if is_ajax:
                    success_message = []
                    if created_count > 0:
                        success_message.append(f'Created {created_count} new annotations.')
                    if updated_count > 0:
                        success_message.append(f'Updated {updated_count} existing annotations.')
                    
                    return JsonResponse({
                        'success': True,
                        'message': ' '.join(success_message),
                        'created_count': created_count,
                        'updated_count': updated_count,
                        'annotated_pois': selected_pois,
                        'session_count': request.session.get('annotation_count', 0)
                    })
                else:
                    # Handle traditional form submission with messages
                    from django.contrib import messages
                    if created_count > 0:
                        messages.success(request, f'Created {created_count} new annotations.')
                    if updated_count > 0:
                        messages.success(request, f'Updated {updated_count} existing annotations.')
                    
            except Exception as e:
                logger.error(f"Error in multiview annotation: {e}")
                
                if is_ajax:
                    return JsonResponse({
                        'success': False,
                        'error': f'Error saving annotations: {str(e)}'
                    }, status=400)
                else:
                    from django.contrib import messages
                    messages.error(request, f'Error saving annotations: {str(e)}')
        else:
            # Handle validation errors
            error_messages = []
            if not selected_pois:
                error_messages.append('No POIs were selected for annotation.')
                logger.warning(f"Form submission with no POIs selected - POST data: {dict(request.POST)}")
            if not classification_id:
                error_messages.append('No classification was selected.')
                logger.warning(f"Form submission with no classification - POST data: {dict(request.POST)}")
            
            if is_ajax:
                return JsonResponse({
                    'success': False,
                    'error': ' '.join(error_messages)
                }, status=400)
            else:
                from django.contrib import messages
                for error_msg in error_messages:
                    messages.error(request, error_msg)
    
    # Get POIs for this vendor_id and project that need annotation
    # Only show POIs that don't have an annotation from the current user
    # Optimize query by selecting only needed fields
    base_pois_query = PointsOfInterest.objects.filter(
        vendor_id=vendor_id,
        project_id=project_id,
        point__isnull=False  # Ensure we have geometry
    ).exclude(
        annotations__user=user
    ).select_related('final_classification', 'final_species', 'final_confidence').only(
        'id', 'catalog_id', 'point', 'epsg_code',
        'final_classification__label', 'final_species__label', 'final_confidence__label'
    ).distinct()
    
    # Apply proximity-based ordering when a specific POI ID is provided
    if poi_id:
        try:
            # Ensure poi_id is an integer
            target_poi_id = int(poi_id)
            logger.info(f"Applying proximity-based ordering around POI {target_poi_id}")
            
            # Create custom ordering based on distance from target POI ID
            # This will load POIs in order of proximity: closest to target POI first
            # Cast both id and poi_id to integers to ensure proper arithmetic
            pois = base_pois_query.annotate(
                distance_from_target=Abs(Cast('id', IntegerField()) - target_poi_id)
            ).order_by('distance_from_target', 'id')
            
            # Log some debug information about the ordering
            if logger.isEnabledFor(logging.INFO):
                total_pois = pois.count()
                logger.info(f"Found {total_pois} POIs for vendor {vendor_id}, ordered by proximity to POI {target_poi_id}")
                
                # Sample the first few POIs to show the ordering
                first_few = list(pois[:5].values('id', 'distance_from_target'))
                logger.info(f"First 5 POIs in proximity order: {first_few}")
        
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid POI ID '{poi_id}' for proximity ordering, falling back to default ordering: {e}")
            # Fall back to default ordering if poi_id is not a valid integer
            pois = base_pois_query.order_by('id')
    else:
        # Default ordering by ID when no specific POI is targeted
        pois = base_pois_query.order_by('id')
    
    # If there are no POIs needing annotation for this user, show a persistent
    # in-page notification and keep the annotation list empty.
    all_annotated = False
    info_message = None
    reference_poi = None
    if not pois.exists():
        # Get one POI for map centering if the image has any POIs at all.
        reference_poi = PointsOfInterest.objects.filter(
            vendor_id=vendor_id,
            project_id=project_id,
            point__isnull=False
        ).only('id', 'point', 'epsg_code').order_by('id').first()

        if reference_poi:
            all_annotated = True
            info_message = "No POI's remain to be annotated"
        else:
            # Truly no POIs exist for this vendor/project
            return render(request, 'multiview_page.html', {
                'error_message': f'No points of interest found for vendor ID: {vendor_id}.',
                'vendor_id': vendor_id,
                'project_id': project_id,
                'project': project,
            })
    
    # Get the POI to use for determining image coordinates and COG URL
    if all_annotated and reference_poi:
        first_poi = reference_poi
    elif poi_id:
        # When a specific POI is requested, try to use it as the center point
        first_poi = PointsOfInterest.objects.filter(
            id=poi_id,
            vendor_id=vendor_id,
            project_id=project_id,
            point__isnull=False,
        ).only('id', 'point', 'epsg_code').first()
        if not first_poi:
            first_poi = pois.first()
            logger.warning(f"POI {poi_id} not found for vendor {vendor_id} in project {project_id}, using first available POI")
    else:
        # Use the first POI from our ordered list
        first_poi = pois.first()
    
    # Calculate center coordinates from all POIs
    # If all POIs share the same vendor_id, they should be from the same image
    # So we can use the first POI's coordinates as the center
    longitude = float(first_poi.point.x)
    latitude = float(first_poi.point.y)
    
    # Check if coordinates need transformation from projected to geographic
    if first_poi.epsg_code and str(first_poi.epsg_code) != '4326':
        try:
            source_crs = CRS(f"EPSG:{first_poi.epsg_code}")
            target_crs = CRS("EPSG:4326")
            transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            longitude, latitude = transformer.transform(longitude, latitude)
        except Exception as e:
            logger.warning(f"Could not transform coordinates for POI {first_poi.id}: {e}")
    
    # Check if COG exists for this vendor_id
    def cog_exists(vendor_id):
        cached_result = cache.get(f'cog_existence_{vendor_id}')
        if cached_result is not None:
            return cached_result

        blob_name = check_cog_existence(vendor_id, directory='cogs/')
        cache.set(f'cog_existence_{vendor_id}', blob_name, timeout=300)  
        return blob_name 
    
    cogurl = cog_exists(vendor_id)
    
    # Progressive loading: Start with smaller initial batch, then load in background
    # Initial load: 10 POIs for fast page load, then progressive loading
    from django.core.paginator import Paginator
    page_number = request.GET.get('page', 1)
    
    # Use different batch sizes for initial vs progressive loads
    is_initial_load = page_number == 1 or page_number == '1'
    batch_size = 10 if is_initial_load else 25  # Small initial batch, larger for background
    
    paginator = Paginator(pois, batch_size)
    page_obj = paginator.get_page(page_number)
    initial_next_page_number = page_obj.number + 1 if page_obj.has_next() else None
    
    # PERFORMANCE OPTIMIZATIONS for large datasets:
    # 1. Pagination to process data in chunks
    # 2. Bulk coordinate transformation using NumPy arrays
    # 3. Group by EPSG code to minimize transformer object creation
    # 4. Only load necessary fields from database using .only()
    # Prepare POI data for the template with optimizations
    poi_list = []
    
    # Use the globally imported numpy if available
    use_bulk_transform = NUMPY_AVAILABLE
    if not use_bulk_transform:
        logger.info("NumPy not available, falling back to individual coordinate transformation")
    
    if use_bulk_transform:
        # Group POIs by EPSG code for bulk transformation
        pois_by_epsg = {}
        for poi in page_obj:
            epsg_key = str(poi.epsg_code) if poi.epsg_code else '4326'
            if epsg_key not in pois_by_epsg:
                pois_by_epsg[epsg_key] = []
            pois_by_epsg[epsg_key].append(poi)
        
        # Process each EPSG group with bulk transformation
        for epsg_code, poi_group in pois_by_epsg.items():
            if epsg_code == '4326':
                # No transformation needed
                for poi in poi_group:
                    poi_data = {
                        'id': poi.id,
                        'catalog_id': poi.catalog_id or '',
                        'longitude': float(poi.point.x),
                        'latitude': float(poi.point.y),
                        'final_classification': poi.final_classification.label if poi.final_classification else None,
                        'final_species': poi.final_species.label if poi.final_species else None,
                        'final_confidence': poi.final_confidence.label if poi.final_confidence else None,
                    }
                    poi_list.append(poi_data)
            else:
                # Bulk transformation for projected coordinates
                try:
                    source_crs = CRS(f"EPSG:{epsg_code}")
                    target_crs = CRS("EPSG:4326")
                    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
                    
                    # Collect all coordinates for bulk transformation
                    x_coords = []
                    y_coords = []
                    for poi in poi_group:
                        x_coords.append(float(poi.point.x))
                        y_coords.append(float(poi.point.y))
                    
                    # Transform all coordinates at once
                    x_array = np.array(x_coords)
                    y_array = np.array(y_coords)
                    lon_array, lat_array = transformer.transform(x_array, y_array)
                    
                    # Create POI data with transformed coordinates
                    for i, poi in enumerate(poi_group):
                        poi_data = {
                            'id': poi.id,
                            'catalog_id': poi.catalog_id or '',
                            'longitude': float(lon_array[i]),
                            'latitude': float(lat_array[i]),
                            'final_classification': poi.final_classification.label if poi.final_classification else None,
                            'final_species': poi.final_species.label if poi.final_species else None,
                            'final_confidence': poi.final_confidence.label if poi.final_confidence else None,
                        }
                        poi_list.append(poi_data)
                        
                except Exception as e:
                    logger.warning(f"Bulk transformation failed for EPSG:{epsg_code}, falling back to individual: {e}")
                    # Fallback to individual transformation
                    for poi in poi_group:
                        poi_longitude = float(poi.point.x)
                        poi_latitude = float(poi.point.y)
                        try:
                            source_crs = CRS(f"EPSG:{epsg_code}")
                            target_crs = CRS("EPSG:4326")
                            transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
                            poi_longitude, poi_latitude = transformer.transform(poi_longitude, poi_latitude)
                        except Exception as transform_e:
                            logger.warning(f"Could not transform coordinates for POI {poi.id}: {transform_e}")
                        
                        poi_data = {
                            'id': poi.id,
                            'catalog_id': poi.catalog_id or '',
                            'longitude': poi_longitude,
                            'latitude': poi_latitude,
                            'final_classification': poi.final_classification.label if poi.final_classification else None,
                            'final_species': poi.final_species.label if poi.final_species else None,
                            'final_confidence': poi.final_confidence.label if poi.final_confidence else None,
                        }
                        poi_list.append(poi_data)
    else:
        # Fallback: Group POIs by EPSG code to minimize transformer creation
        pois_by_epsg = {}
        for poi in page_obj:
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
        
        # Process POIs in batches by EPSG code
        for epsg_code, poi_group in pois_by_epsg.items():
            transformer = transformers.get(epsg_code)
            
            for poi in poi_group:
                # Extract coordinates
                poi_longitude = float(poi.point.x)
                poi_latitude = float(poi.point.y)
                
                # Transform coordinates if needed
                if epsg_code != '4326' and transformer:
                    try:
                        poi_longitude, poi_latitude = transformer.transform(poi_longitude, poi_latitude)
                    except Exception as e:
                        logger.warning(f"Could not transform coordinates for POI {poi.id}: {e}")
                
                poi_data = {
                    'id': poi.id,
                    'catalog_id': poi.catalog_id or '',
                    'longitude': poi_longitude,
                    'latitude': poi_latitude,
                    'final_classification': poi.final_classification.label if poi.final_classification else None,
                    'final_species': poi.final_species.label if poi.final_species else None,
                    'final_confidence': poi.final_confidence.label if poi.final_confidence else None,
                }
                poi_list.append(poi_data)
    
    # Create form instance for the annotation form
    form = AnnotationForm()
    
    # Convert POI list to JSON for JavaScript
    import json
    pois_json = json.dumps(poi_list)
    
    # Handle AJAX requests for pagination
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            logger.info(f"AJAX request received for page {page_obj.number}, returning {len(poi_list)} POIs")
            
            # Calculate next and previous page numbers safely
            next_page_num = None
            if page_obj.has_next():
                next_page_num = page_obj.number + 1
            
            prev_page_num = None
            if page_obj.has_previous():
                prev_page_num = page_obj.number - 1
            
            return JsonResponse({
                'pois': poi_list,
                'poi_count': len(poi_list),
                'total_poi_count': paginator.count,
                'page_number': page_obj.number,
                'num_pages': paginator.num_pages,
                'has_next': page_obj.has_next(),
                'next_page_number': next_page_num,
                'has_previous': page_obj.has_previous(),
                'previous_page_number': prev_page_num,
            })
        except Exception as e:
            logger.error(f"Error in AJAX response: {e}")
            return JsonResponse({'error': str(e)}, status=500)
    
    context = {
        'vendor_id': vendor_id,
        'project_id': project_id,
        'longitude': longitude,
        'latitude': latitude,
        'cogurl': cogurl,
        'pois': poi_list,
        'pois_json': pois_json,
        'poi_count': len(poi_list),
        'total_poi_count': paginator.count,
        'page_obj': page_obj,
        'first_poi': first_poi,
        'user_is_superuser': request.user.is_superuser,
        'form': form,
        'user': user,
        'project': project,
        'all_annotated': all_annotated,
        'info_message': info_message,
        'initial_next_page_number': initial_next_page_number,
    }
    
    return render(request, 'multiview_page.html', context)

def multiview_list(request, project_id):
    """
    Display a paginated list of unique vendor_ids for POIs in the specified project.
    Users can sort the list and click on vendor_ids to view the multiview annotation page.
    """
    # Get the project object
    try:
        project = Project.objects.get(id=project_id)
    except Project.DoesNotExist:
        # Handle case where project doesn't exist
        return render(request, 'multiview_list_page.html', {
            'error_message': f'Project with ID {project_id} not found.',
            'project_id': project_id
        })
    
    # Get sort parameter from request
    sort_order = request.GET.get('sort', 'asc')
    page_number = request.GET.get('page', 1)
    
    # Create cache key for this project's vendor list
    cache_key = f'multiview_vendor_list_{project_id}'
    
    # Try to get cached vendor list
    vendor_list = cache.get(cache_key)
    cache_hit = vendor_list is not None
    
    if vendor_list is None:
        logger.info(f"Cache miss for multiview vendor list, project {project_id}")
        start_time = datetime.now()
        
        try:
            # Use raw SQL for better performance on large datasets
            from django.db import connection
            
            with connection.cursor() as cursor:
                # Set query timeout to prevent worker timeout
                cursor.execute("PRAGMA busy_timeout = 20000")  # 20 second timeout for SQLite
                
                cursor.execute("""
                    SELECT vendor_id, COUNT(*) as poi_count, %s as project_id
                    FROM animal_pointsofinterest 
                    WHERE project_id = %s AND vendor_id != '' AND vendor_id IS NOT NULL
                    GROUP BY vendor_id
                    ORDER BY vendor_id
                """, [project_id, project_id])
                
                vendor_list = []
                for row in cursor.fetchall():
                    vendor_list.append({
                        'id': row[0],  # vendor_id - Template expects 'id' field
                        'poi_count': row[1],
                        'project_id': row[2]
                    })
            
            query_time = (datetime.now() - start_time).total_seconds()
            
            # Cache the vendor list for 4 hours (14400 seconds)
            # This balances performance with data freshness
            cache.set(cache_key, vendor_list, timeout=14400)
            logger.info(f"Cached multiview vendor list for project {project_id}, {len(vendor_list)} vendors, query took {query_time:.3f}s")
        
        except Exception as e:
            logger.error(f"Error building multiview vendor list for project {project_id}: {str(e)}")
            # Return empty list as fallback
            vendor_list = []
            cache.set(cache_key, vendor_list, timeout=300)  # Cache empty result for 5 minutes
        logger.info(f"Cached multiview vendor list for project {project_id}, {len(vendor_list)} vendors, query took {query_time:.3f}s")
    else:
        logger.info(f"Cache hit for multiview vendor list, project {project_id}")
    
    # Apply sorting to cached data
    if sort_order == 'desc':
        vendor_list = sorted(vendor_list, key=lambda x: x['id'], reverse=True)
    else:
        vendor_list = sorted(vendor_list, key=lambda x: x['id'])
    
    # Paginate the results
    paginator = Paginator(vendor_list, 50)  # Show 50 vendor IDs per page
    page_obj = paginator.get_page(page_number)
    
    return render(request, 'multiview_list_page.html', {
        'page_obj': page_obj,
        'sort_order': sort_order,
        'project_id': project_id,
        'project': project,
        'cache_hit': cache_hit,  # For debugging purposes
        'cache_key': cache_key if settings.DEBUG else None  # Only show in debug mode
    })

def invalidate_multiview_vendor_cache(project_id):
    """
    Invalidate the cached vendor list for a specific project.
    Call this function when POIs are created, updated, or deleted.
    """
    cache_key = f'multiview_vendor_list_{project_id}'
    cache.delete(cache_key)
    logger.info(f"Invalidated multiview vendor list cache for project {project_id}")

def clear_multiview_cache(request, project_id):
    """
    Admin-only view to manually clear the multiview vendor cache for a project.
    Useful for debugging or when data needs to be refreshed immediately.
    """
    if not request.user.is_superuser:
        return HttpResponseForbidden("You don't have permission to access this function.")
    
    if request.method == 'POST':
        invalidate_multiview_vendor_cache(project_id)
        from django.contrib import messages
        messages.success(request, f'Multiview vendor cache cleared for project {project_id}.')
        
        # Redirect back to the multiview list
        return redirect('multiview_list', project_id=project_id)
    
    # Show confirmation page
    return render(request, 'cache_clear_confirm.html', {
        'project_id': project_id,
        'cache_type': 'Multiview Vendor List'
    })

def warm_multiview_vendor_cache(project_id):
    """
    Pre-populate the multiview vendor cache for a project.
    This can be called periodically or after bulk operations to ensure cache is fresh.
    """
    cache_key = f'multiview_vendor_list_{project_id}'
    
    try:
        logger.info(f"Warming multiview vendor cache for project {project_id}")
        start_time = datetime.now()
        
        # Get the data
        vendor_ids_query = (
            PointsOfInterest.objects.filter(
                project_id=project_id,
            )
            .exclude(vendor_id='')
            .values('vendor_id')
            .annotate(
                poi_count=Count('id'),
                project_id=Value(project_id, output_field=IntegerField())
            )
            .order_by('vendor_id')
        )
        
        vendor_list = []
        for item in vendor_ids_query:
            vendor_list.append({
                'id': item['vendor_id'],
                'project_id': item['project_id'],
                'poi_count': item['poi_count']
            })
        
        query_time = (datetime.now() - start_time).total_seconds()
        
        # Cache for 30 minutes
        cache.set(cache_key, vendor_list, timeout=1800)
        logger.info(f"Warmed multiview vendor cache for project {project_id}, {len(vendor_list)} vendors, took {query_time:.3f}s")
        
        return len(vendor_list)
        
    except Exception as e:
        logger.error(f"Error warming multiview vendor cache for project {project_id}: {e}")
        return 0

def invalidate_deduplication_cache(project_id):
    """
    Clear deduplication cache for a specific project.
    Should be called when POIs are created, deleted, or their locations are modified.
    """
    cache_keys_to_clear = [
        f'deduplication_list_{project_id}_asc',
        f'deduplication_list_{project_id}_desc',
        f'deduplication_list_v2_{project_id}_asc',
        f'deduplication_list_v2_{project_id}_desc'
    ]
    
    cleared_count = 0
    for cache_key in cache_keys_to_clear:
        if cache.delete(cache_key):
            cleared_count += 1
    
    logger.info(f"Cleared {cleared_count} deduplication cache keys for project {project_id}")
    return cleared_count

def clear_deduplication_cache(request, project_id):
    """
    Admin view to manually clear deduplication cache for a project.
    """
    if not request.user.is_superuser:
        from django.contrib import messages
        messages.error(request, 'Permission denied. Only superusers can clear cache.')
        return redirect('project_detail', project_id=project_id)
    
    if request.method == 'POST':
        cleared_count = invalidate_deduplication_cache(project_id)
        from django.contrib import messages
        messages.success(request, f'Cleared {cleared_count} deduplication cache entries for project {project_id}.')
        return redirect('project_detail', project_id=project_id)
    
    # Show confirmation page
    return render(request, 'cache_clear_confirm.html', {
        'project_id': project_id,
        'cache_type': 'Deduplication List'
    })


@login_required
def multiview_annotated(request, project_id, vendor_id):
    """
    Return annotated POIs for a specific vendor_id and project that have been annotated by the current user.
    This endpoint is used by the "View Annotated Points" feature in the multiview page.
    """
    logger.info(f"multiview_annotated called with project_id={project_id}, vendor_id={vendor_id}")
    
    user = request.user
    project = Project.objects.get(id=project_id)
    
    # Only handle AJAX requests
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'error': 'This endpoint only accepts AJAX requests'}, status=400)
    
    try:
        # Get POIs for this vendor_id and project that HAVE been annotated by the current user
        annotated_pois_query = PointsOfInterest.objects.filter(
            vendor_id=vendor_id,
            project_id=project_id,
            point__isnull=False,  # Ensure we have geometry
            annotations__user=user  # Only POIs with annotations from this user
        ).select_related('final_classification', 'final_species', 'final_confidence').only(
            'id', 'catalog_id', 'point', 'epsg_code',
            'final_classification__label', 'final_species__label', 'final_confidence__label'
        ).distinct().order_by('id')
        
        # Prepare POI data for the frontend
        annotated_poi_list = []
        
        # Group POIs by EPSG code for efficient coordinate transformation
        pois_by_epsg = {}
        for poi in annotated_pois_query:
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
        
        # Process POIs and transform coordinates
        for epsg_code, poi_group in pois_by_epsg.items():
            transformer = transformers.get(epsg_code)
            
            for poi in poi_group:
                poi_longitude = float(poi.point.x)
                poi_latitude = float(poi.point.y)
                
                # Transform coordinates if needed
                if epsg_code != '4326' and transformer:
                    try:
                        poi_longitude, poi_latitude = transformer.transform(poi_longitude, poi_latitude)
                    except Exception as e:
                        logger.warning(f"Could not transform coordinates for annotated POI {poi.id}: {e}")
                
                poi_data = {
                    'id': poi.id,
                    'catalog_id': poi.catalog_id or '',
                    'longitude': poi_longitude,
                    'latitude': poi_latitude,
                    'final_classification': poi.final_classification.label if poi.final_classification else None,
                    'final_species': poi.final_species.label if poi.final_species else None,
                    'final_confidence': poi.final_confidence.label if poi.final_confidence else None,
                }
                annotated_poi_list.append(poi_data)
        
        logger.info(f"Found {len(annotated_poi_list)} annotated POIs for vendor {vendor_id} by user {user.id}")
        
        return JsonResponse({
            'success': True,
            'annotated_pois': annotated_poi_list,
            'count': len(annotated_poi_list),
            'vendor_id': vendor_id,
            'project_id': project_id
        })
        
    except Exception as e:
        logger.error(f"Error in multiview_annotated: {e}")
        return JsonResponse({
            'success': False,
            'error': f'Error fetching annotated POIs: {str(e)}'
        }, status=500)
