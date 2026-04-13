# Cache utilities for the GAIA project
from django.core.cache import cache
from django.conf import settings

def invalidate_project_page_cache(project_id, user_id=None):
    """
    DISABLED: Project page caching functionality has been disabled per user request.
    This function now does nothing but is kept for compatibility.
    """
    # DISABLED: Project page caching functionality
    # Check if caching is enabled
    # cache_enabled = getattr(settings, 'ENABLE_PROJECT_PAGE_CACHE', True)
    # 
    # if not cache_enabled:
    #     return  # Skip cache operations if disabled
    # 
    # if user_id is not None:
    #     # Invalidate cache for specific user
    #     # cache_key_availability = f'project_card_availability_{project_id}_{user_id}'  # DISABLED
    #     cache_key_work_summary = f'project_work_summary_{project_id}_{user_id}'
    #     # cache.delete(cache_key_availability)  # DISABLED: check_card_availability feature
    #     cache.delete(cache_key_work_summary)
    # else:
    #     # For all users, cache will naturally expire after 4 hours
    #     # Or you can call this function with each user_id individually
    #     pass
    return  # Do nothing - caching is disabled

def invalidate_project_page_cache_for_all_users(project_id):
    """
    DISABLED: Project page caching functionality has been disabled per user request.
    This function now does nothing but is kept for compatibility.
    """
    # DISABLED: Project page caching functionality
    # Check if caching is enabled
    # cache_enabled = getattr(settings, 'ENABLE_PROJECT_PAGE_CACHE', True)
    # 
    # if not cache_enabled:
    #     return  # Skip cache operations if disabled
    # 
    # # Get all users who have annotations in this project
    # from animal.models import Annotations
    # user_ids = Annotations.objects.filter(
    #     poi__project_id=project_id
    # ).values_list('user_id', flat=True).distinct()
    # 
    # for user_id in user_ids:
    #     invalidate_project_page_cache(project_id, user_id)
    return  # Do nothing - caching is disabled
