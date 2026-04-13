"""
Middleware for enforcing two-factor authentication and WebAuthn

This middleware is defensive so it can run in development/test without optional
packages. It prefers explicit counts for TOTP/WebAuthn devices and treats static
backup tokens as insufficient to be considered a "real" 2FA method.
"""
from django.shortcuts import redirect
from django.contrib import messages
from django.conf import settings
from .auth_utils import should_enforce_2fa_for_user, get_2fa_enforcement_reason

# Import OTP/WebAuthn with safe fallbacks so middleware can run in dev/test without optional packages
try:
    from django_otp import user_has_device
    from django_otp.models import Device
    from django_otp.plugins.otp_totp.models import TOTPDevice
    OTP_AVAILABLE = True
except Exception:
    def user_has_device(user):
        return False

    Device = None

    class TOTPDevice:
        objects = None

    OTP_AVAILABLE = False

try:
    from .webauthn_models import WebAuthnCredential
    WEBAUTHN_AVAILABLE = True
except Exception:
    WebAuthnCredential = None
    WEBAUTHN_AVAILABLE = False


def user_has_webauthn_credentials(user):
    if not WEBAUTHN_AVAILABLE or WebAuthnCredential is None:
        return False
    try:
        return WebAuthnCredential.objects.filter(user=user).exists()
    except Exception:
        return False


def user_webauthn_verified(request):
    return request.session.get('webauthn_verified', False)


class TwoFactorEnforcementMiddleware:
    """Enforce two-factor setup and verification for authenticated users.

    Behavior summary:
    - Allow unauthenticated users and a set of exempt paths.
    - If user has no "real" confirmed 2FA (TOTP confirmed or WebAuthn), redirect to onboarding.
    - If user has WebAuthn creds but hasn't verified in session, redirect to WebAuthn verification.
    - Otherwise, require the normal two_factor login flow if their session isn't marked verified.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.exempt_urls = [
            '/account/login/',
            '/account/logout/',
            '/accounts/logout/',
            '/logout/',
            '/account/two_factor/',
            '/account/two_factor/setup/',
            '/account/two_factor/qrcode/',
            '/account/two_factor/setup/complete/',
            '/account/two_factor/disable/',
            '/account/setup/',
            '/account/backup_tokens/',
            '/account/qr/',
            '/account/',
            '/admin/logout/',
            '/accounts/password_reset/',
            '/accounts/password_reset/done/',
            '/accounts/password_change/',
            '/accounts/password_change/done/',
            '/static/',
            '/media/',
            '/proxy/',
            '/2fa/',
            '/account/2fa/',
            '/account/2fa/webauthn/',
            '/account/webauthn-verify/',
            '/favicon.ico',
        ]

    def __call__(self, request):
        # Fast-paths
        if not request.user or not request.user.is_authenticated:
            return self.get_response(request)
        if request.user.is_superuser and getattr(settings, 'DEBUG', False):
            return self.get_response(request)

        # Check if 2FA enforcement is enabled for this specific user
        if not should_enforce_2fa_for_user(request.user):
            return self.get_response(request)

        current_path = request.path
        if any(current_path.startswith(p) for p in self.exempt_urls):
            return self.get_response(request)
        if current_path in ('/account/logout/', '/logout/'):
            return self.get_response(request)

        import logging
        logger = logging.getLogger('animal')
        logger.info(f"TwoFactorMiddleware: Processing {request.method} {current_path}")

        # Compute device flags
        has_unconfirmed_device = False
        if OTP_AVAILABLE and TOTPDevice and getattr(TOTPDevice, 'objects', None) is not None:
            try:
                has_unconfirmed_device = TOTPDevice.objects.filter(user=request.user, confirmed=False).exists()
            except Exception:
                has_unconfirmed_device = False

        # Basic checks
        has_device_flag = False
        try:
            has_device_flag = user_has_device(request.user)
        except Exception:
            has_device_flag = False

        has_webauthn = user_has_webauthn_credentials(request.user)
        webauthn_verified = user_webauthn_verified(request)

        # Diagnostic counts (static backup tokens removed)
        totp_confirmed_count = totp_unconfirmed_count = webauthn_count = device_count = 0
        try:
            if OTP_AVAILABLE and TOTPDevice and getattr(TOTPDevice, 'objects', None) is not None:
                totp_confirmed_count = TOTPDevice.objects.filter(user=request.user, confirmed=True).count()
                totp_unconfirmed_count = TOTPDevice.objects.filter(user=request.user, confirmed=False).count()
        except Exception:
            pass
        try:
            if WEBAUTHN_AVAILABLE and WebAuthnCredential is not None:
                webauthn_count = WebAuthnCredential.objects.filter(user=request.user).count()
        except Exception:
            pass
        try:
            if Device is not None and getattr(Device, 'objects', None) is not None:
                device_count = Device.objects.filter(user=request.user).count()
        except Exception:
            pass

        logger.info(f"TwoFactorMiddleware: user_has_device (flag) = {has_device_flag}")
        logger.info(f"TwoFactorMiddleware: has_unconfirmed_device = {has_unconfirmed_device}")
        logger.info(f"TwoFactorMiddleware: has_webauthn_credentials = {has_webauthn}")
        logger.info(f"TwoFactorMiddleware: webauthn_verified = {webauthn_verified}")
        logger.info(
            f"TwoFactorMiddleware: totp_confirmed_count={totp_confirmed_count}, totp_unconfirmed_count={totp_unconfirmed_count}, "
            f"webauthn_count={webauthn_count}, device_count={device_count}"
        )

        # Only treat TOTP-confirmed or WebAuthn as "real" confirmed methods
        has_real_confirmed = (totp_confirmed_count > 0) or (webauthn_count > 0)
        logger.info(f"TwoFactorMiddleware: has_real_confirmed={has_real_confirmed}")

        # If the user has no real confirmed 2FA and isn't in setup flow, send to onboarding
        if not has_real_confirmed and not has_unconfirmed_device:
            if not (current_path.startswith('/account/two_factor/setup/') or
                    current_path.startswith('/account/two_factor/qrcode/') or
                    current_path.startswith('/account/setup/') or
                    current_path.startswith('/2fa/onboarding')):
                logger.info(f"TwoFactorMiddleware: Redirecting to 2FA onboarding from {current_path}")
                try:
                    reason = get_2fa_enforcement_reason(request.user)
                    message = reason if reason else 'Two-factor authentication is required to access this application. Please set it up now.'
                    messages.warning(request, message)
                except Exception:
                    pass
                return redirect('two_factor_onboarding')

        # If user has WebAuthn credentials but hasn't verified this session -> require WebAuthn verification
        if has_webauthn and not webauthn_verified:
            if not current_path.startswith('/account/webauthn-verify/'):
                logger.info("TwoFactorMiddleware: User has WebAuthn credentials but not verified - redirecting to WebAuthn verification")
                try:
                    messages.info(request, 'Please verify your identity using your security key or passkey.')
                except Exception:
                    pass
                return redirect('webauthn_verify_required')

        logger.info(f"TwoFactorMiddleware: Allowing access to {current_path}")
        return self.get_response(request)
