"""
Views for user account management
"""
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.shortcuts import render, redirect
from django.db.models import Q

# Import for two-factor auth - handle import gracefully if not installed yet
try:
    from django_otp import user_has_device
    from django_otp.models import Device
    from django_otp.plugins.otp_totp.models import TOTPDevice
except ImportError:
    # Fallback for development before packages are installed
    def user_has_device(user):
        return False
    Device = None
    TOTPDevice = None

# Import WebAuthn credentials
try:
    from animal.webauthn_models import WebAuthnCredential
    WEBAUTHN_AVAILABLE = True
except ImportError:
    WebAuthnCredential = None
    WEBAUTHN_AVAILABLE = False

from animal.models import Project, ProjectAccess


@login_required
def account_page(request):
    """Display user account information and management options"""
    user = request.user
    
    # Handle password change form
    password_form = None
    if request.method == 'POST' and 'change_password' in request.POST:
        password_form = PasswordChangeForm(request.user, request.POST)
        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)  # Keep user logged in after password change
            messages.success(request, 'Your password was successfully updated!')
            return redirect('account_page')
        else:
            messages.error(request, 'Please correct the errors below.')
    
    if not password_form:
        password_form = PasswordChangeForm(request.user)
    
    # Get user's projects
    if user.is_superuser:
        # Superusers can see all projects
        projects = Project.objects.all().select_related('owner')
    else:
        # Regular users can only see projects they have access to
        projects = Project.objects.filter(projectaccess__user=user).select_related('owner')
    
    # Get two-factor authentication status
    has_2fa = False
    totp_devices = []
    static_tokens_count = 0  # static tokens removed by design; keep variable for templates
    webauthn_credentials = []
    
    # Check OTP devices using the same logic as TwoFactorSettingsView
    if TOTPDevice:
        try:
            # Only count confirmed TOTP devices
            totp_devices = TOTPDevice.objects.filter(user=user, confirmed=True)
        except:
            totp_devices = []
    
    # Static backup tokens are no longer used. static_tokens_count stays 0.
    
    # Check WebAuthn credentials
    if WEBAUTHN_AVAILABLE and WebAuthnCredential:
        try:
            webauthn_credentials = WebAuthnCredential.objects.filter(user=user)
        except:
            webauthn_credentials = []
    
    # Determine if user has 2FA enabled using the same logic as TwoFactorSettingsView
    has_2fa = bool(totp_devices) or bool(webauthn_credentials)
    
    context = {
        'user': user,
        'password_form': password_form,
        'projects': projects,
        'projects_count': projects.count(),
        'has_2fa': has_2fa,
        'totp_devices': totp_devices,
        'static_tokens_count': static_tokens_count,
        'webauthn_credentials': webauthn_credentials,
        'webauthn_available': WEBAUTHN_AVAILABLE,
    }
    
    return render(request, 'account_page.html', context)

@login_required
def disable_all_2fa(request):
    """Disable ALL 2FA devices for the user"""
    if request.method == 'POST':
        try:
            removed_count = 0
            
            # Remove all TOTP devices
            if TOTPDevice:
                totp_devices = TOTPDevice.objects.filter(user=request.user)
                totp_count = totp_devices.count()
                totp_devices.delete()
                removed_count += totp_count
            
            # Static backup tokens are not used; nothing to remove
            
            # Remove all WebAuthn credentials
            if WEBAUTHN_AVAILABLE and WebAuthnCredential:
                webauthn_creds = WebAuthnCredential.objects.filter(user=request.user)
                webauthn_count = webauthn_creds.count()
                webauthn_creds.delete()
                removed_count += webauthn_count
            
            if removed_count > 0:
                messages.success(
                    request, 
                    f'Successfully removed all two-factor authentication devices ({removed_count} devices). '
                    'Two-factor authentication is now disabled.'
                )
            else:
                messages.info(request, 'No two-factor authentication devices were found to remove.')
                
        except Exception as e:
            messages.error(request, f'Error removing two-factor devices: {str(e)}')
    
    return redirect('account_page')
