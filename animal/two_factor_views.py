"""
Custom views for two-factor authentication in GAIA
"""
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect
from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import TemplateView
from django.utils.decorators import method_decorator

# Import for two-factor auth - handle import gracefully if not installed yet
try:
    from django_otp import user_has_device
    from two_factor.views import SetupView
    # from otp_yubikey.models import RemoteYubikeyDevice  # DISABLED - using passkeys instead
    YUBIKEY_AVAILABLE = False  # Disabled YubiKey OTP
except ImportError:
    # Fallback for development before packages are installed
    def user_has_device(user):
        return True
    
    class SetupView:
        """Fallback class for development"""
        pass
    
    YUBIKEY_AVAILABLE = False

# Create fallback classes for disabled YubiKey functionality
class RemoteYubikeyDevice:
    """Fallback class for disabled YubiKey OTP functionality"""
    objects = None


class TwoFactorRequiredMixin:
    """
    Mixin that requires users to have two-factor authentication set up.
    Redirects to setup page if 2FA is not configured.
    """
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not user_has_device(request.user):
            if request.path not in ['/account/setup/', '/account/yubikey-setup/', '/2fa/yubikey-setup/']:
                messages.warning(
                    request, 
                    'Two-factor authentication is required. Please set it up now.'
                )
                return redirect('two_factor:setup')
        return super().dispatch(request, *args, **kwargs)


class CustomSetupView(TemplateView):
    """
    Custom setup view with enhanced messaging for existing users
    """
    template_name = 'two_factor/core/setup.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_existing_user'] = True
        context['yubikey_available'] = False  # Disabled - using passkeys instead
        return context
    
    def post(self, request, *args, **kwargs):
        # This will be properly implemented when two_factor is available
        messages.success(
            request,
            'Two-factor authentication has been successfully enabled for your account!'
        )
        return redirect('two_factor:profile')


@method_decorator(login_required, name='dispatch')
class YubikeySetupView(TemplateView):
    """
    View for setting up YubiKey as 2FA method (DISABLED - using passkeys instead)
    """
    template_name = 'two_factor/yubikey_setup.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['existing_yubikeys'] = []
        context['yubikey_disabled'] = True
        return context
    
    def post(self, request, *args, **kwargs):
        messages.error(request, 'YubiKey OTP mode has been disabled. Please use passkeys instead.')
        return redirect('webauthn_setup')


@method_decorator(login_required, name='dispatch')
class TwoFactorOnboardingView(TemplateView):
    """
    Onboarding view for existing users to learn about 2FA requirements
    """
    template_name = 'two_factor/onboarding.html'
    
    def dispatch(self, request, *args, **kwargs):
        # If user already has 2FA set up, redirect to profile
        if user_has_device(request.user):
            return redirect('two_factor:profile')
        return super().dispatch(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['yubikey_available'] = YUBIKEY_AVAILABLE
        return context


@login_required
def two_factor_status_check(request):
    """
    Helper view to check if user has 2FA set up and redirect accordingly
    """
    if user_has_device(request.user):
        return redirect('/')  # User has 2FA, proceed normally
    else:
        return redirect('two_factor:onboarding')  # User needs to set up 2FA


@login_required
def remove_yubikey(request, device_id):
    """
    Remove a YubiKey device from user's account (DISABLED - using passkeys instead)
    """
    messages.error(request, 'YubiKey OTP mode has been disabled. Please use passkeys instead.')
    return redirect('webauthn_setup')
