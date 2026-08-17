"""
Custom permissions for API Key authentication.
"""

from rest_framework.permissions import BasePermission


class HasAPIKey(BasePermission):
    """
    Permission class that checks for valid API key authentication.
    Works with the APIKeyAuthentication class.
    """

    def has_permission(self, request, view):
        # Check if authentication was performed
        # request.auth is set by DRF when authenticate() returns a tuple
        # (user, auth) - auth is the second element (the API key)
        return getattr(request, 'auth', None) is not None