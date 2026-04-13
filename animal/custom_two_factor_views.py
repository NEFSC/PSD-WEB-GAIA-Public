"""
Custom two-factor views for GAIA that ensure complete device removal
"""
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.views.generic import TemplateView
from django.utils.decorators import method_decorator
from django.contrib.auth.forms import PasswordForm

try:
    from django_otp.plugins.otp_totp.models import TOTPDevice
    from django_otp import user_has_device
except ImportError:
    # Fallback for development
    TOTPDevice = None
    def user_has_device(user):
        return False


class CustomDisableView(TemplateView):
    """
    Custom disable view that ensures ALL 2FA devices are removed
    """
    template_name = 'two_factor/core/disable.html'
    
    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        # Check if user has any 2FA devices
        if not user_has_device(request.user):
            messages.info(request, 'Two-factor authentication is not enabled for your account.')
            return redirect('account_page')
        return super().dispatch(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Add form for password confirmation
        context['form'] = PasswordForm(user=self.request.user)
        return context
    
    def post(self, request, *args, **kwargs):
        form = PasswordForm(user=request.user, data=request.POST)
        
        if form.is_valid():
            # Password is correct, proceed with disabling 2FA
            return self._disable_all_2fa(request)
        else:
            # Password is incorrect
            messages.error(request, 'Invalid password. Please try again.')
            return render(request, self.template_name, {'form': form})
    
    def _disable_all_2fa(self, request):
        """Remove all 2FA devices for the user"""
        try:
            removed_count = 0
            device_names = []
            
            # Remove all TOTP devices
            if TOTPDevice:
                totp_devices = TOTPDevice.objects.filter(user=request.user)
                for device in totp_devices:
                    device_names.append(device.name or 'TOTP Device')
                totp_count = totp_devices.count()
                totp_devices.delete()
                removed_count += totp_count
            
            # Static backup tokens are not used; nothing to remove
            
            if removed_count > 0:
                device_list = ', '.join(set(device_names))
                messages.success(
                    request, 
                    f'Two-factor authentication has been completely disabled. '
                    f'Removed {removed_count} devices: {device_list}.'
                )
            else:
                messages.info(request, 'No two-factor authentication devices were found to remove.')
                
        except Exception as e:
            messages.error(request, f'Error disabling two-factor authentication: {str(e)}')
        
        return redirect('account_page')
