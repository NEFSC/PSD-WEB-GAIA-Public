"""
Context processor for two-factor authentication
Provides YubiKey device information to templates (DISABLED - using passkeys instead)
"""

def yubikey_context(request):
    """
    Add YubiKey device information to template context (DISABLED - using passkeys instead)
    """
    context = {
        'yubikey_devices': [],
        'yubikey_available': False  # Disabled YubiKey OTP - using passkeys instead
    }
    
    return context
    
    # DISABLED CODE BELOW - using passkeys instead of YubiKey OTP
    # if not request.user.is_authenticated:
    #     return context
        
    # try:
    #     from otp_yubikey.models import RemoteYubikeyDevice
    #     yubikey_devices = RemoteYubikeyDevice.objects.filter(
    #         user=request.user, 
    #         confirmed=True
    #     ).order_by('-id')  # Use -id instead of -created_at since RemoteYubikeyDevice might not have created_at
    #     context['yubikey_devices'] = yubikey_devices
    #     context['yubikey_available'] = True
    # except ImportError:
    #     # YubiKey package not available
    #     pass
    # except Exception as e:
    #     # Any other error - log it but don't break the page
    #     import logging
    #     logger = logging.getLogger(__name__)
    #     logger.exception(f"Error in yubikey_context: {e}")
    
    # return context