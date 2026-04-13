"""
WebAuthn/FIDO2 models for GAIA
"""
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import json
import base64


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


class WebAuthnCredential(models.Model):
    """
    Store WebAuthn/FIDO2 credentials (passkeys) for users
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='webauthn_credentials')
    name = models.CharField(max_length=100, help_text="User-friendly name for this credential")
    credential_id = models.TextField(help_text="Base64-encoded credential ID")
    public_key = models.TextField(help_text="Base64-encoded public key")
    sign_count = models.PositiveIntegerField(default=0, help_text="Signature counter for replay protection")
    created_at = models.DateTimeField(default=timezone.now)
    last_used = models.DateTimeField(null=True, blank=True)
    
    # Device information (optional but useful for user management)
    authenticator_type = models.CharField(
        max_length=50, 
        blank=True,
        help_text="Type of authenticator (e.g., 'platform', 'cross-platform')"
    )
    user_agent = models.TextField(blank=True, help_text="User agent when credential was created")
    
    class Meta:
        unique_together = ('user', 'credential_id')
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.username} - {self.name}"
    
    def get_credential_id_bytes(self):
        """Get credential ID as bytes"""
        try:
            return base64url_decode(self.credential_id)
        except:
            # Fallback to standard base64 for existing credentials
            return base64.b64decode(self.credential_id)
    
    def get_public_key_bytes(self):
        """Get public key as bytes"""
        try:
            return base64url_decode(self.public_key)
        except:
            # Fallback to standard base64 for existing credentials
            return base64.b64decode(self.public_key)
    
    def update_last_used(self):
        """Update the last_used timestamp"""
        self.last_used = timezone.now()
        self.save(update_fields=['last_used'])


class WebAuthnChallenge(models.Model):
    """
    Store temporary WebAuthn challenges during authentication/registration
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    challenge = models.TextField(help_text="Base64-encoded challenge")
    challenge_type = models.CharField(
        max_length=20,
        choices=[
            ('registration', 'Registration'),
            ('authentication', 'Authentication'),
        ]
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    
    class Meta:
        ordering = ['-created_at']
    
    def is_expired(self):
        return timezone.now() > self.expires_at
    
    def get_challenge_bytes(self):
        """Get challenge as bytes"""
        try:
            return base64url_decode(self.challenge)
        except:
            # Fallback to standard base64 for existing challenges
            return base64.b64decode(self.challenge)
    
    @classmethod
    def cleanup_expired(cls):
        """Remove expired challenges"""
        cls.objects.filter(expires_at__lt=timezone.now()).delete()
