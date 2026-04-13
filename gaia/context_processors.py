import os

def environment(request):
    return {
        'DJANGO_ENV': os.environ.get('DJANGO_ENV', 'Unknown')
    }

def build_date(request):
    return {'BUILD_DATE': os.environ.get('BUILD_DATE', 'Unknown')}

def yubikey_context(request):
    """Add YubiKey-related context to templates"""
    context = {
        'yubikey_available': False,
        'yubikey_devices': []
    }
    
    try:
        from django_otp.plugins.otp_yubikey.models import YubikeyDevice
        context['yubikey_available'] = True
        
        if request.user.is_authenticated:
            context['yubikey_devices'] = YubikeyDevice.objects.filter(
                user=request.user, 
                confirmed=True
            ).order_by('-created_at')
    except ImportError:
        pass
    
    return context