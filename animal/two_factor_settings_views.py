"""
Unified Two-Factor Authentication views for GAIA
"""
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.urls import reverse_lazy, reverse
from django.views.generic import TemplateView
from django.utils.decorators import method_decorator
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.conf import settings

# Import OTP models (no static backup tokens)
try:
    from django_otp.models import Device
    from django_otp.plugins.otp_totp.models import TOTPDevice
    OTP_AVAILABLE = True
except ImportError:
    # Fallback classes to prevent errors
    class TOTPDevice:
        objects = None
    OTP_AVAILABLE = False

# Import WebAuthn models with fallback
try:
    from .webauthn_models import WebAuthnCredential
    WEBAUTHN_AVAILABLE = True
except ImportError:
    # Fallback class to prevent errors
    class WebAuthnCredential:
        objects = None
    WEBAUTHN_AVAILABLE = False


class TwoFactorSettingsView(LoginRequiredMixin, TemplateView):
    """
    Unified Two-Factor Authentication settings page
    Shows both TOTP devices and WebAuthn credentials
    """
    template_name = 'two_factor/settings.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        
        # Get TOTP devices if available
        totp_devices = []
        if OTP_AVAILABLE and TOTPDevice.objects is not None:
            totp_devices = TOTPDevice.objects.filter(user=user, confirmed=True)
        
        # Get WebAuthn credentials if available
        webauthn_credentials = []
        if WEBAUTHN_AVAILABLE and WebAuthnCredential.objects is not None:
            webauthn_credentials = WebAuthnCredential.objects.filter(user=user)
        
        # Check if user has any 2FA enabled
        has_2fa = (OTP_AVAILABLE and totp_devices) or (WEBAUTHN_AVAILABLE and webauthn_credentials)
        
        context.update({
            'totp_devices': totp_devices,
            'webauthn_credentials': webauthn_credentials,
        
            'has_2fa': has_2fa,
            'webauthn_available': WEBAUTHN_AVAILABLE,
            'otp_available': OTP_AVAILABLE,
        })
        
        return context


@login_required
@require_http_methods(["POST"])
def remove_totp_device(request, device_id):
    """Remove a TOTP device"""
    if not OTP_AVAILABLE or TOTPDevice.objects is None:
        messages.error(request, 'TOTP functionality is not available.')
        return redirect('account_page')
        
    device = get_object_or_404(TOTPDevice, id=device_id, user=request.user)
    device.delete()
    messages.success(request, 'TOTP authenticator has been removed.')
    return redirect('account_page')


# NOTE: Static backup tokens removed by design. The generate_backup_tokens view
# and StaticDevice model are no longer used.


@login_required
def disable_all_2fa_unified(request):
    """Disable all two-factor authentication for the user"""
    if request.method == 'POST':
        user = request.user
        
        # Remove TOTP devices if available
        if OTP_AVAILABLE and TOTPDevice.objects is not None:
            TOTPDevice.objects.filter(user=user).delete()
    # Static backup tokens not used - nothing to remove
        
        # Remove WebAuthn credentials if available
        if WEBAUTHN_AVAILABLE and WebAuthnCredential.objects is not None:
            WebAuthnCredential.objects.filter(user=user).delete()
        
        messages.success(request, 'All two-factor authentication has been disabled for your account.')
        return redirect('two_factor_settings')
    
    return render(request, 'two_factor/disable_all_confirm.html')
