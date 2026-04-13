"""
WebAuthn verification views for enforcing security key usage
"""
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect
from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import TemplateView
from django.utils.decorators import method_decorator
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.conf import settings
import json

# Import WebAuthn models and utilities
try:
    from .webauthn_models import WebAuthnCredential, WebAuthnChallenge
    from .webauthn_views import (
        webauthn_authentication_begin, 
        webauthn_authentication_complete,
        WEBAUTHN_AVAILABLE
    )
    WEBAUTHN_VERIFICATION_AVAILABLE = True
except ImportError:
    WEBAUTHN_VERIFICATION_AVAILABLE = False


@method_decorator(login_required, name='dispatch')
class WebAuthnVerificationRequiredView(TemplateView):
    """
    View that requires users with WebAuthn credentials to verify their identity
    with their security key or passkey before accessing the application.
    """
    template_name = 'two_factor/webauthn_verification_required.html'
    
    def dispatch(self, request, *args, **kwargs):
        # Check if WebAuthn is available
        if not WEBAUTHN_VERIFICATION_AVAILABLE:
            messages.error(request, 'WebAuthn verification is not available.')
            return redirect('/')
            
        # Check if user has WebAuthn credentials
        if not WebAuthnCredential.objects.filter(user=request.user).exists():
            # User doesn't have WebAuthn credentials, no verification needed
            request.session['webauthn_verified'] = True
            return redirect('/')
            
        return super().dispatch(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['webauthn_credentials'] = WebAuthnCredential.objects.filter(user=self.request.user)
        return context


@login_required
@require_http_methods(["POST"])
def webauthn_verification_complete(request):
    """
    Handle completion of WebAuthn verification for session access
    """
    if not WEBAUTHN_VERIFICATION_AVAILABLE:
        return JsonResponse({'error': 'WebAuthn verification is not available'}, status=400)
    
    try:
        # Use the existing webauthn_authentication_complete logic
        # This handles the actual cryptographic verification
        response_data = json.loads(request.body)
        
        # Call the existing authentication complete function
        # We need to simulate the request to make it work
        from django.http import HttpRequest
        auth_request = HttpRequest()
        auth_request.method = 'POST'
        auth_request.user = request.user
        auth_request._body = request.body
        
        # Get the authentication result
        auth_response = webauthn_authentication_complete(auth_request)
        
        if auth_response.status_code == 200:
            # Authentication successful - mark session as WebAuthn verified
            request.session['webauthn_verified'] = True
            request.session['webauthn_verified_at'] = timezone.now().isoformat()
            
            # Also mark user as verified for django-otp compatibility
            try:
                from django_otp import login as otp_login
                from django_otp.models import Device
                
                # Find a WebAuthn credential to use as the "device"
                credential_id = response_data['credential']['id']
                credential = WebAuthnCredential.objects.get(
                    user=request.user,
                    credential_id=credential_id
                )
                
                # Mark the user as OTP-verified using the WebAuthn credential as a device
                # We need to create a fake device-like object that django-otp will accept
                class WebAuthnDeviceShim:
                    def __init__(self, credential):
                        self.credential = credential
                        self.user = credential.user
                        self.confirmed = True
                        self.name = credential.name
                        
                    def verify_token(self, token):
                        return True  # Already verified by WebAuthn
                        
                    def generate_challenge(self):
                        return None
                
                device_shim = WebAuthnDeviceShim(credential)
                otp_login(request, device_shim)
                
                # Update the credential's last_used timestamp
                credential.update_last_used()
                
            except (ImportError, Exception) as e:
                # If django-otp integration fails, still allow WebAuthn-only verification
                import logging
                logger = logging.getLogger('animal')
                logger.warning(f"Failed to integrate WebAuthn with django-otp: {e}")
                
                # Fallback - just set session flags
                request.session['verified_2fa'] = True
                
                try:
                    credential_id = response_data['credential']['id']
                    credential = WebAuthnCredential.objects.get(
                        user=request.user,
                        credential_id=credential_id
                    )
                    credential.update_last_used()
                except (WebAuthnCredential.DoesNotExist, KeyError):
                    pass  # Credential might not be found due to encoding differences
            
            return JsonResponse({
                'status': 'success',
                'message': 'WebAuthn verification successful',
                'redirect_url': request.GET.get('next', '/')
            })
        else:
            # Authentication failed
            auth_data = json.loads(auth_response.content)
            return JsonResponse({
                'status': 'error',
                'error': auth_data.get('error', 'WebAuthn verification failed')
            }, status=400)
            
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'error': f'Verification error: {str(e)}'
        }, status=500)


@login_required
def skip_webauthn_verification(request):
    """
    Allow users to skip WebAuthn verification (emergency bypass)
    This should only be used in exceptional circumstances
    """
    if request.method == 'POST':
        # In a production environment, you might want to:
        # 1. Log this bypass attempt
        # 2. Require additional confirmation
        # 3. Send notification to administrators
        # 4. Set a temporary bypass that expires
        
        # For now, we'll allow the bypass but log it
        import logging
        logger = logging.getLogger('animal')
        logger.warning(f"User {request.user.username} bypassed WebAuthn verification from IP {request.META.get('REMOTE_ADDR')}")
        
        # Mark session as verified (with bypass flag)
        request.session['webauthn_verified'] = True
        request.session['webauthn_bypassed'] = True
        request.session['webauthn_verified_at'] = timezone.now().isoformat()
        
        messages.warning(
            request,
            'WebAuthn verification bypassed. For security reasons, please use your security key next time.'
        )
        
        return redirect(request.GET.get('next', '/'))
    
    return redirect('webauthn_verify_required')
