# Basic stack
import os
import django
from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import render, redirect
from django.db.models import Count, Q, Value, IntegerField
from django.core.cache import cache
from django.conf import settings
from animal.models import Project, PointsOfInterest, Annotations, Fishnet, FishnetReviews
from animal.cache_utils import invalidate_project_page_cache

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gaia.settings')
os.environ["CPL_DEBUG"] = "ON" # Should enable GDAL debuggin
django.setup()

# DISABLED: check_card_availability feature commented out per user request
# def check_card_availability(user, project_id):
#     """
#     Check availability of functionality for each card in the project page.
#     Returns a dictionary indicating which cards should be enabled.
#     """
#     availability = {}
#     
#     # Load Imagery - Always available (collection functionality)
#     availability['load_imagery'] = True
#     
#     # Get user's reviewed fishnet IDs and POI annotations in single queries for efficiency
#     user_reviewed_fishnets = set(FishnetReviews.objects.filter(
#         user_id=user.id
#     ).values_list('fishnet_id', flat=True))
#     
#     user_annotated_pois = set(Annotations.objects.filter(
#         user_id=user.id
#     ).values_list('poi_id', flat=True))
#     
#     # Add Points (detect) - Check if there are fishnet cells available for review
#     def has_cells_to_review():
#         # Get IDs of Fishnets with 2+ annotations
#         full_fishnet_ids = set(FishnetReviews.objects.values('fishnet_id')
#             .annotate(count=Count('fishnet_id'))
#             .filter(count__gte=2)
#             .values_list('fishnet_id', flat=True))
#         
#         # Check if there are any fishnets available for this project
#         available_fishnets = Fishnet.objects.filter(
#             project_id=project_id
#         ).exclude(
#             id__in=list(user_reviewed_fishnets | full_fishnet_ids)
#         ).exists()
#         
#         return available_fishnets
#     
#     availability['add_points'] = has_cells_to_review()
#     
#     # Annotate Points - Check if there are POIs available for annotation
#     def has_pois_to_annotate():
#         # Get IDs of POIs with 3+ annotations
#         full_poi_ids = set(Annotations.objects.values('poi_id')
#             .annotate(count=Count('poi_id'))
#             .filter(count__gte=3)
#             .values_list('poi_id', flat=True))
#         
#         # Check if there are any POIs available for annotation
#         available_pois = PointsOfInterest.objects.filter(
#             project_id=project_id
#         ).exists()
#         
#         return available_pois
#     
#     availability['annotate_points'] = has_pois_to_annotate()
#     
#     # Annotate Batch (multiview) - Check if there are vendor IDs with POIs that need annotation
#     def has_multiview_data():
#         # Check if there are POIs that don't have annotations from the current user
#         pois_needing_annotation = PointsOfInterest.objects.filter(
#             project_id=project_id,
#             point__isnull=False
#         ).exclude(
#             annotations__user=user
#         ).exclude(vendor_id='').exists()
#         
#         return pois_needing_annotation
#     
#     availability['annotate_batch'] = has_multiview_data()
#     
#     # Admin-only checks
#     if user.is_superuser:
#         # Validation - Check if there are POIs with classification=14 and 2+ annotations
#         validation_pois = PointsOfInterest.objects.filter(
#             project_id=project_id
#         ).annotate(
#             classification_14_count=Count('annotations', filter=Q(annotations__classification=14)),
#             total_annotations=Count('annotations', distinct=True)
#         ).filter(
#             classification_14_count__gte=1,
#             total_annotations__gt=2
#         ).exists()
#         
#         availability['validation'] = validation_pois
#         
#         # Deduplication - Check if there are POIs with final_classification=14 that have duplicates within 100m
#         def has_duplicate_pois():
#             from django.contrib.gis.db.models.functions import Distance
#             from django.contrib.gis.measure import D
#             from django.db.models import Exists, OuterRef
#             
#             # Use PostGIS spatial query for efficient duplicate detection
#             # This is much faster than manual Haversine calculations
#             
#             # Create a subquery to find nearby POIs for each POI
#             nearby_pois_subquery = PointsOfInterest.objects.filter(
#                 project_id=project_id,
#                 final_classification_id=14,
#                 point__isnull=False,
#                 point__distance_lte=(OuterRef('point'), D(m=100))  # Within 100 meters
#             ).exclude(
#                 id=OuterRef('id')  # Exclude self
#             )
#             
#             # Check if any POI has nearby duplicates using spatial index
#             duplicate_exists = PointsOfInterest.objects.filter(
#                 project_id=project_id,
#                 final_classification_id=14,
#                 point__isnull=False
#             ).annotate(
#                 has_nearby=Exists(nearby_pois_subquery)
#             ).filter(has_nearby=True).exists()
#             
#             return duplicate_exists
#         
#         availability['deduplication'] = has_duplicate_pois()
#     else:
#         availability['validation'] = False
#         availability['deduplication'] = False
#     
#     # Export Results (dissemination) - Check if there are annotated POIs to export
#     annotated_pois = PointsOfInterest.objects.filter(
#         project_id=project_id,
#         annotations__isnull=False
#     ).exists()
#     
#     availability['export_results'] = annotated_pois
#     
#     return availability

# DISABLED: get_work_summary function commented out per user request
# def get_work_summary(user, project_id):
#     """
#     Get a summary of available work items for the user in this project.
#     Returns counts of items available for each type of work.
#     """
#     summary = {}
#     
#     # Count available fishnet cells for detection
#     user_reviewed_fishnets = set(FishnetReviews.objects.filter(
#         user_id=user.id
#     ).values_list('fishnet_id', flat=True))
#     
#     full_fishnet_ids = set(FishnetReviews.objects.values('fishnet_id')
#         .annotate(count=Count('fishnet_id'))
#         .filter(count__gte=2)
#         .values_list('fishnet_id', flat=True))
#     
#     available_cells = Fishnet.objects.filter(
#         project_id=project_id
#     ).exclude(
#         id__in=list(user_reviewed_fishnets | full_fishnet_ids)
#     ).count()
#     
#     summary['cells_to_review'] = available_cells
#     
#     
#     # Count POIs available for batch annotation (multiview)
#     batch_pois = PointsOfInterest.objects.filter(
#         project_id=project_id,
#     ).exclude(vendor_id='').count()
#     
#     summary['batch_pois_available'] = batch_pois
#     
#     return summary

def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('landing_page')
    else:
        form = AuthenticationForm()
        
    return render(request, 'login.html', {'form': form})

def landing_page(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            # Superusers can see all projects
            projects = Project.objects.select_related('owner', 'zoom_level').all()
        else:
            # Regular users can only see projects they have access to
            projects = Project.objects.select_related('owner', 'zoom_level').filter(projectaccess__user=request.user)
    else:
        projects = Project.objects.none()  # No projects for unauthenticated users
    return render(request, 'landing_page.html', {'projects': projects})

def project_page(request, project_id=None):
    if project_id is None:
        return redirect('landing_page')  # Redirect to landing page if no project_id is provided
    project = Project.objects.get(id=project_id)
    
    # DISABLED: Project page caching functionality commented out per user request
    # Check if caching is enabled in settings
    # cache_enabled = getattr(settings, 'ENABLE_PROJECT_PAGE_CACHE', True)
    cache_enabled = False  # Force disable caching
    
    # Initialize variables
    # card_availability = None  # DISABLED: check_card_availability feature commented out
    work_summary = None
    # cache_hit_availability = False  # DISABLED: check_card_availability feature commented out
    cache_hit_work_summary = False
    
    # DISABLED: check_card_availability feature - setting all cards as available
    card_availability = {
        'load_imagery': True,
        'load_points': True,
        'add_points': True,
        'annotate_points': True,
        'annotate_batch': True,
        'validation': True,
        'deduplication': True,
        'export_results': True,
    }
    cache_hit_availability = False  # Since we're not using cache for card_availability anymore
    
    # DISABLED: Project page caching functionality
    # if cache_enabled:
    #     # Create cache keys for user-specific data
    #     user_id = request.user.id
    #     # cache_key_availability = f'project_card_availability_{project_id}_{user_id}'  # DISABLED
    #     cache_key_work_summary = f'project_work_summary_{project_id}_{user_id}'
    #     
    #     # Try to get cached data first
    #     # card_availability = cache.get(cache_key_availability)  # DISABLED
    #     # if card_availability is not None:  # DISABLED
    #     #     cache_hit_availability = True  # DISABLED
    #     
    #     work_summary = cache.get(cache_key_work_summary)
    #     if work_summary is not None:
    #         cache_hit_work_summary = True
    
    # If cache miss or caching disabled, compute the data
    # if card_availability is None:  # DISABLED: check_card_availability feature commented out
    #     # Check availability of functionality for each card
    #     card_availability = check_card_availability(request.user, project_id)
    #     
    #     # Cache for 4 hours (14400 seconds) only if caching is enabled
    #     if cache_enabled:
    #         cache.set(cache_key_availability, card_availability, timeout=14400)
    
    # DISABLED: Project page caching functionality - always compute work summary fresh
    # if work_summary is None:
    # Count available work items for user feedback
    # work_summary = get_work_summary(request.user, project_id)  # DISABLED
    
    # DISABLED: get_work_summary function - setting empty work summary
    work_summary = {
        'cells_to_review': 0,
        'batch_pois_available': 0,
    }
    
    # DISABLED: Project page caching functionality
    # Cache for 4 hours (14400 seconds) only if caching is enabled
    # if cache_enabled:
    #     cache.set(cache_key_work_summary, work_summary, timeout=14400)
    
    return render(request, 'project_page.html', {
        'project': project,
        'card_availability': card_availability,
        'work_summary': work_summary,
        'cache_debug': {
            'availability_hit': cache_hit_availability,
            'work_summary_hit': cache_hit_work_summary,
            'cache_enabled': cache_enabled,
            'cache_keys': {
                'availability': 'DISABLED - check_card_availability feature commented out',
                'work_summary': 'DISABLED - Project page caching functionality disabled'
            }
        } if request.user.is_superuser else None
    })