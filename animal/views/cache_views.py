# Cache management views for administrators
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.conf import settings
from animal.cache_utils import invalidate_project_page_cache, invalidate_project_page_cache_for_all_users
from animal.models import Project

def is_superuser(user):
    return user.is_superuser

@user_passes_test(is_superuser, login_url='/access-denied/')
def clear_project_page_cache(request, project_id):
    """
    Admin view to manually clear project page cache for a specific project.
    """
    # Check if caching is enabled
    cache_enabled = getattr(settings, 'ENABLE_PROJECT_PAGE_CACHE', True)
    
    if not cache_enabled:
        messages.info(request, 'Project page caching is currently disabled in settings.')
        return redirect(f'/project/{project_id}/')
    
    try:
        project = Project.objects.get(id=project_id)
        
        if request.method == 'POST':
            # Clear cache for all users in this project
            invalidate_project_page_cache_for_all_users(project_id)
            messages.success(request, f'Project page cache cleared for {project.label}')
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': True, 'message': 'Cache cleared successfully'})
            
            return redirect(f'/project/{project_id}/')
        
        return render(request, 'cache_management.html', {
            'project': project,
            'project_id': project_id,
            'cache_enabled': cache_enabled
        })
        
    except Project.DoesNotExist:
        messages.error(request, 'Project not found')
        return redirect('/project/')

@user_passes_test(is_superuser, login_url='/access-denied/')
def clear_user_project_cache(request, project_id, user_id):
    """
    Admin view to clear project page cache for a specific user.
    """
    # Check if caching is enabled
    cache_enabled = getattr(settings, 'ENABLE_PROJECT_PAGE_CACHE', True)
    
    if not cache_enabled:
        messages.info(request, 'Project page caching is currently disabled in settings.')
        return redirect(f'/project/{project_id}/')
    
    try:
        project = Project.objects.get(id=project_id)
        
        if request.method == 'POST':
            invalidate_project_page_cache(project_id, user_id)
            messages.success(request, f'Project page cache cleared for user {user_id} in {project.label}')
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': True, 'message': 'User cache cleared successfully'})
        
        return redirect(f'/project/{project_id}/')
        
    except Project.DoesNotExist:
        messages.error(request, 'Project not found')
        return redirect('/project/')
