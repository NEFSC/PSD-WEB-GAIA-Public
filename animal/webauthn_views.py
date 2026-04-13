"""
WebAuthn/FIDO2 views for GAIA
"""
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect, get_object_or_404
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
import base64
import secrets
from datetime import timedelta

# Base64url encoding/decoding utilities
def base64url_decode(data):
    """Decode base64url string to bytes"""
    # Add padding if needed
    if isinstance(data, str):
        data = data.encode('ascii')
    
    # Add padding
    missing_padding = len(data) % 4
    if missing_padding:
        data += b'=' * (4 - missing_padding)
    
    # Replace base64url characters with base64 characters
    data = data.replace(b'-', b'+').replace(b'_', b'/')
    return base64.b64decode(data)

def base64url_encode(data):
    """Encode bytes to base64url string"""
    if isinstance(data, str):
        data = data.encode('ascii')
    return base64.b64encode(data).replace(b'+', b'-').replace(b'/', b'_').rstrip(b'=').decode('ascii')

# Import WebAuthn library
try:
    from webauthn import generate_registration_options, verify_registration_response
    from webauthn import generate_authentication_options, verify_authentication_response
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria, 
        UserVerificationRequirement,
        AttestationConveyancePreference,
        AuthenticatorAttachment,
        ResidentKeyRequirement,
        PublicKeyCredentialDescriptor,
    )
    from webauthn.helpers.exceptions import InvalidRegistrationResponse, InvalidAuthenticationResponse
    from .webauthn_models import WebAuthnCredential, WebAuthnChallenge
    WEBAUTHN_AVAILABLE = True
except ImportError:
    WEBAUTHN_AVAILABLE = False


# WebAuthn configuration
WEBAUTHN_RP_ID = getattr(settings, 'WEBAUTHN_RP_ID', 'localhost')
WEBAUTHN_RP_NAME = getattr(settings, 'WEBAUTHN_RP_NAME', 'GAIA')
WEBAUTHN_ORIGIN = getattr(settings, 'WEBAUTHN_ORIGIN', 'http://localhost')

# For development, allow multiple localhost origins
# Dynamically build allowed origins from settings.ALLOWED_HOSTS
ALLOWED_ORIGINS = []
for host in getattr(settings, 'ALLOWED_HOSTS', []):
    if host in ('localhost', '127.0.0.1', '::1'):
        ALLOWED_ORIGINS.extend([
            f'http://{host}',
            f'https://{host}',
        ])
    else:
        ALLOWED_ORIGINS.append(f'https://{host}')


@method_decorator(login_required, name='dispatch')
class WebAuthnSetupView(TemplateView):
    """
    WebAuthn/FIDO2 passkey setup view for the unified 2FA page
    """
    template_name = 'two_factor/webauthn_setup_unified.html'
    
    @method_decorator(login_required)
    def dispatch(self, *args, **kwargs):
        if not WEBAUTHN_AVAILABLE:
            messages.error(self.request, 'WebAuthn is not available on this system.')
            return redirect('two_factor_settings')
        return super().dispatch(*args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['existing_credentials'] = WebAuthnCredential.objects.filter(user=self.request.user)
        return context


class WebAuthnSetupViewLegacy(TemplateView):
    """
    Legacy WebAuthn/FIDO2 passkey setup view - kept for backwards compatibility
    """
    template_name = 'two_factor/webauthn_setup.html'
    """
    View for setting up WebAuthn passkeys
    """
    template_name = 'two_factor/webauthn_setup.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['webauthn_available'] = WEBAUTHN_AVAILABLE
        if WEBAUTHN_AVAILABLE:
            context['existing_credentials'] = WebAuthnCredential.objects.filter(user=self.request.user)
        context['webauthn_rp_id'] = WEBAUTHN_RP_ID
        return context


@login_required
@require_http_methods(["POST"])
def webauthn_registration_begin(request):
    """
    Begin WebAuthn registration process
    """
    if not WEBAUTHN_AVAILABLE:
        return JsonResponse({'error': 'WebAuthn is not available'}, status=400)
    
    try:
        # Clean up expired challenges
        WebAuthnChallenge.cleanup_expired()
        
        # Get existing credentials to exclude them
        existing_credentials = WebAuthnCredential.objects.filter(user=request.user)
        exclude_credentials = [
            PublicKeyCredentialDescriptor(id=cred.get_credential_id_bytes())
            for cred in existing_credentials
        ]
        
        # Generate registration options
        registration_options = generate_registration_options(
            rp_id=WEBAUTHN_RP_ID,
            rp_name=WEBAUTHN_RP_NAME,
            user_id=str(request.user.id).encode(),
            user_name=request.user.username,
            user_display_name=request.user.get_full_name() or request.user.username,
            exclude_credentials=exclude_credentials,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.DISCOURAGED,  # Don't store on device
                user_verification=UserVerificationRequirement.DISCOURAGED,  # No PIN required
            ),
            attestation=AttestationConveyancePreference.NONE,
        )
        
        # Store challenge
        WebAuthnChallenge.objects.create(
            user=request.user,
            challenge=base64url_encode(registration_options.challenge),
            challenge_type='registration',
            expires_at=timezone.now() + timedelta(minutes=5)
        )
        
        # Convert to JSON-serializable format
        options_json = {
            "challenge": base64url_encode(registration_options.challenge),
            "rp": {
                "name": registration_options.rp.name,
                "id": registration_options.rp.id,
            },
            "user": {
                "id": base64url_encode(registration_options.user.id),
                "name": registration_options.user.name,
                "displayName": registration_options.user.display_name,
            },
            "pubKeyCredParams": [
                {
                    "type": param.type,
                    "alg": param.alg
                }
                for param in registration_options.pub_key_cred_params
            ],
            "timeout": registration_options.timeout,
            "excludeCredentials": [
                {
                    "type": cred.type,
                    "id": base64url_encode(cred.id),
                }
                for cred in registration_options.exclude_credentials
            ],
            "authenticatorSelection": {
                # Force these settings to prevent PIN prompts
                "userVerification": "discouraged",
                "residentKey": "discouraged",
                "requireResidentKey": False
            },
            "attestation": registration_options.attestation,
        }
        
        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"WebAuthn registration options: {options_json['authenticatorSelection']}")
        
        return JsonResponse(options_json)
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def webauthn_registration_complete(request):
    """
    Complete WebAuthn registration process
    """
    if not WEBAUTHN_AVAILABLE:
        return JsonResponse({'error': 'WebAuthn is not available'}, status=400)
    
    try:
        data = json.loads(request.body)
        credential_name = data.get('name', 'Unnamed Passkey')
        
        # Get the challenge
        challenge_obj = WebAuthnChallenge.objects.filter(
            user=request.user,
            challenge_type='registration'
        ).order_by('-created_at').first()
        
        if not challenge_obj or challenge_obj.is_expired():
            return JsonResponse({'error': 'Invalid or expired challenge'}, status=400)
        
        # Verify registration response - try multiple origins for development
        verification = None
        last_error = None
        for origin in ALLOWED_ORIGINS:
            try:
                verification = verify_registration_response(
                    credential=data['credential'],
                    expected_challenge=challenge_obj.get_challenge_bytes(),
                    expected_origin=origin,
                    expected_rp_id=WEBAUTHN_RP_ID,
                )
                # In webauthn 2.6.0, verification is always truthy if successful
                if verification:
                    break
            except Exception as e:
                last_error = e
                continue
        
        if verification:
            # Store the credential
            WebAuthnCredential.objects.create(
                user=request.user,
                name=credential_name,
                credential_id=base64url_encode(verification.credential_id),
                public_key=base64url_encode(verification.credential_public_key),
                sign_count=verification.sign_count,
                authenticator_type='cross-platform',
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
            
            # Clean up challenge
            challenge_obj.delete()
            
            return JsonResponse({
                'success': True,
                'message': f'Passkey "{credential_name}" registered successfully!'
            })
        else:
            error_msg = f'Registration verification failed'
            if last_error:
                error_msg += f': {str(last_error)}'
            return JsonResponse({'error': error_msg}, status=400)
            
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def webauthn_authentication_begin(request):
    """
    Begin WebAuthn authentication process
    """
    if not WEBAUTHN_AVAILABLE:
        return JsonResponse({'error': 'WebAuthn is not available'}, status=400)
    
    try:
        # Clean up expired challenges
        WebAuthnChallenge.cleanup_expired()
        
        # Get user's credentials
        credentials = WebAuthnCredential.objects.filter(user=request.user)
        if not credentials.exists():
            return JsonResponse({'error': 'No passkeys registered'}, status=400)
        
        allow_credentials = [
            PublicKeyCredentialDescriptor(id=cred.get_credential_id_bytes())
            for cred in credentials
        ]
        
        # Generate authentication options
        authentication_options = generate_authentication_options(
            rp_id=WEBAUTHN_RP_ID,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.DISCOURAGED,  # No PIN required
        )
        
        # Store challenge
        WebAuthnChallenge.objects.create(
            user=request.user,
            challenge=base64url_encode(authentication_options.challenge),
            challenge_type='authentication',
            expires_at=timezone.now() + timedelta(minutes=5)
        )
        
        # Convert to JSON-serializable format
        options_json = {
            "challenge": base64url_encode(authentication_options.challenge),
            "timeout": authentication_options.timeout,
            "rpId": authentication_options.rp_id,
            "allowCredentials": [
                {
                    "type": cred.type,
                    "id": base64url_encode(cred.id),
                }
                for cred in authentication_options.allow_credentials
            ],
            "userVerification": "discouraged",  # Force no PIN requirement
        }
        
        return JsonResponse(options_json)
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def webauthn_authentication_complete(request):
    """
    Complete WebAuthn authentication process
    """
    if not WEBAUTHN_AVAILABLE:
        return JsonResponse({'error': 'WebAuthn is not available'}, status=400)
    
    try:
        data = json.loads(request.body)
        print(f"[DEBUG] Authentication data received: {json.dumps(data, indent=2)}")
        
        # Get the challenge
        challenge_obj = WebAuthnChallenge.objects.filter(
            user=request.user,
            challenge_type='authentication'
        ).order_by('-created_at').first()
        
        if not challenge_obj or challenge_obj.is_expired():
            return JsonResponse({'error': 'Invalid or expired challenge'}, status=400)
        
        print(f"[DEBUG] Challenge found: {challenge_obj.challenge}")
        
        # Find the credential - try different encoding approaches
        try:
            credential_id_raw = base64url_decode(data['credential']['id'])
            print(f"[DEBUG] Successfully decoded credential ID with base64url")
        except Exception as e:
            print(f"[DEBUG] Failed to decode credential ID with base64url: {e}")
            try:
                # Fallback to standard base64
                credential_id_raw = base64.b64decode(data['credential']['id'])
                print(f"[DEBUG] Successfully decoded credential ID with standard base64")
            except Exception as e2:
                print(f"[DEBUG] Failed to decode credential ID with base64: {e2}")
                return JsonResponse({'error': 'Invalid credential ID encoding'}, status=400)
        
        # Try multiple approaches to find the credential
        credential = None
        
        # Approach 1: Direct match with stored credential_id
        try:
            credential = WebAuthnCredential.objects.get(
                user=request.user,
                credential_id=data['credential']['id']  # Use the ID as received
            )
        except WebAuthnCredential.DoesNotExist:
            pass
        
        # Approach 2: Try base64 encoded version
        if not credential:
            try:
                credential_id_b64 = base64.b64encode(credential_id_raw).decode()
                credential = WebAuthnCredential.objects.get(
                    user=request.user,
                    credential_id=credential_id_b64
                )
            except WebAuthnCredential.DoesNotExist:
                pass
        
        # Approach 3: Search through all user credentials and match raw bytes
        if not credential:
            for cred in WebAuthnCredential.objects.filter(user=request.user):
                try:
                    if cred.get_credential_id_bytes() == credential_id_raw:
                        credential = cred
                        break
                except:
                    continue
        
        if not credential:
            print(f"[DEBUG] Credential not found for user {request.user}")
            return JsonResponse({'error': 'Credential not found'}, status=400)
        
        print(f"[DEBUG] Found credential: {credential.name}")
        
        # Verify authentication response - try multiple origins for development
        verification = None
        last_error = None
        for origin in ALLOWED_ORIGINS:
            try:
                print(f"[DEBUG] Trying verification with origin: {origin}")
                verification = verify_authentication_response(
                    credential=data['credential'],
                    expected_challenge=challenge_obj.get_challenge_bytes(),
                    expected_origin=origin,
                    expected_rp_id=WEBAUTHN_RP_ID,
                    credential_public_key=credential.get_public_key_bytes(),
                    credential_current_sign_count=credential.sign_count,
                )
                # In webauthn 2.6.0, verification is always truthy if successful
                if verification:
                    print(f"[DEBUG] Verification successful with origin: {origin}")
                    break
                else:
                    print(f"[DEBUG] Verification failed (no exception) with origin: {origin}")
            except Exception as e:
                print(f"[DEBUG] Verification failed with origin {origin}: {e}")
                last_error = e
                continue
        
        if verification:
            # Update credential
            credential.sign_count = verification.new_sign_count
            credential.update_last_used()
            
            # Clean up challenge
            challenge_obj.delete()
            
            return JsonResponse({
                'success': True,
                'message': 'Authentication successful!'
            })
        else:
            error_msg = f'Authentication verification failed'
            if last_error:
                error_msg += f': {str(last_error)}'
            return JsonResponse({'error': error_msg}, status=400)
            
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def remove_webauthn_credential(request, credential_id):
    """
    Remove a WebAuthn credential from user's account
    """
    if not WEBAUTHN_AVAILABLE:
        messages.error(request, 'WebAuthn support is not available.')
        return redirect('account_page')
    
    try:
        credential = get_object_or_404(
            WebAuthnCredential,
            id=credential_id, 
            user=request.user
        )
        credential_name = credential.name
        credential.delete()
        messages.success(request, f'Passkey "{credential_name}" has been removed from your account.')
    except Exception as e:
        messages.error(request, f'Error removing passkey: {str(e)}')
    
    return redirect('account_page')
