"""
Custom decorators for project access control and two-factor authentication
"""
import sys
from pathlib import Path
from functools import wraps
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.conf import settings
from .auth_utils import should_enforce_2fa_for_user, get_2fa_enforcement_reason

# Import for two-factor auth - will be available after docker rebuild
try:
    from django_otp import user_has_device
except ImportError:
    # Fallback for development before packages are installed
    def user_has_device(user):
        return True

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from animal.utilities import user_has_project_access


def require_project_access(view_func):
    """
    Decorator to check if user has access to a project.
    Expects project_id as a URL parameter.
    """
    @wraps(view_func)
    def wrapper(request, project_id, *args, **kwargs):
        if not user_has_project_access(request.user, project_id):
            return HttpResponseForbidden("You don't have access to this project.")
        return view_func(request, project_id, *args, **kwargs)
    return wrapper


def require_project_access_or_redirect(redirect_url='landing_page'):
    """
    Decorator to check if user has access to a project.
    Redirects to specified URL if access is denied.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, project_id, *args, **kwargs):
            if not user_has_project_access(request.user, project_id):
                # Get project name for the error message
                from .models import Project
                try:
                    project = Project.objects.get(id=project_id)
                    project_name = project.label
                except Project.DoesNotExist:
                    project_name = f"Project {project_id}"
                
                messages.error(request, f"You don't have access to {project_name}. Please contact the project owner to request access.")
                return redirect(redirect_url)
            return view_func(request, project_id, *args, **kwargs)
        return wrapper
    return decorator


def require_two_factor_auth(view_func):
    """
    Decorator to require two-factor authentication for a view.
    Redirects to 2FA setup if user doesn't have it configured.
    Only enforces based on user-specific 2FA enforcement rules.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Only enforce if 2FA enforcement applies to this specific user
        if not should_enforce_2fa_for_user(request.user):
            return view_func(request, *args, **kwargs)
            
        if request.user.is_authenticated and not user_has_device(request.user):
            reason = get_2fa_enforcement_reason(request.user)
            message = reason if reason else 'Two-factor authentication is required. Please set it up to continue.'
            messages.warning(request, message)
            return redirect('two_factor:setup')
        return view_func(request, *args, **kwargs)
    return wrapper


def otp_required_if_configured(view_func):
    """
    Similar to django-otp's otp_required, but more lenient for migration.
    Only requires OTP if user has devices configured and 2FA enforcement applies to the user.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Only enforce if 2FA enforcement applies to this specific user
        if not should_enforce_2fa_for_user(request.user):
            return view_func(request, *args, **kwargs)
            
        if request.user.is_authenticated:
            if user_has_device(request.user) and not request.user.is_verified():
                messages.warning(
                    request, 
                    'Please complete two-factor authentication.'
                )
                return redirect('two_factor:login')
        return view_func(request, *args, **kwargs)
    return wrapper
