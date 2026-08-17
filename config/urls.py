"""
URL configuration for Motorq Vehicle Enrollment project.
"""

from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/enroll/', include('enrollment.urls')),
]