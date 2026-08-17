"""
Custom authentication for API Key authentication.
"""

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class APIKeyAuthentication(BaseAuthentication):
    """
    Simple API Key authentication.
    Client must include X-API-Key header with valid key.
    """

    def authenticate(self, request):
        # Check both META and headers for the API key
        api_key = (
            request.META.get('HTTP_X_API_KEY') or
            request.headers.get('X-API-Key') or
            request.headers.get('X-Api-Key')
        )

        if not api_key:
            return None  # No authentication provided

        expected_key = getattr(settings, 'API_KEY', None)

        if not expected_key:
            raise AuthenticationFailed('API key not configured')

        if api_key != expected_key:
            raise AuthenticationFailed('Invalid API key')

        # Set _auth on the request object (required by DRF)
        request._auth = api_key

        # Return a tuple of (user, auth) where user can be None for API key auth
        return (None, api_key)

    def authenticate_header(self, request):
        return 'X-API-Key'