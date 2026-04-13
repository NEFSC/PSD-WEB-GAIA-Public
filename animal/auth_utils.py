"""
Utility functions for determining 2FA enforcement based on user attributes.
"""
from django.conf import settings


def should_enforce_2fa_for_user(user):
    """
    Determine if 2FA should be enforced for a specific user based on settings and user attributes.
    
    Args:
        user: Django User object
        
    Returns:
        bool: True if 2FA should be enforced for this user, False otherwise.
    """
    # If global 2FA enforcement is enabled, enforce for all users
    if getattr(settings, 'ENFORCE_TWO_FACTOR_AUTH', True):
        return True
    
    # If NOAA-specific enforcement is enabled, check user's email
    if getattr(settings, 'ENFORCE_TWO_FACTOR_AUTH_NOAA', False):
        if user and user.email and user.email.lower().endswith('noaa.gov'):
            return True
    
    # No enforcement applies to this user
    return False


def get_2fa_enforcement_reason(user):
    """
    Get a human-readable reason why 2FA is being enforced for a user.
    
    Args:
        user: Django User object
        
    Returns:
        str: Reason for 2FA enforcement, or empty string if not enforced.
    """
    if getattr(settings, 'ENFORCE_TWO_FACTOR_AUTH', True):
        return "Two-factor authentication is required for all users."
    
    if getattr(settings, 'ENFORCE_TWO_FACTOR_AUTH_NOAA', False):
        if user and user.email and user.email.lower().endswith('noaa.gov'):
            return "Two-factor authentication is required for NOAA.gov users."
    
    return ""
